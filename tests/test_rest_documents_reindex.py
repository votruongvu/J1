"""REST tests for `POST /documents/{id}/reindex`.

Covers:
 * Dispatches a NEW run under the same document_id.
 * The new run carries `runType=reindex` and `parentRunId` pointing
   at the document's previous active run.
 * `activeRunId` does NOT flip immediately — it stays pinned to the
   previous active. (The promotion happens later when the new run
   reaches a terminal state, tested in `test_documents_promotion_hook`.)
 * 409 when document is detached or removed.
 * 404 when document doesn't exist.
 * Inherits processor settings from the previous run's metadata.

REST scope tests live in the existing shared scope suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


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
            "compiler_kind": body.compiler_kind,
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
    # Capabilities default-compiler is what makes the dispatch
    # path resolve `compilerKind` server-side instead of returning
    # a 400 for missing input.
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
    registry, ctx, *, document_id: str, state: str = "attached",
    active_snapshot_id: str | None = None,
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
    run_store, ctx, *, run_id: str, document_id: str,
    status: RunStatus = RunStatus.SUCCEEDED,
    metadata: dict | None = None,
    document_version_id: str | None = None,
):
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        metadata=metadata or {},
        document_version_id=document_version_id,
    ))


# ---- Happy path -----------------------------------------------


def test_reindex_creates_new_run_under_same_document(
    client, registry, run_store, started_jobs, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-1", active_snapshot_id="r-prev")
    _seed_run(run_store, ctx, run_id="r-prev", document_id="doc-1")

    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    assert body["runType"] == "reindex"
    assert body["parentRunId"] == "r-prev"
    new_run_id = body["reindexRunId"]
    # Persisted run record carries the run_type + parent linkage.
    new_run = run_store.get(ctx, new_run_id)
    assert new_run.document_id == "doc-1"
    assert new_run.run_type == "reindex"
    assert new_run.parent_run_id == "r-prev"
    # Job starter was actually called with the new correlation id.
    assert len(started_jobs) == 1
    assert started_jobs[0]["document_id"] == "doc-1"
    assert started_jobs[0]["reindex_of"] == "r-prev"


def test_reindex_does_not_clobber_previous_active_run_id_immediately(
    client, registry, run_store, ctx,
):
    """Dispatch only — `active_run_id` stays pinned to the previous
 active. The promotion happens later when the new run reaches a
 terminal state (tested in `test_documents_promotion_hook`)."""
    _seed_doc(registry, ctx, document_id="doc-1", active_snapshot_id="r-prev")
    _seed_run(run_store, ctx, run_id="r-prev", document_id="doc-1")

    client.post("/documents/doc-1/reindex", headers=_headers(ctx))

    # Document's active_run_id is unchanged.
    assert registry.get(ctx, "doc-1").active_snapshot_id == "r-prev"


def test_reindex_inherits_settings_from_previous_run(
    client, registry, run_store, ctx,
):
    """Re-index should repeat with the same recipe — policy + mode
 inherited from the previous active run's metadata."""
    _seed_doc(registry, ctx, document_id="doc-1", active_snapshot_id="r-prev")
    _seed_run(
        run_store, ctx, run_id="r-prev", document_id="doc-1",
        metadata={"policy": "aggressive", "mode": "ENRICHED"},
        document_version_id="dv-7",
    )

    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    new_run_id = resp.json()["data"]["reindexRunId"]
    new_run = run_store.get(ctx, new_run_id)
    assert new_run.metadata.get("policy") == "aggressive"
    assert new_run.metadata.get("mode") == "ENRICHED"
    assert new_run.document_version_id == "dv-7"


def test_reindex_for_document_without_prior_run(
    client, registry, run_store, started_jobs, ctx,
):
    """A document with no active run yet (just uploaded?) can still
 be re-indexed. parent_run_id ends up None; the new run uses
 deployment defaults for settings."""
    _seed_doc(registry, ctx, document_id="doc-1", active_snapshot_id=None)

    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["parentRunId"] is None
    new_run = run_store.get(ctx, body["reindexRunId"])
    assert new_run.run_type == "reindex"
    assert new_run.parent_run_id is None


# ---- State guards -------------------------------------------


def test_reindex_returns_409_on_detached_document(client, registry, ctx):
    _seed_doc(
        registry, ctx, document_id="doc-1",
        state="detached", active_snapshot_id="r-prev",
    )
    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert "detached" in resp.text.lower()


def test_reindex_returns_409_on_removed_document(client, registry, ctx):
    _seed_doc(
        registry, ctx, document_id="doc-1",
        state="removed", active_snapshot_id=None,
    )
    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert "remove" in resp.text.lower() or "re-upload" in resp.text.lower()


def test_reindex_returns_404_for_unknown_document(client, ctx):
    resp = client.post(
        "/documents/nope/reindex", headers=_headers(ctx),
    )
    assert resp.status_code == 404


def test_reindex_returns_409_when_previous_run_is_still_active(
    client, registry, run_store, ctx,
):
    """If the previous active run is still running / paused, refuse
 to start a reindex — can't have two attempts writing artifacts
 for the same document concurrently."""
    _seed_doc(registry, ctx, document_id="doc-1", active_snapshot_id="r-running")
    _seed_run(
        run_store, ctx, run_id="r-running", document_id="doc-1",
        status=RunStatus.RUNNING,
    )
    resp = client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert resp.status_code == 409
    assert "running" in resp.text.lower() or "cancel" in resp.text.lower()
