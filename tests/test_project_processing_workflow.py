import asyncio
import inspect
from collections.abc import Callable

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProcessingActivityResult,
    ProjectScope,
    SpendSummary,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
    ERROR_TYPE_REQUIRED_STEP_FAILED,
    ERROR_TYPE_UNEXPECTED_ERROR,
    GATE_AFTER_COMPILE,
    GATE_AFTER_ENRICH,
    GATE_AFTER_INDEX,
    OPERATION_COMPILE,
    OPERATION_FINALIZE,
    ProjectProcessingRequest,
    ProjectProcessingResult,
    ProjectProcessingWorkflow,
    WorkflowState,
    WorkflowStatus,
    _BusinessRejection,
)


# Static / shape tests


def test_workflow_has_temporal_marker():
    assert hasattr(ProjectProcessingWorkflow, "__temporal_workflow_definition")


def test_workflow_run_is_async():
    assert inspect.iscoroutinefunction(ProjectProcessingWorkflow.run)


def test_state_enum_has_all_required_states():
    assert {s.value for s in WorkflowState} == {
        "running",
        "paused",
        "waiting_for_budget_approval",
        "waiting_for_review",
        "failed_recoverable",
        "failed_final",
        "completed",
        "cancelled",
    }


@pytest.mark.parametrize(
    "name",
    [
        "pause",
        "resume",
        "cancel",
        "approve_budget",
        "reject_budget",
        "approve_review",
        "reject_review",
    ],
)
def test_signal_methods_exist(name):
    method = getattr(ProjectProcessingWorkflow, name)
    assert callable(method)


def test_query_method_exists():
    assert callable(ProjectProcessingWorkflow.get_status)


def test_request_constructible():
    req = ProjectProcessingRequest(
        scope=ProjectScope(tenant_id="acme", project_id="alpha"),
        compiler_kind="mock.compiler",
        enricher_kind="mock.enricher",
        graph_builder_kind="mock.graph",
        indexer_kind="mock.index",
        budget_limit_amount="10.00",
        review_after=(GATE_AFTER_COMPILE, GATE_AFTER_INDEX),
        correlation_id="run-1",
    )
    assert req.compiler_kind == "mock.compiler"
    assert GATE_AFTER_COMPILE in req.review_after


def test_result_constructible():
    result = ProjectProcessingResult(
        state=WorkflowState.COMPLETED.value,
        artifact_ids=["a-1"],
        documents_total=1,
        documents_completed=1,
    )
    assert result.state == "completed"


# Direct signal/query tests (workflow methods are plain Python)


def test_initial_status():
    wf = ProjectProcessingWorkflow()
    status: WorkflowStatus = wf.get_status()
    assert status.state == WorkflowState.RUNNING.value
    assert status.current_operation is None
    assert status.documents_total == 0
    assert status.documents_completed == 0
    assert status.produced_artifact_ids == []
    assert status.review_gate is None
    assert status.error is None


def test_pause_signal_sets_flag():
    wf = ProjectProcessingWorkflow()
    assert wf._paused is False
    wf.pause()
    assert wf._paused is True


def test_resume_signal_clears_flag():
    wf = ProjectProcessingWorkflow()
    wf.pause()
    wf.resume()
    assert wf._paused is False


def test_cancel_signal_sets_flag():
    wf = ProjectProcessingWorkflow()
    assert wf._cancelled is False
    wf.cancel()
    assert wf._cancelled is True


def test_approve_and_reject_budget_signals():
    wf = ProjectProcessingWorkflow()
    assert wf._budget_approved is None
    wf.approve_budget()
    assert wf._budget_approved is True
    wf.reject_budget()
    assert wf._budget_approved is False


def test_approve_and_reject_review_signals():
    wf = ProjectProcessingWorkflow()
    assert wf._review_approved is None
    wf.approve_review()
    assert wf._review_approved is True
    wf.reject_review()
    assert wf._review_approved is False


# Mock-driven workflow execution


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(monkeypatch, *, exec_handler: Callable, wait_handler: Callable | None = None):
    async def _exec(method, payload=None, **kwargs):
        return exec_handler(method, payload, kwargs)

    if wait_handler is None:

        async def _wait(predicate, **kwargs):
            return None
    else:

        async def _wait(predicate, **kwargs):
            return await wait_handler(predicate, kwargs)

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)


def _activity_name(method) -> str:
    defn = getattr(method, "__temporal_activity_definition", None)
    return defn.name if defn else method.__name__


def test_run_completes_happy_path(monkeypatch):
    calls: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        calls.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity invocation: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="mock.compiler",
    )

    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    assert result.artifact_ids == ["art-1"]
    assert result.documents_total == 1
    assert result.documents_completed == 1
    assert any(c.endswith("compile") for c in calls)
    # The workflow MUST flip the doc off PENDING after a successful
    # process — otherwise subsequent project-wide jobs re-pick it
    # and we get the per-upload reprocessing loop.
    assert any(c.endswith("set_document_status") for c in calls)
    assert calls[-1].endswith("finalize")


def test_target_document_ids_skips_list_pending_and_processes_only_named(monkeypatch):
    """`target_document_ids` lets the user-facing flow scope each
    upload to the just-uploaded document. The workflow must NOT call
    `list_pending_documents` in that case — otherwise every upload
    re-processes every PENDING document in the project (the bug we
    saw with one upload triggering many MinerU starts)."""
    calls: list[str] = []
    statuses: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        calls.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            raise AssertionError(
                "list_pending_documents must not be called when "
                "target_document_ids is set"
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("set_document_status"):
            statuses.append(payload.status)
            return None
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="mock.compiler",
        target_document_ids=("doc-uploaded-just-now",),
    )

    result = asyncio.run(wf.run(request))
    assert result.documents_total == 1
    assert result.documents_completed == 1
    assert statuses == ["succeeded"]


def test_failed_compile_marks_document_failed(monkeypatch):
    """When the per-document pipeline raises, the workflow must
    still flip the doc's registry status (to FAILED). Without this,
    a doc that fails once stays PENDING and gets retried by every
    subsequent bulk job indefinitely."""
    statuses: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-fail"]
        if name.endswith("compile"):
            return ArtifactActivityResult(status="failed", error="boom")
        if name.endswith("set_document_status"):
            statuses.append(payload.status)
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("report_terminal"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="mock.compiler",
    )

    with pytest.raises(Exception):
        asyncio.run(wf.run(request))
    assert statuses == ["failed"]


def test_rebuild_index_only_skips_documents_loop_and_runs_only_index(
    monkeypatch,
):
    """`rebuild_index_only=True` must skip every per-document stage
    (compile / chunks / enrich / graph) and run ONLY the index
    activity against the carry-forward artifact ids. The skipped
    stages must surface as SKIPPED step records with a clear reason
    so the FE timeline still shows the full pipeline shape."""
    dispatched: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        dispatched.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            raise AssertionError(
                "list_pending_documents should not run in "
                "rebuild_index_only mode"
            )
        if name.endswith("compile"):
            raise AssertionError("compile should not run in rebuild mode")
        if name.endswith("enrich"):
            raise AssertionError("enrich should not run in rebuild mode")
        if name.endswith("build_graph"):
            raise AssertionError("build_graph should not run in rebuild mode")
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        resume_from_run_id="prior-run",
        resume_artifact_ids=("prior-chunk-1", "prior-chunk-2"),
        resume_artifact_kinds=("chunk", "chunk"),
        rebuild_index_only=True,
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    # The index activity ran against the carry-forward chunks.
    assert any(n.endswith("index") for n in dispatched)
    # Carry-forward chunks are visible in the result.
    assert "prior-chunk-1" in result.artifact_ids
    assert "prior-chunk-2" in result.artifact_ids
    # Skipped step records cite the rebuild reason so operators can
    # audit why the upstream stages didn't run.
    skipped = {
        r.step for r in wf._step_results
        if r.status.value == "skipped"
        and "rebuild index only" in (r.reason or "")
    }
    assert {"compile", "generate_knowledge_chunks", "enrich", "graph"} <= skipped
    # Index step recorded as COMPLETED.
    assert any(
        r.step == "index" and r.status.value == "completed"
        for r in wf._step_results
    )


def test_rebuild_index_only_rejects_when_no_indexer_kind(monkeypatch):
    """Rebuild mode requires `indexer_kind` — without one, there's
    no activity to dispatch. Workflow rejects at startup so the
    operator gets a clear failure instead of a silently-empty run."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind=None,
        resume_from_run_id="prior",
        resume_artifact_ids=("prior-chunk",),
        resume_artifact_kinds=("chunk",),
        rebuild_index_only=True,
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert "indexer_kind" in str(excinfo.value)


def test_stage_validation_failure_blocks_completed_status(monkeypatch):
    from j1.processing.status import StepStatus

    """When `validate_stage` returns `passed=False`, the workflow MUST
    NOT mark the stage COMPLETED. Instead it records FAILED + raises
    ApplicationError so Temporal sees the workflow as Failed.

    This is the core "never mark succeeded just because a function
    returned" rule. The compile activity returns succeeded; the
    validator (mocked here) says output is invalid; the workflow
    must block the COMPLETED transition."""
    from j1.orchestration.activities.payloads import (
        StageValidationActivityResult,
    )
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            # Compile activity returns "succeeded" — but the
            # validator below disagrees.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c-1"],
                kinds=("chunk",),
            )
        if name.endswith("validate_stage"):
            # Validator says: output is unreadable / scope wrong /
            # zero chunks / etc. Workflow MUST treat this as a
            # stage failure.
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="failed",
                passed=False,
                error_count=1,
                check_count=1,
                errors=["chunk artifact storage missing"],
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        if name.endswith("persist_validation_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["vr-1"],
                kinds=("validation_report",),
            )
        if name.endswith("persist_final_summary"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["fs-1"],
                kinds=("final_summary",),
            )
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        # correlation_id required for the validation gate to engage.
        correlation_id="run-validation-fail-1",
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    # The compile step is recorded as FAILED with the validator's
    # error message in the reason — auditable trail of WHY the
    # stage was rejected.
    compile_failures = [
        r for r in wf._step_results
        if r.step == "compile" and r.status == StepStatus.FAILED
    ]
    assert len(compile_failures) == 1
    assert "storage missing" in (compile_failures[0].reason or "")
    # And critically: NO COMPLETED entry for compile. The workflow
    # state shouldn't carry a misleading "compile succeeded" record
    # when the validator rejected it.
    compile_completed = [
        r for r in wf._step_results
        if r.step == "compile" and r.status == StepStatus.COMPLETED
    ]
    assert compile_completed == []


def test_aggregator_blocks_succeeded_when_durable_stage_skips_validation(monkeypatch):
    """The `_validate_completion` aggregator must reject SUCCEEDED
    when a durable stage was recorded COMPLETED but no
    `stage_validation_report` was persisted. Defense against a
    future code path bypassing the per-stage gate.

    Setup: the test handler returns successful compile + skips the
    `validate_stage` activity entirely (returns None) — simulating
    a bug where the gate is missing from the workflow."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c-1"],
                kinds=("chunk",),
            )
        if name.endswith("validate_stage"):
            # Simulate the gate not running (returns None — not a
            # real StageValidationActivityResult). Triggers the
            # aggregator's "no validation report persisted" rule.
            return None
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        if name.endswith("persist_validation_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["vr-1"],
                kinds=("validation_report",),
            )
        if name.endswith("persist_final_summary"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["fs-1"],
                kinds=("final_summary",),
            )
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        correlation_id="run-aggregator-1",
    )
    # The workflow's `_validate_stage_output` returns a synthetic
    # FAILED result when the activity returns None (because the
    # `result.passed` access raises AttributeError → except clause
    # fires). So the gate trips during compile, before the aggregator
    # ever runs. Either way, no SUCCEEDED.
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))


def test_resume_skips_enrich_and_graph_when_listed_in_resume_context(monkeypatch):
    """When `resume_completed_steps` lists enrich + graph, the workflow
    must NOT dispatch the corresponding activities — it should record
    SKIPPED step results citing the resume source and carry the
    prior-run artifacts forward through `_produced_artifact_ids`.

    This is the contract the resume endpoint relies on: skip exactly
    the LLM-cost stages that already ran, run everything else."""
    dispatched: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        dispatched.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-resume"]
        if name.endswith("compile"):
            # Compile always re-runs — its outputs are the structural
            # backbone every downstream stage reads. Populate
            # `compile_metrics` realistically so the post-compile
            # quality verdict stays GOOD (the new graph/index gates
            # consult final_compile_quality + chunks_count).
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["new-compile"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3,
                    "extracted_text_chars": 800,
                },
            )
        if name.endswith("enrich"):
            # If we hit this the resume short-circuit is broken.
            raise AssertionError(
                "enrich activity should not run on resume "
                "when 'enrich' is in resume_completed_steps"
            )
        if name.endswith("build_graph"):
            raise AssertionError(
                "build_graph activity should not run on resume "
                "when 'graph' is in resume_completed_steps"
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        enricher_kind="e",
        graph_builder_kind="g",
        indexer_kind="i",
        resume_from_run_id="prior-run-1",
        resume_completed_steps=("enrich", "graph"),
        resume_artifact_ids=("prior-enrich", "prior-graph"),
        resume_artifact_kinds=("enriched.tables", "graph_json"),
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    # Carry-forward IDs are visible alongside this run's compile
    # artifacts so downstream surfaces (index, validation_report)
    # see the full set the prior run produced.
    assert "prior-enrich" in result.artifact_ids
    assert "prior-graph" in result.artifact_ids
    assert "new-compile" in result.artifact_ids
    # Step records show the SKIPPED entries with the resume reason
    # so operators can audit why the LLM-cost stages didn't run.
    enrich_steps = [r for r in wf._step_results if r.step == "enrich"]
    graph_steps = [r for r in wf._step_results if r.step == "graph"]
    assert any(
        r.status.value == "skipped"
        and "prior-run-1" in (r.reason or "")
        for r in enrich_steps
    )
    assert any(
        r.status.value == "skipped"
        and "prior-run-1" in (r.reason or "")
        for r in graph_steps
    )


def test_run_completes_full_pipeline(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            # `kinds` is required by `_validate_completion` to enforce
            # per-stage required artifacts. A compile that produces a
            # `chunk` kind here keeps the synthetic
            # generate_knowledge_chunks step from tripping the
            # "no chunk artifact" rule. `compile_metrics` populates
            # `chunks_count` + `extracted_text_chars` so the new
            # graph/index gates see a healthy compile (otherwise
            # the quality verdict drops to LOW and graph is skipped).
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-c1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3,
                    "extracted_text_chars": 800,
                },
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-e1"],
                kinds=("enriched.tables",),
            )
        if name.endswith("build_graph"):
            # Must include `graph_json` — `_validate_completion`
            # rejects a graph step that completed without producing
            # one (the canonical graph output is missing).
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-g1"],
                kinds=("graph_json",),
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        enricher_kind="e",
        graph_builder_kind="g",
        indexer_kind="i",
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    assert sorted(result.artifact_ids) == ["art-c1", "art-e1", "art-g1"]


def test_validate_failure_raises_application_error_and_marks_failed_final(monkeypatch):
    """Validation failure must surface as Temporal `ApplicationError`
    (workflow Failed in UI), not a returned result with
    `state="failed_final"` (workflow Completed in UI). Regression
    against the false-success bug where the workflow swallowed
    failures and returned them encoded in a status field."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=False, message="bad scope")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert excinfo.value.non_retryable is True
    assert "bad scope" in str(excinfo.value)
    # State is still recorded on the instance for `get_status` queries.
    assert wf._state == WorkflowState.FAILED_FINAL


def test_compile_failure_raises_application_error_and_marks_failed_final(monkeypatch):
    """Compile FAILED must propagate as a Temporal workflow failure,
    not a returned result the caller might miss. Regression against
    the false-success bug."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="failed", error="compiler crashed"
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "compiler crashed" in str(excinfo.value)
    assert wf._state == WorkflowState.FAILED_FINAL


def test_unexpected_exception_raises_application_error_and_marks_failed_recoverable(monkeypatch):
    """Unexpected exceptions are wrapped in a typed `ApplicationError`
    so Temporal UI shows a clean failure type — but `non_retryable`
    stays False to preserve the "transient infrastructure"
    classification (parent workflows / operators may legitimately
    retry these)."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            raise RuntimeError("transient db blip")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_UNEXPECTED_ERROR
    assert excinfo.value.non_retryable is False
    assert "transient db blip" in str(excinfo.value)
    assert wf._state == WorkflowState.FAILED_RECOVERABLE


def test_cancel_before_processing_returns_cancelled(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"should not reach {name} after cancel")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    wf.cancel()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.CANCELLED.value
    assert result.documents_completed == 0


def test_budget_approval_path(monkeypatch):
    """Spend exceeds limit → state moves to WAITING_FOR_BUDGET_APPROVAL → approve_budget unblocks."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("compute_spend"):
            return SpendSummary(
                total_amount="100.00", currency="USD", event_count=1
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    # Sequence: workflow hits wait_condition for budget approval. Approve on first wait.
    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        # Inspect intermediate state at the point the workflow blocks for budget.
        assert wf._state is WorkflowState.WAITING_FOR_BUDGET_APPROVAL
        wf.approve_budget()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        budget_limit_amount="10.00",
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value


def test_budget_rejection_path(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("compute_spend"):
            return SpendSummary(
                total_amount="100.00", currency="USD", event_count=1
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        assert wf._state is WorkflowState.WAITING_FOR_BUDGET_APPROVAL
        wf.reject_budget()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        budget_limit_amount="10.00",
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "budget rejected" in str(excinfo.value)
    assert wf._state == WorkflowState.FAILED_FINAL


def test_review_approval_path(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        assert wf._state is WorkflowState.WAITING_FOR_REVIEW
        assert wf._review_gate == GATE_AFTER_COMPILE
        wf.approve_review()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        review_after=(GATE_AFTER_COMPILE,),
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value


def test_review_rejection_path(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        wf.reject_review()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        review_after=(GATE_AFTER_COMPILE,),
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "review rejected" in str(excinfo.value)
    assert wf._state == WorkflowState.FAILED_FINAL


def test_pause_then_resume_during_run(monkeypatch):
    """Pre-pause the workflow; the wait_handler verifies state is PAUSED then resumes."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()
    wf.pause()

    async def wait_handler(predicate, kwargs):
        assert wf._state is WorkflowState.PAUSED
        wf.resume()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value


def test_initial_status_includes_new_fields():
    wf = ProjectProcessingWorkflow()
    s = wf.get_status()
    assert s.pending_operation is None
    assert s.completed_operations == []
    assert s.review_required is False
    assert s.budget_approval_required is False


def test_completed_operations_recorded_in_order(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1", "doc-2"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=[f"art-{payload.document_id}"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    completed = wf.get_status().completed_operations
    assert completed == [
        "validate",
        "list_documents",
        "compile:doc-1",
        "compile:doc-2",
        "finalize",
    ]


def test_pending_operation_set_during_pause(monkeypatch):
    captured: dict[str, WorkflowStatus] = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()
    wf.pause()

    async def wait_handler(predicate, kwargs):
        captured["paused"] = wf.get_status()
        wf.resume()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    paused_status = captured["paused"]
    assert paused_status.state == WorkflowState.PAUSED.value
    assert paused_status.pending_operation == "compile:doc-1"


def test_budget_approval_required_flag_during_wait(monkeypatch):
    captured: dict[str, WorkflowStatus] = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compute_spend"):
            return SpendSummary(
                total_amount="100.00", currency="USD", event_count=1
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        captured["budget"] = wf.get_status()
        wf.approve_budget()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        budget_limit_amount="10.00",
    )
    asyncio.run(wf.run(request))

    s = captured["budget"]
    assert s.state == WorkflowState.WAITING_FOR_BUDGET_APPROVAL.value
    assert s.budget_approval_required is True
    assert s.pending_operation == "compile:doc-1"


def test_review_required_flag_during_wait(monkeypatch):
    captured: dict[str, WorkflowStatus] = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()

    async def wait_handler(predicate, kwargs):
        captured["review"] = wf.get_status()
        wf.approve_review()

    _patch_workflow_runtime(
        monkeypatch, exec_handler=handler, wait_handler=wait_handler
    )

    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        review_after=(GATE_AFTER_COMPILE,),
    )
    asyncio.run(wf.run(request))

    s = captured["review"]
    assert s.state == WorkflowState.WAITING_FOR_REVIEW.value
    assert s.review_required is True
    assert s.review_gate == GATE_AFTER_COMPILE


def test_budget_check_runs_before_compile(monkeypatch):
    """compute_spend must be called BEFORE compile, not after."""
    call_order: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compute_spend"):
            call_order.append("compute_spend")
            return SpendSummary(
                total_amount="0.00", currency="USD", event_count=0
            )
        if name.endswith("compile"):
            call_order.append("compile")
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        budget_limit_amount="10.00",
    )
    asyncio.run(wf.run(request))

    assert call_order == ["compute_spend", "compile"]


def test_failure_reason_in_status(monkeypatch):
    """`get_status` query must remain readable even after the workflow
    raises — Temporal serves queries against the workflow's recorded
    state independently of whether `run()` exited cleanly. The
    workflow records state THEN raises, so this query still works
    after a failure."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=False, message="bad scope")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    s = wf.get_status()
    assert s.state == WorkflowState.FAILED_FINAL.value
    assert "bad scope" in (s.error or "")


def test_get_status_reflects_state_during_run(monkeypatch):
    statuses_observed: list[WorkflowStatus] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            statuses_observed.append(wf.get_status())
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    wf = ProjectProcessingWorkflow()
    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    assert statuses_observed
    mid_status = statuses_observed[0]
    assert mid_status.state == WorkflowState.RUNNING.value
    assert mid_status.current_operation == f"{OPERATION_COMPILE}:doc-1"

    final = wf.get_status()
    assert final.state == WorkflowState.COMPLETED.value
    assert final.current_operation is None
    assert OPERATION_FINALIZE in final.completed_operations


# ============================================================
# Continue-as-new
# ============================================================


class _ContinueAsNewSentinel(BaseException):
    """Stand-in for `temporalio.workflow.ContinueAsNewError` in tests.

    The real `ContinueAsNewError` refuses direct construction outside the
    workflow runtime, so tests substitute this BaseException-derived
    sentinel — same semantics for our purposes (bypasses the workflow's
    `except Exception` clauses, propagates to the test).
    """


def _multi_doc_handler(documents: list[str]):
    """Activity handler that processes a list of documents successfully."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return list(documents)
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=[f"art-{payload.document_id}"],
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    return handler


def test_continue_as_new_triggers_after_threshold_documents(monkeypatch):
    captured: dict = {}

    def fake_continue_as_new(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise _ContinueAsNewSentinel()

    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_multi_doc_handler(["doc-1", "doc-2", "doc-3", "doc-4"]),
    )
    monkeypatch.setattr(workflow, "continue_as_new", fake_continue_as_new)

    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        continue_as_new_after_documents=2,
    )

    with pytest.raises(_ContinueAsNewSentinel):
        asyncio.run(wf.run(request))

    new_request = captured["args"][0]
    assert isinstance(new_request, ProjectProcessingRequest)
    assert new_request.documents_completed == 2
    assert "art-doc-1" in new_request.produced_artifact_ids
    assert "art-doc-2" in new_request.produced_artifact_ids
    # Compile operations are recorded.
    assert any("compile:doc-1" in op for op in new_request.completed_operations)
    assert any("compile:doc-2" in op for op in new_request.completed_operations)


def test_continue_as_new_carries_compact_state_only(monkeypatch):
    """Verify the continuation payload contains IDs/counters/flags — not bytes."""
    captured: dict = {}

    def fake_continue_as_new(*args, **kwargs):
        captured["args"] = args
        raise _ContinueAsNewSentinel()

    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_multi_doc_handler(["d-1", "d-2"]),
    )
    monkeypatch.setattr(workflow, "continue_as_new", fake_continue_as_new)

    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        continue_as_new_after_documents=2,
    )
    with pytest.raises(_ContinueAsNewSentinel):
        asyncio.run(wf.run(request))

    new_request = captured["args"][0]
    # All carried state is primitives / IDs / counters — verify by walking
    # the dataclass fields and asserting nothing is bytes-ish or large.
    from dataclasses import fields

    for f in fields(new_request):
        value = getattr(new_request, f.name)
        assert not isinstance(value, bytes), (
            f"{f.name} carries bytes — violates 'no large payloads' rule"
        )
    # No payload over a few KB.
    import json
    serialized = json.dumps(
        {
            "completed_operations": list(new_request.completed_operations),
            "produced_artifact_ids": list(new_request.produced_artifact_ids),
            "documents_completed": new_request.documents_completed,
        }
    )
    assert len(serialized) < 4096, "continuation payload should stay compact"


def test_continuation_resumes_from_checkpoint(monkeypatch):
    """A second run started with continuation state skips already-processed docs."""
    compile_calls: list[str] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["d-1", "d-2", "d-3", "d-4"]
        if name.endswith("compile"):
            compile_calls.append(payload.document_id)
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=[f"art-{payload.document_id}"],
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)

    # Restart from a checkpoint after 2 docs.
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        completed_operations=("validate", "list_documents", "compile:d-1", "compile:d-2"),
        produced_artifact_ids=("art-d-1", "art-d-2"),
        documents_completed=2,
        workflow_run_id="wf-run-id-1",
    )
    result = asyncio.run(wf.run(request))

    assert result.state == WorkflowState.COMPLETED.value
    # Only docs 3 and 4 should have been compiled this run.
    assert compile_calls == ["d-3", "d-4"]
    # All four artifacts present (2 carried + 2 new).
    assert set(result.artifact_ids) == {"art-d-1", "art-d-2", "art-d-3", "art-d-4"}


def test_continuation_skips_validation(monkeypatch):
    """Validation runs only on the original (non-continuation) start."""
    validate_calls = 0

    def handler(method, payload, kwargs):
        nonlocal validate_calls
        name = _activity_name(method)
        if name.endswith("validate_context"):
            validate_calls += 1
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["d-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)

    wf = ProjectProcessingWorkflow()
    asyncio.run(
        wf.run(
            ProjectProcessingRequest(
                scope=_scope(),
                compiler_kind="c",
                # Continuation: validate must be skipped.
                completed_operations=("validate", "list_documents"),
                documents_completed=0,
                produced_artifact_ids=(),
                workflow_run_id="wf-run-1",
            )
        )
    )
    assert validate_calls == 0


def test_status_after_continuation_reflects_carried_state():
    wf = ProjectProcessingWorkflow()
    # Simulate what restoration looks like by setting fields the way run()
    # would on continuation start.
    wf._completed_operations = ["validate", "list_documents", "compile:d-1"]
    wf._produced_artifact_ids = ["art-d-1"]
    wf._documents_completed = 1
    wf._documents_total = 4
    status = wf.get_status()
    assert status.completed_operations == [
        "validate",
        "list_documents",
        "compile:d-1",
    ]
    assert status.produced_artifact_ids == ["art-d-1"]
    assert status.documents_completed == 1


def test_no_continue_as_new_when_disabled(monkeypatch):
    """With the default threshold (0), continuation never fires."""
    continue_calls: list = []

    def fake_continue_as_new(*args, **kwargs):
        continue_calls.append(args)
        raise _ContinueAsNewSentinel()

    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_multi_doc_handler(["d-1", "d-2", "d-3", "d-4"]),
    )
    monkeypatch.setattr(workflow, "continue_as_new", fake_continue_as_new)

    wf = ProjectProcessingWorkflow()
    result = asyncio.run(
        wf.run(
            ProjectProcessingRequest(
                scope=_scope(),
                compiler_kind="c",
                # continue_as_new_after_documents defaults to 0
            )
        )
    )
    assert continue_calls == []
    assert result.state == WorkflowState.COMPLETED.value
    assert result.documents_completed == 4


def test_continue_as_new_does_not_trigger_on_partial_batch(monkeypatch):
    """3 docs with batch=2 → triggers after doc 2 only, not after doc 3."""
    captured: dict = {}

    def fake_continue_as_new(*args, **kwargs):
        captured["called"] = captured.get("called", 0) + 1
        captured["last_completed"] = args[0].documents_completed
        raise _ContinueAsNewSentinel()

    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_multi_doc_handler(["d-1", "d-2", "d-3"]),
    )
    monkeypatch.setattr(workflow, "continue_as_new", fake_continue_as_new)

    wf = ProjectProcessingWorkflow()
    with pytest.raises(_ContinueAsNewSentinel):
        asyncio.run(
            wf.run(
                ProjectProcessingRequest(
                    scope=_scope(),
                    compiler_kind="c",
                    continue_as_new_after_documents=2,
                )
            )
        )
    assert captured["called"] == 1
    assert captured["last_completed"] == 2  # not 3


def test_continuation_completes_full_pipeline_after_resume(monkeypatch):
    """Multi-step pipeline (compile + index) completes correctly when started
    from a continuation checkpoint."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["d-1", "d-2"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=[f"art-{payload.document_id}"],
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)

    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        # Resume after d-1 already done.
        completed_operations=("validate", "list_documents", "compile:d-1"),
        produced_artifact_ids=("art-d-1",),
        documents_completed=1,
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    assert set(result.artifact_ids) == {"art-d-1", "art-d-2"}


# ---- Completion validation gate -----------------------------------


def test_completion_validation_blocks_succeeded_when_no_artifacts(monkeypatch):
    """Compile reported success but produced ZERO artifacts (a real
    failure mode when the parser silently no-ops on a corrupt PDF).
    The completion gate must fail-fast rather than mark SUCCEEDED."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-empty"]
        if name.endswith("compile"):
            # The pathological success — succeeded with no artifacts.
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="mock.compiler",
    )
    # The completion-validation gate raises BusinessRejection at
    # workflow exit, which the workflow re-raises as a typed
    # ApplicationError — contract: NEVER mark SUCCEEDED on a degenerate
    # run.
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert "completion validation" in str(excinfo.value).lower()


def test_completion_validation_passes_when_artifacts_present(monkeypatch):
    """Sanity-check counterpart: a real artifact gets through the
    gate without changing the existing happy-path semantics."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-real"],
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="mock.compiler",
    )
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.COMPLETED.value
    assert "art-real" in result.artifact_ids


def test_completion_validation_fails_when_graph_step_completed_without_artifact(monkeypatch):
    """Per-stage required-output rule: a `graph` step recorded as
    COMPLETED without a `graph_json` artifact is a contract
    violation, not a SUCCEEDED state. This is the regression test
    for the audit-listed bug class where the workflow could mark a
    graph-enabled run as SUCCEEDED while the canonical graph output
    was missing — operators saw 'completed' but the Knowledge Graph
    tab was empty."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            # `compile_metrics` populated so the new graph gate
            # doesn't skip on LOW quality (this test wants graph
            # to run + then fail completion validation).
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-c1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3,
                    "extracted_text_chars": 800,
                },
            )
        if name.endswith("build_graph"):
            # SUCCEEDED but produces NO graph_json artifact —
            # the bug class this rule catches.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-g1"],
                kinds=("graph_metadata",),  # not the canonical kind
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        return None  # other reporter activities

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        graph_builder_kind="g",
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    msg = str(excinfo.value).lower()
    assert "graph" in msg
    assert "graph_json" in msg


def test_failed_run_persists_error_report_artifact(monkeypatch):
    """Failure path must persist an `error_report` artifact via the
    `j1.processing.persist_error_report` activity BEFORE finalize +
    terminal-event emission, so the FE artifact-listing surface
    carries the failure detail under the failed run alongside any
    partial artifacts produced by earlier stages."""
    seen_persist_inputs: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            # Compile succeeds; downstream graph fails. Populate
            # `compile_metrics` so the new graph gate does NOT skip
            # on LOW quality — this test wants the graph stage to
            # actually run + fail.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-c1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3,
                    "extracted_text_chars": 800,
                },
            )
        if name.endswith("build_graph"):
            return ArtifactActivityResult(
                status="failed", error="graph adapter unavailable",
            )
        if name.endswith("persist_error_report"):
            seen_persist_inputs.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        if name.endswith("validate_stage"):
            # Stage-validation gate: pass through. This test exercises
            # the FAIL path on graph (build_graph activity returns
            # status="failed" upstream); compile's validation gate
            # must NOT block. Return a passed result so the workflow
            # records compile COMPLETED + reaches the build_graph
            # failure that this test is asserting on.
            from j1.orchestration.activities.payloads import (
                StageValidationActivityResult,
            )
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="passed",
                passed=True,
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        graph_builder_kind="g",
        # `correlation_id` is what the workflow uses as the run_id
        # for the error_report artifact. The helper early-returns
        # when it's missing — set it so the activity actually fires.
        correlation_id="run-failed-1",
    )
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    assert len(seen_persist_inputs) == 1, (
        "persist_error_report must be invoked exactly once on FAILED_FINAL"
    )
    payload = seen_persist_inputs[0]
    assert payload.failure_message
    assert "graph" in payload.failure_message.lower()
    assert payload.run_id == "run-failed-1"
    # The step_results snapshot must include the failed graph step.
    step_names = [r.get("step") for r in payload.step_results]
    assert "graph" in step_names


def test_completed_run_persists_validation_report_and_final_summary(monkeypatch):
    """A successful run must persist BOTH `validation_report` and
    `final_summary` artifacts at the COMPLETED transition. They land
    via the standard artifact-listing surface so the FE / operators
    have a single canonical run-outcome artifact to read.

    The validation report carries the rules that ran + an empty
    error list (validation passed). The final summary carries the
    final_status + executed-step tally + artifact-kind counts."""
    seen_validation: list = []
    seen_final_summary: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c-1"],
                kinds=("chunk",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_validation_report"):
            seen_validation.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["vr-1"],
                kinds=("validation_report",),
            )
        if name.endswith("persist_final_summary"):
            seen_final_summary.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["fs-1"],
                kinds=("final_summary",),
            )
        if name.endswith("validate_stage"):
            # Stage-validation gate: pass through. The workflow's
            # aggregator rule requires every COMPLETED durable stage
            # to have a recorded validation; without this branch
            # `_validate_completion` would block the SUCCEEDED
            # transition with "no stage_validation_report" errors.
            from j1.orchestration.activities.payloads import (
                StageValidationActivityResult,
            )
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="passed",
                passed=True,
            )
        return None  # other reporter activities

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        # `correlation_id` is the run_id; helper early-returns
        # without it.
        correlation_id="run-success-1",
    )
    result = asyncio.run(wf.run(request))

    assert result.state == WorkflowState.COMPLETED.value

    # Validation report fired exactly once with passed=True.
    assert len(seen_validation) == 1
    vr = seen_validation[0]
    assert vr.passed is True
    assert vr.errors == []
    assert "at_least_one_artifact_produced" in vr.rules_evaluated

    # Final summary fired exactly once with the success status +
    # the executed-step tally including the compile step.
    assert len(seen_final_summary) == 1
    fs = seen_final_summary[0]
    assert fs.final_status in {"succeeded", "succeeded_with_warnings"}
    step_names = [s.get("step") for s in fs.executed_steps]
    assert "compile" in step_names
    # The chunk-kind artifact compile produced shows up in the
    # aggregate kind tally.
    assert fs.artifact_kind_counts.get("chunk", 0) >= 1


def test_failed_run_persists_final_summary_with_failed_status(monkeypatch):
    """A failed run must persist a `final_summary` artifact too —
    the FE / operators need ONE canonical run-outcome artifact for
    both success and failure paths. Final status is `failed`,
    failure_code + failure_message carry the cause."""
    seen_final_summary: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="failed", error="compile blew up",
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary"):
            seen_final_summary.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["fs-1"],
                kinds=("final_summary",),
            )
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        correlation_id="run-failed-2",
    )
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    assert len(seen_final_summary) == 1
    fs = seen_final_summary[0]
    assert fs.final_status == "failed"
    assert fs.failure_code  # code present
    assert "compile" in (fs.failure_message or "").lower()
