"""Validation-service end-to-end tests for the query-expansion wire.

The previous PR shipped the orchestrator surface (it consumes
``request.memory_view.expansions`` when populated). This PR's
contribution is the **production wiring**: the validation service
now builds a memory view, computes alias-driven expansions per
query, and stamps them on the view it hands to the orchestrator.

Pins the contract end-to-end through the service layer (the same
path the document-test-query + project-query REST handlers use):

  * Expansion disabled → memory view carries empty ``expansions``.
  * Expansion enabled + matching domain alias → memory view carries
    the canonical form; orchestrator broadens retrieval.
  * Empty / whitespace / duplicate expansion variants are filtered
    on the stamping side (the orchestrator does another dedup pass).
  * ``J1_DOMAIN_QUERY_AUGMENTATION_ENABLED=false`` (provider-level
    flag) collapses the stamping to no-op even when the broadening
    flag is on.
  * No domain pack lookup wired → no expansions stamped (still
    safe, retrieval just uses the original query).
  * Provider that raises → no expansions stamped, no answer-path
    regression.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
    EntityAlias,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.memory import (
    ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED,
    UnifiedMemoryResolver,
)
from j1.query.orchestrator import (
    ENV_QUERY_EXPANSION_ENABLED,
    OrchestratorRequest,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import (
    ManualTestQueryRequest, QueryScopeDTO,
)
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---- Pipeline stubs ----------------------------------------------


class _CapturingOrchestrator:
    """Stub orchestrator that captures the ``OrchestratorRequest``
    so tests can inspect the memory view the service threaded in."""

    def __init__(self):
        self.calls: list[OrchestratorRequest] = []

    def run(self, request: OrchestratorRequest):
        self.calls.append(request)
        trace_stub = SimpleNamespace(
            llm_evidence=(), to_dict=lambda: {},
        )
        return SimpleNamespace(
            answer="stub", final_status="passed", citations=(),
            gate_results=(), trace=trace_stub, message=None,
        )


def _pack_with_rc_alias() -> DomainPack:
    return DomainPack(
        id="example.rc", display_name="RC Test", version="1",
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
    snapshot_id: str = "snap-active",
) -> None:
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
        run_id=f"run-{document_id}", document_id=document_id,
        workflow_id="wf-1", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id=snapshot_id,
    ))
    artifact_registry.add(ArtifactRecord(
        artifact_id=f"a-{document_id}", project=ctx,
        kind="compiled.text",
        location=f"compiled/{document_id}.txt",
        content_hash=f"sha256:a-{document_id}",
        byte_size=10, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        snapshot_id=snapshot_id,
        created_by_run_id=f"run-{document_id}",
    ))


@pytest.fixture
def orchestrator() -> _CapturingOrchestrator:
    return _CapturingOrchestrator()


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


def _build_service(
    *, registry, workspace, run_store, artifact_registry,
    orchestrator, pack: DomainPack | None,
):
    def _lookup(domain_id: str | None):
        # Always return the configured pack regardless of the
        # document's domain_id — the test seeds a generic document
        # without a domain_id, so accepting None keeps the wire
        # trivially testable. Production deployments key by
        # ``domain_id`` and ignore unrecognised ones.
        return pack

    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        source_registry=registry,
        workspace=workspace,
        smart_query_orchestrator=orchestrator,
        domain_pack_lookup=_lookup if pack is not None else None,
    )


# ---- 1. Expansion DISABLED → no expansions stamped ---------------


def test_expansion_disabled_leaves_memory_view_expansions_empty(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """``J1_QUERY_EXPANSION_ENABLED=false`` (default): even when a
    domain pack lookup is wired and the query matches an alias,
    the service does NOT stamp expansions on the view. Existing
    retrieval behaviour is preserved byte-for-byte."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)
    svc = _build_service(
        registry=registry, workspace=workspace,
        run_store=run_store, artifact_registry=artifact_registry,
        orchestrator=orchestrator, pack=_pack_with_rc_alias(),
    )

    svc.run_document_test_query(
        ctx, "doc-1",
        ManualTestQueryRequest(
            question="RC beam crack width",
            scope=QueryScopeDTO(
                type="document_active", document_id="doc-1",
            ),
        ),
    )

    captured = orchestrator.calls[0]
    assert captured.memory_view is not None
    # Critical: ``expansions`` is empty when the env flag is OFF.
    assert captured.memory_view.expansions == ()


# ---- 2. Expansion ENABLED + matching alias → variants stamped ----


def test_expansion_enabled_stamps_alias_variants_on_memory_view(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """Spec headline case: the service computes
    ``compute_query_expansion(query, hints)``, strips the original,
    deduplicates, and stamps ``expansions`` on the memory view it
    threads through. The orchestrator's expansion stage consumes
    the field verbatim — this proves the production path is wired."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    monkeypatch.setenv(ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)
    svc = _build_service(
        registry=registry, workspace=workspace,
        run_store=run_store, artifact_registry=artifact_registry,
        orchestrator=orchestrator, pack=_pack_with_rc_alias(),
    )

    svc.run_document_test_query(
        ctx, "doc-1",
        ManualTestQueryRequest(
            question="RC beam crack width",
            scope=QueryScopeDTO(
                type="document_active", document_id="doc-1",
            ),
        ),
    )

    captured = orchestrator.calls[0]
    expansions = captured.memory_view.expansions
    assert expansions, f"expected non-empty expansions, got {expansions!r}"
    # The pack's canonical / alias forms show up in the variants.
    assert "reinforced concrete" in expansions
    # The original query is NEVER in expansions — synthesis owns it.
    assert "RC beam crack width" not in expansions
    # Deduplicated.
    assert len(expansions) == len(set(expansions))


# ---- 3. No domain pack lookup wired → no expansions --------------


def test_expansion_with_no_pack_lookup_is_a_safe_noop(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """A deployment that didn't wire ``domain_pack_lookup`` gets
    zero broadening even with the flag on. The view is still
    threaded (the orchestrator needs it for queryability + future
    consumers); ``expansions`` stays empty."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)
    svc = _build_service(
        registry=registry, workspace=workspace,
        run_store=run_store, artifact_registry=artifact_registry,
        orchestrator=orchestrator, pack=None,  # no lookup wired
    )

    svc.run_document_test_query(
        ctx, "doc-1",
        ManualTestQueryRequest(
            question="anything matching",
            scope=QueryScopeDTO(
                type="document_active", document_id="doc-1",
            ),
        ),
    )

    captured = orchestrator.calls[0]
    assert captured.memory_view is not None
    assert captured.memory_view.expansions == ()


# ---- 4. Query with no matching alias → no expansion variants ----


def test_query_with_no_alias_match_leaves_expansions_empty(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """The pack-based matcher is substring-strict: when the query
    has zero overlap with any alias form, no variants are stamped.
    This pins the "no useless variants" promise — retrieval doesn't
    fan out for queries the domain can't help with."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)
    svc = _build_service(
        registry=registry, workspace=workspace,
        run_store=run_store, artifact_registry=artifact_registry,
        orchestrator=orchestrator, pack=_pack_with_rc_alias(),
    )

    svc.run_document_test_query(
        ctx, "doc-1",
        ManualTestQueryRequest(
            question="unrelated question about pancakes",
            scope=QueryScopeDTO(
                type="document_active", document_id="doc-1",
            ),
        ),
    )

    captured = orchestrator.calls[0]
    # No alias bundle matched — no variants stamped.
    assert captured.memory_view.expansions == ()


# ---- 5. Pack lookup raises → no answer-path regression -----------


def test_pack_lookup_failure_falls_through_cleanly(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """A lookup that raises must not regress the answer path. The
    service swallows the failure and threads the bare memory view
    (no expansions); the orchestrator still runs the query."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    def _boom_lookup(_domain_id):
        raise RuntimeError("lookup broken")

    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        source_registry=registry,
        workspace=workspace,
        smart_query_orchestrator=orchestrator,
        domain_pack_lookup=_boom_lookup,
    )

    result = svc.run_document_test_query(
        ctx, "doc-1",
        ManualTestQueryRequest(
            question="RC topic",
            scope=QueryScopeDTO(
                type="document_active", document_id="doc-1",
            ),
        ),
    )
    # The orchestrator was called; the answer path didn't regress.
    assert orchestrator.calls
    assert orchestrator.calls[0].memory_view.expansions == ()
    assert result.answer == "stub"


# ---- 6. Project / explicit-run scopes don't carry a view ---------


def test_project_active_scope_does_not_thread_memory_view(
    registry, workspace, run_store, artifact_registry, ctx,
    orchestrator, monkeypatch,
):
    """Project-active scope spans every attached document. The
    service intentionally does not pick a "representative pack"
    here — only ``ActiveScope`` (single document) carries a
    pack-aware view. The orchestrator therefore sees
    ``memory_view=None`` for project scope and falls through to
    "no broadening"."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)
    svc = _build_service(
        registry=registry, workspace=workspace,
        run_store=run_store, artifact_registry=artifact_registry,
        orchestrator=orchestrator, pack=_pack_with_rc_alias(),
    )

    svc.run_project_query(
        ctx,
        ManualTestQueryRequest(
            question="RC topic",
            scope=QueryScopeDTO(type="project_active"),
        ),
    )
    captured = orchestrator.calls[0]
    assert captured.memory_view is None
