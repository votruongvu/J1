"""Tests for the explicit snapshot-centric query scope contract.

Locks down the audit-fix contract:

  * The legacy ``validation_scope="run"`` token is REFUSED on UI
    paths (the typed ``scope`` field is the supported route).
  * ``scope={type: "snapshot_explicit", snapshotIds: [...]}`` resolves
    to an explicit allowlist passed through to the orchestrator's
    ``eligible_snapshot_ids``.
  * ``scope={type: "document_active", documentId}`` resolves to
    ``ActiveScope(document_id)`` for the validation service.
  * The diagnostic ``allowRunScope=true`` escape hatch DOES accept
    ``validation_scope="run"`` — operators inspecting raw run-keyed
    artifacts still have a way through.

The Run-keyed-scope-as-knowledge antipattern is intentionally
unreachable from typed callers."""

from __future__ import annotations

from datetime import datetime, timezone

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
from j1.query.scope import ActiveScope, RunScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import ManualTestQueryRequest, QueryScopeDTO


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


# ---- Service-level scope mapping --------------------------------


def _service_under_test(monkeypatch=None, snapshot_store=None):
    """Build a ``ManualTestQueryService`` with the minimum wiring
    needed to exercise ``_resolve_query_scope`` directly. We don't
    care about the LLM / orchestrator — we just call the helper."""
    from j1.validation.service import IngestionValidationService

    # Minimal stub deps — _resolve_query_scope only reads
    # ``self._source_registry`` and ``self._snapshot_store``.
    class _StubSourceRegistry:
        pass

    return ManualTestQueryService.__new__(ManualTestQueryService).__init__ if False else None  # type: ignore[return-value]


def test_resolve_query_scope_document_active_returns_active_scope(
    monkeypatch,
):
    """``scope.type='document_active'`` resolves to ``ActiveScope``."""
    from j1.validation.service import IngestionValidationService

    svc = object.__new__(IngestionValidationService)
    svc._source_registry = None  # type: ignore[attr-defined]
    svc._snapshot_store = None  # type: ignore[attr-defined]

    run = _fake_run()
    req = ManualTestQueryRequest(
        question="q",
        scope=QueryScopeDTO(
            type="document_active", document_id="doc-1",
        ),
    )
    scope, eligible = svc._resolve_query_scope(  # type: ignore[attr-defined]
        ctx=_fake_ctx(), run=run, request=req,
    )
    assert isinstance(scope, ActiveScope)
    assert scope.document_id == "doc-1"
    assert eligible is None


def test_resolve_query_scope_snapshot_explicit_passes_allowlist():
    """``scope.type='snapshot_explicit'`` resolves to ``WorkspaceScope``
    + an explicit ``eligible_snapshot_ids`` allowlist that the
    orchestrator threads into the route context."""
    from j1.validation.service import IngestionValidationService

    svc = object.__new__(IngestionValidationService)
    svc._source_registry = None  # type: ignore[attr-defined]
    svc._snapshot_store = None  # type: ignore[attr-defined]

    run = _fake_run()
    req = ManualTestQueryRequest(
        question="q",
        scope=QueryScopeDTO(
            type="snapshot_explicit",
            snapshot_ids=("snap-A", "snap-B"),
        ),
    )
    scope, eligible = svc._resolve_query_scope(  # type: ignore[attr-defined]
        ctx=_fake_ctx(), run=run, request=req,
    )
    assert isinstance(scope, WorkspaceScope)
    assert eligible == frozenset({"snap-A", "snap-B"})


def test_resolve_query_scope_snapshot_explicit_requires_ids():
    """An empty ``snapshotIds`` is a caller error, not a silent
    pass-through. Surfacing as ValueError lets the REST layer convert
    to a 400."""
    from j1.validation.service import IngestionValidationService

    svc = object.__new__(IngestionValidationService)
    svc._source_registry = None  # type: ignore[attr-defined]
    svc._snapshot_store = None  # type: ignore[attr-defined]

    req = ManualTestQueryRequest(
        question="q",
        scope=QueryScopeDTO(
            type="snapshot_explicit", snapshot_ids=(),
        ),
    )
    with pytest.raises(ValueError, match="snapshot_explicit"):
        svc._resolve_query_scope(  # type: ignore[attr-defined]
            ctx=_fake_ctx(), run=_fake_run(), request=req,
        )


def test_resolve_query_scope_project_active_returns_workspace_scope():
    """``scope.type='project_active'`` resolves to ``WorkspaceScope``.
    The orchestrator's eligibility resolver narrows to attached
    documents from there."""
    from j1.validation.service import IngestionValidationService

    svc = object.__new__(IngestionValidationService)
    svc._source_registry = None  # type: ignore[attr-defined]
    svc._snapshot_store = None  # type: ignore[attr-defined]

    req = ManualTestQueryRequest(
        question="q",
        scope=QueryScopeDTO(type="project_active"),
    )
    scope, eligible = svc._resolve_query_scope(  # type: ignore[attr-defined]
        ctx=_fake_ctx(), run=_fake_run(), request=req,
    )
    assert isinstance(scope, WorkspaceScope)
    assert eligible is None


def test_resolve_query_scope_falls_through_to_legacy_run_token():
    """Legacy callers without ``scope`` get the old
    ``validation_scope="run"`` behaviour (RunScope) — the diagnostic
    surface relies on this for raw run-keyed inspection. UI is
    blocked at the REST layer; this verifies the inner mapping
    still works for the escape-hatch path."""
    from j1.validation.service import IngestionValidationService

    svc = object.__new__(IngestionValidationService)
    svc._source_registry = None  # type: ignore[attr-defined]
    svc._snapshot_store = None  # type: ignore[attr-defined]

    req = ManualTestQueryRequest(
        question="q", validation_scope="run",
    )
    scope, eligible = svc._resolve_query_scope(  # type: ignore[attr-defined]
        ctx=_fake_ctx(), run=_fake_run(), request=req,
    )
    assert isinstance(scope, RunScope)
    assert scope.run_id == "run-1"
    assert eligible is None


# ---- REST-layer guard rails -------------------------------------


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
    review_service, snapshot_service, registry, ctx,
):
    from j1.integration.dto import ProcessingCapabilities
    # Seed a run + document so the test-query endpoint reaches the
    # body-level scope guard. We don't actually run a query; we just
    # want the handler to evaluate the typed-scope refusal.
    registry.add(DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="doc-1.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="sha256:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id="snap-1",
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id="snap-1",
        metadata={},
    ))
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


def test_rest_refuses_legacy_run_scope_without_opt_in(client, ctx):
    """UI callers that send ``validationScope="run"`` (or simply
    accept the legacy default) without the typed ``scope`` field
    and without ``allowRunScope=true`` get HTTP 400. Run is no
    longer a knowledge unit."""
    resp = client.post(
        "/ingestion-runs/run-1/test-query",
        headers=_headers(ctx),
        json={"question": "anything?", "validationScope": "run"},
    )
    assert resp.status_code == 400, resp.text
    msg = resp.json()["error"]["message"].lower()
    assert "run" in msg
    assert "scope" in msg


def test_rest_accepts_legacy_run_scope_when_diagnostic_opt_in(
    client, ctx,
):
    """Operators inspecting raw run-keyed artifacts can still opt
    into the diagnostic ``"run"`` scope via ``allowRunScope=true``.
    The endpoint reaches the validation service (which may fail for
    other reasons — what we check here is that the boundary guard
    didn't refuse)."""
    resp = client.post(
        "/ingestion-runs/run-1/test-query",
        headers=_headers(ctx),
        json={
            "question": "anything?",
            "validationScope": "run",
            "allowRunScope": True,
        },
    )
    # The handler may return 5xx because we haven't wired the
    # validation service for real — what we check is the *boundary*
    # didn't return 400 / "validation_scope=run not accepted".
    assert resp.status_code != 400, resp.text


def test_rest_accepts_typed_document_active_scope(client, ctx):
    """The typed snapshot-centric route works without a special
    opt-in."""
    resp = client.post(
        "/ingestion-runs/run-1/test-query",
        headers=_headers(ctx),
        json={
            "question": "anything?",
            "scope": {
                "type": "document_active", "documentId": "doc-1",
            },
        },
    )
    # Same as above — we're checking the boundary, not the engine.
    assert resp.status_code != 400, resp.text


def test_rest_accepts_typed_snapshot_explicit_scope(client, ctx):
    resp = client.post(
        "/ingestion-runs/run-1/test-query",
        headers=_headers(ctx),
        json={
            "question": "anything?",
            "scope": {
                "type": "snapshot_explicit",
                "snapshotIds": ["snap-1"],
            },
        },
    )
    assert resp.status_code != 400, resp.text


# ---- Test helpers ---------------------------------------------------


def _fake_ctx():
    from j1.projects.context import ProjectContext
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _fake_run():
    return IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id="snap-1",
        metadata={},
    )
