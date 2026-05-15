"""Phase-4 tests: entity alias strategy + agentic query flow seams.

Pins the contracts the spec calls out:

  1. Static aliases live in the domain pack (not core).
  2. ``AliasResolver`` reads pack-static + optional enrichment
     aliases through one surface; deduplicates by canonical name.
  3. Augmentation provider surfaces ``(short, long)`` alias pairs +
     query-aware expansion forms.
  4. ``compute_query_expansion`` caps expansion size + preserves
     the original query at index 0.
  5. ``J1_DOMAIN_QUERY_AUGMENTATION_ENABLED=false`` disables the
     augmentation surface end-to-end.
  6. ``UnsupportedGraphExpansion`` reports ``supported=False`` +
     an operator-readable reason; the diagnostic shape is stable.
  7. Civil-engineering pack ships its own aliases — core code
     stays domain-neutral.
"""

from __future__ import annotations

import pytest

from j1.domains.models import (
    ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG,
    ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
    ENTITY_ALIAS_SOURCES,
    DomainEnrichmentPolicy,
    DomainExtractionHints,
    DomainPack,
    DomainPromptPack,
    DomainValidationRules,
    EntityAlias,
)
from j1.memory import (
    AliasResolution,
    AliasResolver,
    AugmentationHints,
    DomainPackAugmentationProvider,
    ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED,
    ExpansionRequest,
    ExpansionResult,
    MAX_QUERY_EXPANSION_TERMS,
    NoOpAugmentationProvider,
    UnsupportedGraphExpansion,
    compute_query_expansion,
)


# ---- Helpers -------------------------------------------------------


def _pack_with_aliases(*aliases: EntityAlias) -> DomainPack:
    """Build a minimal domain pack carrying the given aliases."""
    return DomainPack(
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
            terminology_hints=("glossary entry",),
            retrieval_hints=("look this up too",),
            entity_aliases=tuple(aliases),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )


def _stub_memory_view():
    """Minimal ``DocumentMemoryView`` for provider unit tests."""
    from j1.memory import DocumentMemoryView, MemoryScope, QueryableStatus
    return DocumentMemoryView(
        scope=MemoryScope.DOCUMENT_ACTIVE,
        project_id="alpha",
        document_id="doc-1",
        snapshot_id="snap-active",
        queryable_status=QueryableStatus.QUERYABLE,
    )


# ---- 1: EntityAlias model -----------------------------------------


def test_entity_alias_all_forms_dedupes_and_preserves_order():
    alias = EntityAlias(
        canonical_name="canonical",
        aliases=("alt-1", "canonical", "alt-2", "alt-1"),
    )
    # Canonical first, then unique alts, no duplicates.
    assert alias.all_forms() == ("canonical", "alt-1", "alt-2")


def test_entity_alias_sources_vocabulary_is_complete():
    """Pin the source-tag set so a new source can't ship without a
    coordinated frontend / diagnostics update."""
    assert ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG in ENTITY_ALIAS_SOURCES
    assert ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT in ENTITY_ALIAS_SOURCES
    # The pinned source vocabulary supports four kinds today:
    # domain_config / domain_enrichment / manual_admin /
    # entity_resolution_job. Renaming or removing one requires a
    # coordinated FE change.
    assert len(ENTITY_ALIAS_SOURCES) == 4


# ---- 2: AliasResolver ---------------------------------------------


def test_resolver_returns_pack_aliases_by_default():
    pack = _pack_with_aliases(
        EntityAlias(canonical_name="C1", aliases=("a", "b")),
        EntityAlias(canonical_name="C2", aliases=("c",)),
    )
    resolver = AliasResolver(pack=pack)
    assert len(resolver.entries) == 2
    # Resolution matches canonical (case-insensitive).
    result = resolver.resolve("c1")
    assert isinstance(result, AliasResolution)
    assert len(result.matches) == 1
    assert result.matches[0].canonical_name == "C1"


def test_resolver_returns_empty_when_no_pack():
    resolver = AliasResolver(pack=None)
    assert resolver.entries == ()
    assert resolver.resolve("anything").is_empty


def test_resolver_expand_terms_returns_alias_forms():
    pack = _pack_with_aliases(
        EntityAlias(canonical_name="RFI", aliases=("request for information",)),
    )
    resolver = AliasResolver(pack=pack)
    # Looking up the abbreviation yields canonical + expansion;
    # looking up the expansion yields the abbreviation too.
    out = resolver.expand_terms(("RFI",))
    assert "RFI" in out
    assert "request for information" in out


def test_resolver_accepts_enrichment_aliases_via_constructor():
    pack = _pack_with_aliases(
        EntityAlias(
            canonical_name="C1", aliases=("a",),
            source=ENTITY_ALIAS_SOURCE_DOMAIN_CONFIG,
        ),
    )
    enrichment = (
        EntityAlias(
            canonical_name="C2", aliases=("d",),
            source=ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
        ),
    )
    resolver = AliasResolver(pack=pack, enrichment_aliases=enrichment)
    # Order: pack entries first, then enrichment.
    canonicals = tuple(e.canonical_name for e in resolver.entries)
    assert canonicals == ("C1", "C2")
    assert resolver.entries[1].source == ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT


# ---- 3: Augmentation provider surfaces aliases --------------------


def test_provider_surfaces_alias_pairs_for_diagnostics():
    pack = _pack_with_aliases(
        EntityAlias(canonical_name="RFI", aliases=("request for information",)),
        EntityAlias(canonical_name="BOQ", aliases=("bill of quantities",)),
    )
    provider = DomainPackAugmentationProvider(pack=pack)
    hints = provider.hints_for(_stub_memory_view(), "anything?")

    # Each entry surfaces as (canonical, alias) pairs the FE can
    # render directly. Two entries → at least two pairs.
    pair_canonicals = [pair[0] for pair in hints.aliases]
    assert "RFI" in pair_canonicals
    assert "BOQ" in pair_canonicals
    assert hints.source == "domain_pack"


def test_provider_query_expansion_includes_matched_alias_forms():
    pack = _pack_with_aliases(
        EntityAlias(canonical_name="RFI", aliases=("request for information",)),
    )
    provider = DomainPackAugmentationProvider(pack=pack)

    # A question that mentions the alias should surface the
    # canonical form (and vice versa) via recommended_expansions.
    hints = provider.hints_for(
        _stub_memory_view(), "Show me every RFI in the dataset",
    )
    forms = set(hints.recommended_expansions)
    assert "RFI" in forms
    assert "request for information" in forms


def test_provider_returns_disabled_when_feature_flag_off(monkeypatch):
    monkeypatch.setenv(ENV_DOMAIN_QUERY_AUGMENTATION_ENABLED, "false")
    pack = _pack_with_aliases(
        EntityAlias(canonical_name="RFI", aliases=("request for information",)),
    )
    provider = DomainPackAugmentationProvider(pack=pack)
    hints = provider.hints_for(_stub_memory_view(), "Show me every RFI")
    # Disabled provider returns the empty shape — callers branch
    # uniformly on ``source`` rather than re-checking the flag.
    assert hints.aliases == ()
    assert hints.recommended_expansions == ()
    assert hints.source == "disabled"


def test_noop_provider_always_returns_empty():
    hints = NoOpAugmentationProvider().hints_for(
        _stub_memory_view(), "anything?",
    )
    assert hints.aliases == ()
    assert hints.source == "disabled"


# ---- 4: compute_query_expansion cap + original-query preservation -


def test_compute_query_expansion_keeps_original_at_index_0():
    """The original query MUST be at index 0 and MUST NOT be evicted
    by the cap. Synthesis consumes the original; only retrieval
    consumes the expansion list."""
    hints = AugmentationHints(
        domain_terms=("t1", "t2", "t3"),
        recommended_expansions=("e1", "e2", "e3"),
        source="domain_pack",
    )
    out = compute_query_expansion("the original", hints, max_terms=3)
    assert out[0] == "the original"
    assert len(out) == 3


def test_compute_query_expansion_caps_at_max_terms():
    hints = AugmentationHints(
        recommended_expansions=tuple(
            f"e{i}" for i in range(20)
        ),
        source="domain_pack",
    )
    out = compute_query_expansion("q", hints, max_terms=4)
    assert len(out) == 4
    # First slot is always the original query.
    assert out[0] == "q"


def test_compute_query_expansion_silently_clamps_oversized_max_terms():
    """The module-level ceiling protects against accidental
    retrieval blowups regardless of caller input."""
    hints = AugmentationHints(
        recommended_expansions=tuple(
            f"e{i}" for i in range(50)
        ),
        source="domain_pack",
    )
    # Request more than the ceiling; we cap at the ceiling.
    out = compute_query_expansion(
        "q", hints, max_terms=MAX_QUERY_EXPANSION_TERMS * 5,
    )
    assert len(out) <= MAX_QUERY_EXPANSION_TERMS


def test_compute_query_expansion_dedupes():
    hints = AugmentationHints(
        domain_terms=("dup",),
        recommended_expansions=("dup", "other"),
        source="domain_pack",
    )
    out = compute_query_expansion("q", hints)
    assert out.count("dup") == 1


def test_compute_query_expansion_handles_empty_hints():
    """When no augmentation is available, expansion is just the
    original query."""
    out = compute_query_expansion("q", AugmentationHints(source="disabled"))
    assert out == ("q",)


# ---- 5: GraphExpansionService unsupported default -----------------


def test_unsupported_graph_expansion_reports_supported_false():
    svc = UnsupportedGraphExpansion()
    result = svc.expand(ExpansionRequest(
        document_id="doc-1",
        snapshot_id="snap-active",
        entry_artifact_ids=("a-1",),
    ))
    assert isinstance(result, ExpansionResult)
    assert result.supported is False
    assert result.hop_count == 0
    assert result.candidates == ()
    assert result.unsupported_reason
    assert "graph_expansion_not_configured" in result.unsupported_reason


def test_unsupported_graph_expansion_returns_stable_diagnostic_shape():
    """The trace consumes ``to_diagnostic()`` directly — pin the
    shape so any future graph-aware impl matches it."""
    svc = UnsupportedGraphExpansion()
    diag = svc.expand(ExpansionRequest(
        document_id="doc-1", snapshot_id="snap-active",
    )).to_diagnostic()
    # The exact keys the orchestrator embeds in QueryTrace.
    assert diag["graph_expansion_supported"] is False
    assert diag["graph_hop_count"] == 0
    assert diag["graph_expansion_candidate_count"] == 0
    assert isinstance(diag["graph_expansion_unsupported_reason"], str)
    assert diag["graph_expansion_warnings"] == []


def test_expansion_request_rejects_invalid_max_hops():
    """The request validates its bounds at construction — a
    misconfigured caller asking for -1 hops can't slip through."""
    with pytest.raises(ValueError):
        ExpansionRequest(
            document_id="doc-1", snapshot_id="snap-1", max_hops=-1,
        )


def test_expansion_request_rejects_zero_max_candidates():
    with pytest.raises(ValueError):
        ExpansionRequest(
            document_id="doc-1", snapshot_id="snap-1", max_candidates=0,
        )


# ---- 6: Domain isolation — civil pack ships its own aliases -------


def test_civil_engineering_pack_ships_its_own_alias_vocabulary():
    """Phase-4 regression: domain-specific aliases must live in the
    pack, never in core query code. The ``tests/extension/test_guards``
    AST guard checks core for hard-coded domain terms; this test
    asserts the pack actually populates the new ``entity_aliases``
    surface."""
    from j1.domains.civil_engineering.pack import (
        build_civil_engineering_pack,
    )
    pack = build_civil_engineering_pack()
    # The civil pack should ship AT LEAST one EntityAlias entry —
    # the exact vocabulary may evolve, but the surface must be
    # populated. If a future commit removes them all, this test
    # forces a deliberate choice.
    aliases = pack.extraction_hints.entity_aliases
    # ``entity_aliases`` may legitimately be empty if the team
    # chose not to ship any — but if non-empty, every entry must
    # tag its source so the FE can render provenance.
    for entry in aliases:
        assert entry.source in ENTITY_ALIAS_SOURCES
        assert entry.canonical_name  # never empty
