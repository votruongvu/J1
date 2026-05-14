"""REST-layer tests for the imported test cases endpoints.

These cover the new document-scoped surface introduced in the
2026-05-14 product change:

  * POST   /documents/{id}/imported-test-cases/import   (multipart CSV)
  * GET    /documents/{id}/imported-test-cases
  * DELETE /documents/{id}/imported-test-cases
  * POST   /documents/{id}/imported-test-cases/execute
  * GET    /documents/{id}/imported-test-cases/execution

Authorization / scope tests live in the shared scope suite; here we
focus on contract, status codes, and the replace-on-import semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.artifacts.registry import JsonArtifactRegistry
from j1.documents.models import DocumentRecord
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation import (
    ImportedTestCaseExecutor,
    IngestionValidationService,
    JsonlImportedTestCaseStore,
)


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


class _FakeOrchestrator:
    """Returns the same canned answer to every call. Enough to make
    the import-then-execute round trip exercise the wire shape."""

    def __init__(self):
        self.calls = []

    def run(self, request):
        self.calls.append(request)
        trace = SimpleNamespace(
            selected_evidence=(),
            evidence_groups=(),
            citations=(SimpleNamespace(
                candidate=SimpleNamespace(
                    artifact_id="a-1",
                    chunk_id=None,
                    document_id="doc-1",
                    run_id="run-1",
                    artifact_kind="chunk",
                    extra={},
                    score=0.8,
                ),
            ),),
        )
        return SimpleNamespace(
            answer="An answer.",
            citations=trace.citations,
            trace=trace,
        )


@pytest.fixture
def headers(ctx: ProjectContext) -> dict[str, str]:
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def validation_service(workspace, run_store):
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=JsonArtifactRegistry(workspace),
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        workspace=workspace,
        imported_test_case_store=JsonlImportedTestCaseStore(workspace),
        imported_test_case_executor=ImportedTestCaseExecutor(
            smart_query_orchestrator=_FakeOrchestrator(),
            run_store=run_store,
        ),
        smart_query_orchestrator=_FakeOrchestrator(),
    )


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade,
        CitationLookupService,
        DocumentIngestionService,
        EventPublisherService,
        RetrievalService,
        SourceLookupService,
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
def client(application_facade, workspace, validation_service):
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        validation_service=validation_service,
    )
    return TestClient(app)


def _seed_document(registry, ctx, document_id: str = "doc-1") -> None:
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
    ))


def _seed_succeeded_run(run_store, ctx, run_id: str, document_id: str) -> None:
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
    ))


# ---- POST /import -------------------------------------------------


def test_import_post_replaces_prior_set(client, registry, ctx, headers):
    _seed_document(registry, ctx)
    first = client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("a.csv", BytesIO(
            b"question\nQ1\nQ2\nQ3\n",
        ), "text/csv")},
    )
    assert first.status_code == 201, first.text
    payload = first.json()["data"]
    assert payload["documentId"] == "doc-1"
    assert [c["question"] for c in payload["cases"]] == ["Q1", "Q2", "Q3"]

    # Re-import wipes the prior set entirely.
    second = client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("b.csv", BytesIO(b"question\nOnly\n"), "text/csv")},
    )
    assert second.status_code == 201
    payload2 = second.json()["data"]
    assert [c["question"] for c in payload2["cases"]] == ["Only"]


def test_import_post_rejects_csv_without_question_column(
    client, registry, ctx, headers,
):
    _seed_document(registry, ctx)
    resp = client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("bad.csv", BytesIO(b"something\nfoo\n"), "text/csv")},
    )
    assert resp.status_code == 400
    assert "question" in resp.text.lower()


# ---- GET / DELETE -------------------------------------------------


def test_get_returns_404_when_no_set_exists(client, registry, ctx, headers):
    _seed_document(registry, ctx)
    resp = client.get(
        "/documents/doc-1/imported-test-cases", headers=headers,
    )
    assert resp.status_code == 404


def test_get_returns_imported_set_after_import(
    client, registry, ctx, headers,
):
    _seed_document(registry, ctx)
    client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("a.csv", BytesIO(b"question\nQ1\n"), "text/csv")},
    )
    resp = client.get(
        "/documents/doc-1/imported-test-cases", headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    assert body["cases"][0]["question"] == "Q1"


def test_delete_clears_the_set_and_is_idempotent(
    client, registry, ctx, headers,
):
    _seed_document(registry, ctx)
    client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("a.csv", BytesIO(b"question\nQ1\n"), "text/csv")},
    )
    first = client.delete(
        "/documents/doc-1/imported-test-cases", headers=headers,
    )
    assert first.status_code == 204
    # GET now 404.
    assert client.get(
        "/documents/doc-1/imported-test-cases", headers=headers,
    ).status_code == 404
    # Second DELETE is still 204 — idempotent.
    second = client.delete(
        "/documents/doc-1/imported-test-cases", headers=headers,
    )
    assert second.status_code == 204


# ---- POST /execute ------------------------------------------------


def test_execute_404_when_no_set_imported(
    client, registry, ctx, headers,
):
    _seed_document(registry, ctx)
    resp = client.post(
        "/documents/doc-1/imported-test-cases/execute",
        headers=headers,
    )
    # The service raises ReviewNotFound, which the global handler
    # maps to 404 — matches every other "missing resource" surface.
    assert resp.status_code == 404


def test_execute_404_when_no_succeeded_run(
    client, registry, ctx, headers,
):
    _seed_document(registry, ctx)
    client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("a.csv", BytesIO(b"question\nQ1\n"), "text/csv")},
    )
    resp = client.post(
        "/documents/doc-1/imported-test-cases/execute",
        headers=headers,
    )
    assert resp.status_code == 404


def test_execute_returns_execution_snapshot_and_get_returns_same(
    client, registry, run_store, ctx, headers,
):
    _seed_document(registry, ctx)
    _seed_succeeded_run(run_store, ctx, "run-1", "doc-1")
    client.post(
        "/documents/doc-1/imported-test-cases/import",
        headers=headers,
        files={"file": ("a.csv", BytesIO(b"question\nQ1\n"), "text/csv")},
    )
    resp = client.post(
        "/documents/doc-1/imported-test-cases/execute",
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["documentId"] == "doc-1"
    assert body["runId"] == "run-1"
    assert body["summary"]["total"] == 1
    assert body["results"][0]["status"] == "answered"

    # GET returns the same snapshot.
    later = client.get(
        "/documents/doc-1/imported-test-cases/execution",
        headers=headers,
    )
    assert later.status_code == 200
    assert later.json()["data"]["runId"] == "run-1"
