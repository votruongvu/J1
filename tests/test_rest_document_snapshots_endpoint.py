"""Tests for ``GET /documents/{id}/snapshots``.

Backs the snapshot-centric Document Detail UI: the FE needs per-
snapshot state to render the Candidate Knowledge section with
``BUILDING / READY / SUPERSEDED / FAILED`` badges. The list is
derived from the snapshot store, ordered most-recent first, and
the document's currently active snapshot is flagged via
``isActive``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.documents.snapshot import DocumentSnapshot, SnapshotState
from j1.documents.snapshot_service import DocumentSnapshotService
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.ingestion_review import IngestionResultReviewService
from j1.jobs.status import ProcessingStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def snapshot_store(workspace):
    return JsonlDocumentSnapshotStore(workspace)


@pytest.fixture
def snapshot_service(snapshot_store):
    return DocumentSnapshotService(store=snapshot_store)


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
def client(
    application_facade, workspace, run_store, lifecycle_service,
    review_service, snapshot_service,
):
    from j1.integration.dto import ProcessingCapabilities
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        snapshot_service=snapshot_service,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app)


@pytest.fixture
def client_no_snapshot_service(
    application_facade, workspace, run_store, lifecycle_service,
    review_service,
):
    """Variant without snapshot_service wired — verifies the
    endpoint returns 503 instead of 500/silent empty."""
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


def _seed_doc(registry, ctx, *, active_snapshot_id):
    registry.add(DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="doc-1.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum="sha256:doc-1",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_snap(
    snapshot_store, ctx, *, snapshot_id, run_id, state,
    created_at=None, promoted_at=None, superseded_at=None,
):
    snapshot_store.upsert(ctx, DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id="doc-1",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        created_by_run_id=run_id,
        state=state,
        created_at=created_at or _NOW,
        promoted_at=promoted_at,
        superseded_at=superseded_at,
    ))


def test_get_snapshots_returns_active_and_history(
    client, registry, snapshot_store, ctx,
):
    """Endpoint returns every snapshot for the document, most recent
    first, with the document's currently active snapshot marked
    ``isActive=True``. State values mirror the SnapshotState enum."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-2")
    # snap-1: older, superseded; snap-2: active; snap-3: candidate (BUILDING).
    _seed_snap(
        snapshot_store, ctx, snapshot_id="snap-1", run_id="r-1",
        state=SnapshotState.SUPERSEDED,
        created_at=_NOW - timedelta(hours=2),
        promoted_at=_NOW - timedelta(hours=2),
        superseded_at=_NOW - timedelta(hours=1),
    )
    _seed_snap(
        snapshot_store, ctx, snapshot_id="snap-2", run_id="r-2",
        state=SnapshotState.READY,
        created_at=_NOW - timedelta(hours=1),
        promoted_at=_NOW - timedelta(hours=1),
    )
    _seed_snap(
        snapshot_store, ctx, snapshot_id="snap-3", run_id="r-3",
        state=SnapshotState.BUILDING,
        created_at=_NOW,
    )

    resp = client.get("/documents/doc-1/snapshots", headers=_headers(ctx))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    snaps = body["snapshots"]
    assert len(snaps) == 3
    # Most recent first.
    assert [s["snapshotId"] for s in snaps] == ["snap-3", "snap-2", "snap-1"]
    # Active flag is set only on snap-2.
    actives = [s for s in snaps if s["isActive"]]
    assert len(actives) == 1
    assert actives[0]["snapshotId"] == "snap-2"
    # State values are the snake-cased SnapshotState string.
    states = {s["snapshotId"]: s["state"] for s in snaps}
    assert states == {
        "snap-3": "building",
        "snap-2": "ready",
        "snap-1": "superseded",
    }


def test_get_snapshots_returns_empty_for_document_with_no_snapshots(
    client, registry, ctx,
):
    """Document exists but no snapshots yet (pre-first-reindex
    state). Endpoint returns an empty list, not 404."""
    _seed_doc(registry, ctx, active_snapshot_id=None)

    resp = client.get("/documents/doc-1/snapshots", headers=_headers(ctx))
    assert resp.status_code == 200
    assert resp.json()["data"]["snapshots"] == []


def test_get_snapshots_404_for_unknown_document(client, ctx):
    resp = client.get("/documents/missing/snapshots", headers=_headers(ctx))
    assert resp.status_code == 404


def test_get_snapshots_503_when_snapshot_service_not_wired(
    client_no_snapshot_service, registry, ctx,
):
    """Deployments that don't wire the snapshot service get a clear
    503 (not a silent empty list); the FE renders a "not available"
    state instead of pretending there are no snapshots."""
    _seed_doc(registry, ctx, active_snapshot_id=None)

    resp = client_no_snapshot_service.get(
        "/documents/doc-1/snapshots", headers=_headers(ctx),
    )
    assert resp.status_code == 503
