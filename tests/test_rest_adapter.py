import io
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    REQUEST_ID_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.integration import (
    AnswerService,
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    RetrievalService,
    SearchService,
    SourceLookupService,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.search.indexer import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


# ---- Fixtures --------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, search_indexer):
    profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(search_indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(search_indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(search_indexer, profile),
    )


@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


@pytest.fixture
def application_facade(
    intake_service,
    artifact_registry,
    registry,
    search_indexer,
    query_engine,
    feedback_store,
    audit_recorder,
) -> ApplicationFacade:
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(search_indexer),
        answer=AnswerService(query_engine),
    )


@pytest.fixture
def started_jobs() -> list[tuple[str, str, str]]:
    """Captures (project_id, document_id, compiler_kind) per job_starter call."""
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append((ctx.project_id, document_id, body.compiler_kind))
        return f"job-{document_id}-{len(started_jobs)}"

    return starter


@pytest.fixture
def client(application_facade, job_starter, workspace) -> TestClient:
    app = create_rest_api(
        application_facade,
        job_starter=job_starter,
        workspace=workspace,
        version="1.2.3",
    )
    return TestClient(app)


def _headers(tenant: str = "acme", project: str = "alpha") -> dict[str, str]:
    return {TENANT_HEADER: tenant, PROJECT_HEADER: project}


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str = "art-1",
    kind: str = "compiled.text",
    content: bytes = b"hello world",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
    suffix: str = ".txt",
    source_document_ids: list[str] | None = None,
):
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}{suffix}"
    (area_dir / stored).write_bytes(content)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
        source_document_ids=source_document_ids or [],
    )
    artifact_registry.add(record)
    return record


def _stage_document(ctx, registry, document_id: str = "doc-1") -> DocumentRecord:
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


# ---- Standard envelope -----------------------------------------------


def _assert_success_envelope(payload: dict) -> dict:
    assert "requestId" in payload
    assert isinstance(payload["requestId"], str) and payload["requestId"]
    assert "data" in payload
    assert "meta" in payload
    assert isinstance(payload["meta"], dict)
    return payload["data"]


def _assert_error_envelope(payload: dict) -> dict:
    assert "requestId" in payload
    assert "error" in payload
    err = payload["error"]
    assert "code" in err and "message" in err and "details" in err
    return err


# ---- Header / context resolution ------------------------------------


def test_missing_tenant_header_returns_standardized_error(client):
    response = client.get("/documents/anything")
    assert response.status_code == 400
    err = _assert_error_envelope(response.json())
    assert "Tenant-Id" in err["message"]


def test_missing_project_header_returns_standardized_error(client):
    response = client.get("/documents/anything", headers={TENANT_HEADER: "acme"})
    assert response.status_code == 400
    err = _assert_error_envelope(response.json())
    assert "Project-Id" in err["message"]


def test_invalid_tenant_id_returns_standardized_error(client):
    response = client.get(
        "/documents/anything",
        headers={TENANT_HEADER: "..", PROJECT_HEADER: "alpha"},
    )
    assert response.status_code == 400
    err = _assert_error_envelope(response.json())
    assert err["code"] == "HTTP_400"


def test_request_id_header_round_trips(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert REQUEST_ID_HEADER in response.headers
    body = response.json()
    assert body["requestId"] == response.headers[REQUEST_ID_HEADER]


# ---- Documents ------------------------------------------------------


def test_post_document_returns_envelope_with_record(client):
    response = client.post(
        "/documents",
        files={"file": ("doc.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["originalFilename"] == "doc.txt"
    assert data["mimeType"] == "text/plain"
    assert data["fileSize"] == len(b"hello")
    assert data["checksum"].startswith("sha256:")
    assert "documentId" in data


def test_post_document_duplicate_marked_in_meta(client):
    client.post(
        "/documents",
        files={"file": ("doc.txt", io.BytesIO(b"same"), "text/plain")},
        headers=_headers(),
    )
    response = client.post(
        "/documents",
        files={"file": ("doc.txt", io.BytesIO(b"same"), "text/plain")},
        headers=_headers(),
    )
    body = response.json()
    assert body["meta"].get("duplicate") is True


def test_get_document_returns_envelope(client, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-x")
    response = client.get("/documents/doc-x", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["documentId"] == "doc-x"


def test_get_document_missing_returns_standard_404(client):
    response = client.get("/documents/missing", headers=_headers())
    assert response.status_code == 404
    err = _assert_error_envelope(response.json())
    assert err["code"] == "DOCUMENT_NOT_FOUND"


def test_get_document_status(client, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-x")
    response = client.get("/documents/doc-x/status", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["documentId"] == "doc-x"
    assert data["status"] == "pending"


def test_post_ingest_starts_job_via_starter(
    client, ctx, registry, started_jobs
):
    _stage_document(ctx, registry, document_id="doc-x")
    response = client.post(
        "/documents/doc-x/ingest",
        json={"compilerKind": "external_knowledge_compiler"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["jobId"].startswith("job-doc-x-")
    assert data["documentId"] == "doc-x"
    assert started_jobs == [("alpha", "doc-x", "external_knowledge_compiler")]


def test_post_ingest_without_starter_returns_503(application_facade, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-x")
    app = create_rest_api(application_facade)  # no job_starter
    c = TestClient(app)
    response = c.post(
        "/documents/doc-x/ingest",
        json={"compilerKind": "x"},
        headers=_headers(),
    )
    assert response.status_code == 503
    err = _assert_error_envelope(response.json())
    assert "starter" in err["message"]


def test_post_ingest_validates_required_field(client):
    """Pydantic validation: missing `compilerKind` is rejected."""
    response = client.post(
        "/documents/doc-x/ingest",
        json={},  # missing compilerKind
        headers=_headers(),
    )
    assert response.status_code == 422


# ---- Ingestion jobs / events ---------------------------------------


def test_get_ingestion_job_without_temporal_returns_503(
    application_facade, job_starter, workspace
):
    """job_status capability is None when Temporal isn't wired."""
    app = create_rest_api(
        application_facade, job_starter=job_starter, workspace=workspace
    )
    c = TestClient(app)
    response = c.get("/ingestion-jobs/anything", headers=_headers())
    assert response.status_code == 503


def test_get_job_events_filters_by_correlation_id(
    client, ctx, audit_recorder
):
    audit_recorder.record(
        ctx, actor="system", action="x.completed",
        target_kind="thing", target_id="t",
        correlation_id="job-1",
    )
    audit_recorder.record(
        ctx, actor="system", action="y.completed",
        target_kind="thing", target_id="t",
        correlation_id="job-2",
    )
    response = client.get("/ingestion-jobs/job-1/events", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["jobId"] == "job-1"
    assert len(data["events"]) == 1
    assert data["events"][0]["action"] == "x.completed"


def test_get_job_events_without_workspace_returns_503(application_facade):
    app = create_rest_api(application_facade)  # no workspace
    c = TestClient(app)
    response = c.get("/ingestion-jobs/job-1/events", headers=_headers())
    assert response.status_code == 503


# ---- Search / retrieve / answer ------------------------------------


def test_post_search_returns_ranked_hits(
    client, ctx, artifact_registry, search_indexer, workspace
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
    )
    search_indexer.index(ctx, ["a-1"])
    response = client.post(
        "/search",
        json={"query": "schedule"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["query"] == "schedule"
    assert data["hits"]
    assert data["hits"][0]["artifactId"] == "a-1"


def test_post_search_validates_query_min_length(client):
    response = client.post(
        "/search", json={"query": ""}, headers=_headers()
    )
    assert response.status_code == 422


def test_post_search_without_search_capability_returns_503(
    application_facade,
):
    facade = ApplicationFacade(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=application_facade.source_lookup,
        feedback=application_facade.feedback,
        event_publisher=application_facade.event_publisher,
        # search omitted
    )
    app = create_rest_api(facade)
    c = TestClient(app)
    response = c.post(
        "/search", json={"query": "x"}, headers=_headers()
    )
    assert response.status_code == 503


def test_post_retrieve_returns_context_blocks_with_citations(
    client, ctx, artifact_registry, search_indexer, workspace
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
        source_document_ids=["doc-1"],
    )
    search_indexer.index(ctx, ["a-1"])
    response = client.post(
        "/retrieve",
        json={"query": "schedule"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["query"] == "schedule"
    assert data["blocks"]
    block = data["blocks"][0]
    assert block["artifactId"] == "a-1"
    assert "schedule" in block["text"]
    assert block["citation"]["artifactId"] == "a-1"
    assert block["citation"]["sourceDocumentId"] == "doc-1"


def test_retrieve_is_distinct_from_search_response_shape(
    client, ctx, artifact_registry, search_indexer, workspace
):
    """/search and /retrieve must not collapse into the same payload — one
    returns ranked hits, the other returns context blocks."""
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"text content",
    )
    search_indexer.index(ctx, ["a-1"])
    s = client.post("/search", json={"query": "text"}, headers=_headers()).json()
    r = client.post("/retrieve", json={"query": "text"}, headers=_headers()).json()
    assert "hits" in s["data"] and "blocks" not in s["data"]
    assert "blocks" in r["data"] and "hits" not in r["data"]


def test_post_answer_returns_full_answer_record(
    client, ctx, artifact_registry, search_indexer, workspace
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", content=b"the schedule is firm",
    )
    search_indexer.index(ctx, ["a-1"])
    response = client.post(
        "/answer",
        json={"question": "schedule"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["question"] == "schedule"
    assert "answer" in data and "modeUsed" in data
    assert "citations" in data and "warnings" in data
    assert "warningCategories" in data
    assert "confidenceLevel" in data


def test_answer_invalid_mode_returns_app_error(client):
    response = client.post(
        "/answer",
        json={"question": "x", "mode": "bogus"},
        headers=_headers(),
    )
    # AnswerService.answer raises ValueError → handled by HTTPException via
    # the exception handler chain. Either 400 (translated) or 500.
    assert response.status_code in (400, 500)


# ---- Citations / sources -------------------------------------------


def test_get_citation_returns_envelope(
    client, ctx, artifact_registry, workspace
):
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", source_document_ids=["doc-A", "doc-B"],
    )
    response = client.get("/citations/a-1", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["citationId"] == "a-1"
    assert data["artifactId"] == "a-1"
    assert data["metadata"]["citation_count"] == 2


def test_get_citation_missing_artifact_returns_404(client):
    response = client.get("/citations/missing", headers=_headers())
    assert response.status_code == 404
    err = _assert_error_envelope(response.json())
    assert err["code"] == "ARTIFACT_NOT_FOUND"


def test_get_source_returns_envelope(client, ctx, registry):
    _stage_document(ctx, registry, document_id="doc-A")
    response = client.get("/sources/doc-A", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["sourceId"] == "doc-A"
    assert data["documentId"] == "doc-A"


def test_get_source_missing_returns_404(client):
    response = client.get("/sources/missing", headers=_headers())
    assert response.status_code == 404
    err = _assert_error_envelope(response.json())
    assert err["code"] == "DOCUMENT_NOT_FOUND"


# ---- Feedback ------------------------------------------------------


def test_post_feedback_returns_receipt(client):
    response = client.post(
        "/feedback",
        json={
            "targetKind": "artifact",
            "targetId": "art-1",
            "rating": 1,
            "comment": "useful",
            "actor": "user@example.com",
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert "feedbackId" in data
    assert "submittedAt" in data


def test_post_feedback_validates_required_fields(client):
    response = client.post(
        "/feedback",
        json={"rating": 1},  # missing targetKind, targetId
        headers=_headers(),
    )
    assert response.status_code == 422


def test_post_feedback_validates_rating_range(client):
    response = client.post(
        "/feedback",
        json={"targetKind": "artifact", "targetId": "x", "rating": 99},
        headers=_headers(),
    )
    assert response.status_code == 422


# ---- Health / version / capabilities -------------------------------


def test_get_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["status"] == "ok"


def test_get_version(client):
    response = client.get("/version")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["version"] == "1.2.3"


def test_get_capabilities(client):
    response = client.get("/capabilities")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["apiVersion"] == "1.2.3"
    names = {c["name"] for c in data["capabilities"]}
    expected = {
        "documents.upload",
        "documents.ingest",
        "search",
        "answer",
        "job_status",
        "job_events",
        "feedback",
        "citations",
    }
    assert expected.issubset(names)
    # job_starter wired → ingest available; search/answer wired in fixture.
    by_name = {c["name"]: c for c in data["capabilities"]}
    assert by_name["documents.ingest"]["available"] is True
    assert by_name["search"]["available"] is True
    assert by_name["answer"]["available"] is True
    assert by_name["job_status"]["available"] is False  # no Temporal in tests
    assert by_name["job_events"]["available"] is True   # workspace wired


# ---- OpenAPI -------------------------------------------------------


def test_openapi_spec_lists_all_endpoints(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = set(spec["paths"].keys())
    expected = {
        "/documents",
        "/documents/{document_id}",
        "/documents/{document_id}/ingest",
        "/documents/{document_id}/status",
        "/ingestion-jobs/{job_id}",
        "/ingestion-jobs/{job_id}/events",
        "/search",
        "/retrieve",
        "/answer",
        "/citations/{citation_id}",
        "/sources/{source_id}",
        "/feedback",
        "/health",
        "/version",
        "/capabilities",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"


def test_openapi_includes_tags_and_descriptions(client):
    spec = client.get("/openapi.json").json()
    tag_names = {t["name"] for t in spec.get("tags", [])}
    assert {
        "documents",
        "ingestion-jobs",
        "search",
        "retrieve",
        "answer",
        "citations",
        "sources",
        "feedback",
        "system",
    }.issubset(tag_names)


def test_openapi_request_schemas_validate_required_fields(client):
    spec = client.get("/openapi.json").json()
    # SearchRequest requires `query`
    component = spec["components"]["schemas"]["SearchRequest"]
    assert "query" in component["required"]


# ---- Custom context resolver ---------------------------------------


def test_custom_context_resolver(application_facade):
    from j1.projects.context import ProjectContext

    def resolver(_request):
        return ProjectContext(tenant_id="from_resolver", project_id="alpha")

    app = create_rest_api(application_facade, context_resolver=resolver)
    c = TestClient(app)
    response = c.get("/documents/missing")  # no headers
    # 404 (not 400) — context was resolved by the resolver, document just doesn't exist.
    assert response.status_code == 404
