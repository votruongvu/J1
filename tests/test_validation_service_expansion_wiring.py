"""Production wire tests: validation service → orchestrator → retrieval.

The previous PR proved the orchestrator consumes
``request.memory_view.expansions`` to broaden retrieval. This file
pins the matching production-side wire: the validation service
actually populates ``memory_view.expansions`` per request when the
deployment opted into ``J1_QUERY_EXPANSION_ENABLED=true`` AND a
domain-pack lookup is wired.

Test contract:

  1. Flag OFF → memory_view has no expansions (orchestrator runs
     retrieval on the original query only).
  2. Flag ON + no domain pack lookup → no expansions (deployment
     not yet wired for pack-aware augmentation).
  3. Flag ON + lookup returns None → no expansions (no matching
     pack for the document's domain id).
  4. Flag ON + lookup returns a pack with aliases → memory_view
     carries the alias-driven expansion list; orchestrator
     dispatches variant retrieval jobs.
  5. Flag ON + lookup returns a pack but query doesn't match any
     alias → empty expansions (no false positives).
  6. The expansion list is deduplicated + empty-stripped before
     handing it to the orchestrator.

The tests use a capturing-orchestrator stub so we can assert on
what landed in ``OrchestratorRequest.memory_view`` without standing
up a real retrieval pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
    EntityAlias,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.query.orchestrator import (
    ENV_QUERY_EXPANSION_ENABLED,
    OrchestratorRequest,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import (
    ManualTestQueryRequest,
    QueryScopeDTO,
)
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


class _CapturingOrchestrator:
    """Stub orchestrator that records the OrchestratorRequest it
    receives and returns a benign result so the service's
    response-projection code completes."""

    def __init__(self) -> None:
        self.calls: list[OrchestratorRequest] = []

    def run(self, request: OrchestratorRequest):
        self.calls.append(request)
        trace_stub = SimpleNamespace(
            llm_evidence=(), to_dict=lambda: {},
        )
        return SimpleNamespace(
            answer="stub",
            final_status="passed",
            citations=(),
            gate_results=(),
            trace=trace_stub,
            message=None,
        )


def _pack_with_alias() -> DomainPack:
    return DomainPack(
        id="example.test", display_name="Test", version="1",
        extends_document_types=(), keyword_signals=(),
        extraction_targets=(), graph_entity_types=(),
        graph_relationship_types=(), prompt_addon="", overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            entity_aliases=(
                EntityAlias(
                    canonical_name="reinforced concrete",
                    aliases=("RC",),
                ),
            ),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )


def _seed_queryable_doc(
    registry, run_store, artifact_registry, ctx,
    *, document_id: str = "doc-1",
):
    """Seed an attached document with an active snapshot + compile
    artifact so the resolver reports the view as queryable."""
    registry.add(DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id="snap-active",
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id="r-baseline", document_id=document_id,
        workflow_id="wf-r", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id="snap-active",
    ))
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-chunk", project=ctx,
        kind="compiled.text",
        location="compiled/a.txt", content_hash="sha256:a-chunk",
        byte_size=10, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        snapshot_id="snap-active",
        created_by_run_id="r-baseline",
    ))


@pytest.fixture
def orchestrator():
    return _CapturingOrchestrator()


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry, artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


def _build_service(
    *,
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator,
    domain_pack_lookup=None,
):
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        workspace=workspace,
        source_registry=registry,
        smart_query_orchestrator=orchestrator,
        domain_pack_lookup=domain_pack_lookup,
    )


def _document_request(question: str):
    return ManualTestQueryRequest(
        question=question,
        scope=QueryScopeDTO(type="document_active", document_id="doc-1"),
    )


# ---- 1. Flag OFF → no expansions on the view ---------------------


def test_flag_off_leaves_expansions_empty(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: _pack_with_alias(),
    )
    svc.run_document_test_query(
        ctx, "doc-1", _document_request("RC beam crack width"),
    )
    [request] = orchestrator.calls
    assert request.memory_view is not None
    assert request.memory_view.expansions == ()


# ---- 2. Flag ON, no lookup → no expansions -----------------------


def test_flag_on_without_lookup_leaves_expansions_empty(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """A deployment can opt into the env flag but not wire a
    domain-pack lookup yet. The service must NOT crash and must
    NOT invent expansions — it just passes the bare view."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=None,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _document_request("RC beam crack width"),
    )
    [request] = orchestrator.calls
    assert request.memory_view is not None
    assert request.memory_view.expansions == ()


# ---- 3. Flag ON, lookup returns None → no expansions -------------


def test_flag_on_lookup_returns_none_leaves_expansions_empty(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: None,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _document_request("RC beam crack width"),
    )
    [request] = orchestrator.calls
    assert request.memory_view.expansions == ()


# ---- 4. Flag ON + pack with matching alias → expansions populated -


def test_flag_on_with_matching_alias_populates_expansions(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """The production-target case: query mentions ``RC``, the active
    pack maps ``RC -> reinforced concrete``, the memory view handed
    to the orchestrator carries ``expansions=("reinforced concrete",)``.
    """
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: _pack_with_alias(),
    )
    svc.run_document_test_query(
        ctx, "doc-1", _document_request("RC beam crack width"),
    )
    [request] = orchestrator.calls
    expansions = request.memory_view.expansions
    # The canonical form lands; the original query is stripped.
    assert "reinforced concrete" in expansions
    assert "RC beam crack width" not in expansions


# ---- 5. Flag ON + query doesn't match → empty expansions ---------


def test_flag_on_query_with_no_alias_match_leaves_expansions_empty(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """No false positives: a query that doesn't mention any alias
    form must NOT generate spurious expansions."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: _pack_with_alias(),
    )
    svc.run_document_test_query(
        ctx, "doc-1",
        _document_request("unrelated topic with no aliases"),
    )
    [request] = orchestrator.calls
    assert request.memory_view.expansions == ()


# ---- 6. Empty / duplicate expansions are stripped ----------------


def test_expansion_list_is_deduped_and_empty_stripped(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """Belt-and-suspenders: even if a custom pack ships a degenerate
    alias entry, the service's stamping path filters empty strings
    and dedupes. The orchestrator's
    ``_expansions_from_memory_view`` does the same on its end —
    pinning both halves keeps the contract resilient."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    # Pack with duplicate alias entries that would resolve to the
    # same canonical form on multiple calls.
    pack = DomainPack(
        id="example.dup", display_name="Dup", version="1",
        extends_document_types=(), keyword_signals=(),
        extraction_targets=(), graph_entity_types=(),
        graph_relationship_types=(), prompt_addon="", overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            entity_aliases=(
                EntityAlias(
                    canonical_name="reinforced concrete",
                    aliases=("RC", "RC", ""),
                ),
            ),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: pack,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _document_request("RC beam"),
    )
    [request] = orchestrator.calls
    expansions = request.memory_view.expansions
    # No empty strings, no duplicates.
    assert "" not in expansions
    assert len(expansions) == len(set(expansions))
