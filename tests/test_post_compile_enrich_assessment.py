"""Tests for the rule-based post-compile enrich assessor.

Pins the verdict matrix the workflow consumes downstream:

 * compile failed / final quality failed → SKIP with blocking_issues
 * empty document (no text/images/tables) → SKIP
 * tables present → recommends table_enrichment
 * images present → recommends image_captioning + vision_enrichment
 * low compile quality → recommends quality_assessment
 * no rich signals → OPTIONAL (with informational reason)

The assessor is pure; tests construct `SourceSignals` directly and
assert on the resulting `PostCompileEnrichPlan` shape. No I/O, no
LLM, no Temporal."""

from __future__ import annotations

import pytest

from j1.processing.enrich_assessment import (
    DECISION_SOURCE_RULE_BASED,
    DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM,
    ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED,
    EnrichRecommendation,
    FastLLMRefinement,
    PostCompileEnrichPlan,
    SCHEMA_VERSION,
    SourceSignals,
    TASK_IMAGE_CAPTIONING,
    TASK_QUALITY_ASSESSMENT,
    TASK_TABLE_ENRICHMENT,
    TASK_VISION_ENRICHMENT,
    apply_fast_llm_refinement,
    assess_post_compile_enrich,
    build_signals_from_compile_metrics,
)


@pytest.fixture(autouse=True)
def _enable_auto_enrichment(monkeypatch):
    """The planner's rule-based unit tests assert on the verdict
    AFTER all overlays — including the deployment-wide auto-run gate
    (``J1_DOMAIN_ENRICHMENT_AUTO_ENABLED``, default ``false``). Flip
    the env var to ``true`` so these tests continue to exercise the
    rule-based + domain-policy logic without the gate forcing every
    verdict to SKIP. Dedicated tests in
    ``test_auto_enrichment_env_gate.py`` cover the gate behaviour
    itself."""
    monkeypatch.setenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, "true")


def _ok_signals(**overrides) -> SourceSignals:
    """Default = succeeded compile, good quality, single text block."""
    base = dict(
        compile_status="succeeded",
        final_compile_quality="good",
        text_block_count=1,
        total_text_chars=100,
    )
    base.update(overrides)
    return SourceSignals(**base)


# ---- Blocking paths ------------------------------------------------


def test_compile_failed_returns_skip_with_blocker():
    plan = assess_post_compile_enrich(
        SourceSignals(compile_status="failed"),
    )
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.blocking_issues, "blocking_issues must explain SKIP"
    assert "compile failed" in plan.blocking_issues[0].lower()
    assert plan.recommended_tasks == ()
    assert plan.decision_source == DECISION_SOURCE_RULE_BASED


def test_final_quality_failed_returns_skip():
    plan = assess_post_compile_enrich(
        _ok_signals(final_compile_quality="failed"),
    )
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.blocking_issues
    assert any("FAILED" in b for b in plan.blocking_issues)


def test_affirmative_empty_document_returns_skip():
    """SKIP only with positive evidence of an empty doc:
 page_count>0 + zero counts everywhere. Catches the
 'compile parsed a real PDF but found nothing useful' case."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        page_count=4,
        total_text_chars=0,
        text_block_count=0,
        image_count=0,
        table_count=0,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.blocking_issues
    assert "no content" in plan.blocking_issues[0].lower()


def test_no_signals_falls_through_to_optional():
    """Defaults (page_count=None, all counts=0) must NOT trip the
 empty-doc SKIP path — the absence of metrics isn't proof of an
 empty document. Test fakes + legacy compilers that don't surface
 content_stats fall through to OPTIONAL."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
    ))
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.blocking_issues == ()


def test_page_count_zero_does_not_trigger_skip():
    """page_count == 0 should NOT trip SKIP. Some sources are
 legitimately page-less (plaintext / single-stream documents);
 the SKIP rule reserves itself for `page_count > 0` + zero
 content as positive evidence."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        page_count=0,
        text_block_count=0,
        image_count=0,
        table_count=0,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.blocking_issues == ()


def test_page_count_unknown_does_not_trigger_skip_even_with_zero_counts():
    """Even when text/image/table counts are zero, SKIP must NOT
 fire if page_count is unknown (None) — we don't have positive
 evidence the document is empty."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        page_count=None,
        text_block_count=0,
        image_count=0,
        table_count=0,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.blocking_issues == ()


def test_compile_failed_emits_blocking_issue_not_misleading_skip():
    """Compile-failed → SKIP with blocking issue mentioning compile
 failure (NOT 'no content blocks' which suggests a successful
 compile that produced nothing). Keeps operator messages
 diagnostic."""
    plan = assess_post_compile_enrich(SourceSignals(compile_status="failed"))
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert any(
        "compile failed" in b.lower()
        for b in plan.blocking_issues
    )
    assert not any(
        "no content blocks" in b.lower()
        for b in plan.blocking_issues
    )


def test_page_count_positive_with_only_text_chars_does_not_skip():
    """Edge case: parser surfaced text content as `total_text_chars`
 rather than block counts. SKIP must not fire — text exists."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        page_count=10,
        total_text_chars=5000,
        text_block_count=0,
        image_count=0,
        table_count=0,
    ))
    assert plan.overall_recommendation != EnrichRecommendation.SKIP


def test_page_count_positive_only_images_does_not_skip():
    """Image-only document with usable page_count — there's content
 (images) even though text counts are zero. SKIP must not fire."""
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        page_count=4,
        total_text_chars=0,
        text_block_count=0,
        image_count=12,
        table_count=0,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert "image_captioning" in plan.recommended_tasks


# ---- Task-by-task recommendations ----------------------------------


def test_tables_present_recommends_table_enrichment():
    plan = assess_post_compile_enrich(_ok_signals(
        has_tables=True, table_count=3,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_TABLE_ENRICHMENT in plan.recommended_tasks
    assert TASK_TABLE_ENRICHMENT not in plan.skipped_tasks


def test_images_present_recommends_image_and_vision():
    plan = assess_post_compile_enrich(_ok_signals(
        has_images=True, image_count=2,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_IMAGE_CAPTIONING in plan.recommended_tasks
    assert TASK_VISION_ENRICHMENT in plan.recommended_tasks


def test_low_quality_recommends_quality_assessment():
    plan = assess_post_compile_enrich(_ok_signals(
        final_compile_quality="low",
    ))
    assert TASK_QUALITY_ASSESSMENT in plan.recommended_tasks
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED


def test_text_only_document_is_optional():
    """Plain text doc: no tables, no images, normal quality.
 Verdict = OPTIONAL (operator can still opt in)."""
    plan = assess_post_compile_enrich(_ok_signals())
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.recommended_tasks == ()
    assert TASK_TABLE_ENRICHMENT in plan.skipped_tasks
    assert TASK_IMAGE_CAPTIONING in plan.skipped_tasks
    # Reason explains why
    assert any("optional" in r.lower() for r in plan.reasons)


# ---- Source signals snapshot --------------------------------------


def test_source_signals_snapshot_round_trips_inputs():
    signals = _ok_signals(
        page_count=12,
        text_extractable_ratio=0.95,
        has_images=True,
        image_count=4,
        has_tables=True,
        table_count=2,
        text_block_count=20,
        total_text_chars=8000,
    )
    plan = assess_post_compile_enrich(signals)
    snap = plan.source_signals
    assert snap["page_count"] == 12
    assert snap["text_extractable_ratio"] == 0.95
    assert snap["image_count"] == 4
    assert snap["table_count"] == 2
    assert snap["compile_status"] == "succeeded"
    assert snap["final_compile_quality"] == "good"


# ---- Payload round-trip --------------------------------------------


def test_to_payload_and_from_payload_round_trip():
    plan = assess_post_compile_enrich(_ok_signals(
        has_tables=True, table_count=1,
    ))
    payload = plan.to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["overall_recommendation"] == "recommended"
    assert TASK_TABLE_ENRICHMENT in payload["recommended_tasks"]
    restored = PostCompileEnrichPlan.from_payload(payload)
    assert restored.overall_recommendation == plan.overall_recommendation
    assert restored.recommended_tasks == plan.recommended_tasks
    assert restored.skipped_tasks == plan.skipped_tasks
    assert restored.source_signals == plan.source_signals


def test_payload_handles_missing_fields_defensively():
    """`from_payload` should populate sensible defaults if older
 artifact versions are read back."""
    plan = PostCompileEnrichPlan.from_payload({
        "overall_recommendation": "optional",
    })
    assert plan.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert plan.reasons == ()
    assert plan.recommended_tasks == ()
    assert plan.decision_source == DECISION_SOURCE_RULE_BASED


# ---- Builder from compile metrics ---------------------------------


def test_build_signals_from_compile_metrics_handles_missing_keys():
    """Missing keys → safe defaults (zero counts / False flags / None
 optionals). This is the workflow's path: it always passes whatever
 `compile_result.content_stats` and `compile_metrics` carry, which
 may be empty for legacy compilers."""
    signals = build_signals_from_compile_metrics(
        compile_status="succeeded",
        final_compile_quality="good",
        content_stats=None,
        compile_metrics=None,
    )
    assert signals.compile_status == "succeeded"
    assert signals.image_count == 0
    assert signals.table_count == 0
    assert signals.text_block_count == 0
    assert signals.has_images is False
    assert signals.has_tables is False


def test_build_signals_from_compile_metrics_promotes_count_to_flag():
    """Even if `has_images` is unset, a non-zero `image_count` should
 flip `has_images=True` so downstream rules fire correctly."""
    signals = build_signals_from_compile_metrics(
        compile_status="succeeded",
        final_compile_quality="good",
        content_stats={"image_count": 3},
        compile_metrics={},
    )
    assert signals.has_images is True
    assert signals.image_count == 3


def test_build_signals_falls_back_to_compile_metrics_for_text_chars():
    """`total_text_chars` lives on content_stats today but
 `extracted_text_chars` lives on compile_metrics. The builder
 accepts either."""
    signals = build_signals_from_compile_metrics(
        compile_status="succeeded",
        final_compile_quality="good",
        content_stats={},
        compile_metrics={"extracted_text_chars": 4200},
    )
    assert signals.total_text_chars == 4200


# ---- End-to-end: compile metrics → plan ---------------------------


# ---- Fast-LLM refinement -------------------------------------------


def test_fast_llm_refinement_upgrades_optional_to_recommended():
    """OPTIONAL → RECOMMENDED is a valid LLM upgrade. The merged plan
 flips `decision_source` to record that an LLM consult shaped it."""
    base = assess_post_compile_enrich(_ok_signals())
    assert base.overall_recommendation == EnrichRecommendation.OPTIONAL
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.RECOMMENDED,
        add_reasons=("LLM judged this is a regulated-domain document",),
        add_recommended_tasks=("requirement_extraction",),
    ))
    assert refined.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert "requirement_extraction" in refined.recommended_tasks
    assert "requirement_extraction" not in refined.skipped_tasks
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM
    assert any("regulated" in r for r in refined.reasons)


def test_fast_llm_refinement_can_downgrade_recommended_to_optional():
    base = assess_post_compile_enrich(_ok_signals(
        has_tables=True, table_count=1,
    ))
    assert base.overall_recommendation == EnrichRecommendation.RECOMMENDED
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.OPTIONAL,
        add_reasons=("LLM judged the table is auto-generated TOC",),
    ))
    assert refined.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM


def test_fast_llm_refinement_never_overrides_skip():
    """SKIP plans carry deterministic blocking conditions; the LLM
 must NOT be allowed to upgrade them. The decision_source still
 flips so we record the consult in the audit log."""
    base = assess_post_compile_enrich(SourceSignals(compile_status="failed"))
    assert base.overall_recommendation == EnrichRecommendation.SKIP
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.RECOMMENDED,
        add_recommended_tasks=("table_enrichment",),
    ))
    assert refined.overall_recommendation == EnrichRecommendation.SKIP
    assert refined.recommended_tasks == ()  # SKIP never carries tasks
    assert refined.blocking_issues == base.blocking_issues
    # Audit signal: even rejected LLM consults flip the source so
    # operators know an LLM was consulted.
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM


def test_fast_llm_refinement_silently_drops_skip_attempted_via_recommendation():
    """An LLM that emits `recommendation=skip` for a non-SKIP plan
 must NOT be allowed to force a SKIP. SKIP is reserved for
 deterministic blocking conditions."""
    base = assess_post_compile_enrich(_ok_signals(has_images=True, image_count=1))
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.SKIP,
    ))
    # Original recommendation preserved (RECOMMENDED), only decision
    # source flipped.
    assert refined.overall_recommendation == base.overall_recommendation


def test_fast_llm_refinement_dedupes_recommended_tasks():
    base = assess_post_compile_enrich(_ok_signals(has_tables=True, table_count=1))
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        # Already in recommended_tasks from the rule-based assessor.
        add_recommended_tasks=("table_enrichment", "image_captioning"),
    ))
    # No duplicate `table_enrichment`.
    assert refined.recommended_tasks.count("table_enrichment") == 1
    # `image_captioning` newly added; gone from skipped.
    assert "image_captioning" in refined.recommended_tasks
    assert "image_captioning" not in refined.skipped_tasks


def test_fast_llm_refinement_caps_reasons():
    """A chatty LLM that emits 50 reasons mustn't bloat the audit
 artifact. The merge caps to a small limit."""
    base = assess_post_compile_enrich(_ok_signals())
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        add_reasons=tuple(f"reason {i}" for i in range(50)),
    ))
    assert len(refined.reasons) <= 8


def test_fast_llm_refinement_with_empty_refinement_only_flips_source():
    base = assess_post_compile_enrich(_ok_signals())
    refined = apply_fast_llm_refinement(base, FastLLMRefinement())
    assert refined.overall_recommendation == base.overall_recommendation
    assert refined.recommended_tasks == base.recommended_tasks
    assert refined.skipped_tasks == base.skipped_tasks
    assert refined.reasons == base.reasons
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM


# ---- End-to-end: compile metrics → plan ---------------------------


def test_e2e_image_heavy_pdf_yields_recommended():
    signals = build_signals_from_compile_metrics(
        compile_status="succeeded",
        final_compile_quality="good",
        content_stats={
            "image_count": 5,
            "table_count": 0,
            "text_block_count": 12,
            "page_count": 8,
        },
        compile_metrics={"chunks_count": 15},
    )
    plan = assess_post_compile_enrich(signals)
    assert plan.overall_recommendation == EnrichRecommendation.RECOMMENDED
    assert TASK_IMAGE_CAPTIONING in plan.recommended_tasks
    assert TASK_VISION_ENRICHMENT in plan.recommended_tasks
    assert TASK_TABLE_ENRICHMENT in plan.skipped_tasks
