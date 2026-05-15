"""Tests for the snapshot-centric Clean Up Run endpoints.

Locks down:

  * ``GET /ingestion-runs/{id}/cleanup-eligibility`` — structured
    pre-flight result the FE can consult before showing the button.
  * ``POST /ingestion-runs/{id}/clean-up`` — always 200; ``cleaned``
    flag carries the outcome; refusal reasons match the
    eligibility codes.
  * ``DELETE /ingestion-runs/{id}`` legacy wrapper — still works,
    same eligibility rules, refusal becomes HTTP 409.
  * Eligibility codes ``ACTIVE_RUN`` / ``PROCESSING_RUN`` /
    ``ONLY_RUN`` / ``RUN_NOT_FOUND`` / ``OK``.
  * On successful cleanup the run record is gone (verified via the
    run store), and the cleanup is idempotent (second call returns
    ``RUN_NOT_FOUND``).
  * Active run's data is NOT touched when cleaning a sibling.
"""

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
from j1.ingestion_review.dtos import (
    CLEANUP_REASON_ACTIVE_RUN,
    CLEANUP_REASON_OK,
    CLEANUP_REASON_ONLY_RUN,
    CLEANUP_REASON_PROCESSING_RUN,
    CLEANUP_REASON_RUN_NOT_FOUND,
)
from j1.jobs.status import ProcessingStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


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
    return TestClient(app, raise_server_exceptions=False)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(registry, ctx, *, document_id="doc-1", active_snapshot_id=None):
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=active_snapshot_id,
    ))


def _seed_run(
    run_store, ctx, *, run_id, document_id="doc-1",
    status=RunStatus.SUCCEEDED, started_at=None,
    target_snapshot_id=None,
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
        target_snapshot_id=target_snapshot_id,
        metadata={},
    ))


def _seed_snapshot(
    snapshot_store, ctx, *, snapshot_id, run_id, document_id="doc-1",
    state=SnapshotState.READY,
):
    snapshot_store.upsert(ctx, DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        created_by_run_id=run_id,
        state=state,
        created_at=_NOW,
        promoted_at=_NOW if state == SnapshotState.READY else None,
    ))


# ---- Eligibility GET ----------------------------------------------


def test_eligibility_ok_for_non_active_terminal_run(
    client, registry, run_store, snapshot_store, ctx,
):
    """Setup: two runs, one active (snap-old, r-active), one
    completed but non-active (r-old, no snapshot pointer to it).
    Eligibility for r-old should be OK."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        target_snapshot_id="snap-active",
    )
    _seed_run(
        run_store, ctx, run_id="r-old",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-active", run_id="r-active")
    resp = client.get(
        "/ingestion-runs/r-old/cleanup-eligibility",
        headers=_headers(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["allowed"] is True
    assert body["reason"] == CLEANUP_REASON_OK
    assert body["runId"] == "r-old"


def test_eligibility_refuses_active_run(
    client, registry, run_store, snapshot_store, ctx,
):
    """The run that produced the active snapshot is protected.
    Eligibility returns ``ACTIVE_RUN`` with the blocking refs the
    UI needs to render a precise message."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        target_snapshot_id="snap-active",
    )
    _seed_run(
        run_store, ctx, run_id="r-old",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-active", run_id="r-active")
    resp = client.get(
        "/ingestion-runs/r-active/cleanup-eligibility",
        headers=_headers(ctx),
    )
    body = resp.json()["data"]
    assert body["allowed"] is False
    assert body["reason"] == CLEANUP_REASON_ACTIVE_RUN
    assert body["blockingReferences"]["documentId"] == "doc-1"
    assert body["blockingReferences"]["activeRunId"] == "r-active"


def test_eligibility_refuses_processing_run(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, active_snapshot_id=None)
    _seed_run(run_store, ctx, run_id="r-other", status=RunStatus.SUCCEEDED)
    _seed_run(
        run_store, ctx, run_id="r-running",
        status=RunStatus.RUNNING,
    )
    resp = client.get(
        "/ingestion-runs/r-running/cleanup-eligibility",
        headers=_headers(ctx),
    )
    body = resp.json()["data"]
    assert body["allowed"] is False
    assert body["reason"] == CLEANUP_REASON_PROCESSING_RUN


def test_eligibility_refuses_only_run(
    client, registry, run_store, ctx,
):
    """When the run is the document's only run, the operator must
    use Remove Knowledge instead."""
    _seed_doc(registry, ctx, active_snapshot_id=None)
    _seed_run(
        run_store, ctx, run_id="r-only", status=RunStatus.FAILED,
    )
    resp = client.get(
        "/ingestion-runs/r-only/cleanup-eligibility",
        headers=_headers(ctx),
    )
    body = resp.json()["data"]
    assert body["allowed"] is False
    assert body["reason"] == CLEANUP_REASON_ONLY_RUN
    assert "Remove Knowledge" in body["message"]


def test_eligibility_not_found_for_unknown_run(client, ctx):
    resp = client.get(
        "/ingestion-runs/missing/cleanup-eligibility",
        headers=_headers(ctx),
    )
    body = resp.json()["data"]
    assert body["allowed"] is False
    assert body["reason"] == CLEANUP_REASON_RUN_NOT_FOUND


# ---- POST /clean-up ----------------------------------------------


def test_post_clean_up_succeeds_for_non_active_run(
    client, registry, run_store, snapshot_store, ctx,
):
    """Happy path: clean up a failed sibling run. HTTP 200,
    ``cleaned=true``, structured ``deletedCounts``, run gone from
    the store. The active run is untouched."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        target_snapshot_id="snap-active",
    )
    _seed_run(
        run_store, ctx, run_id="r-old",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-active", run_id="r-active")

    resp = client.post(
        "/ingestion-runs/r-old/clean-up",
        headers=_headers(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["cleaned"] is True
    assert body["reason"] == CLEANUP_REASON_OK
    assert body["runId"] == "r-old"
    # Structured counts shape (zeros are fine — no artifacts seeded).
    assert "deletedCounts" in body
    counts = body["deletedCounts"]
    for key in (
        "artifacts", "chunks", "enrichments",
        "validationResults", "snapshots", "workspaceFiles",
    ):
        assert key in counts
    assert isinstance(counts["snapshots"], int)
    # Run record is gone.
    assert run_store.get(ctx, "r-old") is None
    # Active run untouched.
    assert run_store.get(ctx, "r-active") is not None


def test_post_clean_up_refuses_active_run_without_http_error(
    client, registry, run_store, snapshot_store, ctx,
):
    """Refusal is HTTP 200 with ``cleaned=false`` (NOT 4xx). This
    is the contract: the FE renders the message without treating
    it as a network error."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        target_snapshot_id="snap-active",
    )
    _seed_run(
        run_store, ctx, run_id="r-old",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-active", run_id="r-active")

    resp = client.post(
        "/ingestion-runs/r-active/clean-up",
        headers=_headers(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["cleaned"] is False
    assert body["reason"] == CLEANUP_REASON_ACTIVE_RUN
    assert "active" in body["message"].lower()
    # Active run is intact.
    assert run_store.get(ctx, "r-active") is not None


def test_post_clean_up_refuses_processing_run(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, active_snapshot_id=None)
    _seed_run(run_store, ctx, run_id="r-other", status=RunStatus.SUCCEEDED)
    _seed_run(
        run_store, ctx, run_id="r-running",
        status=RunStatus.RUNNING,
    )
    resp = client.post(
        "/ingestion-runs/r-running/clean-up",
        headers=_headers(ctx),
    )
    body = resp.json()["data"]
    assert body["cleaned"] is False
    assert body["reason"] == CLEANUP_REASON_PROCESSING_RUN
    # Processing run is intact.
    assert run_store.get(ctx, "r-running") is not None


def test_post_clean_up_is_idempotent(
    client, registry, run_store, snapshot_store, ctx,
):
    """A second call on an already-cleaned run returns
    ``RUN_NOT_FOUND`` (cleanup is idempotent in spirit)."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    _seed_run(
        run_store, ctx, run_id="r-active",
        target_snapshot_id="snap-active",
    )
    _seed_run(
        run_store, ctx, run_id="r-old",
        status=RunStatus.FAILED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-active", run_id="r-active")

    first = client.post(
        "/ingestion-runs/r-old/clean-up",
        headers=_headers(ctx),
    )
    assert first.json()["data"]["cleaned"] is True
    second = client.post(
        "/ingestion-runs/r-old/clean-up",
        headers=_headers(ctx),
    )
    assert second.status_code == 200
    second_body = second.json()["data"]
    assert second_body["cleaned"] is False
    assert second_body["reason"] == CLEANUP_REASON_RUN_NOT_FOUND


# ---- Legacy DELETE endpoint is GONE -------------------------------


def test_legacy_delete_endpoint_no_longer_exists(client, ctx):
    """The legacy ``DELETE /ingestion-runs/{id}`` endpoint was
    removed when no repo-local consumer remained. Hits should
    return 405 (Method Not Allowed) so any straggler client gets
    a clear signal to migrate to ``POST .../clean-up``."""
    resp = client.delete(
        "/ingestion-runs/anything", headers=_headers(ctx),
    )
    assert resp.status_code == 405, resp.text
