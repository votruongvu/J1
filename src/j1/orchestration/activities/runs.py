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

from j1.orchestration.activities.payloads import ProjectScope
from j1.runs.models import RunStatus
from j1.runs.reporter import ProgressReporter
from j1.runs.store import IngestionRunStore

ACTIVITY_REPORT_RUN_TERMINAL = "j1.runs.report_terminal"
ACTIVITY_REPORT_STEP_SKIPPED = "j1.runs.report_step_skipped"
ACTIVITY_REPORT_PLAN_GENERATED = "j1.runs.report_plan_generated"
ACTIVITY_REPORT_PLAN_REVISED = "j1.runs.report_plan_revised"

__all__ = [
    "ACTIVITY_REPORT_PLAN_GENERATED",
    "ACTIVITY_REPORT_PLAN_REVISED",
    "ACTIVITY_REPORT_RUN_TERMINAL",
    "ACTIVITY_REPORT_STEP_SKIPPED",
    "ReportPlanGeneratedInput",
    "ReportPlanRevisedInput",
    "ReportRunTerminalInput",
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


class RunsActivities:
    """Bundle of run-progress activities. Registered alongside the
    other activity classes at worker startup. The workflow calls
    these via `execute_activity_method` so the reporter call happens
    in activity context (where audit-log writes are safe)."""

    def __init__(
        self,
        progress_reporter: ProgressReporter | None = None,
        run_store: IngestionRunStore | None = None,
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

    def all_activities(self) -> list:
        return [
            self.report_run_terminal,
            self.report_step_skipped,
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
        # `cancelled`        → run.cancelled (its own terminal type so
        #                      the SSE stream closes cleanly without
        #                      pretending the run failed).
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
        # review surface (Phase 1 + onwards) can render the per-stage
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

        try:
            self._run_store.upsert(ctx, run)
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow
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
