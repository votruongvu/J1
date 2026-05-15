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

    Carries just enough to render one row (badge, timestamps,
    capability flags) — full per-run detail comes from the existing
    ``/ingestion-runs/{id}`` endpoints. Sorted by ``started_at``
    descending in the parent DTO.

    Capability flags are the source of truth for what actions the
    FE may render on this run. The frontend MUST NOT recompute these
    locally; pass through whatever the projector reports.
    """

    run_id: str
    run_type: RunType
    status: str  # RunStatus value
    started_at: datetime
    completed_at: datetime | None
    failure_code: str | None = None
    is_active: bool = False
    # The snapshot this run was building / built. Populated at run
    # creation by the REST/workflow allocator; ``None`` for legacy
    # runs predating the snapshot model. The FE renders this as the
    # "Produced snapshot" column so operators can see the snapshot
    # boundary directly instead of inferring it from run status.
    target_snapshot_id: str | None = None
    # Operator-visible version chip (``DDMMYYYY-NN`` per document
    # per day). Set by the run-creation layer when allocated;
    # legacy runs that pre-date this field surface as ``None`` and
    # the FE renders nothing — graceful degradation.
    display_version: str | None = None
    # ---- Capability flags (drive the Run Detail action area) -----
    # True when this run is the document's only run. The FE uses
    # this to hide ``Delete Run`` and surface helper text directing
    # the user to ``Remove Knowledge`` at the document level.
    is_only_run: bool = False
    # True when this run can be hard-deleted via DELETE on the run
    # endpoint. Always False for the active run and the only run;
    # always False for runs still in-flight.
    can_delete_run: bool = False
    # True when this run is the document's active run AND its
    # enrichment artifacts already exist (a refresh would replace
    # them). Mutually exclusive with ``can_run_enrichment``.
    can_refresh_enrichment: bool = False
    # True when this run is the document's active run but never
    # produced enrichment artifacts (an initial enrich would create
    # them). Mutually exclusive with ``can_refresh_enrichment``.
    can_run_enrichment: bool = False


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
    # ``refresh_enrich`` is intentionally NOT here — enrichment is a
    # run-level concern; the action lives on Run Detail and is gated
    # by the run-level capability flags.
    "attached": ("view", "reindex", "detach", "remove"),
    "detached": ("view", "attach", "remove"),
    "removed": ("view",),
}


def compute_available_actions(
    *,
    document: DocumentRecord,
    active_run: IngestionRun | None,
) -> tuple[Action, ...]:
    """Return the document-level actions the FE should render.

    Pure function of ``document.knowledge_state``. Run-level actions
    (delete-run, refresh-enrichment, run-enrichment) live on the
    per-run capability flags carried by ``DocumentRunSummaryDTO``.
    """
    del active_run  # kept for caller compatibility; no longer used
    return _BASE_ACTIONS_BY_STATE.get(document.knowledge_state, ("view",))


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

    ``active_snapshot_id`` is the visibility key; ``active_run`` is
    derived heuristically (most recent succeeded run) so the FE's
    "current result" panel has something to render.
    """
    active_run = _find_active_run(document, runs)
    sorted_runs = _sort_runs_desc(runs)
    history = tuple(
        _build_run_summary(r, document=document, all_runs=runs, active_run=active_run)
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
    history = tuple(
        _build_run_summary(r, document=document, all_runs=runs, active_run=active_run)
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
    return tuple(
        _build_run_summary(r, document=document, all_runs=runs, active_run=active_run)
        for r in _sort_runs_desc(runs)
    )


# ---- Internals -------------------------------------------------------


def _find_active_run(
    document: DocumentRecord, runs: list[IngestionRun],
) -> IngestionRun | None:
    """Return the run that produced ``document.active_snapshot_id``.

    Selection rule:

      1. **Canonical**: the run whose ``target_snapshot_id`` equals
         ``document.active_snapshot_id``. This is the producing run
         of the currently-promoted snapshot — the same lineage the
         Unified Memory Resolver returns for ``DocumentMemoryView.active_run_id``.
         Required for promotion correctness: a later succeeded but
         non-promoting run (CAS conflict, refresh-enrich pending
         promotion) MUST NOT be picked as "active".
      2. **Fallback**: latest succeeded / succeeded-with-warnings
         run by timestamp. Kept only for legacy runs that pre-date
         ``target_snapshot_id`` — every fresh-ingested run carries
         it, so the fallback is only reached for projects that
         haven't re-ingested after the snapshot refactor.

    Resumable-failure detection is preserved by the existing
    ``compute_available_actions`` helper, which gates on the
    selected active run's status.
    """
    if not document.active_snapshot_id:
        return None
    # Canonical: match by ``target_snapshot_id == active_snapshot_id``.
    for r in runs:
        if getattr(r, "target_snapshot_id", None) == document.active_snapshot_id:
            return r
    # Legacy fallback for runs predating ``target_snapshot_id``.
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


# Run statuses considered "in-flight" — neither delete-run nor any
# enrichment action is allowed while the workflow is still writing.
_INFLIGHT_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.RUNNING, RunStatus.PAUSED,
    RunStatus.CANCELLING, RunStatus.ASSESSING,
    RunStatus.CREATED,
})


def _run_has_enrichment(run: IngestionRun) -> bool:
    """True when this run's metadata reports a completed enrich step.

    Single source of truth used by both ``can_refresh_enrichment``
    and ``can_run_enrichment`` flag computation. The signal lives in
    ``metadata.step_results.enrich.status`` — same shape the run-
    detail surface reads for its timeline display.
    """
    step_results = run.metadata.get("step_results")
    if not isinstance(step_results, dict):
        return False
    enrich = step_results.get("enrich")
    if not isinstance(enrich, dict):
        return False
    return enrich.get("status") == "completed"


def _build_run_summary(
    run: IngestionRun,
    *,
    document: DocumentRecord,
    all_runs: list[IngestionRun],
    active_run: IngestionRun | None,
) -> DocumentRunSummaryDTO:
    is_active = active_run is not None and run.run_id == active_run.run_id
    is_only_run = len(all_runs) <= 1
    is_inflight = run.status in _INFLIGHT_STATUSES
    document_attached = document.knowledge_state == "attached"

    can_delete_run = (
        not is_active
        and not is_only_run
        and not is_inflight
    )
    enrichment_action_eligible = (
        is_active
        and not is_inflight
        and document_attached
    )
    has_enrichment = _run_has_enrichment(run)
    can_refresh_enrichment = enrichment_action_eligible and has_enrichment
    can_run_enrichment = enrichment_action_eligible and not has_enrichment

    return DocumentRunSummaryDTO(
        run_id=run.run_id,
        run_type=run.run_type,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        started_at=run.started_at,
        completed_at=run.completed_at,
        failure_code=run.failure_code,
        is_active=is_active,
        target_snapshot_id=getattr(run, "target_snapshot_id", None),
        display_version=run.display_version,
        is_only_run=is_only_run,
        can_delete_run=can_delete_run,
        can_refresh_enrichment=can_refresh_enrichment,
        can_run_enrichment=can_run_enrichment,
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
