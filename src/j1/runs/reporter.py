"""ProgressReporter: the hook the workflow / activities call to
publish user-facing progress.

Three concrete implementations ship:

  * `AuditProgressReporter` — writes through the existing
    `AuditRecorder` so progress events become first-class entries in
    the audit log. The frontend reads them via the same JSONL log
    used for everything else (or via the SSE-stream endpoint, which
    re-emits the same shape live).
  * `TemporalHeartbeatReporter` — pumps compact step/progress
    summaries into `temporalio.activity.heartbeat` so Temporal UI
    sees liveness on long-running activities. Stateless.
  * `CompositeProgressReporter` — fan-out to multiple targets.
  * `NoopProgressReporter` — for unit tests.

Design notes:

  * The reporter is a fat protocol with one method per event type.
    Tempting to make a single `report(event_type, **kwargs)` method,
    but the typed methods give callers a stable contract — adding a
    new event type is an additive change, and each method can carry
    its own argument shape (e.g. `report_step_progress` always takes
    `current` / `total`, while `report_step_skipped` always takes
    `reason`).
  * The reporter is transport-free. It does NOT hold HTTP connections
    or React state; the SSE endpoint reads from the same audit log
    via a tail-and-publish model.
  * Throttling lives in the audit-backed reporter (drops sub-5%
    progress deltas to keep audit JSONL volume bounded). Callers
    don't need to throttle themselves.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from j1.audit.recorder import AuditRecorder
from j1.projects.context import ProjectContext
from j1.runs.models import (
    PROGRESS_SEVERITY_ERROR,
    PROGRESS_SEVERITY_INFO,
    PROGRESS_SEVERITY_WARNING,
)

__all__ = [
    # Action constants — exported so the SSE / events-history endpoints
    # can filter audit events by these prefixes.
    "ACTION_PROGRESS_ASSESSMENT_COMPLETED",
    "ACTION_PROGRESS_ASSESSMENT_STARTED",
    "ACTION_PROGRESS_DOCUMENT_RECEIVED",
    "ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED",
    "ACTION_PROGRESS_PLAN_CONFIRMED",
    "ACTION_PROGRESS_PLAN_GENERATED",
    "ACTION_PROGRESS_RUN_CANCELLED",
    "ACTION_PROGRESS_RUN_COMPLETED",
    "ACTION_PROGRESS_RUN_CREATED",
    "ACTION_PROGRESS_RUN_FAILED",
    "ACTION_PROGRESS_STEP_COMPLETED",
    "ACTION_PROGRESS_STEP_FAILED",
    "ACTION_PROGRESS_STEP_PROGRESS",
    "ACTION_PROGRESS_STEP_SKIPPED",
    "ACTION_PROGRESS_STEP_STARTED",
    "ACTION_PROGRESS_STEP_WARNING",
    "AuditProgressReporter",
    "CompositeProgressReporter",
    "NoopProgressReporter",
    "ProgressReporter",
    "TemporalHeartbeatReporter",
    "is_progress_action",
    "PROGRESS_ACTION_PREFIX",
    "PROGRESS_TARGET_KIND",
]


# Action prefix — every progress event's audit action starts with this
# so consumers can filter the audit log to "just the progress timeline".
PROGRESS_ACTION_PREFIX = "j1.progress."

# Event-type strings (also used as audit `action` values) — these are
# the canonical names a frontend filters on. Stable across releases.
ACTION_PROGRESS_RUN_CREATED = PROGRESS_ACTION_PREFIX + "run.created"
ACTION_PROGRESS_DOCUMENT_RECEIVED = PROGRESS_ACTION_PREFIX + "document.received"
ACTION_PROGRESS_ASSESSMENT_STARTED = PROGRESS_ACTION_PREFIX + "assessment.started"
ACTION_PROGRESS_ASSESSMENT_COMPLETED = PROGRESS_ACTION_PREFIX + "assessment.completed"
ACTION_PROGRESS_PLAN_GENERATED = PROGRESS_ACTION_PREFIX + "plan.generated"
ACTION_PROGRESS_PLAN_CONFIRMED = PROGRESS_ACTION_PREFIX + "plan.confirmed"
ACTION_PROGRESS_STEP_STARTED = PROGRESS_ACTION_PREFIX + "step.started"
ACTION_PROGRESS_STEP_PROGRESS = PROGRESS_ACTION_PREFIX + "step.progress"
ACTION_PROGRESS_STEP_SKIPPED = PROGRESS_ACTION_PREFIX + "step.skipped"
ACTION_PROGRESS_STEP_WARNING = PROGRESS_ACTION_PREFIX + "step.warning"
ACTION_PROGRESS_STEP_COMPLETED = PROGRESS_ACTION_PREFIX + "step.completed"
ACTION_PROGRESS_STEP_FAILED = PROGRESS_ACTION_PREFIX + "step.failed"
ACTION_PROGRESS_RUN_COMPLETED = PROGRESS_ACTION_PREFIX + "run.completed"
ACTION_PROGRESS_RUN_FAILED = PROGRESS_ACTION_PREFIX + "run.failed"
ACTION_PROGRESS_RUN_CANCELLED = PROGRESS_ACTION_PREFIX + "run.cancelled"
ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED = PROGRESS_ACTION_PREFIX + "human_review.required"


# Audit `target_kind` for progress entries. Stable so consumers can
# distinguish progress records from compile/enrich/etc audit entries.
PROGRESS_TARGET_KIND = "ingestion_run"


def is_progress_action(action: str) -> bool:
    """True for any J1 progress-event audit action."""
    return action.startswith(PROGRESS_ACTION_PREFIX)


# ---- Protocol --------------------------------------------------------


@runtime_checkable
class ProgressReporter(Protocol):
    """Hook the workflow / activities call to publish progress.

    Implementations MUST be safe to call from anywhere — workflow
    code, activity code, REST handlers. Activity-runtime-only
    operations (Temporal heartbeats) live behind `TemporalHeartbeatReporter`
    which silently no-ops outside an activity context.

    Field hygiene: callers MUST NOT pass document content, prompts,
    or LLM outputs. Short operational strings only."""

    def report_run_created(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str,
        actor: str = "system",
    ) -> str: ...

    def report_document_received(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str,
        actor: str = "system",
    ) -> str: ...

    def report_assessment_started(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        actor: str = "system",
    ) -> str: ...

    def report_assessment_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        profile_metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> str: ...

    def report_plan_generated(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        plan_payload: dict[str, Any],
        actor: str = "system",
    ) -> str: ...

    def report_plan_confirmed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        actor: str = "system",
    ) -> str: ...

    def report_step_started(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        engine: str | None = None,
        provider: str | None = None,
        actor: str = "system",
    ) -> str: ...

    def report_step_progress(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        progress_percent: int,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        engine: str | None = None,
        actor: str = "system",
    ) -> str | None: ...

    def report_step_skipped(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        reason: str,
        actor: str = "system",
    ) -> str: ...

    def report_step_warning(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        message: str,
        engine: str | None = None,
        actor: str = "system",
    ) -> str: ...

    def report_step_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        artifact_count: int = 0,
        actor: str = "system",
    ) -> str: ...

    def report_step_failed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        error_type: str,
        error_message: str,
        retryable: bool = False,
        actor: str = "system",
    ) -> str: ...

    def report_run_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        final_status: str,
        warning_count: int = 0,
        actor: str = "system",
    ) -> str: ...

    def report_run_failed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        failure_code: str,
        failure_message: str,
        actor: str = "system",
    ) -> str: ...

    def report_run_cancelled(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        reason: str | None = None,
        actor: str = "system",
    ) -> str: ...

    def report_human_review_required(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        gate: str,
        actor: str = "system",
    ) -> str: ...


# ---- Audit-backed implementation -----------------------------------


# Throttle threshold for `report_step_progress`. Sub-5% deltas are
# dropped to keep audit volume bounded — the UI doesn't need every
# 1% tick. Step-start/complete/fail events are always emitted.
_PROGRESS_DELTA_THRESHOLD = 5


class AuditProgressReporter:
    """ProgressReporter that writes through `AuditRecorder`.

    Every event becomes one audit entry with action `j1.progress.<type>`
    and payload carrying the structured fields. Reusing the audit log
    means:
      * Existing JSONL persistence + retention apply for free.
      * The SSE stream and `GET /ingestion-runs/{id}/events` endpoint
        read from a single source of truth.
      * Operators can grep audit JSONL with the same tools they
        already use for compile/enrich/graph events."""

    def __init__(self, audit: AuditRecorder) -> None:
        self._audit = audit
        # Last-emitted progress percent per (run_id, stage, step). Used
        # for throttling. Lives in the reporter instance — sufficient
        # because the reporter is per-request scoped at the API layer
        # and per-activity scoped at the worker.
        self._last_progress: dict[tuple[str, str, str], int] = {}

    # ---- Lifecycle events --------------------------------------------

    def report_run_created(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_RUN_CREATED,
            run_id=run_id,
            payload={"document_id": document_id},
        )

    def report_document_received(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_DOCUMENT_RECEIVED,
            run_id=run_id,
            payload={"document_id": document_id, "severity": PROGRESS_SEVERITY_INFO},
        )

    def report_assessment_started(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_ASSESSMENT_STARTED,
            run_id=run_id,
            payload={"severity": PROGRESS_SEVERITY_INFO},
        )

    def report_assessment_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        profile_metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_ASSESSMENT_COMPLETED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_INFO,
                "profile": profile_metadata or {},
            },
        )

    def report_plan_generated(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        plan_payload: dict[str, Any],
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_PLAN_GENERATED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_INFO,
                "plan": plan_payload,
            },
        )

    def report_plan_confirmed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_PLAN_CONFIRMED,
            run_id=run_id,
            payload={"severity": PROGRESS_SEVERITY_INFO},
        )

    # ---- Step events --------------------------------------------------

    def report_step_started(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        engine: str | None = None,
        provider: str | None = None,
        actor: str = "system",
    ) -> str:
        # Reset throttle bookkeeping at step boundary.
        self._last_progress.pop((run_id, stage, step), None)
        payload: dict[str, Any] = {
            "severity": PROGRESS_SEVERITY_INFO,
            "stage": stage,
            "step": step,
            "status": "running",
        }
        if engine is not None:
            payload["engine"] = engine
        if provider is not None:
            payload["provider"] = provider
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_STARTED,
            run_id=run_id,
            payload=payload,
        )

    def report_step_progress(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        progress_percent: int,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        engine: str | None = None,
        actor: str = "system",
    ) -> str | None:
        # Throttle: drop sub-threshold deltas. Always emit at 0% and
        # 100% so the UI sees clean step boundaries.
        clamped = max(0, min(100, int(progress_percent)))
        key = (run_id, stage, step)
        previous = self._last_progress.get(key, -100)
        if (
            clamped not in (0, 100)
            and abs(clamped - previous) < _PROGRESS_DELTA_THRESHOLD
        ):
            return None
        self._last_progress[key] = clamped
        payload: dict[str, Any] = {
            "severity": PROGRESS_SEVERITY_INFO,
            "stage": stage,
            "step": step,
            "status": "running",
            "progress_percent": clamped,
        }
        if current is not None:
            payload["current"] = current
        if total is not None:
            payload["total"] = total
        if message is not None:
            payload["message"] = message
        if engine is not None:
            payload["engine"] = engine
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_PROGRESS,
            run_id=run_id,
            payload=payload,
        )

    def report_step_skipped(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        reason: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_SKIPPED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_INFO,
                "stage": stage,
                "step": step,
                "status": "skipped",
                "reason": reason,
            },
        )

    def report_step_warning(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        message: str,
        engine: str | None = None,
        actor: str = "system",
    ) -> str:
        payload: dict[str, Any] = {
            "severity": PROGRESS_SEVERITY_WARNING,
            "stage": stage,
            "step": step,
            "message": message,
        }
        if engine is not None:
            payload["engine"] = engine
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_WARNING,
            run_id=run_id,
            payload=payload,
        )

    def report_step_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        artifact_count: int = 0,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_COMPLETED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_INFO,
                "stage": stage,
                "step": step,
                "status": "completed",
                "progress_percent": 100,
                "artifact_count": artifact_count,
            },
        )

    def report_step_failed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        stage: str,
        step: str,
        error_type: str,
        error_message: str,
        retryable: bool = False,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_STEP_FAILED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_ERROR,
                "stage": stage,
                "step": step,
                "status": "failed",
                "error_type": error_type,
                "error_message": error_message,
                "retryable": retryable,
            },
        )

    def report_run_completed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        final_status: str,
        warning_count: int = 0,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_RUN_COMPLETED,
            run_id=run_id,
            payload={
                "severity": (
                    PROGRESS_SEVERITY_WARNING if warning_count > 0
                    else PROGRESS_SEVERITY_INFO
                ),
                "status": "completed",
                "final_status": final_status,
                "warning_count": warning_count,
                "progress_percent": 100,
            },
        )

    def report_run_failed(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        failure_code: str,
        failure_message: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_RUN_FAILED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_ERROR,
                "status": "failed",
                "failure_code": failure_code,
                "failure_message": failure_message,
            },
        )

    def report_run_cancelled(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        reason: str | None = None,
        actor: str = "system",
    ) -> str:
        # Mirrors `report_run_failed` shape but is its own terminal
        # event so the SSE stream can close cleanly when a run is
        # cancelled by an operator (or by Temporal cancellation
        # propagation). The `reason` is operator-supplied — short
        # operational text only, never document content.
        payload: dict[str, Any] = {
            "severity": PROGRESS_SEVERITY_WARNING,
            "status": "cancelled",
        }
        if reason is not None:
            payload["reason"] = reason
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_RUN_CANCELLED,
            run_id=run_id,
            payload=payload,
        )

    def report_human_review_required(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        gate: str,
        actor: str = "system",
    ) -> str:
        return self._record(
            ctx,
            actor=actor,
            action=ACTION_PROGRESS_HUMAN_REVIEW_REQUIRED,
            run_id=run_id,
            payload={
                "severity": PROGRESS_SEVERITY_WARNING,
                "status": "requires_human_review",
                "gate": gate,
            },
        )

    # ---- Internal -----------------------------------------------------

    def _record(
        self,
        ctx: ProjectContext,
        *,
        actor: str,
        action: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> str:
        return self._audit.record(
            ctx,
            actor=actor,
            action=action,
            target_kind=PROGRESS_TARGET_KIND,
            target_id=run_id,
            payload=payload,
            correlation_id=run_id,
        )


# ---- Temporal heartbeat reporter ----------------------------------


class TemporalHeartbeatReporter:
    """ProgressReporter that pumps compact progress into Temporal
    activity heartbeats.

    Stateless — no audit writes, no run-record writes. Heartbeats let
    a Temporal-side `heartbeat_timeout` fire if a long-running activity
    stalls, and they show up in Temporal UI as the "Latest Heartbeat
    Details" payload (handy for operator debugging without a frontend).

    Outside an activity context (unit tests, REST handlers) every
    method silently no-ops — `temporalio.activity.heartbeat` raises
    when called outside the activity runtime, and we catch.

    Returns are always None (heartbeats don't have IDs); callers that
    need a stable event ID must use a Composite reporter that also
    includes an `AuditProgressReporter`."""

    def __init__(self) -> None:
        # Lazy-import temporalio so non-Temporal deployments can still
        # load `j1.runs` without the dependency.
        try:
            from temporalio import activity
            self._activity = activity
        except ImportError:
            self._activity = None

    def _hb(self, **details: Any) -> None:
        if self._activity is None:
            return
        try:
            self._activity.heartbeat(details)
        except Exception:  # noqa: BLE001 — visibility never blocks ingest
            pass

    # All methods route through `_hb` and discard return values.

    def report_run_created(self, _ctx, *, run_id, document_id, actor="system"):
        self._hb(event="run.created", run_id=run_id, document_id=document_id)
        return ""

    def report_document_received(self, _ctx, *, run_id, document_id, actor="system"):
        self._hb(event="document.received", run_id=run_id, document_id=document_id)
        return ""

    def report_assessment_started(self, _ctx, *, run_id, actor="system"):
        self._hb(event="assessment.started", run_id=run_id)
        return ""

    def report_assessment_completed(
        self, _ctx, *, run_id, profile_metadata=None, actor="system",
    ):
        self._hb(event="assessment.completed", run_id=run_id)
        return ""

    def report_plan_generated(self, _ctx, *, run_id, plan_payload, actor="system"):
        self._hb(
            event="plan.generated", run_id=run_id,
            mode=plan_payload.get("mode") if isinstance(plan_payload, dict) else None,
        )
        return ""

    def report_plan_confirmed(self, _ctx, *, run_id, actor="system"):
        self._hb(event="plan.confirmed", run_id=run_id)
        return ""

    def report_step_started(
        self, _ctx, *, run_id, stage, step,
        engine=None, provider=None, actor="system",
    ):
        self._hb(event="step.started", run_id=run_id, stage=stage, step=step,
                 engine=engine, provider=provider)
        return ""

    def report_step_progress(
        self, _ctx, *, run_id, stage, step, progress_percent,
        current=None, total=None, message=None, engine=None, actor="system",
    ):
        self._hb(
            event="step.progress", run_id=run_id, stage=stage, step=step,
            progress_percent=progress_percent, current=current, total=total,
            engine=engine,
        )
        return None

    def report_step_skipped(
        self, _ctx, *, run_id, stage, step, reason, actor="system",
    ):
        self._hb(event="step.skipped", run_id=run_id, stage=stage, step=step,
                 reason=reason)
        return ""

    def report_step_warning(
        self, _ctx, *, run_id, stage, step, message,
        engine=None, actor="system",
    ):
        self._hb(event="step.warning", run_id=run_id, stage=stage, step=step,
                 message=message)
        return ""

    def report_step_completed(
        self, _ctx, *, run_id, stage, step, artifact_count=0, actor="system",
    ):
        self._hb(event="step.completed", run_id=run_id, stage=stage, step=step,
                 artifact_count=artifact_count)
        return ""

    def report_step_failed(
        self, _ctx, *, run_id, stage, step, error_type, error_message,
        retryable=False, actor="system",
    ):
        self._hb(event="step.failed", run_id=run_id, stage=stage, step=step,
                 error_type=error_type, error_message=error_message,
                 retryable=retryable)
        return ""

    def report_run_completed(
        self, _ctx, *, run_id, final_status, warning_count=0, actor="system",
    ):
        self._hb(event="run.completed", run_id=run_id, final_status=final_status,
                 warning_count=warning_count)
        return ""

    def report_run_failed(
        self, _ctx, *, run_id, failure_code, failure_message, actor="system",
    ):
        self._hb(event="run.failed", run_id=run_id, failure_code=failure_code)
        return ""

    def report_run_cancelled(self, _ctx, *, run_id, reason=None, actor="system"):
        self._hb(event="run.cancelled", run_id=run_id, reason=reason)
        return ""

    def report_human_review_required(self, _ctx, *, run_id, gate, actor="system"):
        self._hb(event="human_review.required", run_id=run_id, gate=gate)
        return ""


# ---- Composite + Noop --------------------------------------------


class CompositeProgressReporter:
    """Fan-out reporter — calls every delegate in order.

    Returns the FIRST non-empty `event_id` from the delegates so
    callers (typically an audit-backed reporter at index 0) get a
    usable correlation cursor. Heartbeat reporters return `""` and
    don't shadow the audit reporter's ID."""

    def __init__(self, *reporters: ProgressReporter) -> None:
        self._reporters: tuple[ProgressReporter, ...] = tuple(reporters)

    def _fanout(self, method_name: str, *args: Any, **kwargs: Any) -> str:
        first_id = ""
        for r in self._reporters:
            method = getattr(r, method_name)
            try:
                result = method(*args, **kwargs)
            except Exception:  # noqa: BLE001 — observability never blocks ingest
                continue
            if not first_id and isinstance(result, str) and result:
                first_id = result
        return first_id

    # Delegate every method by name. Tedious but explicit — keeps the
    # type-check happy for callers and makes the contract obvious.
    def report_run_created(self, *a, **kw):
        return self._fanout("report_run_created", *a, **kw)
    def report_document_received(self, *a, **kw):
        return self._fanout("report_document_received", *a, **kw)
    def report_assessment_started(self, *a, **kw):
        return self._fanout("report_assessment_started", *a, **kw)
    def report_assessment_completed(self, *a, **kw):
        return self._fanout("report_assessment_completed", *a, **kw)
    def report_plan_generated(self, *a, **kw):
        return self._fanout("report_plan_generated", *a, **kw)
    def report_plan_confirmed(self, *a, **kw):
        return self._fanout("report_plan_confirmed", *a, **kw)
    def report_step_started(self, *a, **kw):
        return self._fanout("report_step_started", *a, **kw)
    def report_step_progress(self, *a, **kw):
        return self._fanout("report_step_progress", *a, **kw) or None
    def report_step_skipped(self, *a, **kw):
        return self._fanout("report_step_skipped", *a, **kw)
    def report_step_warning(self, *a, **kw):
        return self._fanout("report_step_warning", *a, **kw)
    def report_step_completed(self, *a, **kw):
        return self._fanout("report_step_completed", *a, **kw)
    def report_step_failed(self, *a, **kw):
        return self._fanout("report_step_failed", *a, **kw)
    def report_run_completed(self, *a, **kw):
        return self._fanout("report_run_completed", *a, **kw)
    def report_run_failed(self, *a, **kw):
        return self._fanout("report_run_failed", *a, **kw)
    def report_run_cancelled(self, *a, **kw):
        return self._fanout("report_run_cancelled", *a, **kw)
    def report_human_review_required(self, *a, **kw):
        return self._fanout("report_human_review_required", *a, **kw)


class NoopProgressReporter:
    """Reporter that records nothing. Use in unit tests / dry-runs."""

    def report_run_created(self, *_, **__): return ""
    def report_document_received(self, *_, **__): return ""
    def report_assessment_started(self, *_, **__): return ""
    def report_assessment_completed(self, *_, **__): return ""
    def report_plan_generated(self, *_, **__): return ""
    def report_plan_confirmed(self, *_, **__): return ""
    def report_step_started(self, *_, **__): return ""
    def report_step_progress(self, *_, **__): return None
    def report_step_skipped(self, *_, **__): return ""
    def report_step_warning(self, *_, **__): return ""
    def report_step_completed(self, *_, **__): return ""
    def report_step_failed(self, *_, **__): return ""
    def report_run_completed(self, *_, **__): return ""
    def report_run_failed(self, *_, **__): return ""
    def report_run_cancelled(self, *_, **__): return ""
    def report_human_review_required(self, *_, **__): return ""
