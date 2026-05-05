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

from temporalio import activity

from j1.orchestration.activities.payloads import ProjectScope
from j1.runs.reporter import ProgressReporter

ACTIVITY_REPORT_RUN_TERMINAL = "j1.runs.report_terminal"
ACTIVITY_REPORT_STEP_SKIPPED = "j1.runs.report_step_skipped"

__all__ = [
    "ACTIVITY_REPORT_RUN_TERMINAL",
    "ACTIVITY_REPORT_STEP_SKIPPED",
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

    def __init__(self, progress_reporter: ProgressReporter | None = None) -> None:
        # Optional: when None, the activities silently no-op so a
        # deployment that hasn't wired the progress surface stays
        # working.
        self._reporter = progress_reporter

    def all_activities(self) -> list:
        return [self.report_run_terminal, self.report_step_skipped]

    @activity.defn(name=ACTIVITY_REPORT_RUN_TERMINAL)
    def report_run_terminal(self, input: ReportRunTerminalInput) -> None:
        if self._reporter is None:
            return
        ctx = input.scope.to_context()
        # Translate `final_status` to the appropriate reporter call.
        # `failed` / `cancelled` / `timed_out` → run.failed.
        # `succeeded` / `partial_completed` → run.completed (the
        # frontend distinguishes via `warning_count` and `final_status`
        # fields in the event payload).
        if input.final_status in ("failed", "cancelled", "timed_out"):
            try:
                self._reporter.report_run_failed(
                    ctx, run_id=input.run_id,
                    failure_code=input.failure_code or input.final_status.upper(),
                    failure_message=input.failure_message or input.final_status,
                    actor=input.actor,
                )
            except Exception:  # noqa: BLE001 — telemetry never blocks workflow
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
