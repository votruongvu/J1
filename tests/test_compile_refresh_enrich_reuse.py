"""Tests for the compile-stage reuse path triggered by
``metadata.reused_compile_from_run_id`` on the refresh-enrich
candidate run.

Contract:
  * When the activity finds ``run.metadata.reused_compile_from_run_id``
    pointing at a prior run that owns compile artifacts, it clones
    every ``compiled.*`` / ``chunk`` artifact from that run under
    the new run_id and returns SUCCESS — skipping the actual
    compile call entirely.
  * Cloned records carry ``metadata.run_id`` set to the candidate
    run (so the eligibility gate admits them under the new run)
    and ``metadata.reused_from_run_id`` for audit.
  * When the run record can't be found / has no metadata key /
    has no source artifacts, the helper returns None and the
    caller falls through to the normal compile path — degrades
    safely to a full parse, never crashes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.orchestration.activities.payloads import (
    CompileActivityInput, ProjectScope,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.workspace.layout import WorkspaceArea


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


def _stage_artifact(
    workspace, ctx, artifact_registry, *,
    artifact_id: str, run_id: str,
    kind: str = "compiled.text",
    source_document_ids: list[str] | None = None,
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, WorkspaceArea.COMPILED)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / f"{artifact_id}.txt").write_bytes(b"compiled body")
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{WorkspaceArea.COMPILED.value}/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=14,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=source_document_ids or ["doc-1"],
        metadata={"run_id": run_id},
    )
    artifact_registry._raw_add(record)
    return record


def _seed_run(
    run_store, ctx, *,
    run_id: str, metadata: dict | None = None,
):
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=RunStatus.CREATED,
        started_at=_NOW,
        updated_at=_NOW,
        metadata=metadata or {},
    ))


def _make_activities(workspace, registry, artifact_registry, run_store):
    """Minimal ProcessingActivities — no compilers, no cache.
    The reuse short-circuit fires BEFORE the compiler lookup so the
    activity can run without one registered."""
    return ProcessingActivities(
        processing=None,  # type: ignore[arg-type]
        sources=registry,
        artifacts=artifact_registry,
        run_store=run_store,
    )


def test_refresh_enrich_clones_compile_artifacts(
    ctx, workspace, registry, artifact_registry,
):
    from j1.runs.store import JsonlIngestionRunStore
    run_store = JsonlIngestionRunStore(workspace)
    # Source run with two compile artifacts owned by doc-1.
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="src-a", run_id="run-source",
        kind="compiled.text",
    )
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="src-b", run_id="run-source",
        kind="chunk",
    )
    # Candidate run carries the reuse hint.
    _seed_run(
        run_store, ctx, run_id="run-new",
        metadata={"reused_compile_from_run_id": "run-source"},
    )
    activities = _make_activities(
        workspace, registry, artifact_registry, run_store,
    )

    result = activities._maybe_reuse_compile_artifacts(
        ctx, CompileActivityInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock",
            correlation_id="run-new",
        ),
    )
    assert result is not None
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 2

    clones = [
        artifact_registry.get(ctx, aid) for aid in result.artifact_ids
    ]
    # Every clone carries the new run_id + reused-from breadcrumb.
    for clone in clones:
        assert clone.metadata["run_id"] == "run-new"
        assert clone.metadata["reused_from_run_id"] == "run-source"
        assert "reused_from_artifact_id" in clone.metadata
    # The original source rows survive untouched (the source run
    # may still be the document's active run).
    original_a = artifact_registry.get(ctx, "src-a")
    assert original_a.metadata["run_id"] == "run-source"


def test_refresh_enrich_falls_through_when_no_metadata_hint(
    ctx, workspace, registry, artifact_registry,
):
    """No reuse hint → helper returns None so the caller runs the
    normal compile path. (The test confirms safe fall-through; the
    actual compile path isn't exercised here.)"""
    from j1.runs.store import JsonlIngestionRunStore
    run_store = JsonlIngestionRunStore(workspace)
    _seed_run(run_store, ctx, run_id="run-new", metadata={})
    activities = _make_activities(
        workspace, registry, artifact_registry, run_store,
    )
    result = activities._maybe_reuse_compile_artifacts(
        ctx, CompileActivityInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock",
            correlation_id="run-new",
        ),
    )
    assert result is None


def test_refresh_enrich_falls_through_when_source_run_has_no_artifacts(
    ctx, workspace, registry, artifact_registry,
):
    """Pointing at a run with no compile artifacts → degrade to
    full parse rather than return an empty-artifact result."""
    from j1.runs.store import JsonlIngestionRunStore
    run_store = JsonlIngestionRunStore(workspace)
    _seed_run(
        run_store, ctx, run_id="run-new",
        metadata={"reused_compile_from_run_id": "run-ghost"},
    )
    activities = _make_activities(
        workspace, registry, artifact_registry, run_store,
    )
    result = activities._maybe_reuse_compile_artifacts(
        ctx, CompileActivityInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock",
            correlation_id="run-new",
        ),
    )
    assert result is None


def test_refresh_enrich_falls_through_when_run_store_unwired(
    ctx, workspace, registry, artifact_registry,
):
    """No run_store collaborator (legacy/test wiring) → helper
    can't read metadata → fall through."""
    activities = ProcessingActivities(
        processing=None,  # type: ignore[arg-type]
        sources=registry,
        artifacts=artifact_registry,
        run_store=None,
    )
    result = activities._maybe_reuse_compile_artifacts(
        ctx, CompileActivityInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock",
            correlation_id="run-new",
        ),
    )
    assert result is None


def test_compile_activity_short_circuits_when_reuse_hint_present(
    ctx, workspace, registry, artifact_registry,
):
    """End-to-end: invoke the compile activity itself. With the
    reuse hint set, the activity returns SUCCESS without touching
    the compiler — we prove that by registering NO compiler kind
    and confirming the call still succeeds with cloned ids."""
    from j1.runs.store import JsonlIngestionRunStore
    from j1.documents.models import DocumentRecord
    run_store = JsonlIngestionRunStore(workspace)

    registry.add(DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="x.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="sha256:doc-1",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
    ))
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="src-a", run_id="run-source",
        kind="compiled.text",
    )
    _seed_run(
        run_store, ctx, run_id="run-new",
        metadata={"reused_compile_from_run_id": "run-source"},
    )

    # Provide a sentinel compiler so the kind lookup succeeds.
    # Its ``compile`` method MUST NOT be called — if the reuse
    # short-circuit fails to fire, the activity would crash on
    # the missing processing service.
    class _SentinelCompiler:
        kind = "sentinel"
        called = False

        def compile(self, *args, **kwargs):  # pragma: no cover
            type(self).called = True
            raise AssertionError(
                "compiler.compile must NOT be called when "
                "metadata.reused_compile_from_run_id is set",
            )

    activities = ProcessingActivities(
        processing=None,  # type: ignore[arg-type]
        sources=registry,
        artifacts=artifact_registry,
        compilers={"sentinel": _SentinelCompiler()},
        run_store=run_store,
    )
    result = activities.compile(CompileActivityInput(
        scope=ProjectScope.from_context(ctx),
        document_id="doc-1",
        processor_kind="sentinel",
        correlation_id="run-new",
    ))
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1
    assert _SentinelCompiler.called is False
    # Cloned record exists under the new run.
    clone = artifact_registry.get(ctx, result.artifact_ids[0])
    assert clone.metadata["run_id"] == "run-new"


def test_refresh_enrich_only_clones_records_for_this_document(
    ctx, workspace, registry, artifact_registry,
):
    """Source run had artifacts for two documents — the reuse must
    only clone records whose ``source_document_ids`` includes the
    candidate's ``document_id``."""
    from j1.runs.store import JsonlIngestionRunStore
    run_store = JsonlIngestionRunStore(workspace)
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="mine-1", run_id="run-source",
        source_document_ids=["doc-1"],
    )
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="other-1", run_id="run-source",
        source_document_ids=["doc-other"],
    )
    _seed_run(
        run_store, ctx, run_id="run-new",
        metadata={"reused_compile_from_run_id": "run-source"},
    )
    activities = _make_activities(
        workspace, registry, artifact_registry, run_store,
    )

    result = activities._maybe_reuse_compile_artifacts(
        ctx, CompileActivityInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock",
            correlation_id="run-new",
        ),
    )
    assert result is not None
    clones = [
        artifact_registry.get(ctx, aid) for aid in result.artifact_ids
    ]
    assert all(
        "doc-1" in c.source_document_ids for c in clones
    )
    assert not any(
        "doc-other" in c.source_document_ids for c in clones
    )
