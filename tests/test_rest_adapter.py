import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    REQUEST_ID_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.artifacts.models import ArtifactRecord
from j1.cost.aggregator import CostAggregator
from j1.cost.breakdown import CostBreakdown
from j1.documents.models import DocumentRecord
from j1.integration import (
    ApplicationFacade,
    CitationLookupService,
    CostSummaryService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    JsonlFeedbackStore,
    ProjectAdminService,
    RetrievalService,
    ReviewService,
    SearchService,
    SourceLookupService,
    TemporalJobControlService,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.profiles import DEFAULT_PROFILE_ID, ProfileLoader
from j1.review.models import ReviewItem
from j1.workspace.layout import WorkspaceArea


# Phase 8 test stub for the deleted SqliteSearchIndexer.
class DummySearchIndexer:
    kind = "null_indexer"

    def __init__(self, *_, **__):
        pass

    def index(self, *_, **__):
        from j1.processing.results import ProcessingResult
        from j1.processing.status import ResultStatus
        return ProcessingResult(status=ResultStatus.SUCCEEDED)

    def search(self, *_, **__):
        return []

    def delete_by_run_id(self, *_, **__):
        return 0


# ---- Fixtures --------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def search_indexer(workspace, artifact_registry, registry):
    return DummySearchIndexer(workspace, artifact_registry, registry)
@pytest.fixture
def feedback_store(workspace) -> JsonlFeedbackStore:
    return JsonlFeedbackStore(workspace)


class _MockHandle:
    def __init__(self, mock: "_MockTemporalClient", workflow_id: str) -> None:
        self._mock = mock
        self.id = workflow_id

    async def signal(self, name: str, *args: Any, **kwargs: Any) -> None:
        self._mock.signals.append((self.id, name))


class _MockTemporalClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, Any]] = []
        self.signals: list[tuple[str, str]] = []

    async def start_workflow(
        self, fn: Any, arg: Any, *, id: str, task_queue: str, **kwargs: Any
    ) -> _MockHandle:
        self.started.append((id, arg))
        return _MockHandle(self, id)

    def get_workflow_handle(self, workflow_id: str) -> _MockHandle:
        return _MockHandle(self, workflow_id)


@pytest.fixture
def mock_temporal() -> _MockTemporalClient:
    return _MockTemporalClient()


@pytest.fixture
def application_facade(
    intake_service,
    artifact_registry,
    registry,
    search_indexer,
    feedback_store,
    audit_recorder,
    workspace,
    review_queue,
    review_activities,
    cost_sink,
    mock_temporal,
) -> ApplicationFacade:
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(search_indexer),
        project_admin=ProjectAdminService(workspace),
        job_control=TemporalJobControlService(
            client_provider=lambda: mock_temporal,
            task_queue="j1-test",
            workflow_id_factory=lambda ctx: f"wf-{ctx.project_id}",
        ),
        cost_summary=CostSummaryService(CostAggregator(workspace)),
        review=ReviewService(review_queue, review_activities),
    )


@pytest.fixture
def started_jobs() -> list[tuple[str, str, str]]:
    """Captures (project_id, document_id, compiler_kind) per job_starter call."""
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        # Capture the full IngestRequest body in addition to the
        # legacy tuple. Profile-related tests need to inspect
        # `body.selected_profile`; older tests still consume the
        # `started_jobs` tuple unchanged.
        started_jobs.append((ctx.project_id, document_id, body.compiler_kind))
        starter.bodies.append(body)
        return f"job-{document_id}-{len(started_jobs)}"

    starter.bodies = []
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
    """Without `processing_capabilities` AND no `compilerKind` in the
 body, the request is rejected with 400 INVALID_ARGUMENT.

 The schema treats `compilerKind` as optional now; the handler
 enforces presence (or default-resolution) so the error message
 can name the field clearly.
 """
    response = client.post(
        "/documents/doc-x/ingest",
        json={},  # missing compilerKind, no default registered
        headers=_headers(),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert "compilerKind" in body["error"]["message"]


# ---- Execution profile endpoints -----------------------------------


def _stage_document_with_file(
    ctx, registry, workspace, *, document_id: str = "doc-prof",
    content: bytes = b"hello world for profile assessment",
    filename: str = "doc-prof.txt",
) -> DocumentRecord:
    """Stage a registered document AND write its source file under the
    workspace's raw area so the profiler can read it."""
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / filename).write_bytes(content)
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=filename,
        stored_filename=filename,
        mime_type="text/plain",
        file_size=len(content),
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )
    registry.add(record)
    return record


def test_assessment_plan_endpoint_returns_recommendation_and_catalogue(
    client, ctx, registry, workspace,
):
    """`POST /documents/{id}/assessment-plan` runs synchronously,
    returns the recommended profile + the full profile catalogue.
    Pins the response shape the FE picker keys off."""
    _stage_document_with_file(ctx, registry, workspace)
    response = client.post(
        "/documents/doc-prof/assessment-plan",
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["documentId"] == "doc-prof"
    # Plain-text doc → no images, no tables → recommendation is
    # `standard` (the keystone behaviour for text-only inputs).
    assert data["recommendedProfile"] == "standard"
    # The catalogue must include every profile, in declaration order.
    profile_ids = [p["id"] for p in data["availableProfiles"]]
    assert profile_ids == ["minimum_queryable", "standard", "advanced"]
    # Each profile must carry the FE-renderable fields.
    for entry in data["availableProfiles"]:
        assert "label" in entry
        assert "expected_speed" in entry
        assert "expected_llm_usage" in entry
        assert "graph_enabled" in entry
        assert "compile_lightrag_extraction" in entry


def test_assessment_plan_minimum_queryable_is_honest_about_cost(
    client, ctx, registry, workspace,
):
    """The `minimum_queryable` card must disclose that LightRAG's
    built-in extraction is OFF — that's the whole point of the
    profile vs `standard`. Pinned so a future refactor doesn't
    quietly remove the honesty signal."""
    _stage_document_with_file(ctx, registry, workspace)
    response = client.post(
        "/documents/doc-prof/assessment-plan",
        headers=_headers(),
    )
    data = _assert_success_envelope(response.json())
    by_id = {p["id"]: p for p in data["availableProfiles"]}
    assert by_id["minimum_queryable"]["compile_lightrag_extraction"] is False
    assert by_id["standard"]["compile_lightrag_extraction"] is True
    assert by_id["advanced"]["compile_lightrag_extraction"] is True


def test_assessment_plan_missing_document_returns_404(client):
    response = client.post(
        "/documents/does-not-exist/assessment-plan",
        headers=_headers(),
    )
    assert response.status_code == 404


def test_ingestion_run_rejects_unknown_selected_profile(
    client, ctx, registry, workspace, started_jobs,
):
    """An invalid `selectedProfile` value must fail at the boundary
    with 400 — not silently fall through to legacy behaviour."""
    response = client.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        data={"selectedProfile": "premiun_typo"},  # nonsense value
        headers=_headers(),
    )
    assert response.status_code == 400
    err = _assert_error_envelope(response.json())
    assert "selectedProfile" in err["message"]
    # The error message lists the allowed values so the operator
    # doesn't have to grep the codebase.
    assert "minimum_queryable" in err["message"]


# ---- Profile policy enforcement -----------------------------------


def _restricted_client(
    application_facade,
    job_starter,
    workspace,
    *,
    allowed,
    default,
) -> TestClient:
    """Build a TestClient with a deployment-policy that restricts
    which execution profiles are allowed. Mirrors the production
    bootstrap path (env → `load_execution_profile_policy` → pass
    into `create_rest_api`).

    Wires an `ingestion_run_store` so `POST /ingestion-runs` can
    actually create a run record — otherwise every request 503s
    on `_require_run_store()` before the policy check runs.
    """
    from j1.processing.execution_profile import ExecutionProfile
    from j1.processing.execution_profile_policy import (
        ExecutionProfilePolicy,
    )
    from j1.runs.store import JsonlIngestionRunStore

    policy = ExecutionProfilePolicy(
        default_profile=ExecutionProfile(default),
        allowed=frozenset(ExecutionProfile(p) for p in allowed),
    )
    app = create_rest_api(
        application_facade,
        job_starter=job_starter,
        workspace=workspace,
        execution_profile_policy=policy,
        ingestion_run_store=JsonlIngestionRunStore(workspace),
    )
    return TestClient(app)


def test_ingestion_run_rejects_forbidden_profile_with_403(
    application_facade, job_starter, workspace,
):
    """Hard safety cap. Even though `advanced` is a valid profile,
    a deployment that forbids it must REJECT requests for it —
    never silently downgrade. The 403 body lists the allowed
    profiles so the UI can re-render the picker."""
    c = _restricted_client(
        application_facade,
        job_starter,
        workspace,
        allowed=["minimum_queryable", "standard"],
        default="standard",
    )
    response = c.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        data={"compilerKind": "mock", "selectedProfile": "advanced"},
        headers=_headers(),
    )
    assert response.status_code == 403
    err = _assert_error_envelope(response.json())
    assert err["code"] == "PROFILE_NOT_ALLOWED"
    assert "advanced" in err["message"]
    # The FE keys off `allowedProfiles` to re-render the picker
    # without round-tripping to `/assessment-plan` again.
    assert sorted(err["details"]["allowedProfiles"]) == [
        "minimum_queryable",
        "standard",
    ]
    assert err["details"]["requestedProfile"] == "advanced"


def test_ingestion_run_applies_deployment_default_when_unspecified(
    application_facade, job_starter, workspace,
):
    """When the caller omits `selectedProfile` and the deployment
    pins the default to `minimum_queryable` (e.g. on a debug
    stack), the workflow MUST run under that default — not the
    codebase-level `DEFAULT_PROFILE`. Pins that env-driven
    defaults actually flow into the workflow request."""
    c = _restricted_client(
        application_facade,
        job_starter,
        workspace,
        allowed=["minimum_queryable", "standard"],
        default="minimum_queryable",
    )
    response = c.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        # No `selectedProfile` — deployment default should apply.
        data={"compilerKind": "mock"},
        headers=_headers(),
    )
    assert response.status_code == 201
    assert job_starter.bodies, "job_starter must have been invoked"
    started_body = job_starter.bodies[-1]
    assert started_body.selected_profile == "minimum_queryable"


def test_ingestion_run_threads_allowed_profile_through_to_workflow(
    application_facade, job_starter, workspace,
):
    """Sanity check the happy path: explicit + allowed profile
    reaches the workflow request body."""
    c = _restricted_client(
        application_facade,
        job_starter,
        workspace,
        allowed=["minimum_queryable", "standard", "advanced"],
        default="standard",
    )
    response = c.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        data={"compilerKind": "mock", "selectedProfile": "advanced"},
        headers=_headers(),
    )
    assert response.status_code == 201
    assert job_starter.bodies[-1].selected_profile == "advanced"


def test_documents_ingest_rejects_forbidden_profile_with_403(
    application_facade, job_starter, workspace, ctx, registry,
):
    """The JSON-body endpoint must enforce the same policy as the
    multipart `/ingestion-runs` endpoint. Tested separately
    because the body validation runs in a different code path."""
    _stage_document(ctx, registry, document_id="doc-policy")
    c = _restricted_client(
        application_facade,
        job_starter,
        workspace,
        allowed=["minimum_queryable", "standard"],
        default="standard",
    )
    response = c.post(
        "/documents/doc-policy/ingest",
        json={
            "compilerKind": "external_knowledge_compiler",
            "selectedProfile": "advanced",
        },
        headers=_headers(),
    )
    assert response.status_code == 403


def test_documents_ingest_applies_deployment_default_when_unspecified(
    application_facade, job_starter, workspace, ctx, registry,
):
    _stage_document(ctx, registry, document_id="doc-policy-default")
    c = _restricted_client(
        application_facade,
        job_starter,
        workspace,
        allowed=["minimum_queryable", "standard"],
        default="minimum_queryable",
    )
    response = c.post(
        "/documents/doc-policy-default/ingest",
        json={"compilerKind": "external_knowledge_compiler"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert job_starter.bodies[-1].selected_profile == "minimum_queryable"


def test_default_policy_accepts_every_profile(
    application_facade, job_starter, workspace,
):
    """Backward compatibility: when `create_rest_api` is called
    without `execution_profile_policy=`, the adapter builds a
    permissive default — every profile is allowed. Pin so a
    future refactor doesn't tighten this and break existing
    deployments that haven't adopted env-driven policy."""
    from j1.runs.store import JsonlIngestionRunStore

    app = create_rest_api(
        application_facade,
        job_starter=job_starter,
        workspace=workspace,
        # NO execution_profile_policy=, intentionally.
        ingestion_run_store=JsonlIngestionRunStore(workspace),
    )
    c = TestClient(app)
    response = c.post(
        "/ingestion-runs",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        data={"compilerKind": "mock", "selectedProfile": "advanced"},
        headers=_headers(),
    )
    assert response.status_code == 201
    assert job_starter.bodies[-1].selected_profile == "advanced"


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


@pytest.mark.skip(
    reason="Phase 8: tests the deleted SQLite search backend. "
    "PostgreSQL FTS smoke coverage lives in "
    "tests/integration/test_rest_search_live.py.",
)
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


@pytest.mark.skip(
    reason="Phase 8: tests the deleted SQLite-backed retrieve path.",
)
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


# NOTE: tests for POST /answer were removed when the legacy
# HybridQueryEngine + AnswerService stack was deleted. Query
# answers now flow through SmartQueryOrchestrator and the new
# /test-query + /dev/query-trace surfaces; see
# ``test_query_orchestrator.py`` and
# ``test_rest_dev_query_trace.py`` for the replacement coverage.


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


def test_get_health_llm_returns_cached_probe_status(client):
    """`/healthz/llm` reads the process-local probe cache. When the
 cache is empty (no probe has run in this test process), the
 endpoint reports `healthy=true` with empty results — matches
 the conservative 'assume working until proven otherwise'
 contract. The FE banner stays quiet on this response."""
    from j1.llm.probe import cache_probe_results
    cache_probe_results([])  # ensure clean state
    response = client.get("/healthz/llm")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["healthy"] is True
    assert data["results"] == []


def test_get_health_llm_surfaces_failed_role(client):
    """When the cache holds a failed probe result, the endpoint
 reports `healthy=false` and includes per-role detail so the FE
 banner can render the operator-readable error."""
    from j1.llm.probe import ProbeResult, cache_probe_results
    cache_probe_results([
        ProbeResult(
            role="text", ok=False,
            provider="openai_compat", model="m-1",
            error="LLMProviderUnavailable: HTTP 503",
        ),
    ])
    response = client.get("/healthz/llm")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["healthy"] is False
    assert len(data["results"]) == 1
    assert data["results"][0]["role"] == "text"
    assert data["results"][0]["ok"] is False
    assert "503" in data["results"][0]["error"]
    # Reset to keep test isolation when subsequent tests assume a
    # clean cache.
    cache_probe_results([])


def test_post_health_llm_refresh_returns_cached_state_when_no_registry(
    client,
):
    """When `create_rest_api` was called WITHOUT an `llm_registry`
 (the test fixture's case), the refresh endpoint falls back to
 returning the cached snapshot — same shape as GET. This keeps
 the FE 'Retry now' button working in mock / minimal deployments
 instead of 503-ing."""
    from j1.llm.probe import cache_probe_results
    cache_probe_results([])
    response = client.post("/healthz/llm/refresh")
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    # Empty cache → conservative healthy=true ('assume working
    # until proven otherwise'). Same contract as GET.
    assert data["healthy"] is True
    assert data["results"] == []


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
        "job_status",
        "job_events",
        "feedback",
        "citations",
        "projects.create",
        "ingestion-jobs.control",
        "artifacts",
        "cost",
        "reviews",
    }
    assert expected.issubset(names)
    # /answer was deleted with the legacy AnswerService — the
    # "answer" capability is no longer advertised.
    assert "answer" not in names
    by_name = {c["name"]: c for c in data["capabilities"]}
    assert by_name["documents.ingest"]["available"] is True
    assert by_name["search"]["available"] is True
    assert by_name["job_status"]["available"] is False  # no Temporal in tests
    assert by_name["job_events"]["available"] is True   # workspace wired
    assert by_name["projects.create"]["available"] is True
    assert by_name["ingestion-jobs.control"]["available"] is True
    assert by_name["cost"]["available"] is True
    assert by_name["reviews"]["available"] is True


# ---- OpenAPI -------------------------------------------------------


def test_openapi_spec_lists_all_endpoints(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    paths = set(spec["paths"].keys())
    expected = {
        "/projects",
        "/documents",
        "/documents/{document_id}",
        "/documents/{document_id}/ingest",
        "/documents/{document_id}/status",
        "/ingestion-jobs",
        "/ingestion-jobs/{job_id}",
        "/ingestion-jobs/{job_id}/events",
        "/ingestion-jobs/{job_id}/pause",
        "/ingestion-jobs/{job_id}/resume",
        "/ingestion-jobs/{job_id}/cancel",
        "/artifacts",
        "/artifacts/{artifact_id}",
        "/search",
        "/retrieve",
        "/citations/{citation_id}",
        "/sources/{source_id}",
        "/cost",
        "/reviews",
        "/reviews/{review_id}/decision",
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
        "projects",
        "documents",
        "ingestion-jobs",
        "artifacts",
        "search",
        "retrieve",
        "answer",
        "citations",
        "sources",
        "cost",
        "reviews",
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


# ---- Projects -------------------------------------------------------


def test_post_project_provisions_workspace(client, workspace):
    response = client.post(
        "/projects",
        json={"projectId": "beta"},
        headers={TENANT_HEADER: "acme"},
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["projectId"] == "beta"
    assert data["tenantId"] == "acme"
    from j1.projects.context import ProjectContext

    raw_dir = workspace.area(
        ProjectContext(tenant_id="acme", project_id="beta"),
        WorkspaceArea.RAW,
    )
    assert raw_dir.exists()


def test_post_project_requires_tenant_header(client):
    response = client.post("/projects", json={"projectId": "beta"})
    assert response.status_code == 400


def test_post_project_validates_project_id(client):
    response = client.post(
        "/projects", json={"projectId": ""}, headers={TENANT_HEADER: "acme"}
    )
    assert response.status_code == 422


def test_post_project_without_capability_returns_503(application_facade):
    facade = ApplicationFacade(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=application_facade.source_lookup,
        feedback=application_facade.feedback,
        event_publisher=application_facade.event_publisher,
    )
    c = TestClient(create_rest_api(facade))
    response = c.post(
        "/projects", json={"projectId": "x"}, headers={TENANT_HEADER: "acme"}
    )
    assert response.status_code == 503


# ---- Project ingestion job control ---------------------------------


def test_post_ingestion_job_starts_workflow(client, mock_temporal):
    response = client.post(
        "/ingestion-jobs",
        json={"compilerKind": "external_knowledge_compiler"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["jobId"] == "wf-alpha"
    assert data["action"] == "start"
    assert mock_temporal.started and mock_temporal.started[0][0] == "wf-alpha"


def test_post_ingestion_job_validates_required_field(client):
    """Same as `test_post_ingest_validates_required_field` for the
 project-wide endpoint."""
    response = client.post(
        "/ingestion-jobs", json={}, headers=_headers()
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert "compilerKind" in body["error"]["message"]


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_signal_endpoints_dispatch_to_temporal(client, mock_temporal, action):
    response = client.post(
        f"/ingestion-jobs/wf-1/{action}", headers=_headers()
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["jobId"] == "wf-1"
    assert data["action"] == action
    assert ("wf-1", action) in mock_temporal.signals


def test_ingestion_job_control_without_capability_returns_503(application_facade):
    facade = ApplicationFacade(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=application_facade.source_lookup,
        feedback=application_facade.feedback,
        event_publisher=application_facade.event_publisher,
    )
    c = TestClient(create_rest_api(facade))
    response = c.post(
        "/ingestion-jobs",
        json={"compilerKind": "x"},
        headers=_headers(),
    )
    assert response.status_code == 503


# ---- Artifacts ------------------------------------------------------


def test_get_artifacts_lists_records(
    client, ctx, artifact_registry, workspace
):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-2")
    response = client.get("/artifacts", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    ids = {a["artifactId"] for a in data["artifacts"]}
    assert ids == {"a-1", "a-2"}


def test_get_artifacts_filters_by_kind(
    client, ctx, artifact_registry, workspace
):
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="a-1", kind="kind-A"
    )
    _stage_artifact(
        workspace, ctx, artifact_registry, artifact_id="a-2", kind="kind-B"
    )
    response = client.get("/artifacts?kind=kind-A", headers=_headers())
    data = _assert_success_envelope(response.json())
    assert [a["artifactId"] for a in data["artifacts"]] == ["a-1"]


def test_get_artifact_returns_record(client, ctx, artifact_registry, workspace):
    _stage_artifact(workspace, ctx, artifact_registry, artifact_id="a-1")
    response = client.get("/artifacts/a-1", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["artifactId"] == "a-1"
    # Path must be workspace-relative, not absolute.
    assert not data["location"].startswith("/")


def test_get_artifact_missing_returns_404(client):
    response = client.get("/artifacts/missing", headers=_headers())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


# ---- Cost -----------------------------------------------------------


def test_get_cost_summary_aggregates(client, ctx, cost_recorder):
    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=10,
            amount=Decimal("0.10"),
        ),
        correlation_id="run-1",
    )
    response = client.get(
        "/cost?correlationId=run-1", headers=_headers()
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["totalAmount"] == "0.10"
    assert data["byLevel"]["workflow_run"] == "0.10"


def test_get_cost_without_capability_returns_503(application_facade):
    facade = ApplicationFacade(
        ingestion=application_facade.ingestion,
        retrieval=application_facade.retrieval,
        citation_lookup=application_facade.citation_lookup,
        source_lookup=application_facade.source_lookup,
        feedback=application_facade.feedback,
        event_publisher=application_facade.event_publisher,
    )
    c = TestClient(create_rest_api(facade))
    response = c.get("/cost", headers=_headers())
    assert response.status_code == 503


# ---- Reviews --------------------------------------------------------


def test_list_reviews_empty(client):
    response = client.get("/reviews", headers=_headers())
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["items"] == []


def test_list_reviews_returns_pending_items(client, ctx, review_queue):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.get("/reviews", headers=_headers())
    data = _assert_success_envelope(response.json())
    assert len(data["items"]) == 1
    assert data["items"][0]["reviewItemId"] == "r-1"
    assert data["items"][0]["reviewStatus"] == "pending"


def test_apply_review_decision_updates_queue(client, ctx, review_queue):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.post(
        "/reviews/r-1/decision",
        json={"decision": "approved", "actor": "alice"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["reviewItemId"] == "r-1"
    assert data["reviewStatus"] == "approved"
    # Queue is updated in place.
    items = review_queue.list_items(ctx)
    assert items[0].review_status == ReviewStatus.APPROVED


def test_apply_review_decision_unknown_decision_returns_400(
    client, ctx, review_queue
):
    review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.post(
        "/reviews/r-1/decision",
        json={"decision": "wibble", "actor": "alice"},
        headers=_headers(),
    )
    assert response.status_code == 400


def test_apply_review_decision_missing_review_returns_404(client):
    response = client.post(
        "/reviews/missing/decision",
        json={"decision": "approved", "actor": "alice"},
        headers=_headers(),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_ITEM_NOT_FOUND"


# ---- Answer / graph_paths ------------------------------------------


# ---- OpenAPI / Swagger UI surface ---------------------------------


def test_openapi_documents_x_tenant_id_header_per_endpoint(client):
    """`X-Tenant-Id` was previously read from `request.headers` only,
 so Swagger UI had no input field for it and operators couldn't
 test the API. Header-typed dependency parameters now expose it
 on every operation that uses `get_ctx` / `get_tenant`."""
    spec = client.get("/openapi.json").json()
    # Pick an endpoint that uses get_ctx.
    op = spec["paths"]["/documents"]["post"]
    header_params = [
        p for p in op.get("parameters", [])
        if p.get("in") == "header" and p.get("name") == "X-Tenant-Id"
    ]
    assert header_params, (
        f"X-Tenant-Id header not advertised on POST /documents. "
        f"parameters={op.get('parameters')}"
    )
    # And it carries a description so Swagger renders helpful tooltip.
    assert header_params[0].get("description"), (
        "X-Tenant-Id header parameter must have a description"
    )


def test_openapi_documents_x_project_id_header_per_endpoint(client):
    """Same regression as X-Tenant-Id, but for the project header."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/documents"]["post"]
    header_params = [
        p for p in op.get("parameters", [])
        if p.get("in") == "header" and p.get("name") == "X-Project-Id"
    ]
    assert header_params, "X-Project-Id header must be advertised"


def test_openapi_does_not_advertise_security_when_auth_disabled(client):
    """Auth disabled (default fixture) → no Authorize button in
 Swagger. Avoids confusing operators with a credential prompt
 they don't need."""
    spec = client.get("/openapi.json").json()
    components = spec.get("components", {})
    # Either no securitySchemes at all, or empty.
    assert not components.get("securitySchemes"), (
        f"unexpected security schemes in anonymous mode: "
        f"{components.get('securitySchemes')}"
    )
