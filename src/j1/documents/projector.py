"""Read-side projection from documents/runs → document-centric DTOs.

Phase 6 of the document-centric refactor. The projector is the
*only* place where the document-centric wire shape is computed —
the REST adapter calls it and renders the result; the FE consumes
the result; the service layer never builds these DTOs by hand. One
choke point means the available-actions logic stays in one place
and the FE never has to infer state-dependent action rules.

What's exposed:

* ``DocumentSummaryDTO`` — list-view projection. One per
  document, with the fields the FE's document-list row needs.

* ``DocumentDetailDTO`` — detail-view projection. Extends the
  summary with the full run history (most recent first) and the
  active-run result summary.

* ``DocumentRunSummaryDTO`` — compact per-run row for the run
  history panel.

* ``compute_available_actions()`` — the state machine that decides
  which actions a given document/active-run pair allows. Tests
  cover the matrix; the FE just renders whatever this returns.

Action matrix (from the spec's section 8):

  attached:
    view, reindex, detach, remove
    + resume   ← if the active run failed AFTER compile succeeded

  detached:
    view, attach, remove
    (reindex is intentionally NOT here — operator should attach
     first; keeps the UX simple)

  removed:
    view   ← admin/history only; no mutating actions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from j1.documents.models import DocumentRecord, KnowledgeState
from j1.runs.models import IngestionRun, RunStatus, RunType


# All the actions the FE knows how to render. Keeping this as a
# tuple (not a literal) so the projector can dynamically include /
# exclude entries based on document + run state without touching
# the type system.
Action = str


@dataclass(frozen=True)
class DocumentRunSummaryDTO:
    """Compact per-run row for the run-history panel on the
    document detail page.

    Carries just enough to render one row (badge color, label,
    timestamps) — full per-run detail comes from the existing
    `/ingestion-runs/{id}` endpoints. Sorted by `started_at`
    descending in the parent DTO.
    """

    run_id: str
    run_type: RunType
    status: str  # RunStatus value
    started_at: datetime
    completed_at: datetime | None
    failure_code: str | None = None
    is_active: bool = False
    # Operator-visible version chip (``DDMMYYYY-NN`` per document
    # per day). Set by the run-creation layer when allocated;
    # legacy runs that pre-date this field surface as ``None`` and
    # the FE renders nothing — graceful degradation.
    display_version: str | None = None


@dataclass(frozen=True)
class DocumentResultSummaryDTO:
    """Roll-up of the document's *current usable* result.

    Derived from the active run's metadata + status. Empty
    (`status="none"`) when the document has no active run yet
    (just uploaded; first ingestion still queued; or removed and
    `active_run_id` was cleared).
    """

    status: str  # "completed" | "completed_with_warnings" | "failed" | "running" | "none"
    compile_status: str | None = None
    enrichment_status: str | None = None
    validation_status: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class DocumentSummaryDTO:
    """List-view projection. One per document on the document list.

    `current_result_summary` and `available_actions` are derived
    server-side so the FE doesn't have to know the rules.
    """

    document_id: str
    display_name: str
    knowledge_state: KnowledgeState
    active_snapshot_id: str | None
    latest_version_id: str | None
    created_at: datetime
    updated_at: datetime | None
    removed_at: datetime | None
    current_result_summary: DocumentResultSummaryDTO
    available_actions: tuple[Action, ...]
    run_history_summary: tuple[DocumentRunSummaryDTO, ...]


@dataclass(frozen=True)
class DocumentDetailDTO:
    """Detail-view projection. Same as `DocumentSummaryDTO` but with
    the full run history (most recent first) instead of just the
    summary truncation.
    """

    document_id: str
    display_name: str
    knowledge_state: KnowledgeState
    active_snapshot_id: str | None
    latest_version_id: str | None
    created_at: datetime
    updated_at: datetime | None
    removed_at: datetime | None
    current_result_summary: DocumentResultSummaryDTO
    available_actions: tuple[Action, ...]
    run_history: tuple[DocumentRunSummaryDTO, ...]


# ---- Action matrix --------------------------------------------------


_BASE_ACTIONS_BY_STATE: dict[KnowledgeState, tuple[Action, ...]] = {
    # "view" is always available — the FE renders it as the click
    # target on each row. Other actions are state-dependent.
    # ``refresh_enrich`` is appended dynamically below when an
    # active run exists to reuse compile output from.
    "attached": ("view", "reindex", "detach", "remove"),
    "detached": ("view", "attach", "remove"),
    "removed": ("view",),
}


def compute_available_actions(
    *,
    document: DocumentRecord,
    active_run: IngestionRun | None,
) -> tuple[Action, ...]:
    """Return the actions the FE should render for this document.

    Composition: start with the base set for the document's
    knowledge state, then append ``"refresh_enrich"`` when the
    active run already produced a usable compile artifact.

    ``"resume"`` is intentionally NEVER included: a run is an
    immutable execution record. When a previous run failed, the
    user re-runs the document via ``"reindex"`` — the new run
    starts from the original uploaded file.
    """
    base = _BASE_ACTIONS_BY_STATE.get(document.knowledge_state, ("view",))
    actions: list[Action] = list(base)
    if document.knowledge_state == "attached" and active_run is not None:
        # ``refresh_enrich`` is only meaningful when an active run
        # already produced a compile artifact to reuse. Gating it on
        # ``active_run_id`` AND a successful active run avoids the
        # 409 cycle ("refresh-enrich rejected — no active run").
        if active_run.status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        ):
            actions.append("refresh_enrich")
    return tuple(actions)


# ---- DTO builders ----------------------------------------------------


# Maximum runs to include in `run_history_summary` on the LIST
# endpoint. Detail endpoint returns the full history. List view
# only needs the most-recent few — a project with hundreds of
# documents shouldn't ship megabytes of run history per list call.
_LIST_RUN_HISTORY_CAP = 3


def project_document_summary(
    *,
    document: DocumentRecord,
    runs: list[IngestionRun],
) -> DocumentSummaryDTO:
    """Build a `DocumentSummaryDTO` from raw model objects.

    Phase 9: ``active_snapshot_id`` is the visibility key.
    ``active_run`` is derived heuristically (most recent succeeded
    run) for display purposes — operators still want a "current
    result" panel in the FE, but visibility-correctness is owned by
    the snapshot side.
    """
    active_run = _find_active_run(document, runs)
    sorted_runs = _sort_runs_desc(runs)
    active_run_id = active_run.run_id if active_run else None
    history = tuple(
        _to_run_summary(r, is_active=(r.run_id == active_run_id))
        for r in sorted_runs[:_LIST_RUN_HISTORY_CAP]
    )
    return DocumentSummaryDTO(
        document_id=document.document_id,
        display_name=document.original_filename,
        knowledge_state=document.knowledge_state,
        active_snapshot_id=document.active_snapshot_id,
        latest_version_id=document.latest_version_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        removed_at=document.removed_at,
        current_result_summary=_to_result_summary(active_run),
        available_actions=compute_available_actions(
            document=document, active_run=active_run,
        ),
        run_history_summary=history,
    )


def project_document_detail(
    *,
    document: DocumentRecord,
    runs: list[IngestionRun],
) -> DocumentDetailDTO:
    """Detail-view projection — same as the summary but with the
    full run history (not capped)."""
    active_run = _find_active_run(document, runs)
    sorted_runs = _sort_runs_desc(runs)
    active_run_id = active_run.run_id if active_run else None
    history = tuple(
        _to_run_summary(r, is_active=(r.run_id == active_run_id))
        for r in sorted_runs
    )
    return DocumentDetailDTO(
        document_id=document.document_id,
        display_name=document.original_filename,
        knowledge_state=document.knowledge_state,
        active_snapshot_id=document.active_snapshot_id,
        latest_version_id=document.latest_version_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        removed_at=document.removed_at,
        current_result_summary=_to_result_summary(active_run),
        available_actions=compute_available_actions(
            document=document, active_run=active_run,
        ),
        run_history=history,
    )


def project_run_history(
    *, document: DocumentRecord, runs: list[IngestionRun],
) -> tuple[DocumentRunSummaryDTO, ...]:
    """Just the run-history list — used by the dedicated
    `GET /documents/{id}/runs` endpoint when callers want only the
    history without the summary roll-up."""
    active_run = _find_active_run(document, runs)
    active_run_id = active_run.run_id if active_run else None
    return tuple(
        _to_run_summary(r, is_active=(r.run_id == active_run_id))
        for r in _sort_runs_desc(runs)
    )


# ---- Internals -------------------------------------------------------


def _find_active_run(
    document: DocumentRecord, runs: list[IngestionRun],
) -> IngestionRun | None:
    """Phase 9: derive the display-active run heuristically.

    The canonical visibility key is ``document.active_snapshot_id``;
    the projector picks a representative run for the FE's
    "current result" panel. Selection rule:

      1. Latest succeeded / succeeded-with-warnings run.
      2. Otherwise None — the FE renders "no current result".

    Resumable-failure detection is preserved by the existing
    ``compute_available_actions`` helper, which gates on the
    selected active run's status.
    """
    if not document.active_snapshot_id:
        return None
    sorted_runs = _sort_runs_desc(runs)
    for r in sorted_runs:
        if r.status in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        ):
            return r
    return None


def _sort_runs_desc(runs: list[IngestionRun]) -> list[IngestionRun]:
    return sorted(
        runs,
        key=lambda r: (r.started_at, r.updated_at),
        reverse=True,
    )


def _to_run_summary(
    run: IngestionRun, *, is_active: bool,
) -> DocumentRunSummaryDTO:
    return DocumentRunSummaryDTO(
        run_id=run.run_id,
        run_type=run.run_type,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        started_at=run.started_at,
        completed_at=run.completed_at,
        failure_code=run.failure_code,
        is_active=is_active,
        display_version=run.display_version,
    )


def _to_result_summary(active_run: IngestionRun | None) -> DocumentResultSummaryDTO:
    """Project the active run's terminal state into the
    user-facing result summary. When no active run exists the
    summary reports ``status="none"`` so the FE renders a "not
    yet processed" affordance instead of a misleading status.

    Stage statuses (compile / enrichment / validation) come from
    the run's `metadata.step_results` dict when present — same
    contract the existing run-detail surface reads.
    """
    if active_run is None:
        return DocumentResultSummaryDTO(status="none")
    status_value = (
        active_run.status.value
        if hasattr(active_run.status, "value")
        else str(active_run.status)
    )
    step_results = active_run.metadata.get("step_results")
    if not isinstance(step_results, dict):
        step_results = {}
    return DocumentResultSummaryDTO(
        status=status_value,
        compile_status=_step_status(step_results, "compile"),
        enrichment_status=_step_status(step_results, "enrich"),
        validation_status=_step_status(step_results, "validate"),
        failure_code=active_run.failure_code,
    )


def _step_status(step_results: dict, step_name: str) -> str | None:
    entry = step_results.get(step_name)
    if isinstance(entry, dict):
        status = entry.get("status")
        if isinstance(status, str):
            return status
    return None


__all__ = [
    "Action",
    "DocumentDetailDTO",
    "DocumentResultSummaryDTO",
    "DocumentRunSummaryDTO",
    "DocumentSummaryDTO",
    "compute_available_actions",
    "project_document_detail",
    "project_document_summary",
    "project_run_history",
]
