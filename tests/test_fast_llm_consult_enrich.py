"""Tests for the optional fast-LLM enrich-assessment consult.

Layered coverage:
  1. Settings loader: env vars → `FastLLMConsultSettings`, including
     defaults, malformed values, and the `is_actionable()` gate.
  2. Response parser: `parse_fast_llm_refinement` tolerates
     malformed JSON / unknown tasks / SKIP attempts.
  3. Activity: `fast_llm_consult_enrich` returns
     `consulted=False` for every disabled / misconfigured / failure
     path; `consulted=True` only when the callable returns a usable
     refinement.
  4. Workflow gating: `is_consult_warranted` only fires for OPTIONAL
     plans; SKIP / RECOMMENDED / REQUIRED skip the consult entirely.

The activity tests use a stub callable so we never need a real LLM
client — that's the only point at which the consult contract
matters."""

from __future__ import annotations

import os

import pytest

from j1.orchestration.activities.payloads import (
    FastLLMConsultEnrichInput,
    FastLLMConsultEnrichResult,
    ProjectScope,
)
from j1.orchestration.activities.processing import ProcessingActivities
from j1.processing.enrich_assessment import (
    DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM,
    EnrichRecommendation,
    FastLLMConsultPrompt,
    FastLLMRefinement,
    PostCompileEnrichPlan,
    SourceSignals,
    assess_post_compile_enrich,
    apply_fast_llm_refinement,
    is_consult_warranted,
    parse_fast_llm_refinement,
)
from j1.processing.enrich_assessment_settings import (
    DEFAULT_TIMEOUT_SECONDS,
    ENV_FAST_LLM_ENABLED,
    ENV_FAST_LLM_MODEL,
    ENV_FAST_LLM_PROVIDER,
    ENV_FAST_LLM_TIMEOUT_SECONDS,
    FastLLMConsultSettings,
    load_fast_llm_consult_settings,
)


# ---- Settings ------------------------------------------------------


def test_settings_defaults_to_disabled_no_provider_no_model():
    s = load_fast_llm_consult_settings(env={})
    assert s.enabled is False
    assert s.provider is None
    assert s.model is None
    assert s.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert s.is_actionable() is False


def test_settings_enabled_with_provider_and_model_is_actionable():
    s = load_fast_llm_consult_settings(env={
        ENV_FAST_LLM_ENABLED: "true",
        ENV_FAST_LLM_PROVIDER: "openai",
        ENV_FAST_LLM_MODEL: "gpt-4o-mini",
    })
    assert s.is_actionable() is True
    assert s.provider == "openai"
    assert s.model == "gpt-4o-mini"


def test_settings_enabled_without_provider_is_not_actionable():
    """Per spec: 'If enabled but provider/model is missing, log
    warning and use rule-based assessment.'"""
    s = load_fast_llm_consult_settings(env={
        ENV_FAST_LLM_ENABLED: "true",
        ENV_FAST_LLM_MODEL: "gpt-4o-mini",
    })
    assert s.enabled is True
    assert s.is_actionable() is False


def test_settings_invalid_timeout_falls_back_to_default():
    s = load_fast_llm_consult_settings(env={
        ENV_FAST_LLM_ENABLED: "true",
        ENV_FAST_LLM_PROVIDER: "openai",
        ENV_FAST_LLM_MODEL: "gpt-4o-mini",
        ENV_FAST_LLM_TIMEOUT_SECONDS: "not-a-number",
    })
    assert s.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_settings_negative_timeout_falls_back_to_default():
    s = load_fast_llm_consult_settings(env={
        ENV_FAST_LLM_TIMEOUT_SECONDS: "-5",
    })
    assert s.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_settings_custom_timeout_is_honoured():
    s = load_fast_llm_consult_settings(env={
        ENV_FAST_LLM_TIMEOUT_SECONDS: "3.5",
    })
    assert s.timeout_seconds == pytest.approx(3.5)


# ---- Response parser ----------------------------------------------


def test_parse_dict_payload_with_recommendation_and_tasks():
    refinement = parse_fast_llm_refinement({
        "recommendation": "recommended",
        "add_reasons": ["LLM judged regulated content"],
        "add_recommended_tasks": ["requirement_extraction"],
    })
    assert refinement is not None
    assert refinement.recommendation == EnrichRecommendation.RECOMMENDED
    assert refinement.add_reasons == ("LLM judged regulated content",)
    assert refinement.add_recommended_tasks == ("requirement_extraction",)


def test_parse_skip_recommendation_is_dropped():
    """SKIP from the LLM is silently dropped — deterministic blockers
    own SKIP. The refinement returns with reasons/tasks if any, but
    no recommendation."""
    refinement = parse_fast_llm_refinement({
        "recommendation": "skip",
        "add_reasons": ["LLM thinks document is junk"],
    })
    assert refinement is not None
    assert refinement.recommendation is None
    assert refinement.add_reasons == ("LLM thinks document is junk",)


def test_parse_unknown_task_is_silently_dropped():
    refinement = parse_fast_llm_refinement({
        "recommendation": "optional",
        "add_recommended_tasks": ["bogus_task", "table_enrichment"],
    })
    assert refinement is not None
    assert refinement.add_recommended_tasks == ("table_enrichment",)


def test_parse_invalid_json_string_returns_none():
    refinement = parse_fast_llm_refinement("not valid json {{{")
    assert refinement is None


def test_parse_non_object_payload_returns_none():
    assert parse_fast_llm_refinement(["array", "not", "object"]) is None
    assert parse_fast_llm_refinement(None) is None
    assert parse_fast_llm_refinement(42) is None


def test_parse_json_string_payload():
    refinement = parse_fast_llm_refinement(
        '{"recommendation": "required", "add_reasons": ["compliance"]}'
    )
    assert refinement is not None
    assert refinement.recommendation == EnrichRecommendation.REQUIRED


def test_parse_empty_payload_returns_none():
    """A dict with no usable fields → None (no consult result)."""
    assert parse_fast_llm_refinement({}) is None
    assert parse_fast_llm_refinement({"recommendation": "invalid"}) is None


# ---- is_consult_warranted -----------------------------------------


def test_consult_warranted_only_for_optional_plans():
    optional = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
    )
    assert is_consult_warranted(optional) is True
    for rec in (
        EnrichRecommendation.SKIP,
        EnrichRecommendation.RECOMMENDED,
        EnrichRecommendation.REQUIRED,
    ):
        plan = PostCompileEnrichPlan(overall_recommendation=rec)
        assert is_consult_warranted(plan) is False, (
            f"consult must NOT fire for {rec}"
        )


# ---- Activity layer ------------------------------------------------


def _consult_input(rec="optional") -> FastLLMConsultEnrichInput:
    return FastLLMConsultEnrichInput(
        scope=ProjectScope(tenant_id="acme", project_id="alpha"),
        run_id="run-1",
        document_id="doc-1",
        compile_status="succeeded",
        final_compile_quality="good",
        source_signals={"compile_status": "succeeded"},
        provisional_recommendation=rec,
        provisional_recommended_tasks=[],
        provisional_skipped_tasks=["table_enrichment"],
        compile_warnings=[],
    )


def _make_activities(monkeypatch, *, fast_llm_consult=None, env=None):
    """Build a `ProcessingActivities` with a no-op service stub +
    optional fast-LLM consult callable. The activity body uses
    `_processing` only for non-LLM paths, so a None service is safe
    here."""
    if env is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    activities = ProcessingActivities.__new__(ProcessingActivities)
    activities._processing = None
    activities._sources = None
    activities._artifacts = None
    activities._compilers = {}
    activities._enrichers = {}
    activities._graph_builders = {}
    activities._indexers = {}
    activities._query_providers = {}
    activities._reporter = None
    activities._run_store = None
    activities._cache = None
    activities._fast_llm_consult = fast_llm_consult
    return activities


def test_activity_returns_consulted_false_when_settings_disabled(monkeypatch):
    """No env vars set → settings.is_actionable() False → consulted=False."""
    monkeypatch.delenv(ENV_FAST_LLM_ENABLED, raising=False)
    monkeypatch.delenv(ENV_FAST_LLM_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_FAST_LLM_MODEL, raising=False)
    activities = _make_activities(
        monkeypatch, fast_llm_consult=lambda p, s: pytest.fail("must not call"),
    )
    result = activities.fast_llm_consult_enrich(_consult_input())
    assert isinstance(result, FastLLMConsultEnrichResult)
    assert result.consulted is False
    assert "disabled" in (result.fallback_reason or "").lower()


def test_activity_returns_consulted_false_when_no_callable_wired(monkeypatch):
    """Settings actionable but no callable wired → consulted=False
    with a clear fallback reason. Worker bootstrap couldn't construct
    a real LLM client; ingestion still proceeds on rules."""
    activities = _make_activities(
        monkeypatch, fast_llm_consult=None,
        env={
            ENV_FAST_LLM_ENABLED: "true",
            ENV_FAST_LLM_PROVIDER: "openai",
            ENV_FAST_LLM_MODEL: "gpt-4o-mini",
        },
    )
    result = activities.fast_llm_consult_enrich(_consult_input())
    assert result.consulted is False
    assert "no callable wired" in (result.fallback_reason or "")


def test_activity_returns_consulted_true_when_callable_returns_refinement(
    monkeypatch,
):
    captured: dict = {}

    def stub_consult(prompt: FastLLMConsultPrompt, settings: FastLLMConsultSettings):
        captured["prompt"] = prompt
        captured["settings"] = settings
        return FastLLMRefinement(
            recommendation=EnrichRecommendation.RECOMMENDED,
            add_reasons=("LLM-confirmed regulatory content",),
            add_recommended_tasks=("requirement_extraction",),
        )

    activities = _make_activities(
        monkeypatch, fast_llm_consult=stub_consult,
        env={
            ENV_FAST_LLM_ENABLED: "true",
            ENV_FAST_LLM_PROVIDER: "openai",
            ENV_FAST_LLM_MODEL: "gpt-4o-mini",
            ENV_FAST_LLM_TIMEOUT_SECONDS: "5.0",
        },
    )
    result = activities.fast_llm_consult_enrich(_consult_input())
    assert result.consulted is True
    assert result.recommendation == "recommended"
    assert result.add_recommended_tasks == ["requirement_extraction"]
    # Settings forwarded to callable so it can honour timeout.
    assert captured["settings"].timeout_seconds == pytest.approx(5.0)
    assert captured["prompt"].compile_status == "succeeded"
    assert captured["prompt"].provisional_recommendation == EnrichRecommendation.OPTIONAL


def test_activity_swallows_callable_exceptions(monkeypatch):
    """Per spec: 'Never fail ingestion because optional fast-LLM
    assessment failed.' Any callable exception → consulted=False."""
    def boom(_p, _s):
        raise TimeoutError("LLM took too long")

    activities = _make_activities(
        monkeypatch, fast_llm_consult=boom,
        env={
            ENV_FAST_LLM_ENABLED: "true",
            ENV_FAST_LLM_PROVIDER: "openai",
            ENV_FAST_LLM_MODEL: "gpt-4o-mini",
        },
    )
    result = activities.fast_llm_consult_enrich(_consult_input())
    assert result.consulted is False
    assert "TimeoutError" in (result.fallback_reason or "")


def test_activity_swallows_callable_returning_none(monkeypatch):
    """Callable returning None (e.g. invalid JSON path swallowed
    inside the callable) → consulted=False, ingestion continues."""
    activities = _make_activities(
        monkeypatch, fast_llm_consult=lambda _p, _s: None,
        env={
            ENV_FAST_LLM_ENABLED: "true",
            ENV_FAST_LLM_PROVIDER: "openai",
            ENV_FAST_LLM_MODEL: "gpt-4o-mini",
        },
    )
    result = activities.fast_llm_consult_enrich(_consult_input())
    assert result.consulted is False


def test_activity_rejects_unrecognised_provisional_recommendation(monkeypatch):
    """Defensive: a workflow that passes a junk
    `provisional_recommendation` doesn't crash the activity."""
    activities = _make_activities(
        monkeypatch, fast_llm_consult=lambda _p, _s: pytest.fail("must not call"),
        env={
            ENV_FAST_LLM_ENABLED: "true",
            ENV_FAST_LLM_PROVIDER: "openai",
            ENV_FAST_LLM_MODEL: "gpt-4o-mini",
        },
    )
    result = activities.fast_llm_consult_enrich(_consult_input(rec="bogus"))
    assert result.consulted is False
    assert "provisional_recommendation" in (result.fallback_reason or "")


# ---- End-to-end: rule-based + LLM upgrade --------------------------


def test_e2e_optional_plan_upgraded_via_refinement():
    """Realistic flow: rule-based assessor → OPTIONAL → LLM consult
    upgrades to REQUIRED → final plan carries
    `decision_source=rule_based_with_fast_llm`."""
    base = assess_post_compile_enrich(SourceSignals(
        compile_status="succeeded",
        final_compile_quality="good",
        text_block_count=5,
        total_text_chars=1000,
    ))
    assert base.overall_recommendation == EnrichRecommendation.OPTIONAL
    assert base.decision_source == "rule_based"

    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.REQUIRED,
        add_reasons=("compliance gate",),
        add_recommended_tasks=("requirement_extraction",),
    ))
    assert refined.overall_recommendation == EnrichRecommendation.REQUIRED
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM
    assert "requirement_extraction" in refined.recommended_tasks


def test_e2e_skip_plan_never_overruled():
    """Hard rule: deterministic SKIP must not be overridden by a
    rogue LLM. The refinement function still flips decision_source
    so audit logs record the consult attempt."""
    base = assess_post_compile_enrich(
        SourceSignals(compile_status="failed"),
    )
    assert base.overall_recommendation == EnrichRecommendation.SKIP
    refined = apply_fast_llm_refinement(base, FastLLMRefinement(
        recommendation=EnrichRecommendation.RECOMMENDED,
        add_recommended_tasks=("vision_enrichment",),
    ))
    assert refined.overall_recommendation == EnrichRecommendation.SKIP
    assert refined.recommended_tasks == ()
    assert refined.decision_source == DECISION_SOURCE_RULE_BASED_WITH_FAST_LLM
