"""REST tests for `GET /documents`, `GET /documents/{id}`,
`GET /documents/{id}/runs`.

End-to-end coverage from the registry/run-store all the way to the
camelCase JSON envelope. The projector's matrix is exercised in
test_documents_projector.py — here we focus on the REST contract
(status codes, envelope shape, query params, wiring degradation).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.documents.models import DocumentRecord
from j1.intake.registry import JsonSourceRegistry
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
def client(application_facade, workspace, run_store):
    """Read-side client with the run store wired so the projector
 can find runs for each document."""
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
    )
    return TestClient(app)


def _seed_doc(
    registry: JsonSourceRegistry, ctx: ProjectContext,
    *, document_id: str, state: str = "attached",
    active_run_id: str | None = "r-1",
) -> None:
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
        active_run_id=active_run_id,
    ))


def _seed_run(
    store: JsonlIngestionRunStore, ctx: ProjectContext,
    *, run_id: str, document_id: str,
    status: RunStatus = RunStatus.SUCCEEDED,
) -> None:
    store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=_NOW,
        updated_at=_NOW,
    ))


def _headers(ctx: ProjectContext) -> dict[str, str]:
    return {
        "X-Tenant-Id": ctx.tenant_id,
        "X-Project-Id": ctx.project_id,
    }


# ---- GET /documents ----------------------------------------------


def test_list_documents_returns_camelcase_envelope(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-1")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1")
    resp = client.get("/documents", headers=_headers(ctx))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert "documents" in body
    assert len(body["documents"]) == 1
    row = body["documents"][0]
    assert row["documentId"] == "doc-1"
    assert row["displayName"] == "doc-1.pdf"
    assert row["knowledgeState"] == "attached"
    assert row["activeRunId"] == "r-1"
    assert "availableActions" in row
    assert "view" in row["availableActions"]
    assert "currentResultSummary" in row
    assert row["currentResultSummary"]["status"] == "succeeded"


def test_list_excludes_removed_documents_by_default(
    client, registry, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-keep")
    _seed_doc(registry, ctx, document_id="doc-gone", state="removed",
              active_run_id=None)
    resp = client.get("/documents", headers=_headers(ctx))
    ids = {d["documentId"] for d in resp.json()["data"]["documents"]}
    assert ids == {"doc-keep"}


def test_list_includes_removed_documents_when_query_param_set(
    client, registry, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-keep")
    _seed_doc(registry, ctx, document_id="doc-gone", state="removed",
              active_run_id=None)
    resp = client.get(
        "/documents?includeRemoved=true", headers=_headers(ctx),
    )
    ids = {d["documentId"] for d in resp.json()["data"]["documents"]}
    assert ids == {"doc-keep", "doc-gone"}


def test_list_action_matrix_per_state(client, registry, run_store, ctx):
    _seed_doc(registry, ctx, document_id="doc-att", state="attached")
    _seed_doc(registry, ctx, document_id="doc-det", state="detached")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-att")
    _seed_run(run_store, ctx, run_id="r-2", document_id="doc-det")
    resp = client.get("/documents", headers=_headers(ctx))
    rows = {d["documentId"]: d for d in resp.json()["data"]["documents"]}
    attached_actions = set(rows["doc-att"]["availableActions"])
    detached_actions = set(rows["doc-det"]["availableActions"])
    assert "detach" in attached_actions
    assert "attach" not in attached_actions
    assert "attach" in detached_actions
    assert "detach" not in detached_actions
    assert "reindex" not in detached_actions  # spec rule


# ---- GET /documents/{id} -----------------------------------------


def test_detail_returns_full_run_history(client, registry, run_store, ctx):
    _seed_doc(registry, ctx, document_id="doc-1")
    for i in range(5):
        _seed_run(
            run_store, ctx, run_id=f"r-{i}", document_id="doc-1",
        )
    # `/detail` is the document-centric path. `/documents/{id}` stays
    # the existing upload-metadata endpoint for backward compat;
    # they coexist during the document-centric migration.
    resp = client.get(
        "/documents/doc-1/detail", headers=_headers(ctx),
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    # Detail shows the full history; summary caps at 3.
    assert "runHistory" in body
    assert len(body["runHistory"]) == 5


def test_detail_returns_404_for_unknown_document(client, ctx):
    resp = client.get(
        "/documents/missing/detail", headers=_headers(ctx),
    )
    assert resp.status_code == 404


# ---- GET /documents/{id}/runs ------------------------------------


def test_runs_endpoint_returns_history_sorted_descending(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-1")
    # Stage runs with distinct start times so the sort is exercised.
    for i in range(3):
        store_run = IngestionRun(
            run_id=f"r-{i}",
            document_id="doc-1",
            workflow_id=f"wf-{i}",
            workflow_run_id=None,
            status=RunStatus.SUCCEEDED,
            started_at=datetime(2026, 5, 12, 12, i, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 12, 12, i, tzinfo=timezone.utc),
        )
        run_store.upsert(ctx, store_run)
    resp = client.get(
        "/documents/doc-1/runs", headers=_headers(ctx),
    )
    runs = resp.json()["data"]["runs"]
    assert [r["runId"] for r in runs] == ["r-2", "r-1", "r-0"]


def test_runs_endpoint_marks_active_run(
    client, registry, run_store, ctx,
):
    _seed_doc(registry, ctx, document_id="doc-1", active_run_id="r-2")
    _seed_run(run_store, ctx, run_id="r-1", document_id="doc-1")
    _seed_run(run_store, ctx, run_id="r-2", document_id="doc-1")
    resp = client.get(
        "/documents/doc-1/runs", headers=_headers(ctx),
    )
    runs = {r["runId"]: r for r in resp.json()["data"]["runs"]}
    assert runs["r-2"]["isActive"] is True
    assert runs["r-1"]["isActive"] is False


def test_runs_endpoint_returns_404_for_unknown_document(client, ctx):
    resp = client.get(
        "/documents/missing/runs", headers=_headers(ctx),
    )
    assert resp.status_code == 404
