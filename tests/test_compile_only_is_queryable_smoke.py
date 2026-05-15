"""End-to-end smoke test: compile-only knowledge is queryable.

Pins the core Phase-1 invariant verbatim:

> A document is queryable as soon as compile succeeds, the active
> snapshot promotes, and the compile artifacts are durable. Domain
> Enrichment is NOT required for basic queryability.

The flow exercised here goes through every surface that matters:

  1. Document registered, attached, with an ``active_snapshot_id``.
  2. The producing run is in the run store with
     ``target_snapshot_id`` matching the active snapshot.
  3. Compile artifacts exist in the artifact registry, stamped
     with the snapshot id.
  4. NO enrichment run has been dispatched. The deployment-wide
     ``J1_DOMAIN_ENRICHMENT_AUTO_ENABLED`` defaults to ``false``,
     so the planner would skip enrichment anyway — but the smoke
     test goes one further and verifies queryability with the
     environment completely cleared.
  5. ``UnifiedMemoryResolver`` reports the document + project as
     queryable.
  6. The validation service's queryability pre-flight gate passes
     (no ``MemoryNotQueryableError`` raised).
  7. The REST surface
     ``POST /documents/{id}/test-query`` returns 200 — NOT a
     ``MEMORY_NOT_QUERYABLE`` 409.

Anything that breaks this chain regresses the core spec invariant,
so this single test guards the floor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.memory import (
    QueryableStatus,
    UnifiedMemoryResolver,
)
from j1.processing.enrich_assessment import (
    ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED,
)
from j1.query.orchestrator import OrchestratorRequest
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


# ---- Pipeline orchestrator stub ----------------------------------


class _PassThroughOrchestrator:
    """Stub orchestrator that returns a successful empty answer.
    The smoke test cares about REACHING the orchestrator — proving
    queryability — not about the answer text itself. Returning a
    minimal ``OrchestratorResult`` lets the validation service's
    projection complete without dragging the full pipeline into
    scope."""

    def run(self, request: OrchestratorRequest):
        trace_stub = SimpleNamespace(
            llm_evidence=(), to_dict=lambda: {},
        )
        return SimpleNamespace(
            answer="compile-only answer",
            final_status="passed",
            citations=(),
            gate_results=(),
            trace=trace_stub,
            message=None,
        )


@pytest.fixture
def orchestrator():
    return _PassThroughOrchestrator()


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


def _seed_compile_only_document(
    registry, run_store, artifact_registry, ctx,
    *,
    document_id: str = "doc-compile-only",
    snapshot_id: str = "snap-compile-only",
    run_id: str = "run-compile-only",
) -> None:
    """Seed the minimum state Phase-1 calls queryable:
    attached doc + active snapshot + producing run + compile
    artifact. Critically, NO enrichment run is registered."""
    registry.add(DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=snapshot_id,
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id, document_id=document_id,
        workflow_id=f"wf-{run_id}", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id=snapshot_id,
    ))
    artifact_registry.add(ArtifactRecord(
        artifact_id=f"a-chunk-{document_id}", project=ctx,
        kind="compiled.text",
        location=f"compiled/{document_id}.txt",
        content_hash=f"sha256:a-chunk-{document_id}",
        byte_size=10, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        snapshot_id=snapshot_id,
        created_by_run_id=run_id,
    ))


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


# ---- The smoke test ----------------------------------------------


def test_compile_only_document_is_queryable_end_to_end(
    monkeypatch, registry, workspace, run_store, artifact_registry,
    validation_service, client, ctx,
):
    """The Phase-1 invariant: compile success + active snapshot
    promoted → queryable, without any enrichment ever running."""

    # Deployment-wide auto-enrichment OFF (the default, asserted
    # here explicitly so the smoke test is honest about state).
    monkeypatch.delenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, raising=False)
    _seed_compile_only_document(
        registry, run_store, artifact_registry, ctx,
    )

    # 1. UnifiedMemoryResolver — document-active scope.
    resolver = UnifiedMemoryResolver(
        registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
    )
    view = resolver.resolve_document_active_memory(
        ctx, "doc-compile-only",
    )
    assert view.queryable is True
    assert view.queryable_status == QueryableStatus.QUERYABLE
    assert view.compile_status == "succeeded"
    # No enrichment ever ran for this snapshot.
    assert view.enrichment_status is None
    assert view.enrichment_artifact_refs == ()
    # Counts reflect the absent enrichment artifact.
    assert view.domain_terms_count == 0
    assert view.aliases_count == 0
    assert view.quality_warnings_count == 0

    # 2. UnifiedMemoryResolver — project-active aggregate.
    project_view = resolver.resolve_project_active_memory(ctx)
    assert project_view.queryable is True
    assert project_view.queryable_status == QueryableStatus.QUERYABLE
    queryable_ids = {d.document_id for d in project_view.queryable_documents}
    assert "doc-compile-only" in queryable_ids

    # 3. Validation service queryability gate — accepts.
    from j1.validation.dtos import ManualTestQueryRequest, QueryScopeDTO
    response = validation_service.run_document_test_query(
        ctx,
        "doc-compile-only",
        ManualTestQueryRequest(
            question="What is in this document?",
            scope=QueryScopeDTO(
                type="document_active",
                document_id="doc-compile-only",
            ),
        ),
    )
    # The pre-flight gate did NOT raise; the stub orchestrator
    # returned its synthetic answer.
    assert response.answer == "compile-only answer"

    # 4. REST surface — full HTTP round-trip. The 409
    # MEMORY_NOT_QUERYABLE path that would block on missing
    # compile / enrichment / lifecycle is NOT hit.
    rest_resp = client.post(
        "/documents/doc-compile-only/test-query",
        json={"question": "What is in this document?"},
        headers=_headers(ctx),
    )
    assert rest_resp.status_code == 200, rest_resp.text
    body = rest_resp.json()
    # The body is the test-query envelope; we only need to confirm
    # the call succeeded without a structured queryability refusal.
    assert "error" not in body or body.get("error") is None
