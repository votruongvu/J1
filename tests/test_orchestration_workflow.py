import inspect

from j1.orchestration.workflows.document_processing import (
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentProcessingWorkflow,
)


def test_workflow_has_temporal_marker():
    assert hasattr(DocumentProcessingWorkflow, "__temporal_workflow_definition")


def test_workflow_run_is_async():
    assert inspect.iscoroutinefunction(DocumentProcessingWorkflow.run)


def test_request_and_result_are_constructible():
    from j1.orchestration.activities.payloads import ProjectScope

    request = DocumentProcessingRequest(
        scope=ProjectScope(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        compiler_kind="mock.compiler",
        enricher_kind="mock.enricher",
        indexer_kind="mock.index",
        correlation_id="run-1",
    )
    assert request.compiler_kind == "mock.compiler"

    result = DocumentProcessingResult(
        status="succeeded",
        document_id="doc-1",
        artifact_ids=["a-1", "a-2"],
    )
    assert result.status == "succeeded"
    assert result.error is None
