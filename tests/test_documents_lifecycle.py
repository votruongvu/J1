"""Tests for the document-centric lifecycle additions.

Covers, in this order:

* The retrieval filter (``filter_to_attached_artifacts``) is a
  pure-function no-op for pre-refactor records (no
  ``metadata.knowledge_state`` field) and drops records explicitly
  marked detached or removed.

* ``DocumentRecord`` round-trips through the registry with the new
  fields, AND legacy v1 records (no new fields on disk) deserialize
  with the safe defaults.

* ``DocumentVersionStore`` is idempotent on
  ``(document_id, file_hash)`` — the same upload bytes resolve to
  the same version_id.

* ``IngestionRun`` deserializes with the new fields defaulted, and
  the run-store round-trips them when set.

* ``backfill_project`` is idempotent + applies the documented
  active-run selection rule (succeeded → failed-with-checkpoint →
  latest).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.backfill import backfill_project, select_active_run_id
from j1.documents.lifecycle import filter_to_attached_artifacts, is_attached
from j1.documents.models import DocumentRecord, DocumentVersion
from j1.documents.versions import (
    DocumentVersionNotFoundError,
    JsonDocumentVersionStore,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


# ---- filter_to_attached_artifacts: pure-function gate -------------


def _artifact(*, artifact_id: str, knowledge_state: str | None = None) -> ArtifactRecord:
    """Build a minimal artifact record. `knowledge_state=None` means
 "no field on disk" — that's the pre-refactor default and must
 still be treated as attached."""
    metadata: dict = {}
    if knowledge_state is not None:
        metadata["knowledge_state"] = knowledge_state
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="t", project_id="p"),
        kind="chunk",
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata=metadata,
    )


def test_filter_is_noop_for_records_without_knowledge_state_field():
    """Backward-compat contract: every artifact written before this
 refactor lacks ``metadata.knowledge_state``. The filter MUST treat
 those as attached so nothing disappears from existing surfaces."""
    legacy = [_artifact(artifact_id=f"a-{i}") for i in range(3)]
    out = filter_to_attached_artifacts(legacy)
    assert [a.artifact_id for a in out] == ["a-0", "a-1", "a-2"]


def test_filter_drops_detached_artifacts():
    """An artifact explicitly stamped ``knowledge_state="detached"``
 must NOT appear in retrieval results. This is the gate Phase 3
 will exercise when the detach action lands."""
    records = [
        _artifact(artifact_id="ok"),
        _artifact(artifact_id="hidden", knowledge_state="detached"),
        _artifact(artifact_id="also-ok", knowledge_state="attached"),
    ]
    out = filter_to_attached_artifacts(records)
    assert [a.artifact_id for a in out] == ["ok", "also-ok"]


def test_filter_drops_removed_artifacts():
    """Removed (knowledge purged) artifacts are even more excluded
 than detached — they MUST never come back without an explicit
 admin opt-in."""
    records = [
        _artifact(artifact_id="ok"),
        _artifact(artifact_id="gone", knowledge_state="removed"),
    ]
    assert [a.artifact_id for a in filter_to_attached_artifacts(records)] == ["ok"]


def test_filter_tolerates_garbage_state_value():
    """Unknown state strings count as "not attached" so a typo or
 future state value can't silently leak hidden data."""
    records = [
        _artifact(artifact_id="ok"),
        _artifact(artifact_id="weird", knowledge_state="quarantined"),
    ]
    assert [a.artifact_id for a in filter_to_attached_artifacts(records)] == ["ok"]


def test_is_attached_handles_missing_metadata_attr():
    """Defensive: an artifact-like object without a metadata dict
 (e.g. a stub used in tests) is treated as attached. Avoids
 spurious AttributeError at retrieval time."""

    class _Stub:
        metadata = None

    assert is_attached(_Stub()) is True  # type: ignore[arg-type]


# ---- DocumentRecord: new fields + legacy compat --------------------


def test_document_record_defaults(ctx):
    """A freshly built `DocumentRecord` without the new fields gets
 safe defaults so callers that build records by-hand keep working."""
    doc = DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="bridge.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="sha256:abc",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
    )
    assert doc.knowledge_state == "attached"
    assert doc.active_run_id is None
    assert doc.latest_version_id is None
    assert doc.removed_at is None
    assert doc.updated_at is None
    assert doc.is_attached() is True


def test_legacy_document_record_deserializes_with_defaults(workspace, ctx):
    """A documents.json file written by pre-refactor code (no new
 fields) must read back cleanly. The deserializer fills in safe
 defaults — knowledge_state="attached", every pointer None."""
    import json
    runtime = workspace.runtime(ctx)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "documents.json").write_text(json.dumps({
        "version": 1,
        "documents": [{
            "document_id": "doc-legacy",
            "project": {
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "profile": ctx.profile,
            },
            "original_filename": "old.pdf",
            "stored_filename": "old.pdf",
            "mime_type": "application/pdf",
            "file_size": 1,
            "checksum": "sha256:legacy",
            "status": ProcessingStatus.SUCCEEDED.value,
            "created_at": _NOW.isoformat(),
        }],
    }))
    from j1.intake.registry import JsonSourceRegistry
    registry = JsonSourceRegistry(workspace)
    doc = registry.get(ctx, "doc-legacy")
    assert doc.knowledge_state == "attached"
    assert doc.active_run_id is None
    assert doc.is_attached()


def test_unknown_knowledge_state_on_disk_falls_back_to_attached(
    workspace, ctx,
):
    """If someone hand-edits documents.json with a typo, the
 deserializer must not crash — it normalises unknown states back
 to "attached" so retrieval keeps working."""
    import json
    runtime = workspace.runtime(ctx)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "documents.json").write_text(json.dumps({
        "version": 2,
        "documents": [{
            "document_id": "doc-2",
            "project": {
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "profile": ctx.profile,
            },
            "original_filename": "x.pdf",
            "stored_filename": "x.pdf",
            "mime_type": "application/pdf",
            "file_size": 1,
            "checksum": "sha256:x",
            "status": ProcessingStatus.SUCCEEDED.value,
            "created_at": _NOW.isoformat(),
            "knowledge_state": "quarantined-by-mistake",
        }],
    }))
    from j1.intake.registry import JsonSourceRegistry
    doc = JsonSourceRegistry(workspace).get(ctx, "doc-2")
    assert doc.knowledge_state == "attached"


# ---- DocumentVersion: idempotent insertion -------------------------


def _make_version(
    *,
    project: ProjectContext,
    document_version_id: str = "dv-1",
    document_id: str = "doc-1",
    file_hash: str = "sha256:aaa",
) -> DocumentVersion:
    return DocumentVersion(
        document_version_id=document_version_id,
        document_id=document_id,
        project=project,
        file_hash=file_hash,
        original_filename="bridge.pdf",
        storage_uri="intake/dv-1.pdf",
        mime_type="application/pdf",
        size_bytes=1234,
        created_at=_NOW,
    )


def test_document_version_store_round_trips(workspace, ctx):
    store = JsonDocumentVersionStore(workspace)
    v = _make_version(project=ctx)
    store.add(v)
    assert store.get(ctx, "dv-1") == v
    assert store.list_for_document(ctx, "doc-1") == [v]


def test_document_version_store_returns_existing_on_same_hash(
    workspace, ctx,
):
    """Idempotency contract: re-uploading the same file bytes for
 the same document returns the original version_id. This is what
 lets the Phase 4 re-index flow stay cheap on no-op uploads."""
    store = JsonDocumentVersionStore(workspace)
    first = store.add(_make_version(project=ctx))
    # Same hash, different requested id → original wins.
    again = store.add(_make_version(
        project=ctx,
        document_version_id="dv-2-different",
        file_hash="sha256:aaa",
    ))
    assert again.document_version_id == "dv-1"
    assert store.list_for_document(ctx, "doc-1") == [first]


def test_document_version_store_inserts_distinct_hashes(workspace, ctx):
    store = JsonDocumentVersionStore(workspace)
    store.add(_make_version(project=ctx, document_version_id="dv-1"))
    store.add(_make_version(
        project=ctx,
        document_version_id="dv-2",
        file_hash="sha256:bbb",
    ))
    versions = store.list_for_document(ctx, "doc-1")
    assert {v.document_version_id for v in versions} == {"dv-1", "dv-2"}


def test_document_version_get_raises_for_missing(workspace, ctx):
    with pytest.raises(DocumentVersionNotFoundError):
        JsonDocumentVersionStore(workspace).get(ctx, "missing")


# ---- IngestionRun: new fields + legacy compat ---------------------


def test_ingestion_run_defaults():
    """New run fields default to safe values so callers that build
 runs by-hand (tests, migration scripts) don't need updates."""
    run = IngestionRun(
        run_id="r-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
    )
    assert run.run_type == "initial"
    assert run.document_version_id is None
    assert run.parent_run_id is None


def test_run_store_round_trips_new_fields(workspace, ctx):
    """JSONL store writes + reads the new fields. The forward-compat
 deserializer guarantees a legacy snapshot (no run_type on disk)
 reads back as `run_type="initial"`."""
    store = JsonlIngestionRunStore(workspace)
    run = IngestionRun(
        run_id="r-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        run_type="reindex",
        document_version_id="dv-1",
        parent_run_id="r-0",
    )
    store.upsert(ctx, run)
    loaded = store.get(ctx, "r-1")
    assert loaded.run_type == "reindex"
    assert loaded.document_version_id == "dv-1"
    assert loaded.parent_run_id == "r-0"


def test_legacy_run_payload_defaults_run_type_to_initial(workspace, ctx):
    """A snapshot on disk without `run_type` (= every legacy run)
 must read back as `run_type="initial"` rather than throwing."""
    import json
    audit = workspace.area(ctx, _audit_area())
    audit.mkdir(parents=True, exist_ok=True)
    log = audit / "ingestion_runs.jsonl"
    log.write_text(json.dumps({
        "run_id": "r-legacy",
        "document_id": "doc-legacy",
        "workflow_id": "wf-1",
        "workflow_run_id": None,
        "status": "succeeded",
        "started_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }) + "\n")
    loaded = JsonlIngestionRunStore(workspace).get(ctx, "r-legacy")
    assert loaded.run_type == "initial"
    assert loaded.document_version_id is None


# ---- backfill: idempotency + selection rule ------------------------


def _run(
    *,
    run_id: str,
    document_id: str,
    status: RunStatus,
    started_at: datetime,
    has_checkpoint: bool = False,
) -> IngestionRun:
    metadata: dict = {}
    if has_checkpoint:
        metadata["resume_snapshot"] = {"completed_steps": ["compile"]}
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started_at,
        updated_at=started_at,
        metadata=metadata,
    )


def test_select_active_run_returns_none_for_empty_list():
    assert select_active_run_id([]) is None


def test_select_active_run_prefers_latest_succeeded():
    """Tier 1: latest succeeded run wins even when failures come
 later — the user's last GOOD result is what the document
 exposes as "current usable"."""
    t0 = _NOW
    runs = [
        _run(run_id="r-fail-old", document_id="d", status=RunStatus.FAILED, started_at=t0),
        _run(run_id="r-good", document_id="d", status=RunStatus.SUCCEEDED, started_at=t0 + timedelta(hours=1)),
        # A LATER failure shouldn't displace the previous good run.
        _run(run_id="r-fail-new", document_id="d", status=RunStatus.FAILED, started_at=t0 + timedelta(hours=2)),
    ]
    assert select_active_run_id(runs) == "r-good"


def test_select_active_run_succeeded_with_warnings_counts():
    """SUCCEEDED_WITH_WARNINGS is still a usable result — operational
 warnings don't make the knowledge unusable."""
    runs = [
        _run(run_id="r-warn", document_id="d", status=RunStatus.SUCCEEDED_WITH_WARNINGS, started_at=_NOW),
    ]
    assert select_active_run_id(runs) == "r-warn"


def test_select_active_run_tier2_failed_with_checkpoint():
    """Tier 2: if no succeeded runs exist, pick the latest FAILED
 run that has a compile checkpoint — the only failure-mode that's
 resumable."""
    runs = [
        _run(run_id="r-no-cp", document_id="d", status=RunStatus.FAILED, started_at=_NOW),
        _run(
            run_id="r-cp", document_id="d", status=RunStatus.FAILED,
            started_at=_NOW + timedelta(hours=1), has_checkpoint=True,
        ),
    ]
    assert select_active_run_id(runs) == "r-cp"


def test_select_active_run_tier3_falls_back_to_latest():
    """Tier 3: no succeeded, no failed-with-checkpoint → just pick
 the latest run by started_at. Lets the FE always show SOMETHING
 even when every run failed pre-compile."""
    runs = [
        _run(run_id="r-1", document_id="d", status=RunStatus.FAILED, started_at=_NOW),
        _run(run_id="r-2", document_id="d", status=RunStatus.FAILED, started_at=_NOW + timedelta(hours=1)),
    ]
    assert select_active_run_id(runs) == "r-2"


def test_backfill_stamps_active_run_and_default_state(
    workspace, ctx, registry,
):
    """Happy path: a project with one document + one succeeded run
 ends up with `knowledge_state="attached"` + `active_run_id=r-1`
 after the backfill."""
    from j1.intake.registry import JsonSourceRegistry
    from j1.runs.store import JsonlIngestionRunStore

    doc = DocumentRecord(
        document_id="d",
        project=ctx,
        original_filename="x.pdf",
        stored_filename="x.pdf",
        mime_type=None,
        file_size=1,
        checksum="sha256:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
    )
    registry.add(doc)
    run_store = JsonlIngestionRunStore(workspace)
    run_store.upsert(
        ctx,
        _run(run_id="r-1", document_id="d", status=RunStatus.SUCCEEDED, started_at=_NOW),
    )

    summary = backfill_project(ctx, registry=registry, run_store=run_store)
    assert summary["documents_inspected"] == 1
    assert summary["documents_updated"] == 1

    after = registry.get(ctx, "d")
    assert after.knowledge_state == "attached"
    assert after.active_run_id == "r-1"
    assert after.updated_at is not None


def test_backfill_is_idempotent(workspace, ctx, registry):
    """Running the backfill twice leaves the data identical — no
 spurious writes, no churn. Operators can safely call it on every
 worker startup."""
    from j1.runs.store import JsonlIngestionRunStore

    registry.add(DocumentRecord(
        document_id="d",
        project=ctx,
        original_filename="x.pdf",
        stored_filename="x.pdf",
        mime_type=None,
        file_size=1,
        checksum="sha256:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
    ))
    run_store = JsonlIngestionRunStore(workspace)
    run_store.upsert(
        ctx,
        _run(run_id="r-1", document_id="d", status=RunStatus.SUCCEEDED, started_at=_NOW),
    )
    first = backfill_project(ctx, registry=registry, run_store=run_store)
    second = backfill_project(ctx, registry=registry, run_store=run_store)
    assert first["documents_updated"] == 1
    # Second pass updates nothing — the first pass already settled
    # the document into its final state.
    assert second["documents_updated"] == 0
    assert second["documents_unchanged"] == 1


def test_backfill_does_not_overwrite_explicit_state(
    workspace, ctx, registry,
):
    """If an operator detached a document manually between backfill
 runs, the second backfill must NOT flip it back to attached."""
    from j1.runs.store import JsonlIngestionRunStore

    registry.add(DocumentRecord(
        document_id="d",
        project=ctx,
        original_filename="x.pdf",
        stored_filename="x.pdf",
        mime_type=None,
        file_size=1,
        checksum="sha256:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="detached",  # operator-set
    ))
    run_store = JsonlIngestionRunStore(workspace)
    run_store.upsert(
        ctx,
        _run(run_id="r-1", document_id="d", status=RunStatus.SUCCEEDED, started_at=_NOW),
    )
    backfill_project(ctx, registry=registry, run_store=run_store)
    assert registry.get(ctx, "d").knowledge_state == "detached"


# ---- helper ------------------------------------------------------


def _audit_area():
    """Avoid the WorkspaceArea import at module top so this test file
 doesn't gain transitive deps on the workspace package layout."""
    from j1.workspace.layout import WorkspaceArea
    return WorkspaceArea.AUDIT
