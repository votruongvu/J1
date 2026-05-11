"""End-to-end tests for POST /ingestion-runs/{run_id}/test-query.

Verifies the REST surface plumbs the validation service correctly:
 * envelope shape + new validation-specific fields
 * tenant/project header plumbing
 * 404 cross-tenant + 404 missing run
 * 503 when validation_service isn't wired
 * server-derived chunkId / runId on citations + retrieved chunks
 * `validationStatus` ≠ HTTP status (the executionStatus / outcome split)

Detailed check semantics live in test_validation_checks.py;
end-to-end query/answer plumbing in test_validation_service.py — these
tests exercise only the wire envelope and security boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.artifacts.models import ArtifactRecord
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.integration.services import (
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    RetrievalService,
    SourceLookupService,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus
from j1.search import SqliteSearchIndexer
from j1.validation import IngestionValidationService
from j1.workspace.layout import WorkspaceArea


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, indexer):
    from types import SimpleNamespace
    profile_stub = SimpleNamespace(report_templates={})
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(indexer, profile_stub),
    )


@pytest.fixture
def validation_service(
    run_store, artifact_registry, query_engine, audit_sink,
) -> IngestionValidationService:
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=DefaultAuditRecorder(audit_sink),
    )


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(
        workspace.audit(ProjectContext(tenant_id="acme", project_id="alpha"))
        / "feedback.jsonl"
    )


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
def client(
    application_facade, workspace, run_store, validation_service,
) -> TestClient:
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        validation_service=validation_service,
    )
    return TestClient(app)


@pytest.fixture
def client_no_validation(
    application_facade, workspace, run_store,
) -> TestClient:
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        # validation_service intentionally omitted
    )
    return TestClient(app)


# ---- Helpers --------------------------------------------------------


def _make_run(
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    status: RunStatus = RunStatus.SUCCEEDED,
) -> IngestionRun:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf",
        workflow_run_id="wfr",
        status=status,
        started_at=started,
        updated_at=started + timedelta(seconds=5),
        completed_at=started + timedelta(seconds=5),
    )


def _stage_chunk(
    workspace, ctx, artifact_registry, indexer,
    *, artifact_id: str, content: bytes, run_id: str, chunk_id: str,
) -> ArtifactRecord:
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={"run_id": run_id, "chunk_id": chunk_id},
    )
    artifact_registry.add(record)
    indexer.index(ctx, [artifact_id])
    return record


# ---- 404 / 503 ------------------------------------------------------


def test_test_query_returns_404_for_missing_run(client):
    """Run that doesn't exist → 404 with REVIEW_NOT_FOUND code, same
 shape as the rest of the review surface."""
    resp = client.post(
        "/ingestion-runs/missing/test-query",
        json={"question": "anything"},
        headers=_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_test_query_returns_404_for_cross_project(client, run_store, ctx):
    """Run exists in (acme, alpha) but request comes from (acme, beta)
 — must look identical to a missing run. Existence isn't probeable."""
    run_store.upsert(ctx, _make_run(run_id="run-x"))

    other = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}
    resp = client.post(
        "/ingestion-runs/run-x/test-query",
        json={"question": "anything"},
        headers=other,
    )
    assert resp.status_code == 404


def test_test_query_returns_404_for_cross_tenant(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-x"))
    other = {TENANT_HEADER: "enemy", PROJECT_HEADER: "alpha"}
    resp = client.post(
        "/ingestion-runs/run-x/test-query",
        json={"question": "anything"},
        headers=other,
    )
    assert resp.status_code == 404


def test_test_query_returns_503_when_service_not_configured(
    client_no_validation, run_store, ctx,
):
    """When the deployment didn't pass `validation_service=` to
 `create_rest_api`, the endpoint returns 503 — uniform with the
 rest of the optional-service degradation pattern (e.g.
 review_service)."""
    run_store.upsert(ctx, _make_run(run_id="run-x"))
    resp = client_no_validation.post(
        "/ingestion-runs/run-x/test-query",
        json={"question": "anything"},
        headers=_HEADERS,
    )
    assert resp.status_code == 503


# ---- Happy path -----------------------------------------------------


def test_test_query_returns_envelope_with_validation_status(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Wire-shape regression. Body must carry every field —
 new validators / FE renderers depend on each one being present."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world from run A",
        run_id="run-A", chunk_id="chunk-A1",
    )

    resp = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "hello"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    # Envelope keys
    for key in (
        "requestId", "runId", "question", "answer", "modeUsed",
        "retrievedChunks", "citations", "checks",
        "validationStatus", "evidenceFlags",
    ):
        assert key in data, f"missing key {key!r} in response data"
    assert data["runId"] == "run-A"
    assert data["validationStatus"] == "passed"
    assert data["evidenceFlags"]["graphUsed"] is False


def test_citations_carry_server_derived_chunk_id_and_run_id(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Trust rule: chunkId/runId on citations come from the FTS row,
 not from anything the LLM or the request body says. We verify
 the server-side wiring by confirming the FE-visible values
 match the metadata we wrote at index time."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"unique findings about the matter",
        run_id="run-A", chunk_id="chunk-PUBLIC-id-7",
    )

    resp = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "findings"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    citations = resp.json()["data"]["citations"]
    assert len(citations) >= 1
    cite = citations[0]
    assert cite["chunkId"] == "chunk-PUBLIC-id-7"
    assert cite["runId"] == "run-A"
    # Same fields surface on retrievedChunks for inline rendering.
    chunks = resp.json()["data"]["retrievedChunks"]
    assert chunks[0]["chunkId"] == "chunk-PUBLIC-id-7"
    assert chunks[0]["runId"] == "run-A"


def test_validation_status_failed_when_no_chunks_indexed(
    client, run_store, ctx,
):
    """HTTP=200 (the query ran), validationStatus=failed (no
 retrieval). This is the canonical 'split status' demonstration
 at the REST boundary."""
    run_store.upsert(ctx, _make_run(run_id="run-empty"))

    resp = client.post(
        "/ingestion-runs/run-empty/test-query",
        json={"question": "anything"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["validationStatus"] == "failed"
    chunks_check = next(
        c for c in body["checks"]
        if c["name"] == "retrieved_chunks_present"
    )
    assert chunks_check["passed"] is False


def test_top_k_above_50_rejected_by_pydantic(client, run_store, ctx):
    """Pydantic upper bound is 50 (matches the service's hard cap).
 A request asking for more should fail validation early — 422,
 not 200-with-clamping."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    resp = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "x", "topK": 51},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_top_k_zero_rejected_by_pydantic(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    resp = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "x", "topK": 0},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_empty_question_rejected_by_pydantic(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    resp = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": ""},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_include_raw_attaches_raw_response(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """`includeRaw=true` populates rawResponse; default omits it."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello",
        run_id="run-A", chunk_id="c-1",
    )
    on = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "hello", "includeRaw": True},
        headers=_HEADERS,
    ).json()["data"]
    off = client.post(
        "/ingestion-runs/run-A/test-query",
        json={"question": "hello"},
        headers=_HEADERS,
    ).json()["data"]
    assert on["rawResponse"] is not None
    assert off["rawResponse"] is None
