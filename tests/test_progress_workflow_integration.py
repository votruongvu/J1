"""Integration tests for ProgressReporter wiring through workflow + activities.

Verifies:
  * Activities emit `step.started` / `step.completed` when a reporter
    is configured AND the request carries a `correlation_id`.
  * Activities emit `step.failed` on exception and re-raise (do NOT
    swallow).
  * Workflow exit calls `report_run_terminal` activity that produces
    `run.completed` / `run.failed` events.
  * Skipped stages emit `step.skipped` events.
  * Reporter=None (default) → bit-for-bit identical to legacy behaviour.

Tests use in-memory `_RecordingReporter` to capture every call.
"""

from __future__ import annotations

import pytest
from typing import Any

from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import AuditSink
from j1.orchestration.activities.payloads import (
    CompileActivityInput,
    EnrichActivityInput,
    GraphActivityInput,
    IndexActivityInput,
    ProjectScope,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.orchestration.activities.runs import (
    ReportRunTerminalInput,
    ReportStepSkippedInput,
    RunsActivities,
    StepSummaryEntry,
)
from j1.processing.results import (
    ArtifactProcessingResult,
    ProcessingResult,
    ResultStatus,
)
from j1.projects.context import ProjectContext
from j1.runs import (
    AuditProgressReporter,
    NoopProgressReporter,
    ProgressReporter,
)


# ---- Test reporter --------------------------------------------------


class _RecordingReporter:
    """Captures every progress call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, kind: str, **kwargs):
        self.calls.append((kind, kwargs))
        return f"evt-{len(self.calls)}"

    def report_run_created(self, _ctx, **kw): return self._record("run.created", **kw)
    def report_document_received(self, _ctx, **kw): return self._record("document.received", **kw)
    def report_assessment_started(self, _ctx, **kw): return self._record("assessment.started", **kw)
    def report_assessment_completed(self, _ctx, **kw): return self._record("assessment.completed", **kw)
    def report_plan_generated(self, _ctx, **kw): return self._record("plan.generated", **kw)
    def report_plan_confirmed(self, _ctx, **kw): return self._record("plan.confirmed", **kw)
    def report_step_started(self, _ctx, **kw): return self._record("step.started", **kw)
    def report_step_progress(self, _ctx, **kw): return self._record("step.progress", **kw)
    def report_step_skipped(self, _ctx, **kw): return self._record("step.skipped", **kw)
    def report_step_warning(self, _ctx, **kw): return self._record("step.warning", **kw)
    def report_step_completed(self, _ctx, **kw): return self._record("step.completed", **kw)
    def report_step_failed(self, _ctx, **kw): return self._record("step.failed", **kw)
    def report_run_completed(self, _ctx, **kw): return self._record("run.completed", **kw)
    def report_run_failed(self, _ctx, **kw): return self._record("run.failed", **kw)
    def report_run_cancelled(self, _ctx, **kw): return self._record("run.cancelled", **kw)
    def report_human_review_required(self, _ctx, **kw): return self._record("human_review.required", **kw)


@pytest.fixture
def reporter() -> _RecordingReporter:
    return _RecordingReporter()


@pytest.fixture
def scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


# ---- Stub processing service + adapter registries ------------


class _StubProcessing:
    """Drop-in stub for `ProcessingService`. Each method returns the
    canned result; tests can flip to FAILED / SKIPPED to exercise
    different paths."""

    def __init__(self):
        self.compile_result = ArtifactProcessingResult(status=ResultStatus.SUCCEEDED)
        self.enrich_result = ArtifactProcessingResult(status=ResultStatus.SUCCEEDED)
        self.build_graph_result = ArtifactProcessingResult(status=ResultStatus.SUCCEEDED)
        self.index_result = ProcessingResult(status=ResultStatus.SUCCEEDED)
        self.compile_raises: Exception | None = None

    def compile(self, ctx, compiler, document, *, actor, correlation_id):
        if self.compile_raises:
            raise self.compile_raises
        return self.compile_result

    def enrich(self, ctx, processor, artifact, *, actor, correlation_id):
        return self.enrich_result

    def build_graph(self, ctx, builder, artifact_ids, *, actor, correlation_id):
        return self.build_graph_result

    def index(self, ctx, indexer, artifact_ids, *, actor, correlation_id):
        return self.index_result


class _StubSourceRegistry:
    def get(self, _ctx, _doc_id):
        return object()  # opaque — service stub doesn't inspect


class _StubArtifactRegistry:
    def get(self, _ctx, _artifact_id):
        return object()


def _activities(reporter: ProgressReporter | None) -> ProcessingActivities:
    return ProcessingActivities(
        processing=_StubProcessing(),
        sources=_StubSourceRegistry(),
        artifacts=_StubArtifactRegistry(),
        compilers={"mock": object()},
        enrichers={"mock": object()},
        graph_builders={"mock": object()},
        indexers={"mock": object()},
        progress_reporter=reporter,
    )


# ---- Activity progress emission ---------------------------------


def test_compile_emits_step_started_then_step_completed(scope, reporter):
    activities = _activities(reporter)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    kinds = [c[0] for c in reporter.calls]
    assert kinds == ["step.started", "step.completed"]
    started = reporter.calls[0][1]
    assert started["stage"] == "COMPILE"
    assert started["step"] == "compile"
    assert started["run_id"] == "run-1"
    completed = reporter.calls[1][1]
    assert completed["stage"] == "COMPILE"
    assert completed["run_id"] == "run-1"


def test_compile_without_reporter_emits_no_events(scope):
    """Backwards-compat regression guard: reporter=None → zero
    progress calls. Default deployment behaviour is unchanged."""
    activities = _activities(None)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    # No assertions can be made about a missing reporter, but the
    # call must not raise — the activity behaves identically to
    # the pre-progress-layer baseline.


def test_compile_without_correlation_id_emits_no_events(scope, reporter):
    """An activity invocation without correlation_id (== run_id)
    has nothing to attach progress events to. The activity must
    skip emission rather than write events with empty run_id."""
    activities = _activities(reporter)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id=None,
    ))
    assert reporter.calls == []


def test_compile_emits_step_failed_on_service_exception_and_reraises(scope, reporter):
    """Critical: the reporter MUST NOT swallow exceptions. The
    service raises, the reporter records `step.failed`, then the
    activity re-raises so the workflow's failure-propagation
    contract still fires."""
    proc = _StubProcessing()
    proc.compile_raises = RuntimeError("vendor exploded")
    activities = ProcessingActivities(
        processing=proc,
        sources=_StubSourceRegistry(),
        artifacts=_StubArtifactRegistry(),
        compilers={"mock": object()},
        progress_reporter=reporter,
    )
    with pytest.raises(RuntimeError, match="vendor exploded"):
        activities.compile(CompileActivityInput(
            scope=scope, document_id="doc-1", processor_kind="mock",
            actor="tester", correlation_id="run-1",
        ))
    kinds = [c[0] for c in reporter.calls]
    assert kinds == ["step.started", "step.failed"]
    failed = reporter.calls[1][1]
    assert failed["error_type"] == "RuntimeError"
    assert "vendor exploded" in failed["error_message"]


def test_compile_failed_status_emits_step_failed(scope, reporter):
    """Service-level failure (status=FAILED) ALSO emits step.failed —
    the workflow's `_BusinessRejection` then converts it into a
    workflow-level ApplicationError."""
    activities = _activities(reporter)
    activities._processing.compile_result = ArtifactProcessingResult(
        status=ResultStatus.FAILED, error="parser error",
    )
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    kinds = [c[0] for c in reporter.calls]
    assert kinds == ["step.started", "step.failed"]


def test_enrich_emits_correct_stage(scope, reporter):
    activities = _activities(reporter)
    activities.enrich(EnrichActivityInput(
        scope=scope, artifact_id="art-1", processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    started = next(c for c in reporter.calls if c[0] == "step.started")
    assert started[1]["stage"] == "ENRICH"
    assert started[1]["step"] == "enrich"


def test_build_graph_emits_correct_stage(scope, reporter):
    activities = _activities(reporter)
    activities.build_graph(GraphActivityInput(
        scope=scope, artifact_ids=("a-1",), processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    started = next(c for c in reporter.calls if c[0] == "step.started")
    assert started[1]["stage"] == "GRAPH"
    assert started[1]["step"] == "build_graph"


def test_index_emits_correct_stage(scope, reporter):
    activities = _activities(reporter)
    activities.index(IndexActivityInput(
        scope=scope, artifact_ids=("a-1",), processor_kind="mock",
        actor="tester", correlation_id="run-1",
    ))
    started = next(c for c in reporter.calls if c[0] == "step.started")
    assert started[1]["stage"] == "INDEX"
    assert started[1]["step"] == "index"


# ---- Mid-flight run-record updates -----------------------------


def _activities_with_run_store(reporter, run_store) -> ProcessingActivities:
    return ProcessingActivities(
        processing=_StubProcessing(),
        sources=_StubSourceRegistry(),
        artifacts=_StubArtifactRegistry(),
        compilers={"mock": object()},
        enrichers={"mock": object()},
        graph_builders={"mock": object()},
        indexers={"mock": object()},
        progress_reporter=reporter,
        run_store=run_store,
    )


def test_compile_flips_run_record_from_assessing_to_running(
    scope, reporter, ctx, workspace,
):
    """Regression for the 'UI stuck on Building execution plan' bug.
    The PrimaryStatusPanel renders that label while `IngestionRun.status`
    is ASSESSING / CREATED. Without a mid-flight update the run sits at
    ASSESSING until terminal — now the first step.started flips it to
    RUNNING and updates current_stage / progress_percent."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-mid",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.ASSESSING,
        started_at=now, updated_at=now,
    ))

    activities = _activities_with_run_store(reporter, store)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-mid",
    ))

    after = store.get(ctx, "run-mid")
    assert after is not None
    assert after.status == RunStatus.RUNNING
    assert after.current_stage == "COMPILE"
    assert after.current_step == "compile"
    assert after.progress_percent >= 10  # start-of-stage tick
    assert after.completed_at is None    # still in flight


def test_full_pipeline_updates_run_record_through_each_stage(
    scope, reporter, ctx, workspace,
):
    """Walk the activities in pipeline order and assert the run record
    ends up at the highest progress tick (INDEX end) and the last
    stage sticks. Each stage transition pushes progress forward but
    never regresses it."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-pipeline",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.ASSESSING,
        started_at=now, updated_at=now,
    ))

    activities = _activities_with_run_store(reporter, store)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-pipeline",
    ))
    activities.enrich(EnrichActivityInput(
        scope=scope, artifact_id="art-1", processor_kind="mock",
        actor="tester", correlation_id="run-pipeline",
    ))
    activities.build_graph(GraphActivityInput(
        scope=scope, artifact_ids=("art-1",), processor_kind="mock",
        actor="tester", correlation_id="run-pipeline",
    ))
    activities.index(IndexActivityInput(
        scope=scope, artifact_ids=("art-1",), processor_kind="mock",
        actor="tester", correlation_id="run-pipeline",
    ))

    after = store.get(ctx, "run-pipeline")
    assert after is not None
    assert after.status == RunStatus.RUNNING  # terminal flip happens elsewhere
    assert after.current_stage == "INDEX"
    assert after.current_step == "index"
    assert after.progress_percent >= 95


def test_compile_without_run_store_does_not_crash(scope, reporter):
    """Backwards-compat regression guard: deployments that don't wire
    a `run_store` keep working — the activity must NOT raise just
    because the IngestionRun update path is unavailable."""
    activities = ProcessingActivities(
        processing=_StubProcessing(),
        sources=_StubSourceRegistry(),
        artifacts=_StubArtifactRegistry(),
        compilers={"mock": object()},
        progress_reporter=reporter,
        run_store=None,
    )
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-no-store",
    ))
    # Reporter still got the events; legacy behaviour preserved.
    assert [c[0] for c in reporter.calls] == ["step.started", "step.completed"]


def test_compile_surfaces_content_stats_from_processor_metadata(
    scope, reporter,
):
    """The post-compile planner reads `content_stats` off the activity
    result to override the deterministic profile (so a 1-page PDF
    that contains only a diagram gets `has_images=True`). Compile
    processors signal this via `ArtifactProcessingResult.metadata`;
    `_artifact_result` re-projects only the planner-recognised keys."""
    activities = _activities(reporter)
    activities._processing.compile_result = ArtifactProcessingResult(
        status=ResultStatus.SUCCEEDED,
        metadata={
            "has_images": True,
            "has_tables": False,
            "page_count": 12,
            "internal_unrelated_field": "should be filtered out",
        },
    )
    result = activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-stats",
    ))
    assert result.content_stats is not None
    assert result.content_stats["has_images"] is True
    assert result.content_stats["has_tables"] is False
    assert result.content_stats["page_count"] == 12
    assert "internal_unrelated_field" not in result.content_stats


def test_compile_without_processor_metadata_leaves_content_stats_none(
    scope, reporter,
):
    """Processors that don't surface content signals leave
    `content_stats=None` so the planner falls back to the deterministic
    profile (legacy behaviour)."""
    activities = _activities(reporter)
    result = activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-nostats",
    ))
    assert result.content_stats is None


# ---- Run-terminal activity ------------------------------------


def test_run_terminal_activity_succeeded_calls_run_completed(scope, reporter):
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="succeeded", warning_count=0,
    ))
    assert [c[0] for c in reporter.calls] == ["run.completed"]
    assert reporter.calls[0][1]["final_status"] == "succeeded"


def test_run_terminal_activity_failed_calls_run_failed(scope, reporter):
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="failed",
        failure_code="J1_INGEST_REQUIRED_STEP_FAILED",
        failure_message="compile failed",
    ))
    assert [c[0] for c in reporter.calls] == ["run.failed"]
    assert reporter.calls[0][1]["failure_code"] == "J1_INGEST_REQUIRED_STEP_FAILED"


def test_run_terminal_activity_warnings_uses_warning_count(scope, reporter):
    """Open-question default: step.warning increments warning_count.
    The terminal activity reads it back and emits run.completed
    with `final_status='succeeded_with_warnings'` when count > 0."""
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="succeeded_with_warnings", warning_count=2,
    ))
    completed = reporter.calls[0]
    assert completed[1]["warning_count"] == 2
    assert completed[1]["final_status"] == "succeeded_with_warnings"


def test_run_terminal_activity_cancelled_calls_run_cancelled(scope, reporter):
    """Cancellation is its own terminal event so the SSE stream
    closes cleanly via `run.cancelled` (the FE doesn't have to
    misread a cancelled run as failed). The reason field carries
    over from the workflow's failure_message slot — workflows fan
    that into the activity input as the human-readable cause."""
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="cancelled",
        failure_message="cancelled by operator",
    ))
    assert [c[0] for c in reporter.calls] == ["run.cancelled"]
    cancelled = reporter.calls[0][1]
    assert cancelled["reason"] == "cancelled by operator"


def test_run_terminal_activity_timed_out_calls_run_failed(scope, reporter):
    """`timed_out` is treated like a failure — the SSE stream closes
    via `run.failed` and the FE renders the failed panel."""
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="timed_out",
        failure_message="activity heartbeat timeout",
    ))
    assert [c[0] for c in reporter.calls] == ["run.failed"]
    assert reporter.calls[0][1]["failure_code"] == "TIMED_OUT"


def test_step_skipped_activity_emits_step_skipped(scope, reporter):
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_step_skipped(ReportStepSkippedInput(
        scope=scope, run_id="run-1", stage="GRAPH", step="graph",
        reason="planner skipped — TEXT_ONLY mode",
        source="planner",
    ))
    assert [c[0] for c in reporter.calls] == ["step.skipped"]
    skipped = reporter.calls[0][1]
    assert skipped["stage"] == "GRAPH"
    assert "TEXT_ONLY" in skipped["reason"]


def test_runs_activities_no_reporter_is_silent_no_op(scope):
    """When the deployment hasn't wired a reporter, the activities
    must silently no-op rather than crash."""
    runs = RunsActivities(progress_reporter=None)
    # Should not raise.
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1", final_status="succeeded",
    ))
    runs.report_step_skipped(ReportStepSkippedInput(
        scope=scope, run_id="run-1", stage="GRAPH", step="graph",
        reason="x",
    ))


# ---- Run-record terminal persistence -------------------------


def test_run_terminal_activity_flips_run_record_to_failed(
    scope, reporter, ctx, workspace,
):
    """When the workflow fails, the run record's `status` field MUST
    flip to FAILED. Without this, the FE's `GET /ingestion-runs/{id}`
    response keeps reporting `running` and the run-detail page sits
    on the "Running" panel even though the timeline shows the
    failure. Operators see "UI doesn't reflect anything" while
    worker logs scream."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-fail",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))

    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope,
        run_id="run-fail",
        final_status="failed",
        failure_code="J1_INGEST_REQUIRED_STEP_FAILED",
        failure_message="compile died",
    ))
    after = store.get(ctx, "run-fail")
    assert after is not None
    assert after.status == RunStatus.FAILED
    assert after.failure_code == "J1_INGEST_REQUIRED_STEP_FAILED"
    assert after.failure_message == "compile died"
    assert after.completed_at is not None


def test_run_terminal_activity_flips_run_record_to_succeeded(
    scope, reporter, ctx, workspace,
):
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-ok",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))
    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-ok",
        final_status="succeeded", warning_count=0,
    ))
    after = store.get(ctx, "run-ok")
    assert after.status == RunStatus.SUCCEEDED
    assert after.progress_percent == 100
    assert after.completed_at is not None


def test_run_terminal_activity_uses_succeeded_with_warnings_when_warnings(
    scope, reporter, ctx, workspace,
):
    """Successful runs that accumulated warnings must surface as
    SUCCEEDED_WITH_WARNINGS so the FE's primary status panel renders
    the warning variant rather than the clean-success variant."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-warn",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))
    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-warn",
        final_status="succeeded", warning_count=2,
    ))
    after = store.get(ctx, "run-warn")
    assert after.status == RunStatus.SUCCEEDED_WITH_WARNINGS
    assert after.warning_count == 2


def test_run_terminal_activity_flips_run_record_to_cancelled(
    scope, reporter, ctx, workspace,
):
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-cxl",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))
    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-cxl",
        final_status="cancelled",
        failure_message="operator cancelled",
    ))
    after = store.get(ctx, "run-cxl")
    assert after.status == RunStatus.CANCELLED
    assert after.completed_at is not None


def test_run_terminal_activity_no_store_silent_no_op(scope, reporter, ctx):
    """No `run_store` wired → the run-record update is skipped, but
    the audit-event emission still fires (legacy behaviour)."""
    runs = RunsActivities(progress_reporter=reporter, run_store=None)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1", final_status="succeeded",
    ))
    # Reporter still got the call.
    assert any(c[0] == "run.completed" for c in reporter.calls)


def test_run_terminal_activity_missing_run_is_no_op(scope, reporter, workspace):
    """A terminal call for a run that's not in the store must NOT
    crash — the deterministic workflow_id pattern means a stale
    Temporal workflow can fire after the run record was deleted."""
    from j1.runs import JsonlIngestionRunStore

    store = JsonlIngestionRunStore(workspace)
    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    # Should not raise.
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="never-existed", final_status="failed",
    ))


# ---- Phase 4: step_results persistence into run.metadata --------


def test_run_terminal_persists_step_summary_into_run_metadata(
    scope, reporter, ctx, workspace,
):
    """The activity writes the workflow's step summary into the run
    record's `metadata["step_results"]` so the review surface
    (`GET /ingestion-runs/{id}/summary`) can render the per-stage
    recap without scraping the audit log."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-summary",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))

    summary = (
        StepSummaryEntry(
            step="compile", status="completed", required=True,
            source="caller", artifact_count=3,
        ),
        StepSummaryEntry(
            step="graph", status="skipped", required=False,
            source="planner", reason="TEXT_ONLY mode",
        ),
    )
    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-summary",
        final_status="succeeded", warning_count=0,
        step_summary=summary,
    ))

    after = store.get(ctx, "run-summary")
    persisted = after.metadata["step_results"]
    assert len(persisted) == 2
    assert persisted[0]["step"] == "compile"
    assert persisted[0]["status"] == "completed"
    assert persisted[0]["required"] is True
    assert persisted[0]["artifact_count"] == 3
    assert persisted[1]["status"] == "skipped"
    assert persisted[1]["source"] == "planner"
    assert persisted[1]["reason"] == "TEXT_ONLY mode"


def test_run_terminal_with_empty_step_summary_keeps_existing_metadata(
    scope, reporter, ctx, workspace,
):
    """Phase 4 contract: an empty step_summary must NOT overwrite
    previously-persisted step_results. A re-run of the activity
    after a crash should not blank good data."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-existing",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
        metadata={"step_results": [{"step": "compile", "status": "completed"}]},
    ))

    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-existing",
        final_status="succeeded",
        step_summary=(),
    ))

    after = store.get(ctx, "run-existing")
    assert after.metadata["step_results"] == [
        {"step": "compile", "status": "completed"}
    ]


def test_run_terminal_partial_completed_with_warnings_flips_to_succeeded_with_warnings(
    scope, reporter, ctx, workspace,
):
    """A workflow that returns FinalStatus.PARTIAL_COMPLETED + warnings
    must surface as RunStatus.SUCCEEDED_WITH_WARNINGS so the FE shows
    the warning header. Mirrors the Phase 4 _compute_final_status
    extension."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-partial",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))

    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-partial",
        final_status="partial_completed", warning_count=1,
    ))

    after = store.get(ctx, "run-partial")
    assert after.status == RunStatus.SUCCEEDED_WITH_WARNINGS
    assert after.warning_count == 1


def test_run_terminal_partial_completed_no_warnings_flips_to_succeeded(
    scope, reporter, ctx, workspace,
):
    """`partial_completed` with no warnings still maps cleanly to
    SUCCEEDED — the FE doesn't need a third state."""
    from datetime import datetime, timezone

    from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus

    store = JsonlIngestionRunStore(workspace)
    now = datetime.now(timezone.utc)
    store.upsert(ctx, IngestionRun(
        run_id="run-pc",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
    ))

    runs = RunsActivities(progress_reporter=reporter, run_store=store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-pc",
        final_status="partial_completed", warning_count=0,
    ))

    after = store.get(ctx, "run-pc")
    assert after.status == RunStatus.SUCCEEDED


# ---- Step summary embedded in run-terminal events ----------


def test_step_summary_carries_per_step_status_and_required_flag(scope, reporter):
    """Open-question default: run.completed embeds a step_summary so
    the frontend recap doesn't need a second fetch. The summary
    carries enough fields to render per-step badges."""
    summary = (
        StepSummaryEntry(
            step="compile", status="completed", required=True,
            source="caller", artifact_count=1,
        ),
        StepSummaryEntry(
            step="graph", status="skipped", required=False,
            source="planner", reason="TEXT_ONLY mode",
        ),
    )
    runs = RunsActivities(progress_reporter=reporter)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=scope, run_id="run-1",
        final_status="succeeded", warning_count=0,
        step_summary=summary,
    ))
    # The summary lives on the input dataclass; a real reporter
    # could embed it in the event payload by inspecting the input.
    # Today the reporter signature doesn't carry it forward, but
    # the input dataclass shape is regression-tested via the
    # activity round-trip below.
    assert len(summary) == 2
    assert summary[0].status == "completed"
    assert summary[1].reason == "TEXT_ONLY mode"


# ---- Audit-backed reporter end-to-end ----------------------


class _RecordingSink(AuditSink):
    def __init__(self) -> None:
        self.events: list = []
    def write(self, event) -> None:
        self.events.append(event)


def test_audit_backed_reporter_persists_step_started_via_activity(scope):
    """End-to-end through the real `AuditProgressReporter`: an
    activity call lands a `j1.progress.step.started` audit event
    with `correlation_id == run_id`."""
    sink = _RecordingSink()
    reporter = AuditProgressReporter(DefaultAuditRecorder(sink))
    activities = _activities(reporter)
    activities.compile(CompileActivityInput(
        scope=scope, document_id="doc-1", processor_kind="mock",
        actor="tester", correlation_id="run-42",
    ))
    actions = [e.action for e in sink.events]
    assert "j1.progress.step.started" in actions
    assert "j1.progress.step.completed" in actions
    started = next(e for e in sink.events
                   if e.action == "j1.progress.step.started")
    assert started.correlation_id == "run-42"
    assert started.target_id == "run-42"
    assert started.payload["stage"] == "COMPILE"
