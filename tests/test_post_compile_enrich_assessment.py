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
    EnrichRecommendation,
    PostCompileEnrichPlan,
    SCHEMA_VERSION,
    SourceSignals,
    TASK_IMAGE_CAPTIONING,
    TASK_QUALITY_ASSESSMENT,
    TASK_TABLE_ENRICHMENT,
    TASK_VISION_ENRICHMENT,
    assess_post_compile_enrich,
    build_signals_from_compile_metrics,
)


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


def test_empty_document_returns_skip():
    plan = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        text_block_count=0,
        image_count=0,
        table_count=0,
    ))
    assert plan.overall_recommendation == EnrichRecommendation.SKIP
    assert plan.blocking_issues
    assert "no content" in plan.blocking_issues[0].lower()


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
