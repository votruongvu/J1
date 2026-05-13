"""End-to-end REST tests for validation endpoints.

Six routes ship :

 POST /ingestion-runs/{id}/validation-sets/generate
 GET /ingestion-runs/{id}/validation-sets
 GET /ingestion-runs/{id}/validation-sets/{vs}
 POST /ingestion-runs/{id}/validation-runs
 GET /ingestion-runs/{id}/validation-runs
 GET /ingestion-runs/{id}/validation-runs/{vr}

Service-level semantics (idempotency, ownership, lifecycle) are
covered in test_validation_service_; these tests verify the
REST envelope shape, headers/scope plumbing, 404 cross-tenant
uniformity, 503 when the service isn't wired, and request validation.
"""

from __future__ import annotations

import json
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
from j1.validation import (
    DefaultTestCaseGenerator,
    IngestionValidationService,
    JsonlValidationRunStore,
    JsonlValidationSetStore,
)
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
    run_store, artifact_registry, query_engine, audit_sink, workspace,
    stub_smart_query_orchestrator,
) -> IngestionValidationService:
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=DefaultAuditRecorder(audit_sink),
        workspace=workspace,
        validation_set_store=JsonlValidationSetStore(workspace),
        validation_run_store=JsonlValidationRunStore(workspace),
        test_case_generator=DefaultTestCaseGenerator(),
        smart_query_orchestrator=stub_smart_query_orchestrator,
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
    )
    return TestClient(app)


# ---- Helpers --------------------------------------------------------


def _make_run(*, run_id: str = "run-1") -> IngestionRun:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id="wf",
        workflow_run_id="wfr",
        status=RunStatus.SUCCEEDED,
        started_at=started,
        updated_at=started + timedelta(seconds=5),
        completed_at=started + timedelta(seconds=5),
    )


def _stage_chunk(
    workspace, ctx, artifact_registry, indexer,
    *, artifact_id: str, body: str, run_id: str, chunk_id: str,
):
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.json"
    payload = {"chunkId": chunk_id, "body": body}
    (area_dir / stored).write_bytes(json.dumps(payload).encode("utf-8"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(body),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={"run_id": run_id, "chunk_id": chunk_id},
    )
    artifact_registry.add(record)
    indexer.index(ctx, [artifact_id])
    return record


# ---- Generate validation set ----------------------------------------


def test_post_generate_returns_201_with_set(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Happy path: generate writes a set + returns the full DTO."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The proposal is due 20 May 2026.",
        run_id="run-1", chunk_id="chunk-A",
    )

    resp = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={"maxCases": 5},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["runId"] == "run-1"
    assert data["source"] == "generated"
    assert data["status"] == "draft"
    assert isinstance(data["testCases"], list)
    assert len(data["testCases"]) >= 1


def test_post_generate_404_for_missing_run(client):
    resp = client.post(
        "/ingestion-runs/ghost/validation-sets/generate",
        json={},
        headers=_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_post_generate_404_for_cross_tenant(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    other = {TENANT_HEADER: "enemy", PROJECT_HEADER: "alpha"}
    resp = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={},
        headers=other,
    )
    assert resp.status_code == 404


def test_post_generate_503_when_service_not_wired(
    client_no_validation, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client_no_validation.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={},
        headers=_HEADERS,
    )
    assert resp.status_code == 503


def test_post_generate_422_for_max_cases_above_50(client, run_store, ctx):
    """Pydantic upper bound mirrors the service's hard cap.
 51 should be rejected at the boundary, not clamped silently."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={"maxCases": 51},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_post_generate_idempotent_returns_existing_set_id(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Two POSTs with same chunks return the same set id."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )

    a = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    b = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]

    assert a["validationSetId"] == b["validationSetId"]


def test_post_generate_force_creates_new_set(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )

    a = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    forced = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={"force": True}, headers=_HEADERS,
    ).json()["data"]

    assert a["validationSetId"] != forced["validationSetId"]


# ---- List / get sets ------------------------------------------------


def test_list_validation_sets_returns_lightweight_items(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )
    client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    )

    resp = client.get(
        "/ingestion-runs/run-1/validation-sets",
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    # List items omit `testCases` for payload size — caller fetches
    # the full set when they actually need to render the table.
    assert "testCases" not in items[0]
    assert "caseCount" in items[0]
    assert items[0]["caseCount"] >= 1


def test_list_sets_unknown_run_returns_404(client):
    resp = client.get(
        "/ingestion-runs/ghost/validation-sets", headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_get_validation_set_returns_full_payload(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )
    created = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    vs_id = created["validationSetId"]

    resp = client.get(
        f"/ingestion-runs/run-1/validation-sets/{vs_id}",
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["validationSetId"] == vs_id
    assert isinstance(data["testCases"], list)


def test_get_validation_set_unknown_returns_404(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.get(
        "/ingestion-runs/run-1/validation-sets/vs-ghost",
        headers=_HEADERS,
    )
    assert resp.status_code == 404


# ---- Run validation -------------------------------------------------


def test_post_validation_run_returns_201_with_terminal_snapshot(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """v1 is synchronous — 201 means the runner finished. Body's
 `executionStatus` is `completed` and `validationStatus`
 reflects the case outcomes."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )
    vs = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]

    resp = client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": vs["validationSetId"]},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["executionStatus"] == "completed"
    assert data["runId"] == "run-1"
    assert "validationStatus" in data
    assert "summary" in data
    assert isinstance(data["results"], list)


def test_post_validation_run_unknown_set_returns_404(
    client, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": "vs-ghost"},
        headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_post_validation_run_503_when_service_not_wired(
    client_no_validation, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client_no_validation.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": "vs-1"},
        headers=_HEADERS,
    )
    assert resp.status_code == 503


def test_post_validation_run_empty_body_returns_422(
    client, run_store, ctx,
):
    """Pydantic enforces validationSetId at the boundary."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


# ---- List / get runs -----------------------------------------------


def test_list_validation_runs_returns_lightweight_items(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )
    vs = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": vs["validationSetId"]}, headers=_HEADERS,
    )

    resp = client.get(
        "/ingestion-runs/run-1/validation-runs",
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    # List items carry summary but NOT the full results array —
    # caller fetches per-vrun for the detail drawer.
    assert "results" not in items[0]
    assert "summary" in items[0]


def test_get_validation_run_returns_full_payload_with_results(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="The Risk Assessment workflow validates the proposal at Stage 1.", run_id="run-1", chunk_id="c-1",
    )
    vs = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    vrun_created = client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": vs["validationSetId"]}, headers=_HEADERS,
    ).json()["data"]
    vr_id = vrun_created["validationRunId"]

    resp = client.get(
        f"/ingestion-runs/run-1/validation-runs/{vr_id}",
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["validationRunId"] == vr_id
    assert isinstance(data["results"], list)
    if data["results"]:
        result = data["results"][0]
        #  trust contract: every retrieved chunk + citation
        # carries server-derived runId/chunkId.
        if result["retrievedChunks"]:
            assert result["retrievedChunks"][0]["runId"] == "run-1"


def test_get_validation_run_unknown_returns_404(
    client, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.get(
        "/ingestion-runs/run-1/validation-runs/vrun-ghost",
        headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_split_status_persists_across_envelope(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Wire-shape regression: HTTP status (201) and validationStatus
 (in body) are independent. Empty index → completed + failed."""
    run_store.upsert(ctx, _make_run(run_id="empty-run"))
    # Generate a set without indexing any chunks — runner will fail.
    vs = client.post(
        "/ingestion-runs/empty-run/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]

    resp = client.post(
        "/ingestion-runs/empty-run/validation-runs",
        json={"validationSetId": vs["validationSetId"]},
        headers=_HEADERS,
    )
    assert resp.status_code == 201  # job ran
    data = resp.json()["data"]
    assert data["executionStatus"] == "completed"
    # No chunks → smoke case fails on retrieved_chunks_present.
    assert data["validationStatus"] in ("failed", "inconclusive")
