"""Tests for ``j1.memory.UnifiedMemoryResolver``.

The resolver returns a logical projection of "what is currently
queryable for this scope" by composing the document registry, run
store, snapshot store, and artifact registry. These tests cover the
behaviour rules pinned in ``docs/unified-memory-contract.md``:

  1. Compile succeeded + active snapshot promoted → queryable.
  2. Compile succeeded + enrichment never run → still queryable.
  3. Compile succeeded + enrichment failed → still queryable
     (queryable_status reports the failure as augmentation status).
  4. Compile succeeded + enrichment succeeded → enrichment_available.
  5. Compile failed → not queryable.
  6. Missing compile artifacts → not queryable, even when the
     document record has ``active_snapshot_id`` set.
  7. Old non-active run does not participate in active query.
  8. Explicit run scope can resolve a non-active run when its
     compile artifacts still exist.
  9. Project-active aggregate is queryable iff at least one document
     is queryable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.memory import (
    MemoryScope,
    QueryableStatus,
    UnifiedMemoryResolver,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---- Fixtures ------------------------------------------------------


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def resolver(registry, run_store, artifact_registry):
    return UnifiedMemoryResolver(
        registry=registry,
        run_store=run_store,
        artifact_registry=artifact_registry,
    )


def _doc(
    ctx, *, document_id="doc-1", state="attached",
    active_snapshot_id="snap-active",
    lifecycle_status="stable",
):
    return DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,
        active_snapshot_id=active_snapshot_id,
        lifecycle_status=lifecycle_status,
    )


def _run(
    *, run_id, document_id, status=RunStatus.SUCCEEDED,
    target_snapshot_id="snap-active",
    run_type="initial",
    metadata=None,
    started_at: datetime | None = None,
):
    started = started_at or _NOW
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started,
        updated_at=started,
        completed_at=started,
        metadata=metadata or {},
        run_type=run_type,
        target_snapshot_id=target_snapshot_id,
    )


def _artifact(
    ctx, *, artifact_id, kind, snapshot_id, document_id,
    created_by_run_id="r-baseline",
):
    # Production code stamps ``snapshot_id`` into BOTH the typed
    # field and ``metadata`` (see j1.documents.snapshot_artifact.
    # stamp_snapshot_metadata). The resolver reads either; we mirror
    # the dual stamping here so tests reflect real-world records.
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"compiled/{artifact_id}.bin",
        content_hash=f"sha256:{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=[document_id],
        metadata={
            "snapshot_id": snapshot_id,
            "run_id": created_by_run_id,
        },
        snapshot_id=snapshot_id,
        created_by_run_id=created_by_run_id,
    )


# ---- Document-active scope ----------------------------------------


def test_document_active_is_queryable_when_compile_succeeded(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 1: compile succeeded + active snapshot promoted →
    queryable."""
    registry.add(_doc(ctx))
    run_store.upsert(ctx, _run(run_id="r-baseline", document_id="doc-1"))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-chunk",
        kind="compiled.text",
        snapshot_id="snap-active",
        document_id="doc-1",
    ))

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.scope == MemoryScope.DOCUMENT_ACTIVE
    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.QUERYABLE
    assert view.snapshot_id == "snap-active"
    assert view.active_run_id == "r-baseline"
    assert view.compile_status == "succeeded"
    assert "a-chunk" in view.compile_artifact_refs
    assert view.enrichment_status is None
    assert view.queryable_reason is None


def test_document_active_stays_queryable_when_enrichment_failed(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 3: compile succeeded + enrichment failed → still
    queryable, with the enrichment failure surfaced separately."""
    registry.add(_doc(ctx))
    run_store.upsert(ctx, _run(run_id="r-baseline", document_id="doc-1"))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-chunk",
        kind="compiled.text",
        snapshot_id="snap-active",
        document_id="doc-1",
    ))
    # A later run_domain_enrichment attempt failed against this
    # snapshot. The active snapshot must NOT regress.
    run_store.upsert(ctx, _run(
        run_id="r-enrich-failed",
        document_id="doc-1",
        status=RunStatus.FAILED,
        run_type="run_domain_enrichment",
        target_snapshot_id="snap-candidate-enrich",
        metadata={
            "manual_action_source_snapshot_id": "snap-active",
        },
        started_at=_NOW + timedelta(hours=1),
    ))

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.ENRICHMENT_FAILED
    assert view.compile_status == "succeeded"
    assert view.enrichment_status == "failed"


def test_document_active_marks_enrichment_available_on_success(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 4: enrichment succeeded → ``enrichment_available`` +
    enrichment artifact refs surface."""
    registry.add(_doc(ctx))
    run_store.upsert(ctx, _run(run_id="r-baseline", document_id="doc-1"))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-chunk",
        kind="compiled.text",
        snapshot_id="snap-active",
        document_id="doc-1",
    ))
    run_store.upsert(ctx, _run(
        run_id="r-enrich-ok",
        document_id="doc-1",
        status=RunStatus.SUCCEEDED,
        run_type="run_domain_enrichment",
        target_snapshot_id="snap-active",  # promoted in
        metadata={"manual_action_source_snapshot_id": "snap-active"},
        started_at=_NOW + timedelta(hours=1),
    ))
    # Enrichment artifacts under the active snapshot.
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-enrich",
        kind="enriched.metadata",
        snapshot_id="snap-active",
        document_id="doc-1",
        created_by_run_id="r-enrich-ok",
    ))

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.ENRICHMENT_AVAILABLE
    assert view.enrichment_status == "succeeded"
    assert "a-enrich" in view.enrichment_artifact_refs


def test_document_compile_failed_is_not_queryable(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 5: the producing run failed before any active snapshot
    promoted → not queryable, with a clear reason."""
    registry.add(_doc(ctx, active_snapshot_id=None))
    run_store.upsert(ctx, _run(
        run_id="r-failed",
        document_id="doc-1",
        status=RunStatus.FAILED,
    ))

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable is False
    assert view.queryable_status == QueryableStatus.COMPILE_FAILED
    assert view.queryable_reason is not None
    assert "failed" in view.queryable_reason.lower()
    assert view.compile_status == "failed"


def test_document_missing_compile_artifacts_is_not_queryable(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 6: DB pointer is set but artifacts don't resolve →
    not queryable."""
    registry.add(_doc(ctx))
    run_store.upsert(ctx, _run(run_id="r-baseline", document_id="doc-1"))
    # No compile artifacts written for snap-active.

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable is False
    assert view.queryable_status == QueryableStatus.MISSING_ARTIFACTS
    assert view.compile_status == "missing"
    assert view.snapshot_id == "snap-active"


def test_document_not_attached_is_not_queryable(
    ctx, registry, resolver,
):
    """Detached / removed documents are intentionally invisible to
    active query."""
    registry.add(_doc(ctx, state="detached"))
    view = resolver.resolve_document_active_memory(ctx, "doc-1")
    assert view.queryable_status == QueryableStatus.NOT_ATTACHED


def test_document_removed_reports_removed_status(
    ctx, registry, resolver,
):
    registry.add(_doc(
        ctx, state="removed",
        active_snapshot_id=None,
        lifecycle_status="removed",
    ))
    view = resolver.resolve_document_active_memory(ctx, "doc-1")
    assert view.queryable_status == QueryableStatus.REMOVED


def test_document_unknown_id_is_not_queryable_with_reason(
    ctx, resolver,
):
    view = resolver.resolve_document_active_memory(ctx, "ghost")
    assert view.queryable is False
    assert "not found" in (view.queryable_reason or "").lower()


# ---- Stale-run prevention ------------------------------------------


def test_old_succeeded_run_does_not_participate_in_active_view(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 7: an older succeeded run whose snapshot is NOT the
    active one must not be the active_run_id in the document view.
    The active view binds to the producing run of the CURRENT active
    snapshot only."""
    registry.add(_doc(ctx, active_snapshot_id="snap-new"))
    # Older run + its artifacts (snap-old). NOT the active.
    run_store.upsert(ctx, _run(
        run_id="r-old",
        document_id="doc-1",
        target_snapshot_id="snap-old",
        started_at=_NOW - timedelta(hours=2),
    ))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-old",
        kind="compiled.text",
        snapshot_id="snap-old",
        document_id="doc-1",
        created_by_run_id="r-old",
    ))
    # New (active) run + its artifacts.
    run_store.upsert(ctx, _run(
        run_id="r-new",
        document_id="doc-1",
        target_snapshot_id="snap-new",
    ))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-new",
        kind="compiled.text",
        snapshot_id="snap-new",
        document_id="doc-1",
        created_by_run_id="r-new",
    ))

    view = resolver.resolve_document_active_memory(ctx, "doc-1")
    assert view.active_run_id == "r-new"
    assert view.snapshot_id == "snap-new"
    assert "a-new" in view.compile_artifact_refs
    # Critically: old artifacts MUST NOT leak into the active view's
    # ref list.
    assert "a-old" not in view.compile_artifact_refs


# ---- Run-explicit scope --------------------------------------------


def test_run_explicit_resolves_non_active_run_when_artifacts_exist(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 8: explicit run scope can resolve an OLDER run for audit
    if its artifacts still exist. The view reports
    ``is_active_for_document=False``."""
    registry.add(_doc(ctx, active_snapshot_id="snap-new"))
    run_store.upsert(ctx, _run(
        run_id="r-old",
        document_id="doc-1",
        target_snapshot_id="snap-old",
        started_at=_NOW - timedelta(hours=2),
    ))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-old",
        kind="compiled.text",
        snapshot_id="snap-old",
        document_id="doc-1",
        created_by_run_id="r-old",
    ))
    run_store.upsert(ctx, _run(
        run_id="r-new",
        document_id="doc-1",
        target_snapshot_id="snap-new",
    ))

    view = resolver.resolve_run_memory(ctx, "r-old")

    assert view.scope == MemoryScope.RUN_EXPLICIT
    assert view.run_id == "r-old"
    assert view.queryable_status == QueryableStatus.QUERYABLE
    assert view.snapshot_id == "snap-old"
    assert view.is_active_for_document is False
    assert "a-old" in view.compile_artifact_refs


def test_run_explicit_reports_failed_compile_when_run_failed(
    ctx, registry, run_store, resolver,
):
    run_store.upsert(ctx, _run(
        run_id="r-fail",
        document_id="doc-1",
        status=RunStatus.FAILED,
        target_snapshot_id="snap-fail",
    ))
    view = resolver.resolve_run_memory(ctx, "r-fail")
    assert view.queryable is False
    assert view.queryable_status == QueryableStatus.COMPILE_FAILED


def test_run_explicit_unknown_run_returns_run_unknown(ctx, resolver):
    view = resolver.resolve_run_memory(ctx, "ghost")
    assert view.queryable_status == QueryableStatus.RUN_UNKNOWN


def test_run_explicit_missing_artifacts_reports_missing(
    ctx, run_store, resolver,
):
    """Run succeeded according to status, but its compile artifacts
    were cleaned up — explicit run scope reports missing artifacts."""
    run_store.upsert(ctx, _run(
        run_id="r-cleaned",
        document_id="doc-1",
        target_snapshot_id="snap-cleaned",
        status=RunStatus.SUCCEEDED,
    ))
    view = resolver.resolve_run_memory(ctx, "r-cleaned")
    assert view.queryable is False
    assert view.queryable_status == QueryableStatus.MISSING_ARTIFACTS


# ---- Project-active scope -----------------------------------------


def test_project_active_aggregates_queryable_when_any_document_is(
    ctx, registry, run_store, artifact_registry, resolver,
):
    """Rule 9 happy path: a mix of queryable + not-queryable
    documents → the project aggregate is queryable."""
    registry.add(_doc(ctx, document_id="doc-ok"))
    run_store.upsert(ctx, _run(run_id="r-ok", document_id="doc-ok"))
    artifact_registry.add(_artifact(
        ctx, artifact_id="a-ok", kind="compiled.text",
        snapshot_id="snap-active", document_id="doc-ok",
    ))
    # Another document with no active snapshot — not queryable.
    registry.add(_doc(
        ctx, document_id="doc-pending",
        active_snapshot_id=None,
    ))

    view = resolver.resolve_project_active_memory(ctx)

    assert view.scope == MemoryScope.PROJECT_ACTIVE
    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.QUERYABLE
    assert len(view.documents) == 2
    assert len(view.queryable_documents) == 1
    by_id = {d.document_id: d for d in view.documents}
    assert by_id["doc-ok"].queryable_status == QueryableStatus.QUERYABLE
    assert by_id["doc-pending"].queryable_status == QueryableStatus.NOT_STARTED


def test_project_active_with_only_failed_documents_is_not_queryable(
    ctx, registry, run_store, resolver,
):
    """Aggregate reports the most diagnostic failure when no
    document is queryable."""
    registry.add(_doc(ctx, document_id="doc-failed",
                      active_snapshot_id=None))
    run_store.upsert(ctx, _run(
        run_id="r-fail", document_id="doc-failed",
        status=RunStatus.FAILED,
    ))

    view = resolver.resolve_project_active_memory(ctx)
    assert view.queryable is False
    assert view.queryable_status == QueryableStatus.COMPILE_FAILED


def test_project_active_with_no_documents_is_not_started(ctx, resolver):
    view = resolver.resolve_project_active_memory(ctx)
    assert view.queryable_status == QueryableStatus.NOT_STARTED
    assert view.documents == ()
