"""Tests for `processing_capabilities=` on `create_rest_api`.

When supplied, the API:

 * Defaults an omitted `compilerKind` request field to
 `default_compiler_kind`.
 * Validates `compilerKind` / `graphBuilderKind` / `enricherKind` /
 `indexerKind` against the registered set, returning 400 with an
 actionable message instead of letting unknown kinds surface as a
 workflow `UnknownProcessorError` 5 seconds later.

When omitted, the API behaves as before — `compilerKind` is required
in the request body and no validation is performed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.documents.models import DocumentRecord
from j1.integration import (
    ApplicationFacade,
    DocumentIngestionService,
    EventPublisherService,
    ProcessingCapabilities,
    SourceLookupService,
    capabilities_from_bootstrap,
)
from j1.integration.dto import (
    DocumentDTO,
    EventDTO,
    FeedbackResultDTO,
)
from j1.integration.feedback import FeedbackRecord
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


# ---- Minimal helpers ------------------------------------------------


def _headers(tenant: str = "acme", project: str = "alpha") -> dict[str, str]:
    return {TENANT_HEADER: tenant, PROJECT_HEADER: project}


def _ctx(tenant: str = "acme", project: str = "alpha") -> ProjectContext:
    return ProjectContext(tenant_id=tenant, project_id=project)


def _add_document(registry, document_id: str = "doc-1") -> None:
    registry.add(DocumentRecord(
        document_id=document_id, project=_ctx(), original_filename="x.txt",
        stored_filename=f"{document_id}.txt", mime_type="text/plain",
        file_size=1, checksum="sha256:0",
        status=ProcessingStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))


@pytest.fixture
def started_jobs() -> list[tuple[str, str, str | None]]:
    """Captures (project_id, document_id, compiler_kind) per job_starter call."""
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append((ctx.project_id, document_id, body.compiler_kind))
        return f"job-{document_id}"

    return starter


class _MockTemporalClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, Any]] = []

    async def start_workflow(self, fn, arg, *, id, task_queue, **kwargs):
        self.started.append((id, arg))
        class _H:
            id = "wf-1"
            async def signal(self, name, *a, **k): pass
        return _H()

    def get_workflow_handle(self, workflow_id):
        class _H:
            id = workflow_id
            async def signal(self, name, *a, **k): pass
        return _H()


@pytest.fixture
def mock_temporal() -> _MockTemporalClient:
    return _MockTemporalClient()


@pytest.fixture
def minimal_facade(intake_service, registry, mock_temporal, audit_recorder):
    """Smallest facade that supports the two endpoints under test."""
    from j1.integration import TemporalJobControlService

    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=None,
        citation_lookup=None,
        source_lookup=SourceLookupService(registry),
        feedback=None,
        event_publisher=EventPublisherService(audit_recorder),
        job_control=TemporalJobControlService(
            client_provider=lambda: mock_temporal,
            task_queue="j1-test",
            workflow_id_factory=lambda ctx: f"wf-{ctx.project_id}",
        ),
    )


# ---- Per-document /documents/{id}/ingest ---------------------------


def test_ingest_defaults_compiler_kind_when_capabilities_set(
    minimal_facade, job_starter, started_jobs, workspace, registry,
):
    """Omitting `compilerKind` falls back to `default_compiler_kind`."""
    _add_document(registry)
    capabilities = ProcessingCapabilities(
        default_compiler_kind="raganything",
        compiler_kinds=frozenset({"raganything", "mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        job_starter=job_starter,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/documents/doc-1/ingest",
        json={"actor": "tester"},  # NB: no compilerKind
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert started_jobs == [("alpha", "doc-1", "raganything")]


def test_ingest_rejects_unknown_compiler_kind_when_capabilities_set(
    minimal_facade, job_starter, workspace, registry,
):
    _add_document(registry)
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        job_starter=job_starter,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/documents/doc-1/ingest",
        json={"compilerKind": "raganything"},  # not registered
        headers=_headers(),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert "unknown compilerKind 'raganything'" in body["error"]["message"]
    assert "mock" in body["error"]["message"]  # lists the registered kinds


def test_ingest_accepts_provided_compiler_kind_in_registered_set(
    minimal_facade, job_starter, started_jobs, workspace, registry,
):
    _add_document(registry)
    capabilities = ProcessingCapabilities(
        default_compiler_kind="raganything",
        compiler_kinds=frozenset({"raganything", "mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        job_starter=job_starter,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/documents/doc-1/ingest",
        json={"compilerKind": "mock"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert started_jobs == [("alpha", "doc-1", "mock")]


def test_ingest_validates_optional_graph_builder_kind(
    minimal_facade, job_starter, workspace, registry,
):
    _add_document(registry)
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
        graph_builder_kinds=frozenset({"mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        job_starter=job_starter,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/documents/doc-1/ingest",
        json={"graphBuilderKind": "graphify"},  # not registered
        headers=_headers(),
    )
    assert response.status_code == 400
    body = response.json()
    assert "unknown graphBuilderKind 'graphify'" in body["error"]["message"]


# ---- Project-wide /ingestion-jobs ----------------------------------


def test_ingestion_job_defaults_compiler_kind_when_capabilities_set(
    minimal_facade, mock_temporal, workspace,
):
    capabilities = ProcessingCapabilities(
        default_compiler_kind="raganything",
        compiler_kinds=frozenset({"raganything", "mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/ingestion-jobs",
        json={"actor": "tester"},  # no compilerKind
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert mock_temporal.started, "no workflow was started"
    _, started_request = mock_temporal.started[0]
    assert started_request.compiler_kind == "raganything"


def test_ingestion_job_rejects_unknown_compiler_kind_when_capabilities_set(
    minimal_facade, workspace,
):
    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )
    app = create_rest_api(
        minimal_facade,
        workspace=workspace,
        processing_capabilities=capabilities,
    )
    client = TestClient(app)

    response = client.post(
        "/ingestion-jobs",
        json={"compilerKind": "raganything"},
        headers=_headers(),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert "unknown compilerKind 'raganything'" in body["error"]["message"]


# ---- Backward compat: no capabilities passed -----------------------


def test_no_capabilities_skips_validation_but_still_requires_compiler_kind(
    minimal_facade, job_starter, started_jobs, workspace, registry,
):
    """When `processing_capabilities` is omitted (legacy wiring), the
 API requires `compilerKind` in the body and forwards anything
 provided without validation against a registered set."""
    _add_document(registry)
    app = create_rest_api(
        minimal_facade,
        job_starter=job_starter,
        workspace=workspace,
        # processing_capabilities= NOT passed
    )
    client = TestClient(app)

    response = client.post(
        "/documents/doc-1/ingest",
        json={"compilerKind": "anything-goes"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert started_jobs == [("alpha", "doc-1", "anything-goes")]


# ---- capabilities_from_bootstrap helper ----------------------------


def test_capabilities_from_bootstrap_constructs_correctly():
    class _FakeSelection:
        compiler = "mock"

    class _FakeBoot:
        selection = _FakeSelection()
        compilers = {"mock": object(), "raganything": object()}
        graph_builders = {"mock": object()}

    caps = capabilities_from_bootstrap(_FakeBoot())
    assert caps.default_compiler_kind == "mock"
    assert caps.compiler_kinds == frozenset({"mock", "raganything"})
    assert caps.graph_builder_kinds == frozenset({"mock"})
    assert caps.enricher_kinds == frozenset()
    assert caps.indexer_kinds == frozenset()


def test_capabilities_from_bootstrap_accepts_explicit_enricher_indexer_kinds():
    class _FakeSelection:
        compiler = "mock"

    class _FakeBoot:
        selection = _FakeSelection()
        compilers = {"mock": object()}
        graph_builders = {"mock": object()}

    caps = capabilities_from_bootstrap(
        _FakeBoot(),
        enricher_kinds=frozenset({"summary", "extract"}),
        indexer_kinds=frozenset({"sqlite-fts"}),
    )
    assert caps.enricher_kinds == frozenset({"summary", "extract"})
    assert caps.indexer_kinds == frozenset({"sqlite-fts"})
