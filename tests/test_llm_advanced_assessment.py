"""Tests for the optional LLM-based Advanced Assessment.

Pins the contract:

  * Disabled by default. Never runs automatically.
  * Refuses large documents with a structured payload — the FE asks
    the user to pick manually rather than 4xx-ing.
  * Strict JSON parsing: only the allowed vocabulary survives the
    normaliser; everything else is dropped.
  * Output drives the recommendation precedence chain when an
    operator runs Advanced Assessment; user override still wins.
"""

from __future__ import annotations

import json

import pytest

from j1.processing.execution_profile import ExecutionProfile
from j1.processing.execution_profile_policy import ExecutionProfilePolicy
from j1.processing.llm_advanced_assessment import (
    DEFAULT_OK_WARNING,
    LLMAdvancedAssessmentInputs,
    LLMAdvancedAssessmentResult,
    LLMAdvancedAssessmentService,
    MANUAL_SELECTION_HINT,
    REFUSAL_DOCUMENT_TOO_LARGE,
    REFUSAL_LLM_DISABLED,
    REFUSAL_LLM_ERROR,
    REFUSAL_LLM_UNAVAILABLE,
    REFUSAL_MALFORMED_RESPONSE,
    STATUS_OK,
    STATUS_REFUSED,
)
from j1.processing.llm_advanced_assessment_settings import (
    LLMAdvancedAssessmentSettings,
    load_llm_advanced_assessment_settings,
)
from j1.processing.recommendation_resolver import (
    ProfilerInputs,
    SOURCE_LLM_ADVANCED_ASSESSMENT,
    SOURCE_USER_OVERRIDE,
    resolve_recommendation,
)


_DEFAULT_POLICY = ExecutionProfilePolicy(
    default_profile=ExecutionProfile.STANDARD,
    allowed=frozenset(ExecutionProfile),
)


# ---- Settings ------------------------------------------------------


def test_settings_default_to_disabled():
    """Advanced Assessment must be OFF by default. Demo deployments
    shouldn't fire it without an explicit env flip."""
    settings = load_llm_advanced_assessment_settings(env={})
    assert settings.enabled is False
    assert settings.allow_file_upload is False
    # Safe defaults — small enough that even the demo file picker
    # can't accidentally trigger expensive runs.
    assert settings.max_file_size_bytes > 0
    assert settings.max_page_count > 0
    assert settings.max_text_chars > 0
    assert settings.timeout_seconds > 0


def test_settings_parse_env():
    settings = load_llm_advanced_assessment_settings(env={
        "J1_LLM_ADVANCED_ASSESSMENT_ENABLED": "true",
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_FILE_SIZE": "1000",
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_PAGES": "50",
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_CHARS": "30000",
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_SAMPLED_PAGES": "3",
        "J1_LLM_ADVANCED_ASSESSMENT_TIMEOUT_SECONDS": "30",
        "J1_LLM_ADVANCED_ASSESSMENT_ALLOW_FILE_UPLOAD": "true",
    })
    assert settings.enabled is True
    assert settings.max_file_size_bytes == 1000
    assert settings.max_page_count == 50
    assert settings.max_text_chars == 30000
    assert settings.max_sampled_pages == 3
    assert settings.timeout_seconds == 30
    assert settings.allow_file_upload is True


def test_settings_garbage_env_keeps_defaults():
    """A bogus env value must NOT take down the API process. Falls
    back to the safe default."""
    settings = load_llm_advanced_assessment_settings(env={
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_FILE_SIZE": "not_a_number",
        "J1_LLM_ADVANCED_ASSESSMENT_MAX_PAGES": "-1",
    })
    assert settings.max_file_size_bytes == 5_000_000
    assert settings.max_page_count == 200


# ---- Guardrails ----------------------------------------------------


def test_refuses_when_disabled():
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=False),
        llm_call=lambda p, sp: "{}",  # would fire if called
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_LLM_DISABLED
    # Nothing in the OK fields.
    assert r.recommended_profile is None


def test_refuses_when_llm_call_missing():
    """Even with ``enabled=True`` the service must refuse cleanly
    when no LLM is wired."""
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=None,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_LLM_UNAVAILABLE


def test_refuses_large_file_size():
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(
            enabled=True, max_file_size_bytes=1_000,
        ),
        llm_call=lambda p, sp: "{}",
    )
    r = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1", file_size_bytes=2_000,
    ))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_DOCUMENT_TOO_LARGE
    assert r.message == MANUAL_SELECTION_HINT


def test_refuses_large_page_count():
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(
            enabled=True, max_page_count=10,
        ),
        llm_call=lambda p, sp: "{}",
    )
    r = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1", page_count=100,
    ))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_DOCUMENT_TOO_LARGE


def test_refuses_oversized_sampled_text():
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(
            enabled=True, max_text_chars=50,
        ),
        llm_call=lambda p, sp: "{}",
    )
    r = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1", sampled_text="x" * 200,
    ))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_DOCUMENT_TOO_LARGE


def test_refusal_surfaces_manual_selection_hint_in_warnings():
    """The refusal payload's ``warnings`` list must carry the
    operator-readable manual-selection hint so the FE can render it
    verbatim without re-deriving copy."""
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(
            enabled=True, max_file_size_bytes=10,
        ),
        llm_call=lambda p, sp: "{}",
    )
    r = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1", file_size_bytes=100,
    ))
    assert MANUAL_SELECTION_HINT in r.warnings


# ---- Structured JSON output ---------------------------------------


_VALID_LLM_OUTPUT = json.dumps({
    "document_complexity": "complex",
    "recommended_profile": "deep_knowledge_index",
    "confidence": "medium",
    "detected_signals": {
        "likely_tables": "likely",
        "likely_images_or_diagrams": "suspected",
        "likely_equations": "no",
        "likely_requirements": "likely",
        "layout_complexity": "high",
    },
    "recommended_next_steps": [
        "run_domain_enrichment",
        "build_knowledge_memory",
    ],
    "reasoning_summary": [
        "RFP-style document with requirements language.",
        "Sampled pages show tabular schedules.",
    ],
    "warnings": ["Sample size was small."],
})


def test_sample_text_provenance_round_trips_on_available():
    """Every OK result carries sample-text provenance (status /
    source / counts) so the FE can render an honest "what the LLM
    saw" disclosure under the picker."""
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: _VALID_LLM_OUTPUT,
    )
    out = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1",
        sampled_text="page content",
        sample_text_status="available",
        sample_text_source="pypdf",
        sampled_text_char_count=42,
        sampled_page_count=3,
    ))
    assert out.sample_text_status == "available"
    assert out.sample_text_source == "pypdf"
    assert out.sampled_text_char_count == 42
    assert out.sampled_page_count == 3
    # ``to_payload`` round-trips with camelCase keys for the FE.
    payload = out.to_payload()
    assert payload["sampleTextStatus"] == "available"
    assert payload["sampleTextSource"] == "pypdf"
    assert payload["sampledTextCharCount"] == 42
    assert payload["sampledPageCount"] == 3


def test_unreliable_sample_text_emits_warning_and_hedges_likely():
    """When sample text isn't reliable (unsupported / empty /
    garbled), the result must (a) carry the unreliable-text
    warning verbatim AND (b) downgrade every ``likely`` verdict
    the model emitted to ``suspected`` so the FE never claims
    layout detail under missing content."""
    from j1.processing.llm_advanced_assessment import (
        SAMPLE_TEXT_UNRELIABLE_WARNING,
    )
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: _VALID_LLM_OUTPUT,
    )
    out = svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1",
        sampled_text=None,
        sample_text_status="unsupported",
        sample_text_source="unavailable",
    ))
    assert out.sample_text_status == "unsupported"
    assert SAMPLE_TEXT_UNRELIABLE_WARNING in out.warnings
    # The fixture's ``likely_tables=likely`` / ``likely_requirements=likely``
    # get hedged. ``no`` and the orthogonal ``layout_complexity``
    # are left alone.
    assert out.detected_signals["likely_tables"] == "suspected"
    assert out.detected_signals["likely_requirements"] == "suspected"
    assert out.detected_signals["likely_equations"] == "no"


def test_prompt_pins_no_layout_inference_under_unreliable_sample_text():
    """The prompt MUST tell the LLM not to invent layout claims
    when sampled text isn't available. We don't run a real LLM —
    capture the prompt and assert the constraint is present."""
    captured: dict[str, str] = {}

    def _capture(prompt: str, system_prompt: str) -> str:
        captured["prompt"] = prompt
        return _VALID_LLM_OUTPUT

    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=_capture,
    )
    svc.run(LLMAdvancedAssessmentInputs(
        document_id="d1",
        sample_text_status="unsupported",
        sample_text_source="unavailable",
    ))
    prompt = captured["prompt"]
    assert "UNSUPPORTED" in prompt
    assert "MUST NOT" in prompt
    # The "filename + signals + rules ONLY" constraint must be
    # spelled out so the LLM knows what to fall back on.
    assert "filename" in prompt.lower()
    assert "matched rules" in prompt.lower()


def test_returns_structured_ok_result():
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: _VALID_LLM_OUTPUT,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_OK
    assert r.document_complexity == "complex"
    assert r.recommended_profile == "deep_knowledge_index"
    assert r.confidence == "medium"
    assert r.detected_signals["likely_tables"] == "likely"
    assert r.detected_signals["likely_requirements"] == "likely"
    assert r.detected_signals["layout_complexity"] == "high"
    assert r.recommended_next_steps == (
        "run_domain_enrichment", "build_knowledge_memory",
    )
    # Default warning is ALWAYS surfaced even when the LLM
    # supplies its own — keeps the "estimate only" framing in
    # front of the operator.
    assert DEFAULT_OK_WARNING in r.warnings


def test_drops_unknown_signal_values():
    """The normaliser must reject vocabulary the LLM hallucinated
    and fall back to the safe default. Prevents a chatty model from
    smuggling in `likely_tables="definitely"`."""
    raw = json.dumps({
        "document_complexity": "extreme",  # not in vocabulary
        "recommended_profile": "ultra_pro_max",  # not in vocabulary
        "confidence": "stellar",  # not in vocabulary
        "detected_signals": {
            "likely_tables": "absolutely",  # not in vocabulary
            "layout_complexity": "extreme",  # not in vocabulary
        },
    })
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: raw,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_OK
    # Falls back to safe defaults.
    assert r.document_complexity == "moderate"
    assert r.recommended_profile == "standard_index"
    assert r.confidence == "low"
    assert r.detected_signals["likely_tables"] == "no"
    assert r.detected_signals["layout_complexity"] == "low"


def test_strips_unknown_next_steps():
    """Only the canonical manual-action ids survive."""
    raw = json.dumps({
        "document_complexity": "moderate",
        "recommended_profile": "standard_index",
        "confidence": "medium",
        "detected_signals": {},
        "recommended_next_steps": [
            "run_domain_enrichment",
            "secret_evil_action",
            "build_knowledge_memory",
            "format_hard_drive",
        ],
    })
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: raw,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_OK
    assert r.recommended_next_steps == (
        "run_domain_enrichment", "build_knowledge_memory",
    )


def test_tolerates_code_fenced_json():
    """LLMs love ```json fences — the parser must handle them."""
    fenced = f"```json\n{_VALID_LLM_OUTPUT}\n```"
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: fenced,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_OK
    assert r.recommended_profile == "deep_knowledge_index"


def test_malformed_response_is_refused_not_raised():
    """A garbled LLM response must NOT crash the request. The
    service folds it into a refusal so the FE can recover."""
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=lambda p, sp: "this is definitely not json",
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_MALFORMED_RESPONSE


def test_llm_exception_is_refused_not_raised():
    def _explode(_p, _sp):
        raise RuntimeError("upstream LLM provider down")
    svc = LLMAdvancedAssessmentService(
        settings=LLMAdvancedAssessmentSettings(enabled=True),
        llm_call=_explode,
    )
    r = svc.run(LLMAdvancedAssessmentInputs(document_id="d1"))
    assert r.status == STATUS_REFUSED
    assert r.refusal_reason == REFUSAL_LLM_ERROR


# ---- Resolver precedence ------------------------------------------


def _llm_result_for_profile(profile: str) -> dict:
    return {
        "status": "ok",
        "recommendedProfile": profile,
        "reasoningSummary": ["LLM said so."],
        "warnings": ["estimate only"],
    }


def test_llm_result_drives_recommendation_when_present():
    """Precedence: user override > LLM > domain rules > general
    rules > lightweight fallback. With no user override and an LLM
    result, the LLM wins."""
    outcome = resolve_recommendation(
        filename="anything.pdf",
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
        llm_assessment_result=_llm_result_for_profile(
            "deep_knowledge_index",
        ),
    )
    assert outcome.source == SOURCE_LLM_ADVANCED_ASSESSMENT
    assert outcome.profile == ExecutionProfile.ADVANCED
    assert "LLM said so." in outcome.reasons


def test_user_override_still_wins_over_llm():
    """Critical contract: the LLM can never silently override the
    operator's pick. If the user typed a profile, that's what runs."""
    outcome = resolve_recommendation(
        filename="anything.pdf",
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=None,
        user_selected_profile=ExecutionProfile.MINIMUM_QUERYABLE,
        policy=_DEFAULT_POLICY,
        llm_assessment_result=_llm_result_for_profile(
            "deep_knowledge_index",  # LLM says go big
        ),
    )
    assert outcome.source == SOURCE_USER_OVERRIDE
    assert outcome.profile == ExecutionProfile.MINIMUM_QUERYABLE


def test_refused_llm_result_does_not_drive_recommendation():
    """A refusal payload (``status='refused'``) must NOT win.
    The chain falls through to the next branch."""
    outcome = resolve_recommendation(
        filename="archive.bin",  # no rule will match
        title=None,
        active_domain=None,
        general_domain=None,
        profiler_inputs=ProfilerInputs(),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
        llm_assessment_result={
            "status": "refused",
            "refusalReason": "document_too_large",
            "recommendedProfile": "deep_knowledge_index",  # ignored
        },
    )
    assert outcome.source != SOURCE_LLM_ADVANCED_ASSESSMENT


def test_llm_result_maps_quick_index_to_minimum_queryable():
    outcome = resolve_recommendation(
        filename="x.pdf", title=None,
        active_domain=None, general_domain=None,
        profiler_inputs=None,
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
        llm_assessment_result=_llm_result_for_profile("quick_index"),
    )
    assert outcome.profile == ExecutionProfile.MINIMUM_QUERYABLE


def test_llm_result_with_unknown_profile_falls_through():
    """Malformed LLM payloads must not drive — fall to the next
    precedence branch."""
    outcome = resolve_recommendation(
        filename="x.pdf", title=None,
        active_domain=None, general_domain=None,
        profiler_inputs=ProfilerInputs(),
        user_selected_profile=None,
        policy=_DEFAULT_POLICY,
        llm_assessment_result=_llm_result_for_profile(
            "ultra_pro_max",  # not in mapping
        ),
    )
    assert outcome.source != SOURCE_LLM_ADVANCED_ASSESSMENT


# ---- AssessmentDecision carries LLM result --------------------------


def test_assessment_decision_round_trips_llm_result():
    """The decision dataclass must persist + restore the LLM result
    payload and the next-steps list."""
    from j1.processing.assessment_decision import AssessmentDecision

    decision = AssessmentDecision(
        assessment_decision_id="ad-x",
        document_id="d1",
        selected_domain_id="general",
        recommended_profile="advanced",
        effective_profile="advanced",
        recommendation_source=SOURCE_LLM_ADVANCED_ASSESSMENT,
        fallback_used=False,
        llm_assessment_result={
            "status": "ok",
            "recommendedProfile": "deep_knowledge_index",
        },
        recommended_next_steps=(
            "run_domain_enrichment", "build_knowledge_memory",
        ),
    )
    payload = decision.to_payload()
    assert payload["llmAssessmentResult"] == {
        "status": "ok", "recommendedProfile": "deep_knowledge_index",
    }
    assert payload["recommendedNextSteps"] == [
        "run_domain_enrichment", "build_knowledge_memory",
    ]
    restored = AssessmentDecision.from_payload(payload)
    assert restored == decision
