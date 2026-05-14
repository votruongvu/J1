"""Run-level resume / re-index / rebuild-index endpoints are gone.

These three endpoints used to start a NEW run that reused prior-run
outputs (compile / chunks / etc.). They were removed when the
contract was tightened: a run is an immutable execution record, and
the only way to re-process a document is to call
``POST /documents/{document_id}/reindex`` which always starts from
the original uploaded file.

This test ensures the routes are still mounted (so old clients get a
clear error rather than a 404 surprise) but return HTTP 410 with a
message that points at the supported replacement.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.ingestion_review import IngestionResultReviewService
from j1.integration.services import (
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    RetrievalService,
    SourceLookupService,
)
from j1.projects.context import ProjectContext
from j1.runs import JsonlIngestionRunStore


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def review_service(run_store, artifact_registry, workspace) -> IngestionResultReviewService:
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(ProjectContext(
        tenant_id="acme", project_id="alpha",
    )) / "feedback.jsonl")


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store, audit_recorder,
):
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def stub_starter():
    async def _start(ctx, document_id, body) -> str:
        return f"wf-{document_id}-{body.correlation_id}"
    return _start


@pytest.fixture
def client(
    application_facade, workspace, run_store, review_service, stub_starter,
) -> TestClient:
    from j1.integration.dto import ProcessingCapabilities
    capabilities = ProcessingCapabilities(
        default_compiler_kind="raganything",
        compiler_kinds=frozenset({"raganything"}),
        enricher_kinds=frozenset({"composite_enricher"}),
        graph_builder_kinds=frozenset({"lightrag_graph"}),
        indexer_kinds=frozenset({"sqlite_search"}),
    )
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        job_starter=stub_starter,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/ingestion-runs/any-run/resume-from-checkpoint",
        "/ingestion-runs/any-run/full-reindex",
        "/ingestion-runs/any-run/rebuild-index",
    ],
)
def test_run_level_reindex_resume_endpoints_return_410(client, path):
    """Old clients hitting the removed surface get a clear 410 with a
    message pointing at the document-level replacement."""
    resp = client.post(path, headers=_HEADERS)
    assert resp.status_code == 410, resp.text
    body = resp.json()
    detail = body["error"]["message"]
    assert "no longer supported" in detail
    assert "POST /documents/{document_id}/reindex" in detail
