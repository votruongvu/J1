"""REST guards on ``DELETE /ingestion-runs/{run_id}``.

The endpoint now does a single-step hard delete. Two cross-run guards
are enforced at the REST edge (the service stays document-agnostic):

  * **Only-run guard** — if this is the document's only run, refuse
    with HTTP 409; the operator must call ``POST /documents/{id}/
    remove`` instead.
  * **Active-run guard** — if this run is the document's currently
    active run, refuse with HTTP 409; the active run must be
    superseded (by reindex) or removed (by ``Remove Knowledge``)
    before it can be deleted.

These tests sit at the REST layer because the service-level tests in
``tests/test_ingestion_review_service.py`` cover the happy-path
mechanics — files unlinked, registry records gone, run record purged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.ingestion_review import IngestionResultReviewService
from j1.jobs.status import ProcessingStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


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
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


@pytest.fixture
def review_service(run_store, artifact_registry, workspace):
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


@pytest.fixture
def client(
    application_facade, workspace, run_store, lifecycle_service,
    review_service,
):
    from j1.integration.dto import ProcessingCapabilities
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(
    registry, ctx, *, document_id="doc-1", active_snapshot_id="snap-active",
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
        knowledge_state="attached",
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_run(
    run_store, ctx, *, run_id, document_id="doc-1",
    status=RunStatus.SUCCEEDED, started_at=None,
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
        metadata={},
    ))


def test_delete_run_rejects_only_run(client, registry, run_store, ctx):
    """A document with exactly one run can't have that run deleted
    via the run endpoint — the operator must use Remove Knowledge."""
    _seed_doc(registry, ctx)
    _seed_run(run_store, ctx, run_id="r-only")

    resp = client.delete("/ingestion-runs/r-only", headers=_headers(ctx))
    assert resp.status_code == 409, resp.text
    msg = resp.json()["error"]["message"]
    assert "only run" in msg.lower()
    assert "remove" in msg.lower()

    # The run was NOT deleted.
    assert run_store.get(ctx, "r-only") is not None


def test_delete_run_rejects_active_run(client, registry, run_store, ctx):
    """The document's currently active run can't be deleted; replace
    it via reindex or remove the document via Remove Knowledge first."""
    _seed_doc(registry, ctx)
    # Two runs so the only-run guard doesn't fire first. The newer
    # SUCCEEDED run is the document's active.
    _seed_run(
        run_store, ctx, run_id="r-older",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    _seed_run(
        run_store, ctx, run_id="r-active",
        status=RunStatus.SUCCEEDED, started_at=_NOW,
    )

    resp = client.delete("/ingestion-runs/r-active", headers=_headers(ctx))
    assert resp.status_code == 409, resp.text
    msg = resp.json()["error"]["message"]
    # Phase 9 guard: protection comes from the active snapshot's
    # producing run, not the latest-succeeded heuristic. The message
    # now says "produced the active snapshot" — fall back to "active"
    # to keep the assertion intent-compatible with either wording.
    assert "active" in msg.lower()
    assert run_store.get(ctx, "r-active") is not None


def test_delete_run_succeeds_for_non_active_non_only_run(
    client, registry, run_store, ctx,
):
    """Happy path: a historical FAILED run can be deleted while the
    SUCCEEDED active run is preserved."""
    _seed_doc(registry, ctx)
    _seed_run(
        run_store, ctx, run_id="r-older",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=1),
    )
    _seed_run(
        run_store, ctx, run_id="r-active",
        status=RunStatus.SUCCEEDED, started_at=_NOW,
    )

    resp = client.delete("/ingestion-runs/r-older", headers=_headers(ctx))
    assert resp.status_code == 200, resp.text
    # Older gone; active preserved.
    assert run_store.get(ctx, "r-older") is None
    assert run_store.get(ctx, "r-active") is not None


def test_delete_run_404_for_unknown_run(client, ctx):
    resp = client.delete("/ingestion-runs/missing", headers=_headers(ctx))
    assert resp.status_code == 404


def test_delete_run_rejects_inflight_run(client, registry, run_store, ctx):
    """Even a non-active in-flight run can't be deleted — the workflow
    could still be writing artifacts. The service-level
    ``RunStillActive`` maps to HTTP 409."""
    _seed_doc(registry, ctx)
    _seed_run(
        run_store, ctx, run_id="r-active",
        status=RunStatus.SUCCEEDED,
        started_at=_NOW - timedelta(hours=1),
    )
    _seed_run(
        run_store, ctx, run_id="r-inflight",
        status=RunStatus.RUNNING, started_at=_NOW,
    )

    resp = client.delete(
        "/ingestion-runs/r-inflight", headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert run_store.get(ctx, "r-inflight") is not None
