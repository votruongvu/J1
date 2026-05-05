"""Workflow-failure-propagation regression tests.

Pin the contract: ingestion failures must surface as Temporal
workflow failures (not as Completed-with-an-error-string-inside).

Each test exercises a specific failure path through
`ProjectProcessingWorkflow` / `DocumentProcessingWorkflow` and asserts
that:

  * The workflow raises `ApplicationError` (Temporal UI shows Failed).
  * The error carries a stable `type` so dashboards / search queries
    can filter ingestion failures without parsing message strings.
  * The error message includes the originating step's reason so
    operators can diagnose without re-reading the activity history.
  * The recorded `WorkflowState` (visible via the `get_status` query)
    still reflects the terminal-business vs. unexpected-exception
    distinction.

These tests prevent the false-COMPLETED bug from regressing — a
returned result must NEVER be the only signal of failure.
"""

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
    ValidateContextResult,
)
from j1.orchestration.workflows.document_processing import (
    DocumentProcessingRequest,
    DocumentProcessingWorkflow,
)
from j1.orchestration.workflows.project_processing import (
    ERROR_TYPE_REQUIRED_STEP_FAILED,
    ERROR_TYPE_UNEXPECTED_ERROR,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    WorkflowState,
)
from j1.processing.status import FinalStatus


# ---- Test harness (mirrors the pattern in test_project_processing_workflow.py)


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(
    monkeypatch,
    *,
    exec_handler: Callable[[object, object, dict], object] | None = None,
    wait_handler: Callable[[Callable[[], bool], dict], "asyncio.Future"] | None = None,
):
    async def _exec(method, payload=None, **kwargs):
        if exec_handler is None:
            return None
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        if wait_handler is not None:
            await wait_handler(predicate, kwargs)
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)


# ---- Project workflow: required step failures must raise --------------


def test_project_compile_failure_raises_application_error(monkeypatch):
    """Required step (compile) reports FAILED → workflow MUST raise
    `ApplicationError(type=J1_INGEST_REQUIRED_STEP_FAILED)`."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="failed", error="vendor exploded mid-parse"
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
    assert excinfo.value.non_retryable is True
    assert "vendor exploded mid-parse" in str(excinfo.value)


def test_project_index_failure_raises_application_error(monkeypatch):
    """`indexer_kind` is set → index is treated as required → its
    failure must raise the workflow, not return Completed."""
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
        if name.endswith("index"):
            return ProcessingActivityResult(
                status="failed", error="vector index unavailable"
            )
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )

    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))

    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "vector index unavailable" in str(excinfo.value)


def test_project_unexpected_exception_wrapped_as_application_error(monkeypatch):
    """Generic exceptions raised mid-workflow get wrapped in a typed
    `ApplicationError(type=J1_INGEST_UNEXPECTED_ERROR, non_retryable=False)`
    — type allows filtering, `non_retryable=False` preserves the
    "transient infrastructure" classification so a parent workflow
    could retry it."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            raise ConnectionError("kafka broker unreachable")
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
    # Original exception type is named in the wrapped message so
    # operators don't have to dig into the cause chain.
    assert "ConnectionError" in str(excinfo.value)
    assert "kafka broker unreachable" in str(excinfo.value)


# ---- Project workflow: success path returns FinalStatus.COMPLETED ------


def test_project_success_returns_final_status_completed(monkeypatch):
    """Sanity check: when nothing fails, the workflow returns a result
    whose `final_status` is COMPLETED. Tests that assert correctness
    should look at `final_status`, not just `state`."""
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
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
    )

    result = asyncio.run(wf.run(request))

    assert result.state == WorkflowState.COMPLETED.value
    assert result.final_status == FinalStatus.COMPLETED


def test_project_cancellation_returns_final_status_cancelled(monkeypatch):
    """Cancelled workflows are not failures — their `final_status`
    is `CANCELLED`, distinct from `FAILED`."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("finalize"):
            return None
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    wf.cancel()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")

    result = asyncio.run(wf.run(request))

    assert result.state == WorkflowState.CANCELLED.value
    assert result.final_status == FinalStatus.CANCELLED


# ---- Document workflow: same contract -----------------------------------


def test_document_workflow_compile_failure_raises_application_error(monkeypatch):
    """`DocumentProcessingWorkflow` has the same failure-propagation
    contract as the project-level workflow — a failed compile must
    raise, not return a result with `status="failed"`."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="failed", error="parser timed out"
            )
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = DocumentProcessingWorkflow()
    request = DocumentProcessingRequest(
        scope=_scope(), document_id="doc-1", compiler_kind="c",
    )

    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))

    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "parser timed out" in str(excinfo.value)


def test_document_workflow_caller_specified_enrich_failure_raises(monkeypatch):
    """If the caller explicitly supplied `enricher_kind`, enrichment
    is treated as required — its failure must surface as a workflow
    failure. (A future planner-driven mode may emit `required=False`
    for planner-enabled enrich so `continue_optional` policy can let
    it fail; today every enabled step is required.)"""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"],
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="failed", error="vision LLM rate-limited",
            )
        raise AssertionError(name)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = DocumentProcessingWorkflow()
    request = DocumentProcessingRequest(
        scope=_scope(),
        document_id="doc-1",
        compiler_kind="c",
        enricher_kind="vision",
    )

    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))

    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    assert "vision LLM rate-limited" in str(excinfo.value)
