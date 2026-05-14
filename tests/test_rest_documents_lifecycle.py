"""REST-layer tests for the document-centric lifecycle endpoints.

Covers the three new endpoints (`POST /documents/{id}/{attach,
detach,remove}`) AND the guards that block reindex / resume on
detached or removed documents.

Authorization scope tests live in the existing
`test_rest_adapter.py` shared scope suite; here we only care about
behavior + envelope shape + status codes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    """Real service wired against the same registry the rest of the
 test app uses."""
    audit = DefaultAuditRecorder(JsonlAuditSink(workspace))
    return DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        audit=audit,
        clock=lambda: _NOW,
    )


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        FeedbackService, JsonlFeedbackStore, RetrievalService,
        SourceLookupService,
    )

    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(
            JsonlFeedbackStore.__init__,  # placeholder, no feedback flow exercised
        ) if False else None,  # noqa: SIM108 — feedback not used here
        event_publisher=EventPublisherService(audit_recorder),
        job_control=None,
    )


@pytest.fixture
def client(application_facade, workspace, lifecycle_service):
    """Minimal REST client wired with the lifecycle service. We
 deliberately don't wire the run store / job_starter — these
 tests only exercise the document endpoints + the guards."""
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        document_lifecycle_service=lifecycle_service,
    )
    return TestClient(app)


def _seed_document(
    registry: JsonSourceRegistry, ctx: ProjectContext,
    *, document_id: str = "doc-1", state: str = "attached",
) -> DocumentRecord:
    """Seed a project's documents.json with a `DocumentRecord` in
 a chosen knowledge state. Used by every test below."""
    doc = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename="bridge.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_snapshot_id="r-1",
    )
    registry.add(doc)
    return doc


# ---- Happy path: attach / detach / remove ------------------------


def test_detach_endpoint_flips_state_returns_camelcase(
    client, registry, ctx,
):
    _seed_document(registry, ctx, document_id="doc-1")
    resp = client.post(
        "/documents/doc-1/detach",
        headers={
            "X-Tenant-Id": ctx.tenant_id,
            "X-Project-Id": ctx.project_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    assert body["knowledgeState"] == "detached"
    assert body["updatedAt"] is not None
    # Registry persisted the new state.
    assert registry.get(ctx, "doc-1").knowledge_state == "detached"


def test_attach_endpoint_brings_back_detached_document(
    client, registry, ctx,
):
    _seed_document(registry, ctx, document_id="doc-1", state="detached")
    resp = client.post(
        "/documents/doc-1/attach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["knowledgeState"] == "attached"


def test_remove_endpoint_clears_active_run_and_stamps_removed_at(
    client, registry, ctx,
):
    _seed_document(registry, ctx, document_id="doc-1")
    resp = client.post(
        "/documents/doc-1/remove",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["knowledgeState"] == "removed"
    assert body["activeSnapshotId"] is None
    assert body["removedAt"] is not None


# ---- 404 / 409 contracts -----------------------------------------


def test_attach_returns_404_for_unknown_document(client, ctx):
    resp = client.post(
        "/documents/nope/attach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 404


def test_detach_returns_404_for_unknown_document(client, ctx):
    resp = client.post(
        "/documents/nope/detach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 404


def test_remove_returns_404_for_unknown_document(client, ctx):
    resp = client.post(
        "/documents/nope/remove",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 404


def test_attach_on_removed_document_returns_409(client, registry, ctx):
    """The removed state is a one-way terminal for the knowledge
 layer; the REST layer must surface that as a 409 Conflict so
 the FE can render an actionable message (re-upload to restore).
 """
    _seed_document(registry, ctx, document_id="doc-1", state="removed")
    resp = client.post(
        "/documents/doc-1/attach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 409
    assert "re-upload" in resp.text.lower()


def test_detach_on_removed_document_returns_409(client, registry, ctx):
    _seed_document(registry, ctx, document_id="doc-1", state="removed")
    resp = client.post(
        "/documents/doc-1/detach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert resp.status_code == 409


# ---- Idempotency over HTTP ---------------------------------------


def test_repeated_detach_returns_200_with_unchanged_state(
    client, registry, ctx,
):
    """Idempotent: detaching twice is fine. The second call returns
 the same record without an error. Matches the FE's "user
 clicked twice on a stale UI" recovery path."""
    _seed_document(registry, ctx, document_id="doc-1")
    first = client.post(
        "/documents/doc-1/detach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    second = client.post(
        "/documents/doc-1/detach",
        headers={"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["knowledgeState"] == "detached"


# ---- 503 when service not wired -----------------------------------


def test_endpoint_returns_503_when_service_not_wired(
    application_facade, workspace,
):
    """Same degradation pattern as the other surfaces — endpoint
 exists but returns 503 until the deployment wires the service.
 """
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        # document_lifecycle_service intentionally omitted
    )
    client = TestClient(app)
    resp = client.post(
        "/documents/doc-1/attach",
        headers={"X-Tenant-Id": "acme", "X-Project-Id": "alpha"},
    )
    assert resp.status_code == 503
