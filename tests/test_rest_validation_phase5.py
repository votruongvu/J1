"""Phase 5 REST tests — verdict endpoint + report export.

Service-level semantics live in `test_validation_service_phase5.py`;
these tests verify the REST envelope shape, headers/scope plumbing,
input validation, 404 cross-tenant uniformity, and the response
content-types for the report download.
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
    artifact_registry.add(
        ArtifactRecord(
            artifact_id=artifact_id,
            project=ctx,
            kind="chunk",
            location=f"{area.value}/{stored}",
            content_hash=f"sha256:{artifact_id}",
            byte_size=len(body),
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now, updated_at=now,
            source_document_ids=["doc-1"], source_artifact_ids=[],
            metadata={"run_id": run_id, "chunk_id": chunk_id},
        )
    )
    indexer.index(ctx, [artifact_id])


def _seed_run(client, run_store, ctx, workspace, artifact_registry, indexer):
    """Create + index a chunk + generate set + execute validation.
    Returns (vrun_dict, first_result_id) — both the FE would need."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body="alpha keyword", run_id="run-1", chunk_id="c-1",
    )
    vs = client.post(
        "/ingestion-runs/run-1/validation-sets/generate",
        json={}, headers=_HEADERS,
    ).json()["data"]
    vrun = client.post(
        "/ingestion-runs/run-1/validation-runs",
        json={"validationSetId": vs["validationSetId"]},
        headers=_HEADERS,
    ).json()["data"]
    return vrun, vrun["results"][0]["resultId"]


# ---- POST verdict --------------------------------------------------


def test_post_verdict_returns_updated_run_with_verdict_field(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Happy path: verdict + notes round-trip through the response."""
    vrun, result_id = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/{result_id}/verdict",
        json={"verdict": "pass", "notes": "Looks fine."},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    matched = next(
        r for r in data["results"] if r["resultId"] == result_id
    )
    assert matched["testerVerdict"] == "pass"
    assert matched["testerNotes"] == "Looks fine."


def test_post_verdict_keeps_auto_status_unchanged(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """The auto `status` is the deterministic verdict — must stay
    untouched when a tester verdict is recorded."""
    vrun, result_id = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    original = next(r for r in vrun["results"] if r["resultId"] == result_id)
    original_status = original["status"]

    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/{result_id}/verdict",
        json={"verdict": "warning", "notes": ""},
        headers=_HEADERS,
    )
    matched = next(
        r for r in resp.json()["data"]["results"] if r["resultId"] == result_id
    )
    assert matched["status"] == original_status


def test_post_verdict_rejects_invalid_verdict_value(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Pydantic Literal at the boundary: arbitrary verdict strings
    fail validation with 422 before reaching the service."""
    vrun, result_id = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/{result_id}/verdict",
        json={"verdict": "approved"},  # not in the allowed set
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_post_verdict_404_for_unknown_result(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun, _ = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/vr-ghost/verdict",
        json={"verdict": "pass"},
        headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_post_verdict_404_cross_tenant(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun, result_id = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    other = {TENANT_HEADER: "enemy", PROJECT_HEADER: "alpha"}
    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/{result_id}/verdict",
        json={"verdict": "pass"},
        headers=other,
    )
    assert resp.status_code == 404


def test_post_verdict_caps_notes_length(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Pydantic max_length=4096 protects audit-log lines from huge
    pasted text. 4097-char notes must be rejected with 422."""
    vrun, result_id = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.post(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/results/{result_id}/verdict",
        json={"verdict": "pass", "notes": "x" * 5000},
        headers=_HEADERS,
    )
    assert resp.status_code == 422


# ---- GET report ----------------------------------------------------


def test_get_report_default_returns_markdown(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Default format is Markdown with the right Content-Type and
    a downloadable Content-Disposition."""
    vrun, _ = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.get(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/report",
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    # text/markdown + extra params (TestClient surfaces the raw header)
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    assert ".md" in resp.headers["content-disposition"]
    body = resp.text
    assert "Validation Report" in body
    assert "Execution status" in body
    assert "Validation status" in body


def test_get_report_json_format(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    """`?format=json` returns a JSON payload with the right MIME +
    a `.json` extension on the suggested filename."""
    vrun, _ = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.get(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/report",
        params={"format": "json"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert ".json" in resp.headers["content-disposition"]
    parsed = resp.json()
    assert parsed["validation_run_id"] == vrun["validationRunId"]


def test_get_report_unknown_format_returns_400(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun, _ = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    resp = client.get(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/report",
        params={"format": "xml"},
        headers=_HEADERS,
    )
    assert resp.status_code == 400


def test_get_report_404_for_unknown_run(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    resp = client.get(
        "/ingestion-runs/run-1/validation-runs/vrun-ghost/report",
        headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_get_report_404_cross_tenant(
    client, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun, _ = _seed_run(
        client, run_store, ctx, workspace, artifact_registry, indexer,
    )
    other = {TENANT_HEADER: "enemy", PROJECT_HEADER: "alpha"}
    resp = client.get(
        f"/ingestion-runs/run-1/validation-runs/{vrun['validationRunId']}/report",
        headers=other,
    )
    assert resp.status_code == 404
