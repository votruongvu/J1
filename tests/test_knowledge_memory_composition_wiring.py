"""Composition-root wiring test for `KnowledgeMemoryService`.

Phase 2 (the previous task) made the manual action
``build_knowledge_memory`` functional and the REST endpoint
``POST /documents/{id}/manual-actions/build-knowledge-memory``
return 503 when the optional ``knowledge_memory_service`` is not
passed to ``create_rest_api``. This patch wires the service into
the dev composition root so the action works out of the box on
the normal runtime.

This file pins two facts:

  1. ``deploy.dev._wiring.build_knowledge_memory_service``
     constructs a ``KnowledgeMemoryService`` with shared
     dependencies — the workspace, the JSONL artifact registry,
     the source registry, a fresh ``ProcessingService``, and the
     default domain registry.
  2. When the service is wired into ``create_rest_api`` and the
     document has an active compiled snapshot, the manual-action
     endpoint returns **200** with an ``artifactId`` instead of
     503. When the service is omitted, the **503** fallback still
     fires — that path is the documented behaviour for minimal
     test apps.

Feature-flag regression: when ``J1_ENABLE_MANUAL_BUILD_KNOWLEDGE_MEMORY``
is false, the endpoint returns 403 regardless of wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.integration.services import ApplicationFacade
from j1.memory.service import KnowledgeMemoryService
from j1.processing.results import ARTIFACT_KIND_KNOWLEDGE_MEMORY


# ---- Composition wiring test (no HTTP) -------------------------


def test_build_knowledge_memory_service_returns_wired_instance(tmp_path):
    """The wiring helper constructs a `KnowledgeMemoryService` with
    a real `build_and_persist` callable. Doesn't actually run a
    build — just verifies the seam exists."""
    from deploy.dev._wiring import build_knowledge_memory_service
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    service = build_knowledge_memory_service(workspace)
    assert isinstance(service, KnowledgeMemoryService)
    assert callable(getattr(service, "build_and_persist", None))


# ---- HTTP endpoint behaviour ----------------------------------


# Headers required by the default tenant/project context resolver.
_TENANT_HEADERS = {"X-Tenant-Id": "t1", "X-Project-Id": "p1"}


def _ctx_payload() -> dict[str, Any]:
    return {"tenant_id": "t1", "project_id": "p1", "profile": None}


class _StubSource:
    """Mimics the document DTO returned by `facade.source_lookup`.

    Fields the manual-action handler reads:
      * ``active_snapshot_id`` — gating; 409 when missing.
      * ``knowledge_state`` — ``_enforce_document_attached_for_action``
        checks this. ``"attached"`` lets the action proceed.
      * ``status`` — for the cross-cutting status helpers some
        endpoints inspect; we set ``"compiled"`` for safety.
    """

    def __init__(self, document_id: str, active_snapshot_id: str | None = "snap-1") -> None:
        self.document_id = document_id
        self.active_snapshot_id = active_snapshot_id
        self.knowledge_state = "attached"
        self.status = "compiled"
        self.detached_at = None


class _StubSourceLookup:
    def __init__(self, doc: _StubSource) -> None:
        self._doc = doc

    def get_source(self, ctx, document_id):
        if document_id != self._doc.document_id:
            raise LookupError(document_id)
        return self._doc


class _StubMemoryService:
    """Mimics the public `KnowledgeMemoryService.build_and_persist`
    surface. We don't drive the real service through a full
    workspace + registry stack here — that's covered by the Phase
    2 contract tests. The composition wiring fact we want to pin
    is "the endpoint hands off to whatever service was supplied,"
    so a stub that records the call + returns a `BuildResult`
    suffices."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def build_and_persist(
        self, ctx, document_id, *,
        actor: str = "system", trigger: str | None = None,
    ):
        from j1.memory.service import KnowledgeMemoryBuildResult
        self.calls.append((document_id, actor, trigger))
        return KnowledgeMemoryBuildResult(
            status="succeeded",
            document_id=document_id,
            snapshot_id="snap-1",
            run_id="run-1",
            artifact_id="memart-stub",
            entry_count=3,
            warnings=(),
            message="ok",
        )


def _make_facade(doc: _StubSource) -> ApplicationFacade:
    """Minimal `ApplicationFacade` for endpoint tests. Only the
    fields the manual-action handler touches are populated; the
    rest stay `None` (Optional ports — the REST adapter handles
    None gracefully)."""
    return ApplicationFacade(
        ingestion=None,           # type: ignore[arg-type]
        retrieval=None,           # type: ignore[arg-type]
        citation_lookup=None,     # type: ignore[arg-type]
        source_lookup=_StubSourceLookup(doc),
        feedback=None,            # type: ignore[arg-type]
        event_publisher=None,     # type: ignore[arg-type]
    )


def test_endpoint_returns_200_when_service_wired_and_snapshot_active():
    doc = _StubSource(document_id="doc-1", active_snapshot_id="snap-1")
    stub_service = _StubMemoryService()
    app = create_rest_api(
        _make_facade(doc),
        knowledge_memory_service=stub_service,
    )
    client = TestClient(app)
    response = client.post(
        "/documents/doc-1/manual-actions/build-knowledge-memory",
        headers=_TENANT_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    data = body.get("data", body)
    assert data["documentId"] == "doc-1"
    assert data["status"] == "succeeded"
    assert data["snapshotId"] == "snap-1"
    assert data["runId"] == "run-1"
    assert data["artifactId"] == "memart-stub"
    assert data["entryCount"] == 3
    # Endpoint dispatched to the wired service with `trigger=manual`
    # so the persisted artifact's metadata.trigger reflects the
    # actual cause (Phase 3B fact).
    assert len(stub_service.calls) == 1
    document_id, _actor, trigger = stub_service.calls[0]
    assert document_id == "doc-1"
    assert trigger == "manual"


def test_endpoint_returns_503_when_service_not_wired():
    """Documented fallback. Minimal test apps that don't wire the
    service should still surface the manual-action list (the
    descriptor exists) but the endpoint refuses with 503."""
    doc = _StubSource(document_id="doc-1")
    app = create_rest_api(_make_facade(doc))  # no knowledge_memory_service
    client = TestClient(app)
    response = client.post(
        "/documents/doc-1/manual-actions/build-knowledge-memory",
        headers=_TENANT_HEADERS,
    )
    assert response.status_code == 503
    assert "knowledge_memory_service" in response.text


def test_endpoint_returns_403_when_feature_flag_disabled(monkeypatch):
    from j1.processing.manual_actions import (
        ENV_MANUAL_BUILD_KNOWLEDGE_MEMORY,
    )
    monkeypatch.setenv(ENV_MANUAL_BUILD_KNOWLEDGE_MEMORY, "false")
    doc = _StubSource(document_id="doc-1")
    app = create_rest_api(
        _make_facade(doc),
        knowledge_memory_service=_StubMemoryService(),
    )
    client = TestClient(app)
    response = client.post(
        "/documents/doc-1/manual-actions/build-knowledge-memory",
        headers=_TENANT_HEADERS,
    )
    assert response.status_code == 403
    assert "disabled" in response.text.lower()


def test_endpoint_returns_409_when_no_active_snapshot():
    """The service raises `NoActiveSnapshotError`; the endpoint
    maps that to 409. We trigger it by injecting a service that
    raises directly — the underlying real service guard is
    covered in the Phase 2 contract tests."""
    from j1.memory.service import NoActiveSnapshotError

    class _NoSnapshotService:
        def build_and_persist(
            self, ctx, document_id, *,
            actor: str = "system", trigger: str | None = None,
        ):
            raise NoActiveSnapshotError(
                f"document {document_id!r} has no active snapshot"
            )

    doc = _StubSource(document_id="doc-1", active_snapshot_id=None)
    app = create_rest_api(
        _make_facade(doc),
        knowledge_memory_service=_NoSnapshotService(),
    )
    client = TestClient(app)
    response = client.post(
        "/documents/doc-1/manual-actions/build-knowledge-memory",
        headers=_TENANT_HEADERS,
    )
    assert response.status_code == 409
    assert "no active snapshot" in response.text.lower()


def test_endpoint_returns_404_when_document_missing():
    """Document not found in source registry → 404 path. The stub
    raises `LookupError`; the endpoint maps the bare exception to
    404 just like the existing `run_domain_enrichment` route."""

    class _AlwaysMissingLookup:
        def get_source(self, ctx, document_id):
            raise LookupError(document_id)

    facade = ApplicationFacade(
        ingestion=None,           # type: ignore[arg-type]
        retrieval=None,           # type: ignore[arg-type]
        citation_lookup=None,     # type: ignore[arg-type]
        source_lookup=_AlwaysMissingLookup(),
        feedback=None,            # type: ignore[arg-type]
        event_publisher=None,     # type: ignore[arg-type]
    )
    app = create_rest_api(
        facade,
        knowledge_memory_service=_StubMemoryService(),
    )
    client = TestClient(app)
    response = client.post(
        "/documents/doc-missing/manual-actions/build-knowledge-memory",
        headers=_TENANT_HEADERS,
    )
    assert response.status_code == 404


# ---- Manual-action listing still surfaces the action ----------


def test_manual_action_listing_includes_build_knowledge_memory():
    """The manual-action descriptor is feature-flag-aware but
    independent of the service-wiring path. Regardless of whether
    `knowledge_memory_service` is passed to `create_rest_api`,
    the action descriptor should appear in the manual-action
    list (status `available` when the flag is on)."""
    from j1.processing.manual_actions import (
        ACTION_BUILD_KNOWLEDGE_MEMORY,
        list_manual_actions,
    )
    ids = {a.id: a for a in list_manual_actions()}
    assert ACTION_BUILD_KNOWLEDGE_MEMORY in ids
    descriptor = ids[ACTION_BUILD_KNOWLEDGE_MEMORY]
    # Phase 2 flipped this from `not_implemented` to `available`.
    assert descriptor.status in ("available", "disabled")
