"""Tests for the snapshot-centric query endpoints.

Locks down:

  * ``POST /documents/{id}/test-query`` resolves ``document_active``
    scope and does NOT require a run id.
  * ``POST /projects/{id}/query`` resolves ``project_active`` scope.
  * The project endpoint cross-checks the URL ``project_id`` against
    the ``X-Project-Id`` header (400 on mismatch).
  * Both new endpoints refuse ``allowRunScope=true`` — run is not
    a primary routing key for knowledge queries.
  * The document endpoint returns 404 for unknown documents.
  * Service-level: ``run_document_test_query`` defaults to
    ``document_active`` when ``scope`` is None; ``run_project_query``
    defaults to ``project_active``. ``snapshot_explicit`` overrides
    still flow through both surfaces.
  * Service-level: ``_resolve_query_scope`` tolerates ``run=None``
    for the new code paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.documents.snapshot_service import DocumentSnapshotService
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.ingestion_review import IngestionResultReviewService
from j1.jobs.status import ProcessingStatus
from j1.query.orchestrator import OrchestratorRequest
from j1.query.scope import ActiveScope, WorkspaceScope
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import (
    ManualTestQueryRequest, QueryScopeDTO,
)
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


# ---- Test scaffolding ----


class _CapturingOrchestrator:
    """Stub ``SmartQueryOrchestrator`` that captures the request and
    returns a minimal ``OrchestratorResult``. Lets tests assert what
    scope / document / run id the service forwarded without
    standing up a real orchestrator + LLM."""

    def __init__(self) -> None:
        self.calls: list[OrchestratorRequest] = []

    def run(self, request: OrchestratorRequest):
        self.calls.append(request)
        # Build a minimal OrchestratorResult-like object. The service
        # reads ``result.trace``, ``result.gate_results``,
        # ``result.final_status``, ``result.answer``, ``result.message``
        # plus ``trace.llm_evidence`` and ``trace.to_dict()``. A
        # SimpleNamespace with those properties is enough — we don't
        # need a real QueryPlan / QueryTrace here.
        from types import SimpleNamespace

        trace_stub = SimpleNamespace(
            llm_evidence=(),
            to_dict=lambda: {},
        )
        return SimpleNamespace(
            answer="stub-answer",
            final_status="passed",
            citations=(),
            gate_results=(),
            trace=trace_stub,
            message=None,
        )


@pytest.fixture
def capturing_orchestrator():
    return _CapturingOrchestrator()


@pytest.fixture
def validation_service(
    workspace, run_store, artifact_registry, audit_recorder,
    capturing_orchestrator,
):
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        workspace=workspace,
        smart_query_orchestrator=capturing_orchestrator,
    )


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
def snapshot_service(workspace):
    return DocumentSnapshotService(
        store=JsonlDocumentSnapshotStore(workspace),
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
    review_service, snapshot_service, validation_service,
    registry, ctx,
):
    from j1.integration.dto import ProcessingCapabilities
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
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        snapshot_service=snapshot_service,
        validation_service=validation_service,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


# ---- Service-level: scope defaulting ----


def test_document_endpoint_defaults_to_document_active_scope(
    validation_service, capturing_orchestrator, ctx,
):
    """When the caller omits ``scope``, the document endpoint MUST
    default to ``document_active`` keyed off the URL's documentId.
    No run id needed."""
    req = ManualTestQueryRequest(question="anything?")
    validation_service.run_document_test_query(ctx, "doc-1", req)
    assert len(capturing_orchestrator.calls) == 1
    captured = capturing_orchestrator.calls[0]
    assert isinstance(captured.scope, ActiveScope)
    assert captured.scope.document_id == "doc-1"
    # No run_id was threaded — the document endpoint never reaches
    # the run store.
    assert captured.run_id is None
    assert captured.document_id == "doc-1"


def test_document_endpoint_accepts_document_run_scope(
    validation_service, capturing_orchestrator, ctx, run_store,
):
    """Document endpoint with ``document_run`` scope: the run-store
    lookup resolves ``(documentId, run.target_snapshot_id)`` and
    threads it as ``eligible_snapshot_pairs`` so the adapter
    bypasses project-active eligibility entirely. This is the Run
    Detail validation path — it MUST work even when the run's
    snapshot isn't promoted to active."""
    from j1.runs.models import IngestionRun, RunStatus
    # Persist a historical run whose target_snapshot_id is NOT the
    # document's active snapshot. The document's active snapshot
    # could be anything (or nothing) — run scope ignores it.
    historical = IngestionRun(
        run_id="run-historical",
        document_id="doc-1",
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id="snap-historical-output",
        metadata={},
    )
    run_store.upsert(ctx, historical)

    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(
            type="document_run",
            document_id="doc-1",
            run_id="run-historical",
        ),
    )
    validation_service.run_document_test_query(ctx, "doc-1", req)
    captured = capturing_orchestrator.calls[0]
    # Internal scope is RunScope — adapter dispatch uses pre-resolved
    # pairs, not the scope-driven eligibility resolver.
    from j1.query.scope import RunScope as _RS
    assert isinstance(captured.scope, _RS)
    assert captured.scope.run_id == "run-historical"
    assert captured.scope.document_id == "doc-1"
    # The explicit pair is threaded via OrchestratorRequest so the
    # RAGAnything adapter fans out directly — no active-snapshot
    # filter applied.
    assert captured.eligible_snapshot_pairs == frozenset({
        ("doc-1", "snap-historical-output"),
    })


def test_document_endpoint_rejects_cross_document_run(
    validation_service, capturing_orchestrator, ctx, run_store,
):
    """``document_run`` with a runId that belongs to another
    document must yield no pairs. We return the result via the
    adapter's scope-aware refusal — the orchestrator sees no pairs."""
    from j1.runs.models import IngestionRun, RunStatus
    other_doc_run = IngestionRun(
        run_id="run-other",
        document_id="doc-OTHER",
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id="snap-other",
        metadata={},
    )
    run_store.upsert(ctx, other_doc_run)

    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(
            type="document_run",
            document_id="doc-1",  # caller's doc — doesn't match run
            run_id="run-other",
        ),
    )
    validation_service.run_document_test_query(ctx, "doc-1", req)
    captured = capturing_orchestrator.calls[0]
    # No pre-resolved pairs because the cross-document guard tripped.
    assert captured.eligible_snapshot_pairs is None


def test_document_endpoint_accepts_snapshot_explicit_override(
    validation_service, capturing_orchestrator, ctx,
):
    """Operators validating a specific candidate snapshot tied to a
    document can override the default scope. The eligibility
    allowlist is threaded into the orchestrator request."""
    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(
            type="snapshot_explicit",
            snapshot_ids=("snap-candidate-A",),
        ),
    )
    validation_service.run_document_test_query(ctx, "doc-1", req)
    captured = capturing_orchestrator.calls[0]
    assert isinstance(captured.scope, WorkspaceScope)
    assert captured.eligible_snapshot_ids == frozenset(
        {"snap-candidate-A"},
    )


def test_project_endpoint_defaults_to_project_active_scope(
    validation_service, capturing_orchestrator, ctx,
):
    """The project endpoint defaults to ``project_active``: a
    ``WorkspaceScope`` with no pre-resolved allowlist (the
    orchestrator's eligibility resolver narrows downstream)."""
    req = ManualTestQueryRequest(question="anything?")
    validation_service.run_project_query(ctx, req)
    captured = capturing_orchestrator.calls[0]
    assert isinstance(captured.scope, WorkspaceScope)
    assert captured.eligible_snapshot_ids is None
    assert captured.run_id is None


def test_resolve_query_scope_tolerates_run_none_with_typed_scope(
    validation_service, ctx,
):
    """Service-level invariant: the resolver works with ``run=None``
    as long as a typed ``scope`` is supplied."""
    svc = validation_service
    req = ManualTestQueryRequest(
        question="q",
        scope=QueryScopeDTO(type="document_active", document_id="doc-1"),
    )
    scope, eligible = svc._resolve_query_scope(  # type: ignore[attr-defined]
        ctx=ctx, run=None, request=req,
    )
    assert isinstance(scope, ActiveScope)
    assert scope.document_id == "doc-1"
    assert eligible is None


def test_resolve_query_scope_rejects_legacy_token_when_run_none(
    validation_service, ctx,
):
    """Service-level invariant: a legacy ``validation_scope`` token
    without ``run`` is a programmer error (the doc/project endpoints
    must send a typed ``scope``)."""
    svc = validation_service
    req = ManualTestQueryRequest(question="q", validation_scope="run")
    with pytest.raises(ValueError, match="requires a run"):
        svc._resolve_query_scope(  # type: ignore[attr-defined]
            ctx=ctx, run=None, request=req,
        )


# ---- REST-level: endpoint behaviour ----


def test_rest_document_test_query_routes_through_service(
    client, capturing_orchestrator, ctx,
):
    """The new document-level endpoint exists, accepts an empty
    body (defaulting to document_active), and reaches the
    orchestrator with the right scope."""
    resp = client.post(
        "/documents/doc-1/test-query",
        headers=_headers(ctx),
        json={"question": "anything?"},
    )
    assert resp.status_code == 200, resp.text
    assert len(capturing_orchestrator.calls) == 1
    captured = capturing_orchestrator.calls[0]
    assert isinstance(captured.scope, ActiveScope)
    assert captured.scope.document_id == "doc-1"


def test_rest_document_test_query_404_for_unknown_document(client, ctx):
    resp = client.post(
        "/documents/missing/test-query",
        headers=_headers(ctx),
        json={"question": "anything?"},
    )
    assert resp.status_code == 404


def test_rest_document_test_query_refuses_allow_run_scope(client, ctx):
    """The document endpoint does NOT accept the diagnostic
    ``allowRunScope`` opt-in — that surface lives only on the
    run-keyed endpoint."""
    resp = client.post(
        "/documents/doc-1/test-query",
        headers=_headers(ctx),
        json={"question": "q", "allowRunScope": True},
    )
    assert resp.status_code == 400
    assert "run" in resp.json()["error"]["message"].lower()


def test_rest_document_test_query_refuses_project_active_scope(client, ctx):
    """The document URL must not silently widen to a project-wide
    query. Operators wanting project_active scope have a dedicated
    endpoint; refusing 400 here keeps the URL honest."""
    resp = client.post(
        "/documents/doc-1/test-query",
        headers=_headers(ctx),
        json={
            "question": "q",
            "scope": {"type": "project_active"},
        },
    )
    assert resp.status_code == 400, resp.text
    msg = resp.json()["error"]["message"].lower()
    assert "project_active" in msg or "project-wide" in msg
    # Hint mentions the right replacement endpoint.
    assert "/projects/" in resp.json()["error"]["message"]


def test_rest_project_query_routes_through_service(
    client, capturing_orchestrator, ctx,
):
    resp = client.post(
        f"/projects/{ctx.project_id}/query",
        headers=_headers(ctx),
        json={"question": "anything?"},
    )
    assert resp.status_code == 200, resp.text
    captured = capturing_orchestrator.calls[0]
    assert isinstance(captured.scope, WorkspaceScope)
    assert captured.run_id is None


def test_rest_project_query_400_on_mismatched_project_id(client, ctx):
    """URL project_id and X-Project-Id MUST agree. Operator with
    a wrong URL gets a clear 400 instead of a silently-wrong
    cross-project query."""
    resp = client.post(
        "/projects/some-other-project/query",
        headers=_headers(ctx),
        json={"question": "anything?"},
    )
    assert resp.status_code == 400
    msg = resp.json()["error"]["message"].lower()
    assert "project_id" in msg or "project id" in msg


def test_rest_project_query_refuses_allow_run_scope(client, ctx):
    resp = client.post(
        f"/projects/{ctx.project_id}/query",
        headers=_headers(ctx),
        json={"question": "q", "allowRunScope": True},
    )
    assert resp.status_code == 400


def test_rest_project_query_accepts_snapshot_explicit_override(
    client, capturing_orchestrator, ctx,
):
    """Project-wide validation paths can pin a fixed snapshot set
    via ``snapshot_explicit``. The eligibility allowlist flows
    through to the orchestrator."""
    resp = client.post(
        f"/projects/{ctx.project_id}/query",
        headers=_headers(ctx),
        json={
            "question": "anything?",
            "scope": {
                "type": "snapshot_explicit",
                "snapshotIds": ["snap-a", "snap-b"],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    captured = capturing_orchestrator.calls[0]
    assert captured.eligible_snapshot_ids == frozenset(
        {"snap-a", "snap-b"},
    )


# ---- Back-compat: the run-keyed endpoint still works ----


def test_legacy_run_endpoint_still_works_for_snapshot_explicit(
    client, capturing_orchestrator, run_store, ctx,
):
    """Snapshot validation (the candidate-snapshot use case on Run
    Detail) still routes through the legacy run-keyed endpoint —
    Approve/Reject diagnostic flows depend on it."""
    from j1.runs.models import IngestionRun, RunStatus
    run_store.upsert(ctx, IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id="snap-candidate",
        metadata={},
    ))
    resp = client.post(
        "/ingestion-runs/run-1/test-query",
        headers=_headers(ctx),
        json={
            "question": "anything?",
            "scope": {
                "type": "snapshot_explicit",
                "snapshotIds": ["snap-candidate"],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    captured = capturing_orchestrator.calls[0]
    assert captured.eligible_snapshot_ids == frozenset(
        {"snap-candidate"},
    )
