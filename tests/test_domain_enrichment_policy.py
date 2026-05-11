"""Wave 2 completion tests: DomainEnrichmentPolicy + planner consumption.

Covers three contract surfaces:

1. `DomainEnrichmentPolicy` dataclass — vocabulary validation,
   to_dict serialisation, defaults.
2. `DomainPack` YAML loader — civil pack carries an `enrichment_policy`
   block that round-trips through the loader; missing block → defaults.
3. `assess_post_compile_enrich(signals, domain_pack=…)` — policy
   overlays correctly upgrade/downgrade verdicts, apply force /
   denied task lists, and respect blocking conditions.
"""

from __future__ import annotations

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    ENRICHMENT_POLICY_ALWAYS,
    ENRICHMENT_POLICY_AUTO,
    ENRICHMENT_POLICY_NEVER,
    DomainEnrichmentPolicy,
    DomainPack,
)
from j1.processing.enrich_assessment import (
    TASK_IMAGE_CAPTIONING,
    TASK_REQUIREMENT_EXTRACTION,
    TASK_RISK_EXTRACTION,
    TASK_TABLE_ENRICHMENT,
    TASK_VISION_ENRICHMENT,
    EnrichRecommendation,
    SourceSignals,
    assess_post_compile_enrich,
)


# ---- DomainEnrichmentPolicy dataclass ----------------------------


def test_policy_defaults_to_auto_with_empty_lists():
    p = DomainEnrichmentPolicy()
    assert p.policy == "auto"
    assert p.force_recommended_tasks == ()
    assert p.optional_tasks == ()
    assert p.denied_tasks == ()
    assert p.require_enrichment_success is False
    assert p.default_model_tier is None
    assert p.reasoning == ""


def test_policy_vocabulary_constants_are_stable():
    """The wire vocabulary is documented in YAML and rendered by the
    FE — pin the values so a rename here is intentional."""
    assert ENRICHMENT_POLICY_AUTO == "auto"
    assert ENRICHMENT_POLICY_ALWAYS == "always"
    assert ENRICHMENT_POLICY_NEVER == "never"


def test_policy_rejects_unknown_vocabulary_at_construction():
    with pytest.raises(ValueError, match="unknown enrichment policy"):
        DomainEnrichmentPolicy(policy="aggressive")


def test_policy_to_dict_carries_every_field():
    p = DomainEnrichmentPolicy(
        policy="always",
        force_recommended_tasks=("requirement_extraction",),
        denied_tasks=("image_captioning",),
        require_enrichment_success=True,
        default_model_tier="premium",
        reasoning="legal-doc pipeline requires extraction",
    )
    payload = p.to_dict()
    assert payload["policy"] == "always"
    assert payload["force_recommended_tasks"] == ["requirement_extraction"]
    assert payload["denied_tasks"] == ["image_captioning"]
    assert payload["require_enrichment_success"] is True
    assert payload["default_model_tier"] == "premium"
    assert payload["reasoning"] == "legal-doc pipeline requires extraction"


# ---- DomainPack YAML loader -------------------------------------


def test_civil_pack_carries_always_policy_from_yaml():
    pack = build_civil_engineering_pack()
    assert pack.enrichment_policy.policy == ENRICHMENT_POLICY_ALWAYS
    assert TASK_REQUIREMENT_EXTRACTION in pack.enrichment_policy.force_recommended_tasks
    assert TASK_RISK_EXTRACTION in pack.enrichment_policy.force_recommended_tasks
    assert pack.enrichment_policy.default_model_tier == "fast"
    assert pack.enrichment_policy.reasoning  # non-empty


def test_general_pack_uses_auto_policy_defaults():
    """The general pack carries no policy block in YAML → defaults
    to auto/empty so it's a no-op in the assessor."""
    pack = build_general_pack()
    assert pack.enrichment_policy.policy == ENRICHMENT_POLICY_AUTO
    assert pack.enrichment_policy.force_recommended_tasks == ()
    assert pack.enrichment_policy.denied_tasks == ()


# ---- assess_post_compile_enrich domain consumption --------------


def _good_signals() -> SourceSignals:
    """Plain-text-doc compile signals — no images/tables, good quality.
    The rule-based assessor returns OPTIONAL for these; domain policy
    can lift to RECOMMENDED."""
    return SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        total_text_chars=5000,
        text_block_count=20,
    )


def test_assessor_without_pack_matches_legacy_behaviour():
    """Calling without `domain_pack` keeps the pre-Wave-2 verdict
    intact. No domain_id, empty policy dict."""
    plan = assess_post_compile_enrich(_good_signals())
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.recommended_tasks == ()
    assert plan.domain_id is None
    assert plan.domain_enrichment_policy == {}


def test_assessor_with_general_pack_is_a_noop_overlay():
    """The general pack's auto/empty policy should not alter the
    assessor's verdict — same as no-pack except `domain_id` is set."""
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_general_pack(),
    )
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.recommended_tasks == ()
    assert plan.domain_id == "general"
    assert plan.domain_enrichment_policy["policy"] == "auto"


def test_civil_pack_upgrades_optional_to_recommended():
    """policy=always lifts OPTIONAL → RECOMMENDED and force tasks
    show up in recommended_tasks."""
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_civil_engineering_pack(),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_REQUIREMENT_EXTRACTION in plan.recommended_tasks
    assert TASK_RISK_EXTRACTION in plan.recommended_tasks
    assert plan.domain_id == "civil_engineering"
    assert plan.domain_enrichment_policy["policy"] == "always"
    # Force tasks should not appear in skipped_tasks (they moved out
    # of the rule-based "skipped requirement_extraction" path).
    assert TASK_REQUIREMENT_EXTRACTION not in plan.skipped_tasks
    assert TASK_RISK_EXTRACTION not in plan.skipped_tasks
    # Reasoning copy lands in reasons so the FE can render it.
    assert any("domain reasoning" in r for r in plan.reasons)


def test_policy_never_collapses_to_skip_with_blocking_reason():
    """A domain that opts out (policy=never) collapses the verdict
    to SKIP with a domain-cited blocking reason."""
    pack = DomainPack(
        id="opted_out_domain",
        display_name="Opted-Out",
        version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(policy="never"),
    )
    plan = assess_post_compile_enrich(_good_signals(), domain_pack=pack)
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.recommended_tasks == ()
    assert any("policy=never" in b for b in plan.blocking_issues)


def test_denied_tasks_remove_recommendations_added_by_signals():
    """A pack that denies a task should drop it from recommended_tasks
    even when compile signals would otherwise pick it up."""
    pack = DomainPack(
        id="no_images",
        display_name="No-Images",
        version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(
            policy="auto",
            denied_tasks=(TASK_IMAGE_CAPTIONING, TASK_VISION_ENRICHMENT),
        ),
    )
    signals = SourceSignals(
        compile_status="succeeded",
        has_images=True,
        image_count=3,
        total_text_chars=1000,
        text_block_count=10,
    )
    plan = assess_post_compile_enrich(signals, domain_pack=pack)
    assert TASK_IMAGE_CAPTIONING not in plan.recommended_tasks
    assert TASK_VISION_ENRICHMENT not in plan.recommended_tasks
    assert TASK_IMAGE_CAPTIONING in plan.skipped_tasks
    assert TASK_VISION_ENRICHMENT in plan.skipped_tasks


def test_blocking_compile_failure_wins_over_policy_always():
    """policy=always must NEVER override a blocking compile failure.
    SKIP for safety reasons is authoritative."""
    pack = build_civil_engineering_pack()
    plan = assess_post_compile_enrich(
        SourceSignals(compile_status="failed"), domain_pack=pack,
    )
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.blocking_issues  # the original "compile failed" message
    # Domain id is still recorded for FE banner copy.
    assert plan.domain_id == "civil_engineering"


def test_plan_payload_round_trips_with_domain_fields():
    """`PostCompileEnrichPlan.from_payload(plan.to_payload())` must
    preserve the new domain_id + domain_enrichment_policy fields."""
    pack = build_civil_engineering_pack()
    original = assess_post_compile_enrich(_good_signals(), domain_pack=pack)
    payload = original.to_payload()
    from j1.processing.enrich_assessment import PostCompileEnrichPlan
    restored = PostCompileEnrichPlan.from_payload(payload)
    assert restored.domain_id == "civil_engineering"
    assert restored.domain_enrichment_policy["policy"] == "always"
    assert restored.overall_recommendation == original.overall_recommendation
    assert restored.recommended_tasks == original.recommended_tasks


# ---- Enricher prompt_addon plumbing -----------------------------


def test_enricher_accepts_domain_prompt_addon_kwarg():
    """The base enricher learns the addon at construction so the
    LLM call site can prepend it. Empty default keeps legacy
    behaviour for callers that don't pass it."""
    from j1.enrichers import DocumentClassifier
    from j1.profiles.model import Profile

    profile = Profile(profile_id="t", metadata={}, prompts={})
    enricher = DocumentClassifier(
        profile,
        domain_prompt_addon="Civil engineering context.",
        domain_id="civil_engineering",
    )
    assert enricher._domain_prompt_addon == "Civil engineering context."
    assert enricher._domain_id == "civil_engineering"


def test_enricher_metadata_records_domain_provenance():
    """When an enricher runs with a domain addon, every artifact's
    metadata must carry `domain_id` + an `addon_applied` flag so the
    provenance trail is auditable downstream."""
    from j1.enrichers import DocumentClassifier
    from j1.profiles.model import Profile

    profile = Profile(profile_id="t", metadata={}, prompts={})
    enricher = DocumentClassifier(
        profile,
        domain_prompt_addon="Civil context.",
        domain_id="civil_engineering",
    )
    meta = enricher._build_metadata("art-1")
    assert meta["domain_id"] == "civil_engineering"
    assert meta["domain_prompt_addon_applied"] == "true"


def test_enricher_without_domain_addon_keeps_legacy_metadata():
    """No domain context → no domain_id / addon flag in metadata.
    The legacy-shape contract is preserved for runs without a pack."""
    from j1.enrichers import DocumentClassifier
    from j1.profiles.model import Profile

    profile = Profile(profile_id="t", metadata={}, prompts={})
    enricher = DocumentClassifier(profile)
    meta = enricher._build_metadata("art-1")
    assert "domain_id" not in meta
    assert "domain_prompt_addon_applied" not in meta
