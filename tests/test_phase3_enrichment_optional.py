"""Phase-3 tests: Domain Enrichment as optional memory augmentation.

Pins the contracts the spec calls out:

  1. ``J1_DOMAIN_ENRICHMENT_AUTO_ENABLED=false`` (default) wins over
     any planner recommendation — the verdict becomes SKIP with an
     audit-friendly reason.
  2. A domain pack with ``ENRICHMENT_POLICY_ALWAYS`` overrides the
     env-disabled default — compliance-driven opt-in survives.
  3. The Unified Memory View surfaces the new Phase-3 augmentation
     counts (``domain_terms_count``, ``aliases_count``,
     ``quality_warnings_count``, ``last_enriched_at``).
  4. ``DomainPackAugmentationProvider`` reads aliases / terms from
     the active domain pack, NOT from hard-coded query code.
  5. ``J1_DOMAIN_QUERY_AUGMENTATION_ENABLED=false`` makes the
     provider return empty hints so A/B comparisons are trivial.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.memory import (
    DomainPackAugmentationProvider,
    ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED,
    NoOpAugmentationProvider,
    QueryableStatus,
    UnifiedMemoryResolver,
    is_augmentation_enabled,
)
from j1.processing.enrich_assessment import (
    ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED,
    EnrichRecommendation,
    SourceSignals,
    assess_post_compile_enrich,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---- 1 + 2: Auto-enrichment env gate -------------------------------


def _rich_signals() -> SourceSignals:
    """Signals strong enough that the rule-based planner would
    recommend enrichment if the gate weren't in the way."""
    return SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        text_block_count=20,
        total_text_chars=5000,
        has_images=True,
        image_count=5,
        has_tables=True,
        table_count=3,
    )


def test_auto_enrichment_disabled_by_default_forces_skip(monkeypatch):
    """Default deployment posture: the env flag is unset →
    auto-enrichment is OFF → planner reports SKIP regardless of
    compile signals. The reason is operator-facing."""
    monkeypatch.delenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, raising=False)

    plan = assess_post_compile_enrich(_rich_signals())

    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert any(
        "auto_enrichment_disabled" in r for r in plan.reasons
    ), plan.reasons
    assert plan.blocking_issues, plan.blocking_issues


def test_auto_enrichment_env_override_false_wins_over_assessment(
    monkeypatch,
):
    """Even when the rule-based assessor would say RECOMMENDED,
    the env gate forces SKIP. The user-facing recommendation
    surfaces the gate, not the underlying signal — operators see
    "deployment said no" rather than a confusing "tables present
    but skipped"."""
    monkeypatch.setenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, "false")

    plan = assess_post_compile_enrich(_rich_signals())
    assert plan.overall_recommendation == EnrichRecommendation.SKIP


def test_auto_enrichment_explicit_true_lets_planner_recommend(monkeypatch):
    """Opt-in: when the deployment turns auto-enrichment ON, the
    rule-based planner runs to completion and emits its real
    verdict."""
    monkeypatch.setenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, "true")

    plan = assess_post_compile_enrich(_rich_signals())
    assert plan.overall_recommendation in (
        EnrichRecommendation.RECOMMENDED, EnrichRecommendation.REQUIRED,
    )


def test_domain_policy_always_overrides_env_disabled(monkeypatch):
    """Per-domain compliance opt-in: a pack with
    ``ENRICHMENT_POLICY_ALWAYS`` bypasses the env gate so the
    deployment-wide disable doesn't override a compliance
    requirement."""
    monkeypatch.delenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, raising=False)
    from j1.domains.models import (
        DomainEnrichmentPolicy,
        DomainExtractionHints,
        DomainPack,
        DomainPromptPack,
        DomainValidationRules,
        ENRICHMENT_POLICY_ALWAYS,
    )
    pack = DomainPack(
        id="compliance.always",
        display_name="Compliance Always",
        version="1",
        extends_document_types=(),
        keyword_signals=(),
        extraction_targets=(),
        graph_entity_types=(),
        graph_relationship_types=(),
        prompt_addon="",
        overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(
            policy=ENRICHMENT_POLICY_ALWAYS,
        ),
        extraction_hints=DomainExtractionHints(),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )

    plan = assess_post_compile_enrich(
        _rich_signals(), domain_pack=pack,
    )
    # ALWAYS policy bypasses the env gate.
    assert plan.overall_recommendation in (
        EnrichRecommendation.RECOMMENDED, EnrichRecommendation.REQUIRED,
    )


# ---- 3: UMV Phase-3 augmentation counts ----------------------------


@pytest.fixture
def resolver(registry, workspace, artifact_registry):
    return UnifiedMemoryResolver(
        registry=registry,
        run_store=JsonlIngestionRunStore(workspace),
        artifact_registry=artifact_registry,
    )


def _seed_queryable_doc(
    registry, run_store, artifact_registry, ctx, *,
    enrichment_metadata=None,
):
    """Seed an attached document with a compile artifact + an
    optional enriched artifact carrying ``enrichment_metadata``."""
    registry.add(DocumentRecord(
        document_id="doc-1", project=ctx,
        original_filename="doc-1.pdf", stored_filename="doc-1.pdf",
        mime_type="application/pdf", file_size=42,
        checksum="sha256:doc-1",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached", active_snapshot_id="snap-active",
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id="r-baseline", document_id="doc-1",
        workflow_id="wf-baseline", workflow_run_id=None,
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
        source_document_ids=["doc-1"],
        snapshot_id="snap-active",
        created_by_run_id="r-baseline",
    ))
    if enrichment_metadata is not None:
        enriched_at = enrichment_metadata.pop(
            "_updated_at", _NOW.replace(hour=14),
        )
        run_store.upsert(ctx, IngestionRun(
            run_id="r-enrich", document_id="doc-1",
            workflow_id="wf-enrich", workflow_run_id=None,
            status=RunStatus.SUCCEEDED,
            started_at=enriched_at, updated_at=enriched_at,
            completed_at=enriched_at,
            metadata={
                "manual_action_source_snapshot_id": "snap-active",
            },
            run_type="run_domain_enrichment",
            target_snapshot_id="snap-active",
        ))
        artifact_registry.add(ArtifactRecord(
            artifact_id="a-enrich", project=ctx,
            kind="enriched.metadata",
            location="enriched/a.json",
            content_hash="sha256:a-enrich",
            byte_size=10, status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=enriched_at, updated_at=enriched_at,
            source_document_ids=["doc-1"],
            snapshot_id="snap-active",
            created_by_run_id="r-enrich",
            metadata=enrichment_metadata,
        ))


def test_umv_surfaces_zero_counts_when_no_enrichment(
    registry, workspace, artifact_registry, resolver, ctx,
):
    """Phase-3 fields default to ``0`` / ``None`` when no
    enrichment artifact is attached to the active snapshot."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_queryable_doc(registry, run_store, artifact_registry, ctx)

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable_status == QueryableStatus.QUERYABLE
    assert view.domain_terms_count == 0
    assert view.aliases_count == 0
    assert view.quality_warnings_count == 0
    assert view.last_enriched_at is None


def test_umv_surfaces_enrichment_counts_from_artifact_metadata(
    registry, workspace, artifact_registry, resolver, ctx,
):
    """The resolver reads the per-snapshot enrichment counts from
    the artifact's metadata. Multiple keys are accepted so producers
    that stamp counts under different names all work."""
    run_store = JsonlIngestionRunStore(workspace)
    enriched_at = datetime(2026, 5, 15, 14, 0, 0, tzinfo=timezone.utc)
    _seed_queryable_doc(
        registry, run_store, artifact_registry, ctx,
        enrichment_metadata={
            "domain_terms_count": 7,
            "aliases_count": 3,
            "quality_warnings_count": 1,
            "_updated_at": enriched_at,
        },
    )

    view = resolver.resolve_document_active_memory(ctx, "doc-1")

    assert view.queryable_status == QueryableStatus.ENRICHMENT_AVAILABLE
    assert view.domain_terms_count == 7
    assert view.aliases_count == 3
    assert view.quality_warnings_count == 1
    assert view.last_enriched_at == enriched_at


def test_umv_derives_counts_from_list_metadata(
    registry, workspace, artifact_registry, resolver, ctx,
):
    """Forgiving derivation: when the producer stamps the raw lists
    instead of pre-computed counts, the resolver derives counts via
    ``len(...)``. Pre-Phase-3 enrichment producers do this."""
    run_store = JsonlIngestionRunStore(workspace)
    _seed_queryable_doc(
        registry, run_store, artifact_registry, ctx,
        enrichment_metadata={
            "terminology_map": ["BOQ", "RFI", "ACI 318"],
            "aliases": [("RFI", "request for information")],
            "warnings": ["table 4 has missing units"],
        },
    )

    view = resolver.resolve_document_active_memory(ctx, "doc-1")
    assert view.domain_terms_count == 3
    assert view.aliases_count == 1
    assert view.quality_warnings_count == 1


# ---- 4 + 5: DomainQueryAugmentationProvider ------------------------


def _document_memory_view_stub():
    """Build a minimal ``DocumentMemoryView`` for provider unit
    tests. The provider doesn't read most fields — supplying a
    queryable view is enough to satisfy the contract."""
    from j1.memory import DocumentMemoryView, MemoryScope
    return DocumentMemoryView(
        scope=MemoryScope.DOCUMENT_ACTIVE,
        project_id="alpha",
        document_id="doc-1",
        snapshot_id="snap-active",
        queryable_status=QueryableStatus.QUERYABLE,
    )


def test_provider_reads_terms_from_domain_pack(monkeypatch):
    """The provider's terms come from the domain pack's
    ``extraction_hints.terminology_hints``. NO query-layer hard-
    coding — passing a different pack changes the output."""
    monkeypatch.setenv(ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED, "true")
    from j1.domains.models import (
        DomainEnrichmentPolicy, DomainExtractionHints, DomainPack,
        DomainPromptPack, DomainValidationRules,
    )
    pack = DomainPack(
        id="example.test",
        display_name="Example Test",
        version="1",
        extends_document_types=(),
        keyword_signals=(),
        extraction_targets=(),
        graph_entity_types=(),
        graph_relationship_types=(),
        prompt_addon="",
        overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            terminology_hints=("BOQ", "RFI"),
            retrieval_hints=("bill of quantities",),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )
    provider = DomainPackAugmentationProvider(pack=pack)

    hints = provider.hints_for(_document_memory_view_stub(), "anything?")

    assert hints.source == "domain_pack"
    assert hints.domain_terms == ("BOQ", "RFI")
    assert "BOQ" in hints.recommended_expansions
    assert "bill of quantities" in hints.recommended_expansions
    # Aliases stay empty in Phase 3 — Phase 4 (entity normalization)
    # will populate them.
    assert hints.aliases == ()


def test_provider_returns_empty_when_no_pack():
    """The provider construction-time pack reference is the source
    of truth. Without one, hints are empty / source='disabled' —
    the call site sees a uniform shape either way."""
    provider = DomainPackAugmentationProvider(pack=None)
    hints = provider.hints_for(_document_memory_view_stub(), "anything?")
    assert hints.domain_terms == ()
    assert hints.recommended_expansions == ()
    assert hints.source == "disabled"


def test_provider_returns_empty_when_feature_flag_off(monkeypatch):
    """A/B comparison support: when the augmentation feature flag
    is off, even a configured pack provider returns empty hints.
    Callers don't need to branch on the flag separately."""
    monkeypatch.setenv(ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED, "false")
    from j1.domains.models import (
        DomainEnrichmentPolicy, DomainExtractionHints, DomainPack,
        DomainPromptPack, DomainValidationRules,
    )
    pack = DomainPack(
        id="example.test", display_name="Example", version="1",
        extends_document_types=(), keyword_signals=(),
        extraction_targets=(), graph_entity_types=(),
        graph_relationship_types=(), prompt_addon="", overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            terminology_hints=("anything",),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )
    provider = DomainPackAugmentationProvider(pack=pack)

    hints = provider.hints_for(_document_memory_view_stub(), "anything?")
    assert hints.domain_terms == ()
    assert hints.source == "disabled"


def test_noop_provider_always_returns_empty():
    """The fallback provider is the trivial "no augmentation"
    surface used when no domain pack is selected."""
    provider = NoOpAugmentationProvider()
    hints = provider.hints_for(_document_memory_view_stub(), "anything?")
    assert hints.domain_terms == ()
    assert hints.aliases == ()
    assert hints.recommended_expansions == ()
    assert hints.source == "disabled"


def test_is_augmentation_enabled_defaults_to_true():
    """The feature flag default lets a deployment without env
    config still surface the augmentation panel."""
    assert is_augmentation_enabled({}) is True


# ---- 6: Domain isolation — civil pack uses pack hints, not core ---


def test_civil_engineering_aliases_live_in_pack_not_query_layer():
    """Sanity: the civil-engineering pack must populate its own
    terminology hints. The query layer (``src/j1/query/``) MUST
    NOT contain hard-coded ACI / BOQ / etc. strings — this is
    enforced by code inspection at build time + this regression
    check that asserts the pack ships its own vocabulary."""
    from j1.domains.civil_engineering.pack import (
        build_civil_engineering_pack,
    )
    pack = build_civil_engineering_pack()
    hints = pack.extraction_hints
    # The pack ships SOME terminology / retrieval hints (the exact
    # vocabulary may evolve; what matters is they're not empty).
    has_any_domain_vocab = (
        bool(hints.terminology_hints) or bool(hints.retrieval_hints)
        or bool(hints.entity_hints) or bool(hints.metadata_fields)
        or bool(pack.keyword_signals)
    )
    assert has_any_domain_vocab, (
        "Civil engineering pack must ship its own vocabulary; "
        "domain-specific strings MUST NOT live in core query code."
    )
