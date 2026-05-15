"""REST + service tests for the Unified Memory queryability gate.

Locks the contract: when the project / document active scope is not
queryable (compile failed, document detached, artifacts missing,
etc.), the validation surface refuses with a structured
``MEMORY_NOT_QUERYABLE`` payload carrying ``queryableStatus`` and
``queryableReason``.

The run-explicit and snapshot-explicit scopes intentionally BYPASS
this gate — they are operator allowlists / diagnostics and the
eligibility resolver already handles them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.jobs.status import ProcessingStatus
from j1.memory import (
    MemoryNotQueryableError,
    QueryableStatus,
    UnifiedMemoryResolver,
)
from j1.query.orchestrator import OrchestratorRequest
from j1.query.scope import ActiveScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import ManualTestQueryRequest, QueryScopeDTO
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


class _CapturingOrchestrator:
    def __init__(self):
        self.calls: list[OrchestratorRequest] = []

    def run(self, request: OrchestratorRequest):
        self.calls.append(request)
        trace_stub = SimpleNamespace(llm_evidence=(), to_dict=lambda: {})
        return SimpleNamespace(
            answer="ok", final_status="passed", citations=(),
            gate_results=(), trace=trace_stub, message=None,
        )


@pytest.fixture
def orchestrator():
    return _CapturingOrchestrator()


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def validation_service(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator,
):
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        workspace=workspace,
        source_registry=registry,
        smart_query_orchestrator=orchestrator,
    )


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry, artifact_registry=artifact_registry,
        clock=lambda: _NOW,
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
    application_facade, workspace, run_store, validation_service,
    lifecycle_service,
):
    from j1.integration.dto import ProcessingCapabilities
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        validation_service=validation_service,
        document_lifecycle_service=lifecycle_service,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(
    registry, ctx, *, document_id="doc-1", state="attached",
    active_snapshot_id=None,
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
        knowledge_state=state,
        active_snapshot_id=active_snapshot_id,
    ))


# ---- Service-level: gate refuses on not-queryable scope ----------


def test_document_active_query_refused_when_no_active_snapshot(
    validation_service, registry, ctx, orchestrator,
):
    """A document with no active snapshot is not queryable. The
    service raises ``MemoryNotQueryableError`` BEFORE handing the
    request to the orchestrator — orchestrator must not see it."""
    _seed_doc(registry, ctx, active_snapshot_id=None)

    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(type="document_active", document_id="doc-1"),
    )
    with pytest.raises(MemoryNotQueryableError) as exc:
        validation_service.run_document_test_query(ctx, "doc-1", req)
    assert exc.value.queryable_status == QueryableStatus.NOT_STARTED
    assert orchestrator.calls == []


def test_project_active_query_refused_when_no_documents(
    validation_service, ctx, orchestrator,
):
    """Empty project → ``NOT_STARTED``. Refused at the gate."""
    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(type="project_active"),
    )
    with pytest.raises(MemoryNotQueryableError) as exc:
        validation_service.run_project_query(ctx, req)
    assert exc.value.queryable_status == QueryableStatus.NOT_STARTED
    assert orchestrator.calls == []


def test_snapshot_explicit_scope_bypasses_gate(
    validation_service, ctx, orchestrator,
):
    """Snapshot-explicit is an operator allowlist by definition —
    the gate must NOT refuse based on resolver verdict."""
    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(
            type="snapshot_explicit",
            snapshot_ids=("snap-pinned",),
        ),
    )
    validation_service.run_project_query(ctx, req)
    assert len(orchestrator.calls) == 1


# ---- REST integration: structured 409 payload --------------------


def test_rest_document_test_query_409_on_not_queryable(
    client, registry, ctx,
):
    """The REST surface translates the resolver's refusal into a
    structured 409 with ``queryableStatus`` and ``queryableReason``."""
    _seed_doc(registry, ctx, active_snapshot_id=None)

    resp = client.post(
        "/documents/doc-1/test-query",
        json={"question": "anything?"},
        headers=_headers(ctx),
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    err = body["error"]
    assert err["code"] == "MEMORY_NOT_QUERYABLE"
    details = err["details"]
    assert details["queryableStatus"] == "not_started"
    assert details["queryableReason"]
    assert "snapshot" in details["queryableReason"].lower()


def test_rest_project_query_409_on_empty_project(client, ctx):
    resp = client.post(
        "/query",
        json={"question": "anything?"},
        headers=_headers(ctx),
    )
    # The project query route exists under a couple of names —
    # exercise both happy paths. If 404, fall through to the
    # alternate route.
    if resp.status_code == 404:
        resp = client.post(
            "/projects/alpha/query",
            json={"question": "anything?"},
            headers=_headers(ctx),
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "MEMORY_NOT_QUERYABLE"
    assert body["error"]["details"]["queryableStatus"] == "not_started"


# ---- Resolver direct invocation from the service ----------------


def test_resolver_direct_returns_queryable_for_seeded_active_doc(
    registry, run_store, artifact_registry, ctx,
):
    """Sanity: with a properly-seeded document + compile artifact,
    the resolver wired into the service returns QUERYABLE."""
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ReviewStatus
    _seed_doc(registry, ctx, active_snapshot_id="snap-active")
    run_store.upsert(ctx, IngestionRun(
        run_id="r-baseline", document_id="doc-1",
        workflow_id="wf-r", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id="snap-active",
    ))
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-chunk", project=ctx,
        kind="compiled.text",
        location="compiled/a.txt", content_hash="sha256:a",
        byte_size=1, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={"snapshot_id": "snap-active", "run_id": "r-baseline"},
        snapshot_id="snap-active",
        created_by_run_id="r-baseline",
    ))
    resolver = UnifiedMemoryResolver(
        registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
    )
    view = resolver.resolve_document_active_memory(ctx, "doc-1")
    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.QUERYABLE


# ---- Propagation guardrails ----------------------------------------
#
# These tests pin the invariant that ``MemoryNotQueryableError``
# bubbles out of the validation surface cleanly — neither swallowed
# into a generic 500 nor wrapped by a broad ``except Exception``.


def _doc_sentinel(*, status=QueryableStatus.COMPILE_FAILED, reason="x"):
    """Construct a not-queryable ``DocumentMemoryView`` to wrap in the
    exception. The fields ``MemoryNotQueryableError`` actually reads
    from are ``queryable_status`` + ``queryable_reason``."""
    from j1.memory import DocumentMemoryView, MemoryScope
    return DocumentMemoryView(
        scope=MemoryScope.DOCUMENT_ACTIVE,
        project_id="alpha",
        document_id="doc-1",
        queryable_status=status,
        queryable_reason=reason,
    )


def _project_sentinel(*, status=QueryableStatus.NOT_STARTED, reason="x"):
    from j1.memory import MemoryScope, ProjectActiveMemoryView
    return ProjectActiveMemoryView(
        scope=MemoryScope.PROJECT_ACTIVE,
        project_id="alpha",
        queryable_status=status,
        queryable_reason=reason,
        documents=(),
    )


def test_memory_not_queryable_propagates_through_run_document_test_query(
    validation_service, registry, ctx, orchestrator, monkeypatch,
):
    """Inject the raise at the gate; the public entry method must
    let it bubble out unmodified (not wrap it, not convert it). Pins
    the invariant against a future broad-except regression in
    ``_run_manual_query_via_orchestrator``."""
    _seed_doc(registry, ctx, active_snapshot_id="snap-real")

    def _raise(*_a, **_kw):
        raise MemoryNotQueryableError(
            _doc_sentinel(reason="injected sentinel reason"),
        )

    monkeypatch.setattr(
        validation_service, "_enforce_memory_queryability", _raise,
    )

    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(type="document_active", document_id="doc-1"),
    )
    with pytest.raises(MemoryNotQueryableError) as exc:
        validation_service.run_document_test_query(ctx, "doc-1", req)
    assert exc.value.queryable_status == QueryableStatus.COMPILE_FAILED
    assert exc.value.queryable_reason == "injected sentinel reason"
    assert orchestrator.calls == []


def test_memory_not_queryable_propagates_through_run_project_query(
    validation_service, ctx, orchestrator, monkeypatch,
):
    def _raise(*_a, **_kw):
        raise MemoryNotQueryableError(_project_sentinel())

    monkeypatch.setattr(
        validation_service, "_enforce_memory_queryability", _raise,
    )
    req = ManualTestQueryRequest(
        question="anything?",
        scope=QueryScopeDTO(type="project_active"),
    )
    with pytest.raises(MemoryNotQueryableError):
        validation_service.run_project_query(ctx, req)
    assert orchestrator.calls == []


def test_imported_test_cases_executor_does_not_swallow_memory_error(
    ctx,
):
    """The CSV runner wraps each per-question orchestrator call in
    ``except Exception``. Any ``MemoryNotQueryableError`` raised
    from inside the orchestrator must bubble out as a SCOPE-level
    refusal — converting it into a per-question ``status="error"``
    would silently produce a misleading summary across the batch.
    Pinned via the explicit re-raise in ``_execute_one``."""
    from datetime import datetime, timezone
    from j1.validation.imported_test_cases import (
        ImportedTestCase,
        ImportedTestCaseExecutor,
        ImportedTestCaseSet,
    )

    sentinel = _doc_sentinel(
        status=QueryableStatus.MISSING_ARTIFACTS,
        reason="batch-scoped refusal",
    )

    class _BoomOrchestrator:
        def run(self, _request):
            raise MemoryNotQueryableError(sentinel)

    executor = ImportedTestCaseExecutor(
        smart_query_orchestrator=_BoomOrchestrator(),
        run_store=None,  # unused on the raising path
    )
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    imported = ImportedTestCaseSet(
        document_id="doc-1",
        cases=(
            ImportedTestCase(test_case_id="t-1", question="anything?"),
        ),
        imported_at=now,
        source_filename="t.csv",
    )
    with pytest.raises(MemoryNotQueryableError):
        executor.execute(ctx, imported, run_id="r-1")
