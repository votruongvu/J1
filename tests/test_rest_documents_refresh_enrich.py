"""REST tests for the retired ``POST /ingestion-runs/{run_id}/
refresh-enrichment`` route.

The endpoint used to allocate a candidate run that reused the
active run's compile output and re-ran enrichment + graph + index.
It was replaced by the explicit Manual Domain Enrichment action:

    POST /documents/{document_id}/manual-actions/run-domain-enrichment

To stop two competing enrichment paths from co-existing, the route
now returns HTTP 410 Gone with a structured error envelope pointing
callers at the replacement. The handler is intentionally side-effect
free — no run is allocated, no workflow is started, no audit event
is emitted beyond the FastAPI request log.

This test file pins:

  * 410 status + the ``REFRESH_ENRICHMENT_RETIRED`` code.
  * Structured ``details`` payload carrying ``replacementRoute``.
  * No job dispatch happens (the job starter is never invoked).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.jobs.status import ProcessingStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        RetrievalService, SourceLookupService,
    )
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=None,
        event_publisher=EventPublisherService(audit_recorder),
        job_control=None,
    )


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def started_jobs() -> list:
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append({
            "document_id": document_id,
            "correlation_id": body.correlation_id,
        })
        return f"wf-{body.correlation_id}"
    return starter


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


@pytest.fixture
def client(
    application_facade, workspace, run_store, job_starter,
    lifecycle_service,
):
    from j1.integration.dto import ProcessingCapabilities
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        job_starter=job_starter,
        document_lifecycle_service=lifecycle_service,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(
    registry, ctx, *, document_id="doc-1", state="attached",
    active_snapshot_id="snap-active",
):
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_run(
    run_store, ctx, *, run_id, document_id,
    status=RunStatus.SUCCEEDED, metadata=None,
    started_at: datetime | None = None,
):
    started = started_at or _NOW
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started,
        updated_at=started,
        completed_at=started,
        metadata=metadata or {},
    ))


# ---- Retired contract --------------------------------------------


def test_refresh_enrichment_route_returns_410_with_structured_body(
    client, registry, run_store, started_jobs, ctx,
):
    """The route is retired. Every call — valid or otherwise —
    returns HTTP 410 with a structured envelope. The job starter
    is never invoked."""
    _seed_doc(registry, ctx)
    _seed_run(run_store, ctx, run_id="r-active", document_id="doc-1")

    resp = client.post(
        "/ingestion-runs/r-active/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 410, resp.text
    body = resp.json()
    err = body["error"]
    assert err["code"] == "REFRESH_ENRICHMENT_RETIRED"
    # The message points at the replacement route so external
    # callers know exactly where to migrate.
    assert "manual-actions/run-domain-enrichment" in err["message"]
    details = err["details"]
    assert details["deprecatedRunId"] == "r-active"
    assert details["replacementRoute"] == (
        "/documents/{documentId}/manual-actions/run-domain-enrichment"
    )
    # No job was dispatched; the handler is side-effect free.
    assert started_jobs == []


def test_refresh_enrichment_route_410_even_for_unknown_run(
    client, started_jobs, ctx,
):
    """The handler is intentionally not gated on run existence —
    it short-circuits to 410 before any store lookup. External
    callers that pass a bogus run id get the same retirement
    notice as callers that pass a valid one."""
    resp = client.post(
        "/ingestion-runs/missing/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 410, resp.text
    assert resp.json()["error"]["code"] == "REFRESH_ENRICHMENT_RETIRED"
    assert started_jobs == []


def test_refresh_enrichment_route_is_deprecated_in_openapi(
    client,
):
    """The route remains mounted with ``deprecated=true`` so
    generated clients flag it during code review."""
    schema = client.app.openapi()
    path = schema["paths"][
        "/ingestion-runs/{run_id}/refresh-enrichment"
    ]
    op = path.get("post")
    assert op is not None
    assert op.get("deprecated") is True
