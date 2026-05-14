"""Tests for `j1.documents.projector` — the document-centric
read-side projection.

These tests pin the action matrix from the spec's section 8 so the
FE can rely on server-side `availableActions`. The matrix is:

  attached:
    view, reindex, detach, remove
    + resume   ← when active run failed AFTER compile succeeded

  detached:
    view, attach, remove
    (NO reindex — operator must attach first)

  removed:
    view   ← admin/history only, no mutating actions

Test names spell out the matrix row by row so a regression on the
state machine is obvious in the failure output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.documents.projector import (
    compute_available_actions,
    project_document_detail,
    project_document_summary,
    project_run_history,
)
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _doc(
    *, state: str = "attached",
    active_snapshot_id: str | None = "r-1",
    removed_at: datetime | None = None,
) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        project=ProjectContext(tenant_id="t", project_id="p"),
        original_filename="bridge.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum="sha256:abc",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id=active_snapshot_id,
        latest_version_id="dv-1",
        removed_at=removed_at,
        updated_at=_NOW,
    )


def _run(
    *,
    run_id: str = "r-1",
    status: RunStatus = RunStatus.SUCCEEDED,
    started_at: datetime | None = None,
    has_compile_checkpoint: bool = False,
    failure_code: str | None = None,
    run_type: str = "initial",
) -> IngestionRun:
    # ``has_compile_checkpoint`` is accepted for caller compatibility
    # but ignored — the projector no longer offers "resume" so no test
    # actually exercises a compile-checkpoint-only state any more.
    del has_compile_checkpoint
    return IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started_at or _NOW,
        updated_at=started_at or _NOW,
        completed_at=_NOW if status == RunStatus.SUCCEEDED else None,
        failure_code=failure_code,
        metadata={},
        run_type=run_type,  # type: ignore[arg-type]
    )


# ---- Action matrix --------------------------------------------------


def test_attached_document_with_succeeded_active_run_offers_reindex_detach_remove():
    """Spec row 1: attached + succeeded → standard set, no resume."""
    actions = compute_available_actions(
        document=_doc(state="attached"),
        active_run=_run(status=RunStatus.SUCCEEDED),
    )
    assert "view" in actions
    assert "reindex" in actions
    assert "detach" in actions
    assert "remove" in actions
    assert "resume" not in actions
    assert "attach" not in actions  # already attached


def test_attached_document_with_failed_run_post_compile_never_offers_resume():
    """Run-level resume is gone: an immutable run never reanimates.
    A failure (whenever it happened) only offers re-index — the new
    run starts from the original uploaded file."""
    actions = compute_available_actions(
        document=_doc(state="attached"),
        active_run=_run(
            status=RunStatus.FAILED,
            has_compile_checkpoint=True,
            failure_code="ENRICH_FAILED",
        ),
    )
    assert "resume" not in actions
    assert "reindex" in actions


def test_attached_document_with_failed_run_pre_compile_does_not_offer_resume():
    """Same as above for failures BEFORE compile — resume is never
    offered, only re-index."""
    actions = compute_available_actions(
        document=_doc(state="attached"),
        active_run=_run(
            status=RunStatus.FAILED,
            has_compile_checkpoint=False,
            failure_code="ASSESS_FAILED",
        ),
    )
    assert "resume" not in actions
    assert "reindex" in actions


def test_attached_document_without_active_run_omits_resume():
    """A document with no active run yet (just uploaded, first
 ingestion still queued) can't offer resume."""
    actions = compute_available_actions(
        document=_doc(state="attached", active_snapshot_id=None),
        active_run=None,
    )
    assert actions == ("view", "reindex", "detach", "remove")


def test_detached_document_offers_attach_remove_view():
    """Spec row 2: detached → view, attach, remove. NOT reindex
 (operator must attach first per the spec's UX-simplicity rule)."""
    actions = compute_available_actions(
        document=_doc(state="detached"),
        active_run=_run(status=RunStatus.SUCCEEDED),
    )
    assert "view" in actions
    assert "attach" in actions
    assert "remove" in actions
    assert "reindex" not in actions
    assert "detach" not in actions  # already detached
    assert "resume" not in actions  # detached doc resume is hidden


def test_detached_document_does_not_offer_resume_even_when_compile_checkpoint_exists():
    """Detached must hide resume even when the active failed run
 would technically be resumable. Spec: 'Hide or disable Resume.
 User should attach the document first.'"""
    actions = compute_available_actions(
        document=_doc(state="detached"),
        active_run=_run(
            status=RunStatus.FAILED, has_compile_checkpoint=True,
        ),
    )
    assert "resume" not in actions


def test_removed_document_offers_only_view():
    """Spec row 3: removed → view only. No mutating actions —
 the FE's normal list excludes removed docs entirely; the
 admin/history view uses the view action to surface them."""
    actions = compute_available_actions(
        document=_doc(state="removed", active_snapshot_id=None,
                      removed_at=_NOW),
        active_run=None,
    )
    assert actions == ("view",)


def test_removed_document_with_residual_active_run_still_only_offers_view():
    """Defensive: even if a removed document somehow still has a
 succeeded active_run pointing somewhere (data quirk), the
 actions must stay restricted to view."""
    actions = compute_available_actions(
        document=_doc(state="removed", active_snapshot_id="r-1",
                      removed_at=_NOW),
        active_run=_run(status=RunStatus.SUCCEEDED),
    )
    assert actions == ("view",)


# ---- Summary projection --------------------------------------------


def test_summary_includes_active_run_status_in_result_summary():
    """The current-result summary reflects the active run's
 status. FE renders status badge directly from this."""
    runs = [_run(run_id="r-1", status=RunStatus.SUCCEEDED)]
    dto = project_document_summary(document=_doc(), runs=runs)
    assert dto.current_result_summary.status == "succeeded"
    assert dto.display_name == "bridge.pdf"
    assert dto.knowledge_state == "attached"
    assert dto.active_snapshot_id == "r-1"


def test_summary_returns_none_status_when_no_active_run():
    """Uploaded but unprocessed doc: status="none" so the FE shows
 "not yet processed" rather than a misleading empty badge."""
    dto = project_document_summary(
        document=_doc(active_snapshot_id=None),
        runs=[],
    )
    assert dto.current_result_summary.status == "none"
    assert dto.active_snapshot_id is None


def test_summary_caps_run_history_to_three_most_recent():
    """List view caps run history at 3 — keeps payloads bounded.
 Detail endpoint returns the full history."""
    runs = [
        _run(run_id=f"r-{i}", started_at=_NOW + timedelta(minutes=i))
        for i in range(10)
    ]
    dto = project_document_summary(
        document=_doc(active_snapshot_id="r-9"), runs=runs,
    )
    assert len(dto.run_history_summary) == 3
    # Most recent first.
    assert dto.run_history_summary[0].run_id == "r-9"


def test_summary_marks_active_run_in_history():
    """The active run row carries `is_active=True` so the FE can
 highlight it in the run-history table."""
    runs = [
        _run(run_id="r-1", started_at=_NOW),
        _run(run_id="r-2", started_at=_NOW + timedelta(minutes=1)),
    ]
    dto = project_document_summary(
        document=_doc(active_snapshot_id="r-2"), runs=runs,
    )
    active_rows = [r for r in dto.run_history_summary if r.is_active]
    assert len(active_rows) == 1
    assert active_rows[0].run_id == "r-2"


def test_summary_falls_back_to_none_when_no_active_snapshot():
    """Phase 9: without an active_snapshot_id, the projector
 doesn't surface a current result — even when a succeeded run
 exists. The snapshot pointer is the visibility gate."""
    dto = project_document_summary(
        document=_doc(active_snapshot_id=None),
        runs=[_run(run_id="r-1")],
    )
    assert dto.current_result_summary.status == "none"


def test_summary_includes_step_statuses_from_run_metadata():
    """Stage statuses (compile/enrich/validate) come from the
 active run's `metadata.step_results` dict — same contract the
 existing run-detail surface reads."""
    run = _run(run_id="r-1", status=RunStatus.SUCCEEDED)
    run.metadata["step_results"] = {
        "compile":  {"status": "completed"},
        "enrich":   {"status": "completed"},
        "validate": {"status": "failed"},
    }
    dto = project_document_summary(
        document=_doc(active_snapshot_id="r-1"), runs=[run],
    )
    assert dto.current_result_summary.compile_status == "completed"
    assert dto.current_result_summary.enrichment_status == "completed"
    assert dto.current_result_summary.validation_status == "failed"


# ---- Detail projection --------------------------------------------


def test_detail_returns_full_history_not_capped():
    """Detail view returns ALL runs, not just the top 3."""
    runs = [_run(run_id=f"r-{i}", started_at=_NOW + timedelta(minutes=i))
            for i in range(10)]
    dto = project_document_detail(
        document=_doc(active_snapshot_id="r-9"), runs=runs,
    )
    assert len(dto.run_history) == 10
    # Still most-recent-first.
    assert dto.run_history[0].run_id == "r-9"
    assert dto.run_history[-1].run_id == "r-0"


def test_detail_and_summary_share_action_matrix():
    """Whatever actions the summary computes, the detail view must
 emit the same — they're computed by the same helper."""
    doc = _doc(state="detached")
    runs = [_run(status=RunStatus.SUCCEEDED)]
    summary = project_document_summary(document=doc, runs=runs)
    detail = project_document_detail(document=doc, runs=runs)
    assert summary.available_actions == detail.available_actions


# ---- Run-history-only endpoint ------------------------------------


def test_project_run_history_returns_sorted_compact_rows():
    runs = [
        _run(run_id="r-1", started_at=_NOW),
        _run(run_id="r-2", started_at=_NOW + timedelta(hours=1)),
    ]
    history = project_run_history(
        document=_doc(active_snapshot_id="r-2"), runs=runs,
    )
    assert [h.run_id for h in history] == ["r-2", "r-1"]
    assert history[0].is_active is True
    assert history[1].is_active is False


def test_project_run_history_returns_empty_for_unprocessed_document():
    history = project_run_history(
        document=_doc(active_snapshot_id=None), runs=[],
    )
    assert history == ()


# ---- Run-level capability flags --------------------------------


def _enriched_step_metadata() -> dict:
    return {"step_results": {"enrich": {"status": "completed"}}}


def test_run_capability_flags_for_active_enriched_run():
    """The active run with completed enrichment exposes
    ``can_refresh_enrichment`` (not ``can_run_enrichment``) and is
    NOT delete-eligible."""
    older = _run(
        run_id="r-old", status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    active = _run(run_id="r-active", status=RunStatus.SUCCEEDED)
    # The projector reads ``step_results`` off ``metadata``.
    active.metadata = _enriched_step_metadata()

    history = project_run_history(
        document=_doc(active_snapshot_id="snap-active"),
        runs=[older, active],
    )
    by_id = {h.run_id: h for h in history}

    assert by_id["r-active"].is_active is True
    assert by_id["r-active"].is_only_run is False
    assert by_id["r-active"].can_delete_run is False
    assert by_id["r-active"].can_refresh_enrichment is True
    assert by_id["r-active"].can_run_enrichment is False


def test_run_capability_flags_for_active_run_without_enrichment():
    """An active run that never produced enrichment exposes
    ``can_run_enrichment`` instead of refresh."""
    older = _run(
        run_id="r-old", status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    active = _run(run_id="r-active", status=RunStatus.SUCCEEDED)
    # No step_results.enrich → flag flips to run_enrichment.

    history = project_run_history(
        document=_doc(active_snapshot_id="snap-active"),
        runs=[older, active],
    )
    by_id = {h.run_id: h for h in history}
    assert by_id["r-active"].can_refresh_enrichment is False
    assert by_id["r-active"].can_run_enrichment is True


def test_run_capability_flags_for_non_active_run():
    """A historical non-active run is delete-eligible but NOT
    enrichment-eligible."""
    older = _run(
        run_id="r-old", status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    active = _run(run_id="r-active", status=RunStatus.SUCCEEDED)

    history = project_run_history(
        document=_doc(active_snapshot_id="snap-active"),
        runs=[older, active],
    )
    older_summary = next(h for h in history if h.run_id == "r-old")
    assert older_summary.is_active is False
    assert older_summary.is_only_run is False
    assert older_summary.can_delete_run is True
    assert older_summary.can_refresh_enrichment is False
    assert older_summary.can_run_enrichment is False


def test_run_capability_flags_for_only_run_blocks_delete():
    """A document with exactly one run cannot have that run deleted
    via the run endpoint — ``is_only_run`` is True and
    ``can_delete_run`` is False even though the run isn't active."""
    only = _run(run_id="r-only", status=RunStatus.FAILED)

    history = project_run_history(
        document=_doc(active_snapshot_id=None), runs=[only],
    )
    summary = history[0]
    assert summary.is_only_run is True
    assert summary.is_active is False
    assert summary.can_delete_run is False


def test_run_capability_flags_for_inflight_run_blocks_everything():
    """A RUNNING run is in-flight; delete is refused and no
    enrichment action is offered."""
    active = _run(
        run_id="r-active", status=RunStatus.SUCCEEDED,
        started_at=_NOW - timedelta(hours=1),
    )
    inflight = _run(run_id="r-inflight", status=RunStatus.RUNNING)

    history = project_run_history(
        document=_doc(active_snapshot_id="snap-active"),
        runs=[active, inflight],
    )
    by_id = {h.run_id: h for h in history}
    assert by_id["r-inflight"].can_delete_run is False
    assert by_id["r-inflight"].can_refresh_enrichment is False
    assert by_id["r-inflight"].can_run_enrichment is False


def test_run_capability_flags_when_document_detached():
    """A detached document still allows historical runs to be
    deleted (so operators can clean up), but never offers
    enrichment actions."""
    older = _run(
        run_id="r-old", status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    active = _run(run_id="r-active", status=RunStatus.SUCCEEDED)
    active.metadata = _enriched_step_metadata()

    history = project_run_history(
        document=_doc(state="detached", active_snapshot_id="snap-active"),
        runs=[older, active],
    )
    by_id = {h.run_id: h for h in history}
    # No enrichment actions on a detached document.
    assert by_id["r-active"].can_refresh_enrichment is False
    assert by_id["r-active"].can_run_enrichment is False
    # The older run is still safely delete-eligible.
    assert by_id["r-old"].can_delete_run is True
