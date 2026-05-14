"""Tests for the active-run promotion hook (Phase 4).

The hook lives in `RunsActivities._persist_run_terminal` — when a
run flips to a usable terminal state (succeeded / succeeded-with-
warnings) the activity updates the parent document's
`active_run_id` to point at the new run.

The load-bearing rule: a FAILED run must NOT promote, which is
exactly what makes "failed reindex doesn't clobber the previous
good run" hold.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus
from j1.orchestration.activities.payloads import ProjectScope
from j1.orchestration.activities.runs import (
    ReportRunTerminalInput, RunsActivities, StepSummaryEntry,
)
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def activities(run_store, registry, workspace):
    # Phase 7: promotion writes ``active_snapshot_id`` only —
    # ``active_run_id`` is no longer written. The activity needs a
    # ``DocumentSnapshotService`` wired so the snapshot-side
    # promotion runs.
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore

    snapshot_store = JsonlDocumentSnapshotStore(workspace)
    snapshot_service = DocumentSnapshotService(store=snapshot_store)
    return RunsActivities(
        progress_reporter=None,
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
    )


def _seed_document(
    registry, ctx: ProjectContext,
    *, document_id: str = "doc-1",
    state: str = "attached",
    active_snapshot_id: str | None = None,
) -> None:
    """Add a `DocumentRecord` to the project's documents.json."""
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_run(
    store, ctx: ProjectContext,
    *, run_id: str, document_id: str,
    status: RunStatus = RunStatus.RUNNING,
    parent_run_id: str | None = None,
) -> None:
    store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=_NOW,
        updated_at=_NOW,
        parent_run_id=parent_run_id,
    ))


def _terminate(
    activities, ctx: ProjectContext, run_id: str,
    *, final_status: str = "succeeded",
) -> None:
    """Invoke the activity to flip a run to terminal. Bypasses the
 Temporal wrapper — we test the activity body directly so the
 promotion side-effect is observable in-process."""
    activities._persist_run_terminal(
        ctx,
        ReportRunTerminalInput(
            scope=ProjectScope.from_context(ctx),
            run_id=run_id,
            final_status=final_status,
        ),
    )


# ---- Promotion-on-success ---------------------------------------


def test_succeeded_run_becomes_documents_active_snapshot(
    activities, run_store, registry, ctx,
):
    """Phase 7 headline contract: a usable-terminal run gets
    promoted on the SNAPSHOT side. ``active_snapshot_id`` is the
    canonical visibility key; ``active_run_id`` is no longer
    written by the promotion path."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id=None)
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1")

    _terminate(activities, ctx, "r-1", final_status="succeeded")

    doc = registry.get(ctx, "doc-1")
    # Snapshot side promoted.
    assert doc.active_snapshot_id is not None
    assert doc.active_snapshot_id.startswith("snap_")
    # Phase 9: ``active_run_id`` was deleted from the model;
    # nothing on the run side to assert against.


def test_succeeded_with_warnings_also_promotes(
    activities, run_store, registry, ctx,
):
    """`succeeded` and `succeeded_with_warnings` are both usable
 results — both promote the snapshot."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id=None)
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1")

    _terminate(
        activities, ctx, "r-1",
        final_status="succeeded_with_warnings",
    )

    assert registry.get(ctx, "doc-1").active_snapshot_id is not None


# ---- Non-promotion rules ----------------------------------------


def test_failed_run_does_not_promote(
    activities, run_store, registry, ctx,
):
    """The load-bearing rule: a FAILED run leaves the document's
 active_run_id pointing at the previous good run. This is what
 makes "failed reindex doesn't clobber" true."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id="r-good")
    _seed_run(run_store, ctx, run_id="r-bad", document_id="doc-1")

    _terminate(activities, ctx, "r-bad", final_status="failed")

    # active_run_id stays pinned to the previous run.
    assert registry.get(ctx, "doc-1").active_snapshot_id == "r-good"


def test_cancelled_run_does_not_promote(
    activities, run_store, registry, ctx,
):
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id="r-good")
    _seed_run(run_store, ctx, run_id="r-cancel", document_id="doc-1")

    _terminate(activities, ctx, "r-cancel", final_status="cancelled")

    assert registry.get(ctx, "doc-1").active_snapshot_id == "r-good"


def test_timed_out_run_does_not_promote(
    activities, run_store, registry, ctx,
):
    """`timed_out` maps to FAILED internally — same no-promotion rule."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id="r-good")
    _seed_run(run_store, ctx, run_id="r-timeout", document_id="doc-1")

    _terminate(activities, ctx, "r-timeout", final_status="timed_out")

    assert registry.get(ctx, "doc-1").active_snapshot_id == "r-good"


# ---- Reindex flow contract --------------------------------------


def test_failed_reindex_preserves_previous_successful_active(
    activities, run_store, registry, ctx,
):
    """Phase 7: failed re-index doesn't replace the previously
    promoted snapshot."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id=None)
    # First run: succeeds and becomes active.
    _seed_run(run_store, ctx, run_id="r-initial", document_id="doc-1")
    _terminate(activities, ctx, "r-initial", final_status="succeeded")
    promoted_snapshot = registry.get(ctx, "doc-1").active_snapshot_id
    assert promoted_snapshot is not None

    # Re-index attempt: fails. Must NOT replace ``active_snapshot_id``.
    _seed_run(run_store, ctx, run_id="r-reindex", document_id="doc-1")
    _terminate(activities, ctx, "r-reindex", final_status="failed")
    assert registry.get(ctx, "doc-1").active_snapshot_id == promoted_snapshot


def test_successful_reindex_promotes_to_new_active(
    activities, run_store, registry, ctx,
):
    """Phase 7: a SUCCESSFUL reindex flips the snapshot side."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id=None)
    _seed_run(run_store, ctx, run_id="r-old", document_id="doc-1")
    _terminate(activities, ctx, "r-old", final_status="succeeded")
    first_snapshot = registry.get(ctx, "doc-1").active_snapshot_id
    assert first_snapshot is not None

    _seed_run(
        run_store, ctx,
        run_id="r-new", document_id="doc-1",
        parent_run_id="r-old",
    )

    _terminate(activities, ctx, "r-new", final_status="succeeded")

    new_snapshot = registry.get(ctx, "doc-1").active_snapshot_id
    assert new_snapshot is not None
    assert new_snapshot != first_snapshot


# ---- Edge cases -------------------------------------------------


def test_promotion_is_noop_when_no_source_registry_wired(
    run_store, ctx,
):
    """Backward compat: deployments that haven't adopted the
 document-centric flow (no source_registry passed) still get
 clean terminal-status writes."""
    activities_no_registry = RunsActivities(
        progress_reporter=None,
        run_store=run_store,
        source_registry=None,
    )
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1")
    # Should not raise even though no registry is available.
    _terminate(activities_no_registry, ctx, "r-1", final_status="succeeded")
    # Run still got its terminal status.
    assert run_store.get(ctx, "r-1").status == RunStatus.SUCCEEDED


def test_promotion_skips_removed_documents(
    activities, run_store, registry, ctx,
):
    """A removed document has had its knowledge disowned. Even if a
 run mysteriously succeeds on it (race: workflow finished after
 the operator clicked Remove), we MUST NOT promote — that would
 silently bring the document back into retrieval."""
    _seed_document(
        registry, ctx, document_id="doc-1",
        state="removed", active_snapshot_id=None,
    )
    _seed_run(run_store, ctx, run_id="r-late", document_id="doc-1")

    _terminate(activities, ctx, "r-late", final_status="succeeded")

    # active_run_id stays None — removed documents have no usable
    # active result by definition.
    assert registry.get(ctx, "doc-1").active_snapshot_id is None
    assert registry.get(ctx, "doc-1").knowledge_state == "removed"


def test_promotion_idempotent_when_already_pointing_at_run(
    activities, run_store, registry, ctx,
):
    """Defensive: re-running the terminal hook for the same run
 (e.g. continue-as-new replays at a workflow boundary) shouldn't
 churn the document record."""
    _seed_document(registry, ctx, document_id="doc-1", active_snapshot_id="r-1")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1",
              status=RunStatus.SUCCEEDED)

    # Already active — second terminal-write shouldn't write again.
    _terminate(activities, ctx, "r-1", final_status="succeeded")
    assert registry.get(ctx, "doc-1").active_snapshot_id == "r-1"


def test_promotion_tolerates_missing_document(
    activities, run_store, registry, ctx,
):
    """If the document somehow got removed from the registry
 between run creation and run completion (race / manual cleanup
 / test fixture quirk), the activity must not raise. The run
 still flips to its terminal status; the promotion is just a
 no-op."""
    _seed_run(run_store, ctx, run_id="r-orphan", document_id="ghost-doc")

    _terminate(activities, ctx, "r-orphan", final_status="succeeded")

    # Run got its terminal status — no exception bubbled.
    assert run_store.get(ctx, "r-orphan").status == RunStatus.SUCCEEDED
