"""End-to-end tests for the GET /ingestion-runs/{id}/summary endpoint.

Covers wiring + envelope shape + 404/503 semantics. Detailed projection
behavior (artifact counts, availableViews reasons, warnings) lives in
test_ingestion_review_service.py — these tests only verify that the
endpoint plumbs the right data through and observes the standard
envelope conventions.
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
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.runs import (
    AuditProgressReporter,
    IngestionRun,
    JsonlIngestionRunStore,
    RunStatus,
)
from j1.workspace.layout import WorkspaceArea


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def reporter(workspace) -> AuditProgressReporter:
    return AuditProgressReporter(DefaultAuditRecorder(JsonlAuditSink(workspace)))


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
    """Minimal facade — review endpoints don't depend on the facade,
    but `create_rest_api` requires one."""
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def client(application_facade, workspace, run_store, review_service) -> TestClient:
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
    )
    return TestClient(app)


# ---- Helpers --------------------------------------------------------


def _make_run(
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    status: RunStatus = RunStatus.SUCCEEDED,
    metadata: dict | None = None,
) -> IngestionRun:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=5)
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf-1",
        workflow_run_id="wfr-1",
        status=status,
        started_at=started,
        updated_at=completed,
        completed_at=completed,
        metadata=metadata or {},
    )


def _make_artifact(
    ctx: ProjectContext, *, artifact_id: str, kind: str,
    source_document_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> ArtifactRecord:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"compiled/{artifact_id}.json",
        content_hash=f"hash-{artifact_id}",
        byte_size=128,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=source_document_ids or [],
        metadata=metadata or {},
    )


# ---- 404 / 503 ------------------------------------------------------


def test_get_summary_returns_404_for_missing_run(client):
    resp = client.get("/ingestion-runs/missing/summary", headers=_HEADERS)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_summary_returns_404_for_cross_project_access(
    client, run_store, ctx,
):
    """A run in tenant=acme/project=alpha must not be visible from
    tenant=acme/project=beta. Same 404 shape as a missing run — never
    leaks existence across projects."""
    run_store.upsert(ctx, _make_run(run_id="cross-project"))

    other_headers = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}
    resp = client.get(
        "/ingestion-runs/cross-project/summary",
        headers=other_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_summary_returns_503_when_review_service_not_configured(
    application_facade, workspace, run_store,
):
    """Standard graceful-degrade pattern — the endpoint 503s with a
    clear message rather than crashing on a None lookup."""
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=None,
    )
    test_client = TestClient(app)

    resp = test_client.get("/ingestion-runs/run-1/summary", headers=_HEADERS)
    assert resp.status_code == 503


# ---- Happy path -----------------------------------------------------


def test_get_summary_returns_envelope_with_camel_case(
    client, run_store, artifact_registry, reporter, ctx,
):
    run_store.upsert(ctx, _make_run(
        document_id="doc-A",
        metadata={
            "step_results": [
                {"step": "COMPILE", "status": "completed",
                 "required": True, "source": "default",
                 "duration_ms": 800},
            ],
        },
    ))
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="c1", kind="chunk",
            source_document_ids=["doc-A"],
        )
    )
    reporter.report_step_warning(
        ctx, run_id="run-1", stage="ENRICH", step="x", message="careful",
    )

    resp = client.get("/ingestion-runs/run-1/summary", headers=_HEADERS)

    assert resp.status_code == 200
    payload = resp.json()
    # Standard envelope shape.
    assert "requestId" in payload
    assert "data" in payload
    data = payload["data"]
    # camelCase serialization.
    assert data["runId"] == "run-1"
    assert data["status"] == "succeeded"
    assert data["durationMs"] == 5000
    assert data["documentIds"] == ["doc-A"]
    assert data["artifactCounts"] == {"chunk": 1}
    assert data["totalBytes"] == 128
    # Step results round-trip with camelCase keys.
    step = data["steps"][0]
    assert step["step"] == "COMPILE"
    assert step["durationMs"] == 800
    # Warnings present.
    assert len(data["warnings"]) == 1
    assert data["warnings"][0]["severity"] == "warning"
    # availableViews uses the camelized rawArtifacts key.
    views = data["availableViews"]
    assert views["chunks"]["available"] is True
    assert views["chunks"]["reason"] is None
    assert views["graph"]["available"] is False
    assert views["graph"]["reason"]
    assert views["rawArtifacts"]["available"] is True


def test_get_summary_omits_quality_summary_when_no_data(
    client, run_store, ctx,
):
    run_store.upsert(ctx, _make_run())

    resp = client.get("/ingestion-runs/run-1/summary", headers=_HEADERS)

    data = resp.json()["data"]
    assert data["qualitySummary"] is None


def test_get_summary_includes_quality_summary_when_warnings_present(
    client, run_store, reporter, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="qs"))
    reporter.report_step_warning(
        ctx, run_id="qs", stage="ENRICH", step="x", message="careful",
    )

    resp = client.get("/ingestion-runs/qs/summary", headers=_HEADERS)

    data = resp.json()["data"]
    assert data["qualitySummary"] is not None
    assert data["qualitySummary"]["warningCount"] == 1


# =====================================================================
# Phase 2 — /ingestion-runs/{id}/artifacts + .../{artifact_id}/content
# =====================================================================


def _write_artifact_file(workspace, ctx, *, area, location, body):
    full_path = workspace.area(ctx, area) / location.split("/", 1)[1]
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(body)


# ---- /artifacts list ------------------------------------------------


def test_list_run_artifacts_returns_paginated_envelope(
    client, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    for i in range(5):
        artifact_registry.add(_make_artifact(
            ctx, artifact_id=f"a{i}", kind="chunk",
            source_document_ids=["doc-A"],
        ))

    resp = client.get(
        "/ingestion-runs/run-1/artifacts?page=1&pageSize=2",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["page"] == 1
    assert data["pageSize"] == 2
    assert data["total"] == 5
    assert len(data["items"]) == 2


def test_list_run_artifacts_filters_by_kind(
    client, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="c1", kind="chunk",
        source_document_ids=["doc-A"],
    ))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="t1", kind="enriched.tables",
        source_document_ids=["doc-A"],
    ))

    resp = client.get(
        "/ingestion-runs/run-1/artifacts?kind=chunk",
        headers=_HEADERS,
    )

    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["kind"] == "chunk"


def test_list_run_artifacts_returns_404_for_cross_project(
    client, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    other_headers = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}

    resp = client.get(
        "/ingestion-runs/leak/artifacts",
        headers=other_headers,
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_list_run_artifacts_validates_page_size_upper_bound(client, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    resp = client.get(
        "/ingestion-runs/run-1/artifacts?pageSize=999",
        headers=_HEADERS,
    )
    # FastAPI's Query(le=200) rejects with 422.
    assert resp.status_code == 422


# ---- /artifacts/{id}/content ----------------------------------------


def test_get_artifact_content_returns_bytes_with_etag(
    client, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="json1", kind="chunk",
        source_document_ids=["doc-A"],
    ))
    _write_artifact_file(
        workspace, ctx, area=WorkspaceArea.COMPILED,
        location="compiled/json1.json",
        body=b'{"k": 1}',
    )

    resp = client.get(
        "/ingestion-runs/run-1/artifacts/json1/content",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.content == b'{"k": 1}'
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["etag"] == '"hash-json1"'
    # No attachment header for inline-renderable types.
    assert "content-disposition" not in resp.headers


def test_get_artifact_content_unknown_extension_serves_attachment(
    client, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    bin_record = ArtifactRecord(
        artifact_id="binblob",
        project=ctx,
        kind="binary",
        location="compiled/binblob.bin",
        content_hash="hash-binblob",
        byte_size=4,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-A"],
    )
    artifact_registry.add(bin_record)
    _write_artifact_file(
        workspace, ctx, area=WorkspaceArea.COMPILED,
        location="compiled/binblob.bin",
        body=b"\x00\x01\x02\x03",
    )

    resp = client.get(
        "/ingestion-runs/run-1/artifacts/binblob/content",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.content == b"\x00\x01\x02\x03"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert "attachment" in resp.headers["content-disposition"]
    assert "binblob.bin" in resp.headers["content-disposition"]


def test_get_artifact_content_404_for_unknown_artifact(client, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    resp = client.get(
        "/ingestion-runs/run-1/artifacts/missing/content",
        headers=_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_artifact_content_404_when_artifact_belongs_to_other_run(
    client, run_store, artifact_registry, workspace, ctx,
):
    """The most important security test: the run-scoped content
    endpoint must not let you read an artifact tagged for a different
    run, even if you guess its id."""
    run_store.upsert(ctx, _make_run(run_id="mine", document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="theirs", kind="chunk",
        source_document_ids=["doc-other"],
        metadata={"run_id": "their-run"},
    ))
    _write_artifact_file(
        workspace, ctx, area=WorkspaceArea.COMPILED,
        location="compiled/theirs.json",
        body=b'{"secret": true}',
    )

    resp = client.get(
        "/ingestion-runs/mine/artifacts/theirs/content",
        headers=_HEADERS,
    )

    assert resp.status_code == 404


def test_get_artifact_content_404_for_path_traversal_attempt(
    client, run_store, artifact_registry, ctx,
):
    """Tampered registry — `location` escapes the area — surfaces as
    a uniform 404 (PathTraversalError → REVIEW_NOT_FOUND)."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    bad = ArtifactRecord(
        artifact_id="evil",
        project=ctx,
        kind="chunk",
        location="compiled/../../../etc/passwd",
        content_hash="hash-evil",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-A"],
    )
    artifact_registry.add(bad)

    resp = client.get(
        "/ingestion-runs/run-1/artifacts/evil/content",
        headers=_HEADERS,
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


# =====================================================================
# Phase 3 — /chunks list / detail / NDJSON export
# =====================================================================


import json as _json


def _seed_chunks_run(
    run_store, artifact_registry, workspace, ctx,
    *,
    chunks: list[dict],
    artifact_id: str = "ca1",
    run_id: str = "run-1",
):
    run_store.upsert(ctx, _make_run(run_id=run_id, document_id="doc-A"))
    location = f"compiled/{artifact_id}.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / f"{artifact_id}.json"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_json.dumps({"chunks": chunks}), encoding="utf-8")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-A"],
    ))


# ---- /chunks list --------------------------------------------------


def test_list_chunks_returns_paginated_envelope(
    client, run_store, artifact_registry, workspace, ctx,
):
    chunks = [{"chunk_id": f"ch-{i}", "body": f"b{i}"} for i in range(5)]
    _seed_chunks_run(run_store, artifact_registry, workspace, ctx, chunks=chunks)

    resp = client.get(
        "/ingestion-runs/run-1/chunks?page=1&pageSize=2",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["page"] == 1
    assert data["pageSize"] == 2
    assert data["total"] == 5
    assert len(data["items"]) == 2
    # camelCase round-trip on a chunk preview.
    item = data["items"][0]
    assert item["chunkId"] == "ch-0"
    assert "preview" in item


def test_list_chunks_filters_by_min_confidence(
    client, run_store, artifact_registry, workspace, ctx,
):
    chunks = [
        {"chunk_id": "high", "body": "x", "confidence": 0.9},
        {"chunk_id": "low", "body": "x", "confidence": 0.1},
    ]
    _seed_chunks_run(run_store, artifact_registry, workspace, ctx, chunks=chunks)

    resp = client.get(
        "/ingestion-runs/run-1/chunks?minConfidence=0.5",
        headers=_HEADERS,
    )

    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["chunkId"] == "high"


def test_list_chunks_validates_min_confidence_range(client, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    resp = client.get(
        "/ingestion-runs/run-1/chunks?minConfidence=1.5",
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_list_chunks_404_for_missing_run(client):
    resp = client.get("/ingestion-runs/nope/chunks", headers=_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_list_chunks_404_cross_project(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    other_headers = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}
    resp = client.get(
        "/ingestion-runs/leak/chunks",
        headers=other_headers,
    )
    assert resp.status_code == 404


# ---- /chunks/{id} detail -------------------------------------------


def test_get_chunk_returns_full_body_and_lineage(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "target", "body": "the body", "tokenCount": 2}],
    )

    resp = client.get(
        "/ingestion-runs/run-1/chunks/target",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["chunkId"] == "target"
    assert data["body"] == "the body"
    assert data["tokenCount"] == 2
    assert data["lineage"]["documentIds"] == ["doc-A"]
    assert data["lineage"]["sourceArtifactId"] == "ca1"


def test_get_chunk_404_for_unknown_chunk(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "exists", "body": "x"}],
    )

    resp = client.get(
        "/ingestion-runs/run-1/chunks/missing",
        headers=_HEADERS,
    )

    assert resp.status_code == 404


# ---- /exports/chunks.ndjson ---------------------------------------


def test_export_chunks_streams_ndjson(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[
            {"chunk_id": "ch-1", "body": "one"},
            {"chunk_id": "ch-2", "body": "two"},
        ],
    )

    resp = client.get(
        "/ingestion-runs/run-1/exports/chunks.ndjson",
        headers=_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in resp.content.split(b"\n") if ln]
    assert len(lines) == 2
    parsed = [_json.loads(ln) for ln in lines]
    assert [p["chunkId"] for p in parsed] == ["ch-1", "ch-2"]


def test_export_chunks_404_for_missing_run(client):
    """Eager validation in the service means 404 hits before any
    bytes go out — not a partial 200 response."""
    resp = client.get(
        "/ingestion-runs/missing/exports/chunks.ndjson",
        headers=_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_summary_chunks_available_when_chunk_artifact_present(
    client, run_store, artifact_registry, workspace, ctx,
):
    """End-to-end check: when a chunk artifact exists, the summary
    endpoint reports `availableViews.chunks.available=true`."""
    _seed_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "x", "body": "y"}],
    )

    resp = client.get("/ingestion-runs/run-1/summary", headers=_HEADERS)

    views = resp.json()["data"]["availableViews"]
    assert views["chunks"]["available"] is True
    assert views["chunks"]["reason"] is None


# ---- Phase 4 end-to-end: terminal activity → review summary -----


# =====================================================================
# Phase 5 — /quality-report
# =====================================================================


def _seed_quality_run(
    run_store, artifact_registry, workspace, ctx,
    *,
    confidence_payload: dict | None = None,
    consistency_payload: dict | None = None,
    step_results: list | None = None,
    run_id: str = "run-1",
):
    metadata = {"step_results": step_results} if step_results else {}
    run_store.upsert(ctx, _make_run(
        run_id=run_id, document_id="doc-A", metadata=metadata,
    ))
    if confidence_payload is not None:
        full = workspace.area(ctx, WorkspaceArea.ENRICHED) / "ca1.json"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(_json.dumps(confidence_payload), encoding="utf-8")
        artifact_registry.add(ArtifactRecord(
            artifact_id="ca1",
            project=ctx,
            kind="enriched.confidence_assessment",
            location="enriched/ca1.json",
            content_hash="hash-ca1",
            byte_size=0,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-A"],
        ))
    if consistency_payload is not None:
        full = workspace.area(ctx, WorkspaceArea.ENRICHED) / "cf1.json"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(_json.dumps(consistency_payload), encoding="utf-8")
        artifact_registry.add(ArtifactRecord(
            artifact_id="cf1",
            project=ctx,
            kind="enriched.consistency_findings",
            location="enriched/cf1.json",
            content_hash="hash-cf1",
            byte_size=0,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-A"],
        ))


def test_get_quality_report_returns_envelope_with_camelcase(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_quality_run(
        run_store, artifact_registry, workspace, ctx,
        confidence_payload={
            "assessments": [
                {"modality": "tables", "confidence": 0.8},
                {"modality": "ocr", "confidence": 0.5,
                 "page": 7, "category": "low_confidence"},
            ],
        },
        step_results=[
            {"step": "graph", "status": "skipped", "required": False,
             "source": "policy", "reason": "text-only mode"},
        ],
    )

    resp = client.get(
        "/ingestion-runs/run-1/quality-report", headers=_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overallConfidence"] == 0.65
    by_modality = {m["modality"]: m for m in data["modalityConfidences"]}
    assert by_modality["tables"]["confidence"] == 0.8
    assert by_modality["ocr"]["sampleCount"] == 1
    # Low-confidence finding from the assessment.
    assert len(data["lowConfidenceFindings"]) == 1
    finding = data["lowConfidenceFindings"][0]
    assert finding["page"] == 7
    assert finding["chunkId"] is None
    # Skipped step surfaces with policy.
    assert data["skippedSteps"] == [
        {"step": "graph", "reason": "text-only mode", "policy": "policy"}
    ]
    # rawDebug omitted by default.
    assert data["rawDebug"] is None


def test_get_quality_report_include_raw_query_param(
    client, run_store, artifact_registry, workspace, ctx,
):
    payload = {"default_confidence": 0.6}
    _seed_quality_run(
        run_store, artifact_registry, workspace, ctx,
        confidence_payload=payload,
    )

    resp = client.get(
        "/ingestion-runs/run-1/quality-report?includeRaw=true",
        headers=_HEADERS,
    )

    data = resp.json()["data"]
    assert data["rawDebug"] is not None
    # rawDebug is opaque debug data — the dict's KEYS pass through
    # verbatim (CamelModel only camelizes declared fields), so the
    # internal grouping stays snake_case for the rare debug consumer.
    assert data["rawDebug"]["confidence_assessment"][0] == payload


def test_get_quality_report_404_for_missing_run(client):
    resp = client.get(
        "/ingestion-runs/nope/quality-report", headers=_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_quality_report_404_cross_project(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    other_headers = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}
    resp = client.get(
        "/ingestion-runs/leak/quality-report",
        headers=other_headers,
    )
    assert resp.status_code == 404


def test_get_quality_report_503_when_review_service_not_configured(
    application_facade, workspace, run_store,
):
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=None,
    )
    test_client = TestClient(app)
    resp = test_client.get(
        "/ingestion-runs/run-1/quality-report", headers=_HEADERS,
    )
    assert resp.status_code == 503


def test_get_quality_report_returns_empty_shape_when_no_quality_data(
    client, run_store, ctx,
):
    """Run exists but there's nothing to report — endpoint still
    succeeds with all-empty fields. The Quality tab will show its
    empty state."""
    run_store.upsert(ctx, _make_run())

    resp = client.get(
        "/ingestion-runs/run-1/quality-report", headers=_HEADERS,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overallConfidence"] is None
    assert data["modalityConfidences"] == []
    assert data["warnings"] == []
    assert data["skippedSteps"] == []
    assert data["failedOptionalSteps"] == []
    assert data["lowConfidenceFindings"] == []
    assert data["rawDebug"] is None


# =====================================================================
# Phase 6 — /graph
# =====================================================================


def _seed_graph_run(
    run_store, artifact_registry, workspace, ctx,
    *,
    entities_payload: dict | list | None = None,
    relations_payload: dict | list | None = None,
    step_results: list | None = None,
    run_id: str = "run-1",
):
    metadata = {"step_results": step_results} if step_results else {}
    run_store.upsert(ctx, _make_run(
        run_id=run_id, document_id="doc-A", metadata=metadata,
    ))
    if entities_payload is not None:
        full = workspace.area(ctx, WorkspaceArea.GRAPH) / "vdb_entities.json"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(_json.dumps(entities_payload), encoding="utf-8")
        artifact_registry.add(ArtifactRecord(
            artifact_id="ge1",
            project=ctx,
            kind="graph_json",
            location="graph/vdb_entities.json",
            content_hash="hash-ge1",
            byte_size=0,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-A"],
        ))
    if relations_payload is not None:
        full = workspace.area(ctx, WorkspaceArea.GRAPH) / "vdb_relationships.json"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(_json.dumps(relations_payload), encoding="utf-8")
        artifact_registry.add(ArtifactRecord(
            artifact_id="gr1",
            project=ctx,
            kind="graph_json",
            location="graph/vdb_relationships.json",
            content_hash="hash-gr1",
            byte_size=0,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-A"],
        ))


def test_get_graph_returns_envelope_with_camelcase(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_graph_run(
        run_store, artifact_registry, workspace, ctx,
        entities_payload={
            "alice": {
                "__id__": "alice",
                "__name__": "Alice",
                "__entity_type__": "PERSON",
                "__source_id__": "ch-1;ch-2",
            },
        },
        relations_payload=[
            {"__src__": "alice", "__tgt__": "bob",
             "__keywords__": "knows", "__weight__": 0.8},
        ],
    )

    resp = client.get("/ingestion-runs/run-1/graph", headers=_HEADERS)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["unavailable"] is None
    assert data["stats"]["entityCount"] == 1
    assert data["stats"]["relationCount"] == 1
    entity = data["entities"][0]
    assert entity["id"] == "alice"
    assert entity["sourceChunkIds"] == ["ch-1", "ch-2"]
    relation = data["relations"][0]
    assert relation["sourceEntityId"] == "alice"
    assert relation["targetEntityId"] == "bob"
    assert relation["weight"] == 0.8
    # Per-list truncation defaults to false; limits surfaced.
    assert data["truncated"]["entities"] is False
    assert data["truncated"]["relations"] is False
    assert data["truncated"]["limits"]["maxNodes"] == 5000


def test_get_graph_respects_max_nodes_query(
    client, run_store, artifact_registry, workspace, ctx,
):
    _seed_graph_run(
        run_store, artifact_registry, workspace, ctx,
        entities_payload=[{"id": f"e{i}"} for i in range(10)],
    )

    resp = client.get(
        "/ingestion-runs/run-1/graph?maxNodes=3",
        headers=_HEADERS,
    )

    data = resp.json()["data"]
    assert data["stats"]["entityCount"] == 10
    assert len(data["entities"]) == 3
    assert data["truncated"]["entities"] is True
    assert data["truncated"]["limits"]["maxNodes"] == 3


def test_get_graph_validates_max_nodes_upper_bound(client, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    resp = client.get(
        "/ingestion-runs/run-1/graph?maxNodes=100000",
        headers=_HEADERS,
    )
    assert resp.status_code == 422


def test_get_graph_returns_unavailable_skipped_by_policy(
    client, run_store, ctx,
):
    """End-to-end: workflow recorded GRAPH skipped by policy →
    `/graph`'s `unavailable.reason` carries that copy."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "graph", "status": "skipped", "source": "policy",
             "required": False},
        ],
    }))

    resp = client.get("/ingestion-runs/run-1/graph", headers=_HEADERS)

    data = resp.json()["data"]
    assert "policy" in data["unavailable"]["reason"].lower()
    assert data["entities"] == []
    assert data["relations"] == []


def test_get_graph_returns_unavailable_failure(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "graph", "status": "failed", "source": "planner",
             "required": False},
        ],
    }))

    resp = client.get("/ingestion-runs/run-1/graph", headers=_HEADERS)

    data = resp.json()["data"]
    assert "fail" in data["unavailable"]["reason"].lower()


def test_get_graph_404_for_missing_run(client):
    resp = client.get("/ingestion-runs/nope/graph", headers=_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_graph_404_cross_project(client, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    other_headers = {TENANT_HEADER: "acme", PROJECT_HEADER: "beta"}
    resp = client.get(
        "/ingestion-runs/leak/graph",
        headers=other_headers,
    )
    assert resp.status_code == 404


def test_get_graph_503_when_review_service_not_configured(
    application_facade, workspace, run_store,
):
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=None,
    )
    test_client = TestClient(app)
    resp = test_client.get(
        "/ingestion-runs/run-1/graph", headers=_HEADERS,
    )
    assert resp.status_code == 503


def test_summary_surfaces_step_results_after_workflow_terminal(
    client, run_store, ctx,
):
    """Closes the Phase 4 loop end-to-end: the workflow's terminal
    activity persists step_summary into IngestionRun.metadata, and
    the review-service summary endpoint hydrates them into the FE-
    facing `steps` field. No mock — uses the real RunsActivities."""
    from j1.orchestration.activities.payloads import ProjectScope
    from j1.orchestration.activities.runs import (
        ReportRunTerminalInput,
        RunsActivities,
        StepSummaryEntry,
    )

    run_store.upsert(ctx, _make_run(run_id="e2e"))

    runs = RunsActivities(progress_reporter=None, run_store=run_store)
    runs.report_run_terminal(ReportRunTerminalInput(
        scope=ProjectScope(tenant_id=ctx.tenant_id, project_id=ctx.project_id),
        run_id="e2e",
        final_status="succeeded",
        step_summary=(
            StepSummaryEntry(
                step="compile", status="completed", required=True,
                source="caller", artifact_count=2,
            ),
            StepSummaryEntry(
                step="graph", status="skipped", required=False,
                source="policy", reason="text-only mode",
            ),
        ),
    ))

    resp = client.get("/ingestion-runs/e2e/summary", headers=_HEADERS)
    assert resp.status_code == 200
    data = resp.json()["data"]
    steps = data["steps"]
    assert [s["step"] for s in steps] == ["compile", "graph"]
    assert steps[1]["status"] == "skipped"
    assert steps[1]["source"] == "policy"
    # Graph availability reason now correctly traces to "skipped by policy"
    # because Phase 4 persisted the step_results that Phase 1's
    # availability resolver inspects.
    assert "policy" in data["availableViews"]["graph"]["reason"].lower()
