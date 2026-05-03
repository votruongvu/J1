import asyncio
import inspect
from collections.abc import Callable

import pytest
from temporalio import workflow

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProcessingActivityResult,
    ProjectScope,
    SpendSummary,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
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
    assert calls[-1].endswith("finalize")


def test_run_completes_full_pipeline(monkeypatch):
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-c1"]
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-e1"]
            )
        if name.endswith("build_graph"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-g1"]
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


def test_validate_failure_marks_failed_final(monkeypatch):
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
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.FAILED_FINAL.value
    assert "bad scope" in (result.error or "")


def test_compile_failure_marks_failed_final(monkeypatch):
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
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.FAILED_FINAL.value
    assert "compiler crashed" in (result.error or "")


def test_unexpected_exception_marks_failed_recoverable(monkeypatch):
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
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.FAILED_RECOVERABLE.value
    assert "transient db blip" in (result.error or "")


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
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.FAILED_FINAL.value
    assert "budget rejected" in (result.error or "")


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
    result = asyncio.run(wf.run(request))
    assert result.state == WorkflowState.FAILED_FINAL.value
    assert "review rejected" in (result.error or "")


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
