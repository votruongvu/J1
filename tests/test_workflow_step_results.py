"""Phase A.5 regression tests: workflow records `StepResult` per stage.

The contract: every enabled stage produces a `StepResult` with
status=COMPLETED or FAILED; every caller-disabled stage produces a
`StepResult` with status=SKIPPED and `source=CALLER` plus a reason.

Operators inspect `WorkflowStatus.step_results` (via the `get_status`
query) and `ProjectProcessingResult.step_results` (in the workflow's
return value) to answer "what ran / was skipped / failed" without
re-reading workflow history.
"""

import asyncio
from collections.abc import Callable

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProcessingActivityResult,
    ProjectScope,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    WorkflowState,
)
from j1.processing.status import FinalStatus, StepSource, StepStatus


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(monkeypatch, *, exec_handler):
    async def _exec(method, payload=None, **kwargs):
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)


def _full_pipeline_handler(*, compile_status="succeeded", index_status="succeeded"):
    """Build a handler that returns success for every stage by default."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status=compile_status,
                artifact_ids=["art-1"] if compile_status == "succeeded" else [],
                error=None if compile_status == "succeeded" else "compile boom",
            )
        if name.endswith("index"):
            return ProcessingActivityResult(
                status=index_status,
                error=None if index_status == "succeeded" else "index boom",
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)
    return handler


# ---- Step records on the success path -----------------------------


def test_step_results_record_completed_compile_and_index(monkeypatch):
    _patch_workflow_runtime(monkeypatch, exec_handler=_full_pipeline_handler())
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED
    steps = {s.step: s for s in result.step_results}
    assert "compile" in steps and steps["compile"].status == StepStatus.COMPLETED
    assert "index" in steps and steps["index"].status == StepStatus.COMPLETED
    # Required-flag carries through to the result so callers can
    # later distinguish required failures from optional ones.
    assert steps["compile"].required is True
    assert steps["index"].required is True


def test_step_results_include_skipped_entries_for_caller_disabled_stages(monkeypatch):
    """When the caller doesn't supply enricher_kind / graph_builder_kind /
    indexer_kind, the workflow records SKIPPED entries with
    source=CALLER and a reason. Operators reading the audit shouldn't
    have to infer the skip from absence."""
    _patch_workflow_runtime(monkeypatch, exec_handler=_full_pipeline_handler())
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    skipped = {
        s.step: s for s in wf.get_status().step_results
        if s.status == StepStatus.SKIPPED
    }
    assert "enrich" in skipped
    assert "graph" in skipped
    assert "index" in skipped
    for step in skipped.values():
        assert step.source == StepSource.CALLER
        assert step.required is False
        assert step.reason and "request" in step.reason.lower()


# ---- Step records on the failure path -----------------------------


def test_step_results_capture_compile_failure_with_error_metadata(monkeypatch):
    """A failed required step must have a StepResult with FAILED
    status, the error message, and required=True. The workflow then
    raises — but the recorded state is still readable via
    `wf.get_status()` (used by Temporal queries / status endpoints)."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(compile_status="failed"),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    status = wf.get_status()
    failed = [s for s in status.step_results if s.status == StepStatus.FAILED]
    assert failed, "expected a FAILED step result for compile"
    compile_step = next(s for s in failed if s.step == "compile")
    assert compile_step.required is True
    assert compile_step.error is not None
    assert "compile boom" in compile_step.error.message


def test_get_status_exposes_final_status_only_after_terminal_exit(monkeypatch):
    """`final_status` is None while the workflow is in flight, and is
    populated only on terminal state transitions. Tests / status
    endpoints can check `final_status is None` to detect "in
    progress" without reading the lower-level `state` field."""
    _patch_workflow_runtime(monkeypatch, exec_handler=_full_pipeline_handler())
    wf = ProjectProcessingWorkflow()
    # Before run, state is RUNNING; final_status MUST be None.
    pre = wf.get_status()
    assert pre.state == WorkflowState.RUNNING.value
    assert pre.final_status is None

    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )
    asyncio.run(wf.run(request))

    post = wf.get_status()
    assert post.final_status == FinalStatus.COMPLETED


# ---- Step records survive the workflow's raise --------------------


def test_step_results_in_status_after_failed_workflow_raise(monkeypatch):
    """When the workflow raises, callers reading the in-process result
    don't get one — but the `get_status` query still works on the
    workflow instance. Step results recorded before the raise
    must be visible."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(compile_status="failed"),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    status = wf.get_status()
    assert status.final_status == FinalStatus.FAILED
    # At least the failed compile step must be present.
    assert any(
        s.step == "compile" and s.status == StepStatus.FAILED
        for s in status.step_results
    )
