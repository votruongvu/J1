""" closure tests.

Pins the spec-required surface on `PostCompileEnrichPlan` that
landed in this slice:

 * `should_enrich` boolean property.
 * `confidence` derivation (1.0 SKIP / 0.85 policy-driven /
 0.75 strong-signals / 0.5 ambiguous).
 * `expected_outputs` mapping from recommended_tasks.
 * `require_enrichment_success` sourced from domain policy.
 * `model_tier_selection` + `concurrency_hints` sourced from
 domain policy.
 * `warnings` field — distinct from `reasons` + `blocking_issues`.
 * Decision criteria:
 - low compile quality biases OPTIONAL → RECOMMENDED.
 - low parser score biases OPTIONAL → RECOMMENDED.
 - compile warnings present → recorded + biases up.
 * bridge: `build_signals_from_normalized_compile_result`
 feeds the assessor with the typed compile result.
 * Round-trip through `to_payload` / `from_payload` preserves
 every closure field.
"""

from __future__ import annotations

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainPack,
)
from j1.orchestration.activities.payloads import ArtifactActivityResult
from j1.processing.compile_result import normalize_compile_result
from j1.processing.enrich_assessment import (
    TASK_IMAGE_CAPTIONING,
    TASK_QUALITY_ASSESSMENT,
    TASK_REQUIREMENT_EXTRACTION,
    TASK_RISK_EXTRACTION,
    TASK_TABLE_ENRICHMENT,
    TASK_VISION_ENRICHMENT,
    EnrichRecommendation,
    PostCompileEnrichPlan,
    SourceSignals,
    assess_post_compile_enrich,
    build_signals_from_normalized_compile_result,
)


def _good_signals(**overrides) -> SourceSignals:
    base = dict(
        compile_status="succeeded", final_compile_quality="good",
        total_text_chars=5000, text_block_count=20,
    )
    base.update(overrides)
    return SourceSignals(**base)


# ---- should_enrich property ------------------------------------------


def test_should_enrich_is_true_for_optional_recommendation():
    plan = assess_post_compile_enrich(_good_signals())
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.should_enrich is True


def test_should_enrich_is_true_for_recommended():
    plan = assess_post_compile_enrich(
        _good_signals(has_images=True, image_count=2),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert plan.should_enrich is True


def test_should_enrich_is_false_for_skip():
    signals = SourceSignals(compile_status="failed")
    plan = assess_post_compile_enrich(signals)
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.should_enrich is False


# ---- confidence ------------------------------------------------------


def test_confidence_is_1_for_blocking_skip():
    plan = assess_post_compile_enrich(SourceSignals(compile_status="failed"))
    assert plan.confidence == 1.0


def test_confidence_is_high_when_domain_policy_drove_decision():
    """policy=always → confidence reflects the deliberate operator
 choice. 0.85 is the pinned value."""
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_civil_engineering_pack(),
    )
    assert plan.confidence == 0.85


def test_confidence_lifts_when_strong_signals_back_recommendation():
    """No domain policy, but tables/images make RECOMMENDED clearly
 justified. 0.75 is the pinned value."""
    plan = assess_post_compile_enrich(
        _good_signals(has_tables=True, table_count=3),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert plan.confidence == 0.75


def test_confidence_is_lower_for_ambiguous_optional():
    plan = assess_post_compile_enrich(_good_signals())
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.confidence == 0.5


# ---- expected_outputs ------------------------------------------------


def test_expected_outputs_maps_table_enrichment_to_enriched_tables():
    plan = assess_post_compile_enrich(
        _good_signals(has_tables=True, table_count=2),
    )
    assert "enriched.tables" in plan.expected_outputs


def test_expected_outputs_maps_image_tasks_to_visuals_dedup():
    """image_captioning + vision_enrichment both produce
 `enriched.visuals` — the projection deduplicates."""
    plan = assess_post_compile_enrich(
        _good_signals(has_images=True, image_count=1),
    )
    assert plan.expected_outputs.count("enriched.visuals") == 1


def test_expected_outputs_includes_civil_force_tasks():
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_civil_engineering_pack(),
    )
    assert "enriched.requirements" in plan.expected_outputs
    assert "enriched.risks" in plan.expected_outputs


def test_expected_outputs_empty_when_no_tasks_recommended():
    """No-domain SKIP on failed compile → no tasks → no expected
 outputs."""
    plan = assess_post_compile_enrich(SourceSignals(compile_status="failed"))
    assert plan.expected_outputs == ()


# ---- require_enrichment_success + model_tier_selection ---------------


def test_require_enrichment_success_sourced_from_domain_policy():
    pack = DomainPack(
        id="strict_pack", display_name="Strict", version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(
            policy="always", require_enrichment_success=True,
        ),
    )
    plan = assess_post_compile_enrich(_good_signals(), domain_pack=pack)
    assert plan.require_enrichment_success is True


def test_require_enrichment_success_defaults_to_false_no_policy():
    plan = assess_post_compile_enrich(_good_signals())
    assert plan.require_enrichment_success is False


def test_model_tier_selection_carries_policy_default():
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_civil_engineering_pack(),
    )
    assert plan.model_tier_selection == "fast"


def test_concurrency_hints_mirror_model_tier():
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_civil_engineering_pack(),
    )
    assert plan.concurrency_hints.get("default_model_tier") == "fast"


# ---- warnings field --------------------------------------------------


def test_warnings_records_parser_compile_warnings():
    plan = assess_post_compile_enrich(
        _good_signals(compile_warnings=("low-density page 3",)),
    )
    assert "low-density page 3" in plan.warnings


def test_warnings_carries_low_quality_caveat():
    plan = assess_post_compile_enrich(
        _good_signals(final_compile_quality="low"),
    )
    assert any("LOW" in w for w in plan.warnings)


def test_warnings_carries_scanned_pages_caveat():
    plan = assess_post_compile_enrich(
        _good_signals(has_scanned_pages=True),
    )
    assert any("scanned" in w.lower() for w in plan.warnings)


def test_warnings_distinct_from_reasons_and_blocking_issues():
    """`warnings` is non-blocking caveat surface. Reasons explain
 the verdict; blocking_issues lock SKIP. Three separate fields."""
    plan = assess_post_compile_enrich(SourceSignals(compile_status="failed"))
    # blocking_issues populated (terminal SKIP), warnings empty for
    # this case (no parser warnings, no scanned pages).
    assert plan.blocking_issues
    assert plan.reasons
    assert plan.warnings == ()


# ---- decision criteria: low-quality bias ----------------------------


def test_low_compile_quality_lifts_optional_to_recommended():
    """No tables/images, no domain pack — would normally be OPTIONAL.
 With low quality, the assessor should bias up to RECOMMENDED so
 enrichment can add retrieval-friendly metadata."""
    plan = assess_post_compile_enrich(
        _good_signals(final_compile_quality="low"),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_QUALITY_ASSESSMENT in plan.recommended_tasks


def test_low_parser_score_triggers_quality_assessment_even_when_quality_good():
    """`final_compile_quality=good` but parse_quality_score<0.5 is
 a concrete degraded-extraction signal — assessor still adds
 quality_assessment."""
    plan = assess_post_compile_enrich(
        _good_signals(parse_quality_score=0.35),
    )
    assert TASK_QUALITY_ASSESSMENT in plan.recommended_tasks
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED


def test_compile_warnings_bias_optional_to_recommended():
    """Compile warnings = degraded extraction signal even without
 low-quality verdict or low scores. Bias up so enrichment can
 add retrieval-friendly metadata."""
    plan = assess_post_compile_enrich(
        _good_signals(compile_warnings=("page-3 partial", "ocr fallback")),
    )
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert any("warning" in r.lower() for r in plan.reasons)


# ---- bridge --------------------------------------------------


def test_build_signals_from_normalized_compile_result_projects_quality():
    ar = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=["a-1"],
        kinds=("chunk",),
        content_stats={
            "has_images": True, "image_count": 1,
            "page_count": 5, "total_text_chars": 1000,
            "images": [{"image_id": "img-1"}],
            "parse_quality_score": 0.4,
            "text_sufficiency_score": 0.6,
        },
        compile_metrics={
            "chunks_count": 1, "extracted_text_chars": 1000,
            "plan_warnings": ["w1"],
        },
    )
    normalized = normalize_compile_result(
        ar, document_id="doc-1", final_quality_verdict="low",
    )
    signals = build_signals_from_normalized_compile_result(normalized)
    assert signals.compile_status == "succeeded"
    assert signals.final_compile_quality == "low"
    assert signals.parse_quality_score == 0.4
    assert signals.text_sufficiency_score == 0.6
    assert signals.has_images is True
    assert signals.image_count == 1
    assert "w1" in signals.compile_warnings


def test_bridged_signals_feed_into_assessor_correctly():
    ar = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=["a-1"],
        kinds=("chunk",),
        content_stats={
            "has_tables": True, "table_count": 2,
            "page_count": 8, "total_text_chars": 6000,
            "parse_quality_score": 0.9,
        },
        compile_metrics={"chunks_count": 1, "extracted_text_chars": 6000},
    )
    normalized = normalize_compile_result(ar, document_id="doc-1")
    signals = build_signals_from_normalized_compile_result(normalized)
    plan = assess_post_compile_enrich(signals)
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_TABLE_ENRICHMENT in plan.recommended_tasks


# ---- round-trip preserves closure fields -----------------------------


def test_round_trip_preserves_every_closure_field():
    pack = build_civil_engineering_pack()
    original = assess_post_compile_enrich(
        _good_signals(
            has_images=True, image_count=2,
            compile_warnings=("w1",),
            parse_quality_score=0.4,
        ),
        domain_pack=pack,
    )
    payload = original.to_payload()
    # Payload carries every closure field on the wire.
    for key in (
        "should_enrich",
        "confidence",
        "expected_outputs",
        "require_enrichment_success",
        "model_tier_selection",
        "concurrency_hints",
        "warnings",
    ):
        assert key in payload, f"missing closure field {key!r} on payload"
    restored = PostCompileEnrichPlan.from_payload(payload)
    assert restored.should_enrich == original.should_enrich
    assert restored.confidence == original.confidence
    assert restored.expected_outputs == original.expected_outputs
    assert restored.require_enrichment_success == original.require_enrichment_success
    assert restored.model_tier_selection == original.model_tier_selection
    assert restored.concurrency_hints == original.concurrency_hints
    assert restored.warnings == original.warnings


# ---- general pack is a no-op overlay --------------------------------


def test_general_pack_does_not_set_require_success_or_model_tier():
    plan = assess_post_compile_enrich(
        _good_signals(), domain_pack=build_general_pack(),
    )
    assert plan.require_enrichment_success is False
    assert plan.model_tier_selection is None
    assert plan.concurrency_hints == {}
