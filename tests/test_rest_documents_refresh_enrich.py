"""REST tests for ``POST /ingestion-runs/{run_id}/refresh-enrichment``.

Refresh-enrichment is a run-level action that's only valid on the
document's currently active run. It allocates a new candidate run
that reuses the active run's compile output and re-runs only
enrichment + graph + index. Promotion to ``activeSnapshotId`` is
CAS-on-terminal-success — a failed refresh preserves the previous
active.

Covers:
  * Happy path: posting against the active run creates a candidate.
  * Active-run guard: posting against a non-active run is rejected
    with HTTP 409.
  * In-flight guard: rejected while the active run is still RUNNING.
  * Detached / removed documents are rejected with HTTP 409.
  * Unknown run → HTTP 404.
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
            "reindex_of": body.reindex_of,
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


# ---- Happy path ---------------------------------------------------


def test_refresh_enrichment_on_active_run_creates_candidate(
    client, registry, run_store, started_jobs, ctx,
):
    """Posting against the active run allocates a new candidate run
    that reuses the active run's compile output."""
    _seed_doc(registry, ctx)
    _seed_run(run_store, ctx, run_id="r-active", document_id="doc-1")

    resp = client.post(
        "/ingestion-runs/r-active/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]

    assert body["documentId"] == "doc-1"
    assert body["runType"] == "refresh_enrich"
    assert body["parentRunId"] == "r-active"
    assert body["reusedCompileFromRunId"] == "r-active"

    new_run = run_store.get(ctx, body["refreshRunId"])
    assert new_run.run_type == "refresh_enrich"
    assert new_run.parent_run_id == "r-active"
    assert new_run.metadata["reused_compile_from_run_id"] == "r-active"


def test_refresh_enrichment_does_not_flip_active_immediately(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(run_store, ctx, run_id="r-active", document_id="doc-1")

    client.post(
        "/ingestion-runs/r-active/refresh-enrichment",
        headers=_headers(ctx),
    )
    # active_snapshot_id unchanged — promotion is CAS-on-terminal-success.
    assert registry.get(ctx, "doc-1").active_snapshot_id == "snap-active"


# ---- Refusal paths -------------------------------------------------


def test_refresh_enrichment_rejects_non_active_run(
    client, registry, run_store, ctx,
):
    """Posting against a non-active run (older / superseded) is
    rejected. Only the document's currently active run can be
    refresh-enriched."""
    from datetime import timedelta
    _seed_doc(registry, ctx, active_snapshot_id="snap-newer")
    # The active run for this document is r-newer; r-older is a
    # historical attempt and must not be refresh-enrichable.
    _seed_run(
        run_store, ctx, run_id="r-older", document_id="doc-1",
        started_at=_NOW - timedelta(hours=1),
    )
    _seed_run(
        run_store, ctx, run_id="r-newer", document_id="doc-1",
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
    )

    resp = client.post(
        "/ingestion-runs/r-older/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert "not the active run" in resp.text.lower()


def test_refresh_enrichment_rejected_when_document_has_no_active_run(
    client, registry, run_store, ctx,
):
    """A document with no terminally-succeeded run yet has no active
    run. Refresh-enrichment is meaningless until a successful initial
    run exists; the error message points the user at reindex."""
    _seed_doc(registry, ctx, active_snapshot_id=None)
    _seed_run(
        run_store, ctx, run_id="r-failed",
        document_id="doc-1", status=RunStatus.FAILED,
    )
    resp = client.post(
        "/ingestion-runs/r-failed/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert "no active run" in resp.text.lower()


def test_refresh_enrichment_rejected_when_document_detached(
    client, registry, run_store, ctx,
):
    _seed_doc(
        registry, ctx, state="detached", active_snapshot_id="snap-active",
    )
    _seed_run(run_store, ctx, run_id="r-active", document_id="doc-1")
    resp = client.post(
        "/ingestion-runs/r-active/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 409


def test_refresh_enrichment_rejected_while_active_run_is_running(
    client, registry, run_store, ctx,
):
    """If the candidate active run is still RUNNING (we caught it
    mid-flight), refuse the refresh — the workflow could still be
    writing artifacts."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        document_id="doc-1", status=RunStatus.RUNNING,
    )
    resp = client.post(
        "/ingestion-runs/r-active/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 409


def test_refresh_enrichment_404_on_unknown_run(client, ctx):
    resp = client.post(
        "/ingestion-runs/missing/refresh-enrichment",
        headers=_headers(ctx),
    )
    assert resp.status_code == 404
