"""Contract — lightweight capability recommendations.

Pins the load-bearing seams of the deterministic
recommendation layer:

  1. Confidence levels surface (``low`` / ``medium`` / ``high``).
  2. Multiple contributing signals bump confidence to ``high``.
  3. Filename hints contribute to each capability independently.
  4. Equation-symbol sample-text heuristic fires deterministically.
  5. Domain keywords (caller-supplied) bump confidence + appear
     as ``domain_hint`` sources without making the core depend on
     any specific vertical.
  6. ``CapabilityRecommendation.from_payload`` round-trips the
     wire shape.
  7. ``AssessmentPlan`` persists the recommendation snapshot
     alongside the user's selection.
  8. ``with_user_selection`` generates an ``override_warnings``
     entry when the user disables a high-confidence
     recommendation.
  9. No-signal capabilities still emit a ``low`` recommendation
     with a "no signal" reason — the FE always has copy to
     render.
"""

from __future__ import annotations

import pytest

from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    UserSelectedCapabilities,
)
from j1.processing.execution_profile import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CapabilityRecommendation,
    CapabilityRecommendations,
    recommend_capabilities_from_assessment,
)


# ---- Contract 1: confidence levels surface ----------------------


def test_contract_1_low_confidence_when_no_signals_fire():
    """Plain text file, no signals → all three capabilities
    return ``confidence="low"`` + ``recommended=False``."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.99, page_count=10,
    )
    for cap in (
        recs.image_processing,
        recs.table_processing,
        recs.equation_processing,
    ):
        assert cap.recommended is False
        assert cap.confidence == CONFIDENCE_LOW
        # The "no signal" reason is still surfaced so the FE has
        # copy.
        assert cap.reasons


def test_contract_1_medium_confidence_with_single_signal():
    """Tables flag set, no filename hint → single signal, so
    confidence is ``medium`` (recommended but not auto-checked)."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
    )
    assert recs.table_processing.recommended is True
    assert recs.table_processing.confidence == CONFIDENCE_MEDIUM
    assert recs.table_processing.sources == ("table_like_text_layout",)


def test_contract_1_high_confidence_with_two_signals():
    """Tables flag + filename hint → two sources → high
    confidence → the FE picker pre-checks the box."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="quarterly-schedule.pdf",
    )
    assert recs.table_processing.recommended is True
    assert recs.table_processing.confidence == CONFIDENCE_HIGH
    assert "table_like_text_layout" in recs.table_processing.sources
    assert "filename_hint" in recs.table_processing.sources


# ---- Contract 2: domain keyword bumps single signal to high ----


def test_contract_2_domain_keyword_elevates_single_signal():
    """A single content signal + a domain keyword (caller
    supplied) → high confidence. Operators who name a file
    ``boq_2024.pdf`` are signalling intent strongly."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="finance-report.pdf",
        domain_keywords=frozenset({"finance"}),
    )
    assert recs.table_processing.confidence == CONFIDENCE_HIGH
    assert "domain_hint" in recs.table_processing.sources
    # The domain hint appears as a top-level field too.
    assert recs.domain_hints == ("finance",)


def test_contract_2_no_domain_keyword_when_caller_omits():
    """The core stays domain-neutral — when no caller-supplied
    keywords are provided, ``domain_hints`` is empty regardless
    of filename content."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="boq_civil_engineering_construction.pdf",
    )
    assert recs.domain_hints == ()


# ---- Contract 3: filename hints per capability -----------------


@pytest.mark.parametrize("filename,keyword,capability_attr", [
    ("annual-figure-report.pdf", "figure", "image_processing"),
    ("scanned-spec-v3.pdf", "scanned", "image_processing"),
    ("blueprint-final.pdf", "blueprint", "image_processing"),
    ("project-schedule.pdf", "schedule", "table_processing"),
    ("cost-estimate-2024.pdf", "estimate", "table_processing"),
    ("structural-formula-handbook.pdf", "formula", "equation_processing"),
    ("stress-calculation.pdf", "calculation", "equation_processing"),
])
def test_contract_3_filename_hint_drives_recommendation(
    filename: str, keyword: str, capability_attr: str,
):
    """Each capability has its own filename-hint vocabulary —
    matching the filename gets the box recommended."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename=filename,
    )
    rec = getattr(recs, capability_attr)
    assert rec.recommended is True, (
        f"filename {filename!r} should recommend {capability_attr} "
        f"(keyword {keyword!r}); got {rec!r}"
    )
    assert "filename_hint" in rec.sources


# ---- Contract 4: equation-symbol heuristic ---------------------


def test_contract_4_equation_symbols_trigger_recommendation():
    """Several equation-like symbols in the sample text → the
    equation recommendation flips on."""
    sample = (
        "F = m × a; "
        "stress σ = F ÷ A; "
        "sum ∑x; "
        "θ ≈ Δy/Δx; "
        "ratio π × r²"
    )
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.95, page_count=5,
        sample_text=sample,
    )
    assert recs.equation_processing.recommended is True
    assert (
        "equation_symbol_signal" in recs.equation_processing.sources
    )


def test_contract_4_no_sample_text_keeps_equation_off():
    """The assessor is honest about not knowing — without a
    sample slice it doesn't claim equation content."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.99, page_count=5,
        sample_text=None,
    )
    assert recs.equation_processing.recommended is False
    assert recs.equation_processing.confidence == CONFIDENCE_LOW


# ---- Contract 5: payload round-trip ----------------------------


def test_contract_5_capability_recommendation_payload_round_trip():
    rec = CapabilityRecommendation(
        recommended=True,
        confidence=CONFIDENCE_HIGH,
        sources=("table_like_text_layout", "filename_hint"),
        reasons=("Tables detected.", "Filename suggests a schedule."),
    )
    payload = rec.to_payload()
    assert payload == {
        "recommended": True,
        "confidence": "high",
        "sources": ["table_like_text_layout", "filename_hint"],
        "reasons": ["Tables detected.", "Filename suggests a schedule."],
    }
    roundtripped = CapabilityRecommendation.from_payload(payload)
    assert roundtripped == rec


def test_contract_5_capability_recommendations_payload_round_trip():
    recs = recommend_capabilities_from_assessment(
        has_images=True, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.5, page_count=10,
        filename="figure-table-report.pdf",
    )
    payload = recs.to_payload()
    roundtripped = CapabilityRecommendations.from_payload(payload)
    assert roundtripped.image_processing == recs.image_processing
    assert roundtripped.table_processing == recs.table_processing
    assert roundtripped.equation_processing == recs.equation_processing
    assert roundtripped.domain_hints == recs.domain_hints


# ---- Contract 6: AssessmentPlan persists snapshot --------------


def _plan_with_recommendations(
    recs: CapabilityRecommendations,
) -> AssessmentPlan:
    return AssessmentPlan(
        document_id="doc-1",
        mode=CompileMode.STANDARD,
        document_type="pdf",
        complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
        assessment_recommendations=recs.to_payload(),
    )


def test_contract_6_plan_stores_recommendation_snapshot():
    """Plan carries the recommendations dict so audits /
    dashboards can render "we recommended X but the user
    picked Y"."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="schedule.pdf",
    )
    plan = _plan_with_recommendations(recs)
    assert plan.assessment_recommendations is not None
    assert plan.assessment_recommendations["table_processing"][
        "recommended"
    ] is True
    assert plan.assessment_recommendations["table_processing"][
        "confidence"
    ] == "high"


def test_contract_6_payload_round_trip_preserves_snapshot():
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="schedule.pdf",
    )
    plan = _plan_with_recommendations(recs)
    roundtripped = AssessmentPlan.from_payload(plan.to_payload())
    assert roundtripped.assessment_recommendations == (
        plan.assessment_recommendations
    )


# ---- Contract 7: override warning generation --------------------


def test_contract_7_override_warning_when_user_disables_high_recommendation():
    """The keystone behaviour: the user unchecked Process tables
    despite a high-confidence recommendation. The plan stamps an
    informational override warning so the FE can render an info
    banner + the audit log can flag the mismatch."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="schedule.pdf",  # filename_hint + table signal → high
    )
    plan = _plan_with_recommendations(recs)
    overridden = plan.with_user_selection(UserSelectedCapabilities(
        image_processing=False,
        table_processing=False,  # disabled despite high recommendation
        equation_processing=False,
    ))
    assert len(overridden.override_warnings) == 1
    msg = overridden.override_warnings[0]
    assert "strongly recommended" in msg.lower()
    assert "process tables" in msg.lower()


def test_contract_7_no_override_warning_when_user_accepts_recommendation():
    """User checked the box the assessment recommended — no
    warning."""
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="schedule.pdf",
    )
    plan = _plan_with_recommendations(recs)
    overridden = plan.with_user_selection(UserSelectedCapabilities(
        image_processing=False,
        table_processing=True,
        equation_processing=False,
    ))
    assert overridden.override_warnings == ()


def test_contract_7_no_override_warning_for_low_confidence_disable():
    """Disabling a LOW-confidence recommendation is not flagged —
    the assessment wasn't strongly recommending it. Only high
    confidence triggers the banner."""
    # Build a recommendation set where image is low-confidence /
    # not recommended; user disables it (default state).
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.99, page_count=10,
    )
    plan = _plan_with_recommendations(recs)
    overridden = plan.with_user_selection(UserSelectedCapabilities(
        image_processing=False,
        table_processing=False,
        equation_processing=False,
    ))
    assert overridden.override_warnings == ()


def test_contract_7_no_override_warning_when_no_recommendation_snapshot():
    """Legacy / replay path: plan has no recommendation snapshot.
    The override-warning logic short-circuits without raising."""
    plan = AssessmentPlan(
        document_id="doc-1",
        mode=CompileMode.STANDARD,
        document_type="pdf",
        complexity=Complexity.MEDIUM,
        confidence=0.85,
        # No assessment_recommendations field — legacy plan.
    )
    overridden = plan.with_user_selection(UserSelectedCapabilities(
        image_processing=False,
        table_processing=False,
        equation_processing=False,
    ))
    assert overridden.override_warnings == ()


# ---- Contract 8: no LLM regression -----------------------------


def test_contract_8_recommender_is_deterministic_and_pure():
    """Same inputs → same output. The recommender is a pure
    deterministic function — no LLM call, no I/O, no clock."""
    args = dict(
        has_images=True, has_tables=True, has_scanned_pages=False,
        text_extractable_ratio=0.5, page_count=10,
        filename="figure-table-report.pdf",
        sample_text="F = m × a; ∑x; σ²",
    )
    a = recommend_capabilities_from_assessment(**args)
    b = recommend_capabilities_from_assessment(**args)
    assert a == b


def test_contract_8_no_llm_imports_in_recommender_module():
    """Module-level guard: the execution-profile module MUST NOT
    pull in any LLM client. A future "let's use an LLM here"
    refactor would surface as an import + fail this test."""
    import j1.processing.execution_profile as mod
    # The module's symbol table must not advertise LLM bindings.
    for forbidden in (
        "LLMClient", "OpenAI", "anthropic", "LLMTextClient",
        "fast_llm", "FastLLMConsult",
    ):
        assert not hasattr(mod, forbidden), (
            f"recommender module exposes {forbidden!r} — the "
            "lightweight assessor must stay LLM-free"
        )
