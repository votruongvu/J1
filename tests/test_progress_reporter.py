"""Tests for the `ProgressReporter` abstraction."""

from __future__ import annotations

import pytest

from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import AuditSink
from j1.projects.context import ProjectContext
from j1.runs import (
    ACTION_PROGRESS_DOCUMENT_RECEIVED,
    ACTION_PROGRESS_PLAN_GENERATED,
    ACTION_PROGRESS_RUN_CANCELLED,
    ACTION_PROGRESS_RUN_COMPLETED,
    ACTION_PROGRESS_RUN_CREATED,
    ACTION_PROGRESS_STEP_COMPLETED,
    ACTION_PROGRESS_STEP_FAILED,
    ACTION_PROGRESS_STEP_PROGRESS,
    ACTION_PROGRESS_STEP_SKIPPED,
    ACTION_PROGRESS_STEP_STARTED,
    AuditProgressReporter,
    CompositeProgressReporter,
    NoopProgressReporter,
    PROGRESS_TARGET_KIND,
    ProgressReporter,
)


class _RecordingSink(AuditSink):
    """In-memory sink that captures every write."""

    def __init__(self) -> None:
        self.events: list = []

    def write(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@pytest.fixture
def reporter(sink) -> AuditProgressReporter:
    return AuditProgressReporter(DefaultAuditRecorder(sink))


# ---- Lifecycle events -----------------------------------------------


def test_run_created_emits_audit_event_with_correct_action_and_correlation(
    reporter, sink, ctx,
):
    event_id = reporter.report_run_created(
        ctx, run_id="run-1", document_id="doc-1",
    )
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_id == event_id
    assert event.action == ACTION_PROGRESS_RUN_CREATED
    assert event.target_kind == PROGRESS_TARGET_KIND
    assert event.target_id == "run-1"
    assert event.correlation_id == "run-1"
    assert event.payload["document_id"] == "doc-1"


def test_document_received_emits_info_severity(reporter, sink, ctx):
    reporter.report_document_received(ctx, run_id="r1", document_id="d1")
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_DOCUMENT_RECEIVED
    assert event.payload.get("severity") == "INFO"


def test_plan_generated_carries_plan_payload(reporter, sink, ctx):
    plan = {"mode": "TEXT_ONLY", "steps": [{"name": "compile", "decision": "RUN"}]}
    reporter.report_plan_generated(ctx, run_id="r1", plan_payload=plan)
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_PLAN_GENERATED
    assert event.payload["plan"] == plan


# ---- Step events ----------------------------------------------------


def test_step_started_resets_progress_throttle(reporter, sink, ctx):
    """Reporting step.started must clear any prior throttle state for
    the same (run, stage, step) so a re-run of the same step starts
    fresh — otherwise the first progress tick of the second run would
    be silently dropped."""
    # Drive the throttle into a non-zero state.
    reporter.report_step_started(ctx, run_id="r1", stage="COMPILE", step="parse")
    reporter.report_step_progress(
        ctx, run_id="r1", stage="COMPILE", step="parse", progress_percent=50,
    )
    sink.events.clear()
    # Restart and immediately progress 1% — should fire because we
    # cleared the throttle on step.started.
    reporter.report_step_started(ctx, run_id="r1", stage="COMPILE", step="parse")
    reporter.report_step_progress(
        ctx, run_id="r1", stage="COMPILE", step="parse", progress_percent=1,
    )
    actions = [e.action for e in sink.events]
    assert ACTION_PROGRESS_STEP_STARTED in actions
    # 1% is below the throttle (5%) but the post-restart 0% baseline
    # makes 1% a 1-point delta… still under threshold. Test the
    # boundary more carefully:
    # Reset and verify a 5% step IS emitted after restart.
    sink.events.clear()
    reporter.report_step_started(ctx, run_id="r1", stage="COMPILE", step="parse")
    sink.events.clear()
    eid = reporter.report_step_progress(
        ctx, run_id="r1", stage="COMPILE", step="parse", progress_percent=5,
    )
    assert eid is not None  # emitted (>= threshold against fresh baseline)


def test_step_progress_throttles_sub_5_percent_deltas(reporter, sink, ctx):
    """Drop progress events that move <5% to keep audit volume bounded.
    0% and 100% are always emitted (step boundaries)."""
    reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=0,
    )
    # 1% → dropped
    eid = reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=1,
    )
    assert eid is None
    # 5% → emitted (matches threshold against last-emitted 0%)
    eid = reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=5,
    )
    assert eid is not None
    # 100% → always emitted
    eid = reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=100,
    )
    assert eid is not None


def test_step_progress_clamps_out_of_range_percentages(reporter, sink, ctx):
    """Defensive: out-of-range values get clamped to 0..100 rather
    than written verbatim into the audit log."""
    reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=-10,
    )
    reporter.report_step_progress(
        ctx, run_id="r1", stage="C", step="x", progress_percent=200,
    )
    pcts = [e.payload.get("progress_percent") for e in sink.events]
    assert all(0 <= p <= 100 for p in pcts)


def test_step_progress_carries_engine_and_provider(reporter, sink, ctx):
    reporter.report_step_progress(
        ctx, run_id="r1", stage="COMPILE", step="LAYOUT", progress_percent=50,
        current=22, total=44, message="Layout: 22/44 pages",
        engine="MinerU",
    )
    event = sink.events[-1]
    assert event.payload["engine"] == "MinerU"
    assert event.payload["current"] == 22
    assert event.payload["total"] == 44


def test_step_skipped_records_reason_and_skipped_status(reporter, sink, ctx):
    reporter.report_step_skipped(
        ctx, run_id="r1", stage="GRAPH", step="graph",
        reason="planner skipped — TEXT_ONLY mode",
    )
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_STEP_SKIPPED
    assert event.payload["status"] == "skipped"
    assert "TEXT_ONLY" in event.payload["reason"]


def test_step_failed_carries_error_type_and_severity(reporter, sink, ctx):
    reporter.report_step_failed(
        ctx, run_id="r1", stage="COMPILE", step="parse",
        error_type="ProviderUnavailable", error_message="vendor exploded",
        retryable=False,
    )
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_STEP_FAILED
    assert event.payload["severity"] == "ERROR"
    assert event.payload["error_type"] == "ProviderUnavailable"
    assert event.payload["retryable"] is False


def test_step_completed_sets_progress_to_100(reporter, sink, ctx):
    reporter.report_step_completed(
        ctx, run_id="r1", stage="COMPILE", step="parse", artifact_count=3,
    )
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_STEP_COMPLETED
    assert event.payload["progress_percent"] == 100
    assert event.payload["artifact_count"] == 3


# ---- Run completion -------------------------------------------------


def test_run_completed_with_warnings_uses_warning_severity(reporter, sink, ctx):
    reporter.report_run_completed(
        ctx, run_id="r1", final_status="succeeded_with_warnings", warning_count=2,
    )
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_RUN_COMPLETED
    assert event.payload["severity"] == "WARNING"
    assert event.payload["warning_count"] == 2


def test_run_completed_clean_uses_info_severity(reporter, sink, ctx):
    reporter.report_run_completed(
        ctx, run_id="r1", final_status="succeeded", warning_count=0,
    )
    assert sink.events[-1].payload["severity"] == "INFO"


def test_run_cancelled_records_cancelled_action_and_reason(reporter, sink, ctx):
    """`run.cancelled` is its own terminal event (not a flavour of
    run.failed). The audit `action` and the payload `status` both
    carry the cancellation marker so consumers can distinguish
    operator-cancellation from a failure."""
    reporter.report_run_cancelled(
        ctx, run_id="r1", reason="operator-cancelled",
    )
    event = sink.events[-1]
    assert event.action == ACTION_PROGRESS_RUN_CANCELLED
    assert event.payload["status"] == "cancelled"
    assert event.payload["severity"] == "WARNING"
    assert event.payload["reason"] == "operator-cancelled"


def test_run_cancelled_omits_reason_when_not_supplied(reporter, sink, ctx):
    reporter.report_run_cancelled(ctx, run_id="r1")
    payload = sink.events[-1].payload
    assert payload["status"] == "cancelled"
    assert "reason" not in payload


# ---- Composite ------------------------------------------------------


def test_composite_fans_out_to_every_delegate(ctx):
    """Composite must call every delegate (not stop on first one)."""
    a = NoopProgressReporter()
    b = NoopProgressReporter()
    calls = {"a": 0, "b": 0}

    def _wrap(name, base):
        original = base.report_run_created
        def wrapped(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        base.report_run_created = wrapped
        return base
    _wrap("a", a)
    _wrap("b", b)

    composite = CompositeProgressReporter(a, b)
    composite.report_run_created(ctx, run_id="r1", document_id="d1")

    assert calls == {"a": 1, "b": 1}


def test_composite_returns_first_non_empty_event_id(ctx, sink):
    """Composite returns the first delegate's event_id so callers
    that need a stable cursor (e.g. the SSE Last-Event-Id resume
    point) get the audit-backed reporter's ID."""
    audit = AuditProgressReporter(DefaultAuditRecorder(sink))
    noop = NoopProgressReporter()

    composite = CompositeProgressReporter(audit, noop)
    eid = composite.report_run_created(ctx, run_id="r1", document_id="d1")

    assert eid  # non-empty
    assert sink.events[-1].event_id == eid


def test_composite_swallows_delegate_exceptions(ctx, sink):
    """A failing delegate must NOT prevent other delegates from
    seeing the event — telemetry is best-effort."""
    class _Boom:
        def report_run_created(self, *_, **__):
            raise RuntimeError("boom")
        # other methods can stay missing — the composite uses getattr
        # only for the called method, so an unrelated method's absence
        # doesn't matter.

    audit = AuditProgressReporter(DefaultAuditRecorder(sink))
    composite = CompositeProgressReporter(_Boom(), audit)
    eid = composite.report_run_created(ctx, run_id="r1", document_id="d1")

    assert eid  # audit reporter still ran
    assert len(sink.events) == 1


# ---- Noop -----------------------------------------------------------


def test_noop_reporter_implements_protocol():
    """The Noop reporter must satisfy the `ProgressReporter` Protocol
    so test doubles can be used wherever a reporter is expected."""
    reporter = NoopProgressReporter()
    assert isinstance(reporter, ProgressReporter)
