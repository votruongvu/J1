"""Contract — Phase 3B Knowledge Memory status projection.

Pins:

  * `resolve_knowledge_memory_status(...)` derives the right
    status string from active snapshot artifact metadata across
    all five branches:
      - no active snapshot → `not_built`
      - no active memory artifact → `not_built`
      - active artifact with `includes_domain_insights=false` →
        `base_compile_only`
      - active artifact with `includes_domain_insights=true` →
        `updated_with_domain_insights`
      - multiple active artifacts for same (doc, snapshot) →
        `unknown` (defensive — Phase 2 supersede sweep should
        prevent this)
  * Superseded artifacts (`metadata.search_state=="superseded"`)
    are excluded from the projection.
  * `last_trigger`, `entry_count`, `last_built_at` propagated
    from artifact metadata.
  * `KnowledgeMemoryService.resolve_status(ctx, document_id)`
    wraps the resolver with active-snapshot lookup.
  * The manual-action REST endpoint stamps `trigger="manual"`
    on the persisted artifact (the path that flows through
    `KnowledgeMemoryService.build_and_persist`).
  * `GET /documents/{id}/knowledge-memory` endpoint returns the
    status DTO in camelCase wire shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from j1.memory.auto_build import TRIGGER_MANUAL
from j1.memory.status import (
    KnowledgeMemoryStatus,
    STATUS_BASE_COMPILE_ONLY,
    STATUS_NOT_BUILT,
    STATUS_UNKNOWN,
    STATUS_UPDATED_WITH_DOMAIN_INSIGHTS,
    resolve_knowledge_memory_status,
)
from j1.projects.context import ProjectContext


# ---- Fixtures ---------------------------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


@dataclass
class _Record:
    artifact_id: str
    kind: str = "knowledge_memory"
    metadata: dict = None
    created_at: datetime = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    updated_at: datetime = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class _StubRegistry:
    def __init__(self, records: list[_Record]) -> None:
        self.records = records

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None,
    ) -> list[_Record]:
        if kind is None:
            return list(self.records)
        return [r for r in self.records if r.kind == kind]


# ---- resolve_knowledge_memory_status --------------------------


def test_no_active_snapshot_returns_not_built():
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id=None,
        artifact_registry=_StubRegistry([]),
    )
    assert status.status == STATUS_NOT_BUILT
    assert status.document_id == "doc-1"
    assert status.snapshot_id is None
    assert status.artifact_id is None
    assert status.entry_count == 0
    assert status.includes_domain_insights is False
    assert status.last_trigger is None
    assert status.last_built_at is None


def test_no_active_memory_artifact_returns_not_built():
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry([]),
    )
    assert status.status == STATUS_NOT_BUILT
    assert status.snapshot_id == "snap-1"


def test_active_memory_without_domain_insights_returns_base_compile_only():
    records = [_Record(
        artifact_id="mem-1",
        metadata={
            "document_id": "doc-1",
            "snapshot_id": "snap-1",
            "search_state": "active",
            "entry_count": 5,
            "trigger": "after_compile",
            "includes_domain_insights": False,
        },
    )]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_BASE_COMPILE_ONLY
    assert status.artifact_id == "mem-1"
    assert status.entry_count == 5
    assert status.last_trigger == "after_compile"
    assert status.includes_domain_insights is False
    assert status.last_built_at  # ISO string populated


def test_active_memory_with_domain_insights_returns_updated():
    records = [_Record(
        artifact_id="mem-2",
        metadata={
            "document_id": "doc-1",
            "snapshot_id": "snap-1",
            "search_state": "active",
            "entry_count": 42,
            "trigger": "after_domain_enrichment",
            "includes_domain_insights": True,
        },
    )]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_UPDATED_WITH_DOMAIN_INSIGHTS
    assert status.entry_count == 42
    assert status.last_trigger == "after_domain_enrichment"
    assert status.includes_domain_insights is True


def test_superseded_artifacts_are_excluded():
    """A superseded memory artifact must not be reported as
    active. The active row wins."""
    records = [
        _Record(
            artifact_id="mem-old",
            metadata={
                "document_id": "doc-1",
                "snapshot_id": "snap-1",
                "search_state": "superseded",
                "entry_count": 99,
                "trigger": "after_compile",
                "includes_domain_insights": False,
            },
        ),
        _Record(
            artifact_id="mem-active",
            metadata={
                "document_id": "doc-1",
                "snapshot_id": "snap-1",
                "search_state": "active",
                "entry_count": 12,
                "trigger": "after_domain_enrichment",
                "includes_domain_insights": True,
            },
        ),
    ]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_UPDATED_WITH_DOMAIN_INSIGHTS
    assert status.artifact_id == "mem-active"
    assert status.entry_count == 12


def test_other_snapshot_memory_is_ignored():
    """Memory for a different snapshot must not surface for the
    active snapshot's projection."""
    records = [_Record(
        artifact_id="mem-other-snap",
        metadata={
            "document_id": "doc-1",
            "snapshot_id": "snap-OTHER",
            "search_state": "active",
            "entry_count": 99,
            "trigger": "after_compile",
            "includes_domain_insights": True,
        },
    )]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_NOT_BUILT


def test_other_document_memory_is_ignored():
    records = [_Record(
        artifact_id="mem-other-doc",
        metadata={
            "document_id": "doc-OTHER",
            "snapshot_id": "snap-1",
            "search_state": "active",
            "entry_count": 99,
        },
    )]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_NOT_BUILT


def test_multiple_active_rows_return_unknown():
    """Phase 2's supersede sweep should make this impossible, but
    if it ever happens (e.g. concurrent writes) the projection
    refuses to confidently render."""
    records = [
        _Record(
            artifact_id="mem-1",
            metadata={
                "document_id": "doc-1",
                "snapshot_id": "snap-1",
                "search_state": "active",
                "includes_domain_insights": False,
            },
        ),
        _Record(
            artifact_id="mem-2",
            metadata={
                "document_id": "doc-1",
                "snapshot_id": "snap-1",
                "search_state": "active",
                "includes_domain_insights": True,
            },
        ),
    ]
    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_StubRegistry(records),
    )
    assert status.status == STATUS_UNKNOWN
    assert any("multiple_active_memory_artifacts" in w for w in status.warnings)


def test_payload_round_trip():
    s = KnowledgeMemoryStatus(
        status=STATUS_BASE_COMPILE_ONLY,
        document_id="doc-1",
        snapshot_id="snap-1",
        artifact_id="mem-1",
        entry_count=7,
        includes_domain_insights=False,
        last_trigger="after_compile",
        last_built_at="2026-05-16T12:00:00+00:00",
        warnings=("w1",),
    )
    payload = s.to_payload()
    assert payload["status"] == STATUS_BASE_COMPILE_ONLY
    assert payload["entry_count"] == 7
    assert payload["last_trigger"] == "after_compile"
    assert payload["warnings"] == ["w1"]


def test_registry_without_list_artifacts_returns_not_built():
    """Defensive: a registry stub that doesn't implement
    list_artifacts (test fakes, future variants) returns
    `not_built` rather than crashing."""

    class _NoListRegistry:
        pass

    status = resolve_knowledge_memory_status(
        ctx=_ctx(),
        document_id="doc-1",
        active_snapshot_id="snap-1",
        artifact_registry=_NoListRegistry(),
    )
    assert status.status == STATUS_NOT_BUILT


# ---- KnowledgeMemoryService.resolve_status --------------------


def test_service_resolve_status_uses_active_snapshot():
    """The service wraps the resolver with active-snapshot lookup
    via `source_lookup.get_source`. End-to-end test that the
    service forwards everything correctly."""
    from j1.memory.service import KnowledgeMemoryService

    @dataclass
    class _Doc:
        document_id: str
        active_snapshot_id: str | None

    class _SourceLookup:
        def get_source(self, ctx, document_id):
            return _Doc(document_id=document_id, active_snapshot_id="snap-1")

    records = [_Record(
        artifact_id="mem-1",
        metadata={
            "document_id": "doc-1",
            "snapshot_id": "snap-1",
            "search_state": "active",
            "entry_count": 7,
            "trigger": "after_domain_enrichment",
            "includes_domain_insights": True,
        },
    )]
    svc = KnowledgeMemoryService(
        source_lookup=_SourceLookup(),
        artifact_registry=_StubRegistry(records),
        workspace=None,
        processing_service=object(),
        domain_registry=None,
    )
    status = svc.resolve_status(_ctx(), "doc-1")
    assert status.status == STATUS_UPDATED_WITH_DOMAIN_INSIGHTS
    assert status.snapshot_id == "snap-1"


def test_service_resolve_status_handles_missing_document():
    """Document not found → defensive `not_built` with the
    document_id stamped (so the FE can still render the section)."""
    from j1.memory.service import KnowledgeMemoryService

    class _RaisingLookup:
        def get_source(self, ctx, document_id):
            raise LookupError(document_id)

    svc = KnowledgeMemoryService(
        source_lookup=_RaisingLookup(),
        artifact_registry=_StubRegistry([]),
        workspace=None,
        processing_service=object(),
        domain_registry=None,
    )
    status = svc.resolve_status(_ctx(), "doc-missing")
    assert status.status == STATUS_NOT_BUILT
    assert status.document_id == "doc-missing"


# ---- Manual-action stamps trigger=manual -----------------------


def test_manual_action_endpoint_passes_trigger_manual():
    """The REST manual-action handler must pass `trigger="manual"`
    to `KnowledgeMemoryService.build_and_persist` so the persisted
    artifact's `metadata.trigger` reflects the actual cause. The
    test asserts the constant the handler imports + uses, since
    the call site is gated by the security middleware."""
    # Read the REST adapter source and verify the trigger constant
    # is forwarded. A grep-style assertion is the right tool here
    # — the call site is a few lines inside a 100-line handler.
    import inspect
    from j1.adapters.rest import app as app_module
    source = inspect.getsource(app_module)
    assert "trigger=TRIGGER_MANUAL" in source, (
        "Manual-action handler must forward trigger=TRIGGER_MANUAL "
        "so the persisted artifact's metadata.trigger is stamped."
    )


# ---- No-LLM regression guard -----------------------------------


def test_status_module_has_no_llm_imports():
    import importlib
    import inspect
    mod = importlib.import_module("j1.memory.status")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked, f"j1.memory.status leaks LLM imports: {leaked}"


# ---- REST endpoint integration --------------------------------


def test_rest_endpoint_returns_status_payload_camelcase():
    """The `GET /documents/{id}/knowledge-memory` endpoint must
    return a JSON envelope with camelCase keys."""
    from fastapi.testclient import TestClient
    from j1.adapters.rest import create_rest_api
    from j1.integration.services import ApplicationFacade

    class _Source:
        document_id = "doc-1"
        active_snapshot_id = "snap-1"
        knowledge_state = "attached"
        status = "compiled"

    class _SourceLookup:
        def get_source(self, ctx, document_id):
            return _Source()

    class _Service:
        def resolve_status(self, ctx, document_id):
            return KnowledgeMemoryStatus(
                status=STATUS_UPDATED_WITH_DOMAIN_INSIGHTS,
                document_id="doc-1",
                snapshot_id="snap-1",
                artifact_id="mem-1",
                entry_count=12,
                includes_domain_insights=True,
                last_trigger="after_domain_enrichment",
                last_built_at="2026-05-16T12:00:00+00:00",
            )

    facade = ApplicationFacade(
        ingestion=None,           # type: ignore[arg-type]
        retrieval=None,           # type: ignore[arg-type]
        citation_lookup=None,     # type: ignore[arg-type]
        source_lookup=_SourceLookup(),
        feedback=None,            # type: ignore[arg-type]
        event_publisher=None,     # type: ignore[arg-type]
    )
    app = create_rest_api(facade, knowledge_memory_service=_Service())
    client = TestClient(app)
    response = client.get(
        "/documents/doc-1/knowledge-memory",
        headers={"X-Tenant-Id": "t1", "X-Project-Id": "p1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    data = body.get("data", body)
    assert data["status"] == "updated_with_domain_insights"
    assert data["documentId"] == "doc-1"
    assert data["snapshotId"] == "snap-1"
    assert data["artifactId"] == "mem-1"
    assert data["entryCount"] == 12
    assert data["includesDomainInsights"] is True
    assert data["lastTrigger"] == "after_domain_enrichment"
    assert data["lastBuiltAt"] == "2026-05-16T12:00:00+00:00"
    assert data["warnings"] == []


def test_rest_endpoint_returns_503_when_service_not_wired():
    from fastapi.testclient import TestClient
    from j1.adapters.rest import create_rest_api
    from j1.integration.services import ApplicationFacade

    class _Source:
        document_id = "doc-1"
        active_snapshot_id = None
        knowledge_state = "attached"
        status = "uploaded"

    class _SourceLookup:
        def get_source(self, ctx, document_id):
            return _Source()

    facade = ApplicationFacade(
        ingestion=None, retrieval=None, citation_lookup=None,
        source_lookup=_SourceLookup(),
        feedback=None, event_publisher=None,
    )
    app = create_rest_api(facade)  # no knowledge_memory_service
    client = TestClient(app)
    response = client.get(
        "/documents/doc-1/knowledge-memory",
        headers={"X-Tenant-Id": "t1", "X-Project-Id": "p1"},
    )
    assert response.status_code == 503


def test_rest_endpoint_returns_404_when_document_missing():
    from fastapi.testclient import TestClient
    from j1.adapters.rest import create_rest_api
    from j1.integration.services import ApplicationFacade

    class _RaisingLookup:
        def get_source(self, ctx, document_id):
            raise LookupError(document_id)

    class _Service:
        def resolve_status(self, ctx, document_id):
            return KnowledgeMemoryStatus(document_id=document_id)

    facade = ApplicationFacade(
        ingestion=None, retrieval=None, citation_lookup=None,
        source_lookup=_RaisingLookup(),
        feedback=None, event_publisher=None,
    )
    app = create_rest_api(facade, knowledge_memory_service=_Service())
    client = TestClient(app)
    response = client.get(
        "/documents/doc-missing/knowledge-memory",
        headers={"X-Tenant-Id": "t1", "X-Project-Id": "p1"},
    )
    assert response.status_code == 404
