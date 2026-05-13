"""Workflow-exit progress events as Temporal activities.

Workflow code is replay-deterministic and cannot directly call into
non-deterministic side effects (file I/O, audit-log writes). Progress
events that fire at workflow exit (`run.completed`, `run.failed`,
`step.skipped` for planner-disabled stages) therefore go through
short-lived Temporal activities defined here.

Inputs are intentionally minimal — the audit log is the source of
truth for the full run state; these activities only need enough
context to emit the event."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from temporalio import activity

from j1.artifacts.registry import ArtifactRegistry
from j1.intake.registry import SourceRegistry
from j1.orchestration.activities.payloads import ProjectScope
from j1.runs.models import RunStatus
from j1.runs.reporter import ProgressReporter
from j1.runs.store import IngestionRunStore

ACTIVITY_REPORT_RUN_TERMINAL = "j1.runs.report_terminal"
ACTIVITY_REPORT_STEP_SKIPPED = "j1.runs.report_step_skipped"
ACTIVITY_REPORT_PLAN_GENERATED = "j1.runs.report_plan_generated"
ACTIVITY_REPORT_PLAN_REVISED = "j1.runs.report_plan_revised"
ACTIVITY_REPORT_STEP_LIFECYCLE = "j1.runs.report_step_lifecycle"

__all__ = [
    "ACTIVITY_REPORT_PLAN_GENERATED",
    "ACTIVITY_REPORT_PLAN_REVISED",
    "ACTIVITY_REPORT_RUN_TERMINAL",
    "ACTIVITY_REPORT_STEP_LIFECYCLE",
    "ACTIVITY_REPORT_STEP_SKIPPED",
    "ReportPlanGeneratedInput",
    "ReportPlanRevisedInput",
    "ReportRunTerminalInput",
    "ReportStepLifecycleInput",
    "ReportStepSkippedInput",
    "RunsActivities",
    "StepSummaryEntry",
]


@dataclass(frozen=True)
class StepSummaryEntry:
    """One entry in the run-terminal step summary.

 Mirrors `StepResult` but lives in the activity-payload module
 (Temporal-serialisable) so workflow → activity → reporter
 round-trips cleanly. Kept compact — operators consume this in
 the run.completed event payload, not the full StepResult."""

    step: str
    status: str
    required: bool
    source: str
    reason: str | None = None
    artifact_count: int = 0


@dataclass(frozen=True)
class ReportRunTerminalInput:
    """Workflow → activity payload for run.completed / run.failed.

 The activity reports through the configured ProgressReporter.
 `final_status` is one of the FinalStatus enum values
 (succeeded / partial_completed / failed / cancelled / timed_out)
 — the activity decides whether to call report_run_completed or
 report_run_failed based on this string."""

    scope: ProjectScope
    run_id: str
    final_status: str
    warning_count: int = 0
    failure_code: str | None = None
    failure_message: str | None = None
    actor: str = "system"
    step_summary: tuple[StepSummaryEntry, ...] = field(default_factory=tuple)
    # Resume-from-checkpoint snapshot. Plain dict so the Temporal
    # data converter handles it without dataclass schema coupling.
    # Persisted to `IngestionRun.metadata["resume_snapshot"]` by
    # `_persist_run_terminal`. None on cancelled / unknown-terminal
    # paths where resume isn't a sensible affordance.
    resume_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReportPlanGeneratedInput:
    """Workflow → activity payload for `plan.generated` events.

 The planner runs in workflow code (replay-deterministic, no I/O),
 but the audit-log write that backs the FE's
 `GET /ingestion-runs/{id}/plan` endpoint must happen in activity
 context. This payload carries the serialised `IngestPlan` (as a
 plain dict for Temporal-data-converter compatibility) plus the
 scope + correlation needed to record it under the right run."""

    scope: ProjectScope
    run_id: str
    plan_payload: dict[str, Any]
    actor: str = "system"


@dataclass(frozen=True)
class ReportPlanRevisedInput:
    """Workflow → activity payload for `plan.revised` events.

 Emitted after a successful post-compile replan that changed at
 least one step's enabled state. Carries the same plan shape as
 `ReportPlanGeneratedInput` plus a human-readable `reason` string
 summarising what changed (used by the FE plan card to explain
 "why did this run unlock the graph step?")."""

    scope: ProjectScope
    run_id: str
    plan_payload: dict[str, Any]
    reason: str
    actor: str = "system"


@dataclass(frozen=True)
class ReportStepSkippedInput:
    """Workflow → activity payload for step.skipped events that fire
 at workflow time (planner / policy / config decided to skip),
 not at activity-execution time."""

    scope: ProjectScope
    run_id: str
    stage: str
    step: str
    reason: str
    source: str = "planner"
    actor: str = "system"


@dataclass(frozen=True)
class ReportStepLifecycleInput:
    """Workflow → activity payload for synthetic `step.started` /
 `step.completed` events.

 Some user-facing steps (e.g. `build_content_inventory`,
 `generate_knowledge_chunks`) don't run as standalone activities
 — they're sub-phases of `compile`. The workflow synthesises
 their step.* events through this activity so the audit timeline,
 SSE stream, and FE all see them as first-class steps with
 accurate ordering. The underlying compile activity still emits
 its own `compile` events; the synthetic events are additive
 and use distinct step names so consumers don't see duplicates.
 """

    scope: ProjectScope
    run_id: str
    stage: str
    step: str
    # `started` or `completed`. Failed/skipped sub-phases keep
    # using the existing dedicated activities — keeping this
    # action-string narrow guards against the workflow synthesising
    # contradictory events.
    action: str
    artifact_count: int = 0
    engine: str | None = None
    actor: str = "system"


class RunsActivities:
    """Bundle of run-progress activities. Registered alongside the
 other activity classes at worker startup. The workflow calls
 these via `execute_activity_method` so the reporter call happens
 in activity context (where audit-log writes are safe)."""

    def __init__(
        self,
        progress_reporter: ProgressReporter | None = None,
        run_store: IngestionRunStore | None = None,
        source_registry: "SourceRegistry | None" = None,
        artifact_registry: "ArtifactRegistry | None" = None,
        cleanup_service: "DocumentCleanupService | None" = None,
        diagnostic_recorder: "DiagnosticRecorder | None" = None,
    ) -> None:
        # `progress_reporter` writes the audit-log progress events
        # the FE's SSE timeline reads. `run_store` updates the
        # IngestionRun record's `status` / `failure_*` /
        # `completed_at` fields the FE's run header / primary status
        # panel reads via `GET /ingestion-runs/{id}`. Either or both
        # may be None — when one is missing, that surface degrades
        # silently (legacy behaviour). Wiring both gives operators
        # the full belt-and-braces guarantee: even if the SSE event
        # write fails, the run record reflects the terminal state
        # so the FE's polling fallback sees the truth.
        self._reporter = progress_reporter
        self._run_store = run_store
        # `source_registry` enables the document-centric "promote
        # run to active on terminal success" hook (Phase 4 of the
        # document-centric refactor). When None, the hook is a
        # no-op — the run still transitions cleanly, just no
        # `document.active_run_id` update. Deployments that haven't
        # adopted the document-centric flow keep working unchanged.
        self._source_registry = source_registry
        # `artifact_registry` powers the post-promotion supersede
        # sweep: when a new run becomes the document's active, the
        # previous active's artifacts get stamped
        # `search_state=superseded` so retrieval stops surfacing
        # them. Without this, retrieval after a successful reindex
        # would return mixed-run results (the "graph_json from old
        # run still in search results" failure mode).
        self._artifact_registry = artifact_registry
        # `cleanup_service` is the idempotent cleanup primitives
        # (per-run and per-document). When wired, CAS-orphaned
        # candidate runs delegate here to drop their artifacts +
        # workspace; Remove flow chains through it to wipe an
        # entire document.
        self._cleanup_service = cleanup_service
        # Phase-1 ingestion diagnostics recorder. Persists the per-
        # run timing + LLM + enrichment summary as a
        # ``compiled.ingestion_diagnostic_report`` artifact at
        # terminal time. Optional — None preserves legacy
        # behaviour.
        self._diagnostics = diagnostic_recorder

    def all_activities(self) -> list:
        return [
            self.report_run_terminal,
            self.report_step_skipped,
            self.report_step_lifecycle,
            self.report_plan_generated,
            self.report_plan_revised,
        ]

    @activity.defn(name=ACTIVITY_REPORT_PLAN_GENERATED)
    def report_plan_generated(self, input: ReportPlanGeneratedInput) -> None:
        """Write `j1.progress.plan.generated` to the audit log.

 The FE's `GET /ingestion-runs/{id}/plan` reads from this
 entry, so without the activity firing the run-detail page
 sits on "Generating plan…" forever. Best-effort like the
 other reporter activities — failure is logged, never raised."""
        if self._reporter is None:
            return
        ctx = input.scope.to_context()
        try:
            self._reporter.report_plan_generated(
                ctx, run_id=input.run_id,
                plan_payload=dict(input.plan_payload),
                actor=input.actor,
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
            pass

    @activity.defn(name=ACTIVITY_REPORT_STEP_LIFECYCLE)
    def report_step_lifecycle(
        self, input: ReportStepLifecycleInput,
    ) -> None:
        """Write a synthetic `step.started` / `step.completed` to
 the audit log.

 Synthesised by the workflow for user-facing sub-steps that
 don't have their own activity (e.g. `build_content_inventory`,
 `generate_knowledge_chunks` — both happen inside compile but
 the FE renders them as separate steps). Best-effort like
 every reporter activity — failure is logged, never raised."""
        if self._reporter is None:
            return
        ctx = input.scope.to_context()
        try:
            if input.action == "started":
                self._reporter.report_step_started(
                    ctx,
                    run_id=input.run_id,
                    stage=input.stage,
                    step=input.step,
                    engine=input.engine,
                    actor=input.actor,
                )
            elif input.action == "completed":
                self._reporter.report_step_completed(
                    ctx,
                    run_id=input.run_id,
                    stage=input.stage,
                    step=input.step,
                    artifact_count=input.artifact_count,
                    actor=input.actor,
                )
            # Unknown actions are ignored — the contract is narrow
            # by design (started / completed only). Failures + skips
            # use their own dedicated activities so callers can't
            # synthesise contradictory state via this entrypoint.
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
            pass

    @activity.defn(name=ACTIVITY_REPORT_PLAN_REVISED)
    def report_plan_revised(self, input: ReportPlanRevisedInput) -> None:
        """Write `j1.progress.plan.revised` to the audit log.

 Same best-effort contract as `report_plan_generated`. The
 FE polls `GET /ingestion-runs/{id}/plan` after a revision
 event and reads the latest `plan.revised` if present (else
 falls back to `plan.generated`)."""
        if self._reporter is None:
            return
        ctx = input.scope.to_context()
        try:
            self._reporter.report_plan_revised(
                ctx, run_id=input.run_id,
                plan_payload=dict(input.plan_payload),
                reason=input.reason,
                actor=input.actor,
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
            pass

    @activity.defn(name=ACTIVITY_REPORT_RUN_TERMINAL)
    def report_run_terminal(self, input: ReportRunTerminalInput) -> None:
        ctx = input.scope.to_context()
        # Order: persist the run record FIRST, then emit the audit
        # event. The run record is the FE's polling fallback — if the
        # event write fails for any reason, the FE's `GET /ingestion-
        # runs/{id}` response still shows the terminal state (FAILED /
        # SUCCEEDED / CANCELLED) so the run-detail page doesn't sit
        # on "Running" forever. Both are best-effort; failures here
        # never block the workflow.
        self._persist_run_terminal(ctx, input)
        if self._reporter is None:
            return
        # Translate `final_status` to the appropriate reporter call.
        # `cancelled` → run.cancelled (its own terminal type so
        #  the SSE stream closes cleanly without
        #  pretending the run failed).
        # `failed` / `timed_out` → run.failed.
        # `succeeded` / `partial_completed` → run.completed (the
        # frontend distinguishes via `warning_count` and `final_status`
        # fields in the event payload).
        if input.final_status == "cancelled":
            try:
                self._reporter.report_run_cancelled(
                    ctx, run_id=input.run_id,
                    reason=input.failure_message,
                    actor=input.actor,
                )
            except Exception:  # noqa: BLE001 — telemetry never blocks workflow
                pass
            return
        if input.final_status in ("failed", "timed_out"):
            try:
                self._reporter.report_run_failed(
                    ctx, run_id=input.run_id,
                    failure_code=input.failure_code or input.final_status.upper(),
                    failure_message=input.failure_message or input.final_status,
                    actor=input.actor,
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            self._reporter.report_run_completed(
                ctx, run_id=input.run_id,
                final_status=input.final_status,
                warning_count=input.warning_count,
                actor=input.actor,
            )
        except Exception:  # noqa: BLE001
            pass

    def _persist_run_terminal(self, ctx, input: ReportRunTerminalInput) -> None:
        """Update the IngestionRun record's status / failure / timing
 fields so the FE's polling sees the terminal state even if
 the audit-event emission below fails.

 Maps `final_status` (operator-facing string) to `RunStatus`
 (the run record's enum). Unknown values fall back to FAILED
 with the original string in `failure_code` so the FE has a
 breadcrumb.

 Also persists the workflow's `step_summary` into
 `metadata["step_results"]` so the review surface
 (`/ingestion-runs/{id}/summary`) can render the per-stage
 recap without scraping the audit log. Same atomic write as
 the status flip — if the upsert fails for any reason, the FE
 sees neither change."""
        if self._run_store is None:
            return
        run = None
        try:
            run = self._run_store.get(ctx, input.run_id)
        except Exception:  # noqa: BLE001 — store may not exist yet
            return
        if run is None:
            return
        now = datetime.now(timezone.utc)
        if input.final_status == "cancelled":
            run.status = RunStatus.CANCELLED
            run.completed_at = now
            if input.failure_message:
                run.failure_message = input.failure_message
        elif input.final_status in ("failed", "timed_out"):
            run.status = RunStatus.FAILED
            run.completed_at = now
            run.failure_code = input.failure_code or input.final_status.upper()
            run.failure_message = input.failure_message or input.final_status
        elif input.final_status == "succeeded_with_warnings" or (
            input.final_status in ("succeeded", "partial_completed")
            and input.warning_count > 0
        ):
            run.status = RunStatus.SUCCEEDED_WITH_WARNINGS
            run.completed_at = now
            run.warning_count = max(run.warning_count, input.warning_count)
            run.progress_percent = 100
        elif input.final_status in ("succeeded", "partial_completed"):
            run.status = RunStatus.SUCCEEDED
            run.completed_at = now
            run.progress_percent = 100
        else:
            # Unknown terminal label — record as failed so the FE
            # doesn't sit on RUNNING. Carry the original string
            # forward for diagnosability.
            run.status = RunStatus.FAILED
            run.completed_at = now
            run.failure_code = "UNKNOWN_TERMINAL_STATUS"
            run.failure_message = input.final_status
        run.updated_at = now

        # Persist step_summary into metadata["step_results"] so the
        # review surface ( + onwards) can render the per-stage
        # recap directly off the run record. Plain dicts only — keep
        # the JSONL store free of dataclass coupling. Empty summaries
        # leave the existing key alone (a re-run after a crash should
        # not blank previously-good data).
        if input.step_summary:
            run.metadata["step_results"] = [
                {
                    "step": entry.step,
                    "status": entry.status,
                    "required": entry.required,
                    "source": entry.source,
                    "reason": entry.reason,
                    "artifact_count": entry.artifact_count,
                }
                for entry in input.step_summary
            ]

        # Resume-from-checkpoint snapshot. Always overwrite when the
        # workflow supplies one — a fresh terminal transition means
        # the prior snapshot is stale (its step_results no longer
        # describe the latest attempt). When `None` we leave the
        # existing key alone (cancelled-path emit doesn't carry a
        # snapshot but shouldn't blow away a previously-good one
        # captured by a continue-as-new boundary).
        if input.resume_snapshot is not None:
            run.metadata["resume_snapshot"] = dict(input.resume_snapshot)

        try:
            self._run_store.upsert(ctx, run)
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
            pass

        # Document-centric promotion (Phase 4): when this run
        # reached a usable terminal state, point the document at it
        # as the current "active" result. Failed / cancelled /
        # warning-only runs do NOT promote, which is exactly what
        # makes "failed reindex doesn't clobber the previous
        # successful run" true: the previous active_run_id stays
        # pointing at the prior good run.
        self._maybe_promote_to_active(ctx, run)

        # Phase-1 ingestion diagnostics. Materialise the in-memory
        # collector into a ``compiled.ingestion_diagnostic_report``
        # artifact alongside the existing strategy / enrich-plan
        # reports. Always emitted at every terminal — including
        # failures — so operators can see WHERE the run spent its
        # time even when it didn't finish cleanly. No-op when the
        # recorder isn't wired.
        if self._diagnostics is not None:
            try:
                self._diagnostics.write_report(
                    ctx=ctx,
                    run_id=run.run_id,
                    document_id=run.document_id,
                    filename=None,
                )
            except Exception:  # noqa: BLE001
                # Recorder already logs internally; never let a
                # diagnostic IO failure break the terminal path.
                pass

    def _maybe_promote_to_active(self, ctx, run) -> None:
        """CAS-promote ``document.active_run_id`` to this run when
        it reached a usable terminal state.

        Definition of "usable" matches the Phase 1 backfill's tier-1
        rule (`SUCCEEDED` + `SUCCEEDED_WITH_WARNINGS`). Anything
        else — including failures with a compile checkpoint — does
        NOT promote, because a failed reindex must preserve the
        previous good active for retrieval / answer generation.

        Promotion is CAS: the candidate's ``parent_run_id`` (for
        reindex/refresh runs) or ``None`` (for an initial run) is
        passed as the expected current ``active_run_id``. If
        another reindex won the slot first, OR the document was
        removed mid-run, the CAS returns ``None`` and we skip
        promotion — the candidate is now orphan and the caller
        side-effects cleanup. Without CAS we'd silently overwrite
        the winner OR re-promote a run onto a removed document.

        Quiet on every failure path (no registry wired, lookup
        miss, write fails): the run-status update is the
        load-bearing operation; the promotion is a best-effort
        side effect.
        """
        if self._source_registry is None:
            return
        if run.status not in (
            RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS,
        ):
            return
        if not getattr(run, "document_id", None):
            return
        try:
            doc = self._source_registry.get(ctx, run.document_id)
        except Exception:  # noqa: BLE001 — best-effort
            return
        if doc.active_run_id == run.run_id:
            return  # already pointing at us; nothing to do
        previous_active_run_id = doc.active_run_id
        # CAS precondition: for reindex / refresh runs, the expected
        # active is the candidate's parent_run_id. Initial runs
        # expect None (no prior active). Either way, mismatch
        # means we lost the race or the document was removed —
        # both warrant a no-op + cleanup, not a clobbering write.
        expected_active = getattr(run, "parent_run_id", None)
        promoted = None
        try:
            promoted = self._source_registry.try_promote_active_run_id(
                ctx,
                run.document_id,
                new_run_id=run.run_id,
                expected_active_run_id=expected_active,
                completed_at=run.completed_at or run.updated_at,
            )
        except Exception:  # noqa: BLE001 — quiet best-effort
            return
        if promoted is None:
            # CAS failed: candidate is orphaned. Best-effort drop
            # of its workspace + artifacts so the run doesn't
            # linger as queryable phantom evidence.
            self._cleanup_orphan_candidate(ctx, run)
            return

        # Post-promotion supersede sweep. Mark the previous active
        # run's artifacts as `search_state=superseded` so retrieval
        # stops surfacing them. Without this, a successful reindex
        # leaves both runs' artifacts active in retrieval — the
        # "graph_json from old run still appears in search results"
        # failure mode the lineage hardening targets.
        if self._artifact_registry is None or not previous_active_run_id:
            return
        try:
            from j1.documents.artifact_state import (
                supersede_previous_active_artifacts,
            )
            supersede_previous_active_artifacts(
                ctx=ctx,
                artifacts=self._artifact_registry,
                document_id=run.document_id,
                new_run_id=run.run_id,
                previous_run_id=previous_active_run_id,
            )
        except Exception:  # noqa: BLE001 — best-effort; never blocks promotion
            pass

    def _cleanup_orphan_candidate(self, ctx, run) -> None:
        """Best-effort cleanup of a candidate run that lost the CAS
        promotion race (or was promoted onto a now-removed document).

        Delegates to ``DocumentCleanupService.cleanup_run`` when the
        service is wired; otherwise marks the run's
        ``cleanup_status="cleanup_failed"`` so an operator sweep
        can spot orphans. Quiet on every failure path — the run is
        already terminal, this is purely housekeeping."""
        if self._cleanup_service is None:
            # No service wired (legacy / test path). Mark the run so
            # operators see the orphan.
            try:
                run.cleanup_status = "cleanup_failed"  # type: ignore[assignment]
                self._run_store.upsert(ctx, run)
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            self._cleanup_service.cleanup_run(
                ctx, document_id=run.document_id, run_id=run.run_id,
            )
            run.cleanup_status = "cleaned"  # type: ignore[assignment]
            self._run_store.upsert(ctx, run)
        except Exception:  # noqa: BLE001 — best-effort
            try:
                run.cleanup_status = "cleanup_failed"  # type: ignore[assignment]
                self._run_store.upsert(ctx, run)
            except Exception:  # noqa: BLE001
                pass

    @activity.defn(name=ACTIVITY_REPORT_STEP_SKIPPED)
    def report_step_skipped(self, input: ReportStepSkippedInput) -> None:
        if self._reporter is None:
            return
        ctx = input.scope.to_context()
        try:
            self._reporter.report_step_skipped(
                ctx, run_id=input.run_id,
                stage=input.stage, step=input.step,
                reason=input.reason, actor=input.actor,
            )
        except Exception:  # noqa: BLE001
            pass
