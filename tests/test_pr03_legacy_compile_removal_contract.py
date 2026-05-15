"""PR-03 contract — Removed legacy compile paths.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-03, J1
MUST guarantee:

  1. ``CompileMode`` exposes only ``STANDARD`` and ``DEEP``. The
     legacy ``FAST`` enum value was deleted.
  2. The planner emits only valid two-mode values across the
     full extension surface.
  3. Pre-two-mode payloads with ``mode="fast"`` round-trip safely
     by falling back to ``STANDARD`` — the ``from_payload``
     ``ValueError`` catch is the contract surface.
  4. Pre-two-mode ``recommended_path`` strings (``fast_text_compile``
     / ``multimodal_compile`` / ``ocr_parse``) coerce to
     ``STANDARD_COMPILE`` without crashing replay.
  5. The retry ladder no longer references ``FAST``.
  6. The RAGAnything plan mapper accepts only ``STANDARD`` / ``DEEP``;
     a synthetic ``FAST``-like state can never be constructed
     since the enum value is gone.

This module is the single navigable PR-03 regression document.
Adjacent tests cover finer-grained mapper / planner edges; the
contracts pinned here are the load-bearing ones.
"""

from __future__ import annotations

import pytest

from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    DefaultAssessmentPlanner,
    FallbackPolicy,
    RecommendedProcessingPath,
)
from j1.processing.compile_retry import next_compile_mode
from j1.processing.profiling import DocumentProfile


# ---- Contract 1: CompileMode enum is two-mode only --------------


def test_contract_1_compile_mode_has_only_standard_and_deep():
    """The enum's wire surface MUST list exactly two values.
    Adding a third (e.g. resurrecting FAST) is a regression — the
    two-mode model is the architecture's stated commitment."""
    members = {m.value for m in CompileMode}
    assert members == {"standard", "deep"}, (
        f"CompileMode regressed; expected {{'standard', 'deep'}}, "
        f"got {members!r}"
    )


def test_contract_1_compile_mode_does_not_expose_legacy_fast_attribute():
    """The ``FAST`` enum value was retired in PR-03. Accessing it
    MUST raise AttributeError — any branch still consuming
    ``CompileMode.FAST`` will fail loudly rather than silently
    operate on a different value."""
    assert not hasattr(CompileMode, "FAST")
    with pytest.raises(AttributeError):
        CompileMode.FAST  # type: ignore[attr-defined]


# ---- Contract 2: planner never emits FAST anywhere --------------


def _profile(*, extension: str, page_count: int = 1) -> DocumentProfile:
    """Minimal profile fixture — the planner mostly keys off
    extension + a few signals."""
    return DocumentProfile(
        document_id="d",
        extension=extension,
        file_size_bytes=1_000,
        page_count=page_count,
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
    )


@pytest.mark.parametrize("extension", [
    # 100%-text formats (pre-PR-03 these would have gone to FAST).
    ".txt", ".md", ".json", ".yaml", ".log",
    # Binary containers (always non-FAST).
    ".pdf", ".docx", ".pptx", ".xlsx",
    # Likely-scanned formats.
    ".tiff", ".bmp",
    # Unknown — defaults still safe.
    ".weirdext",
])
def test_contract_2_planner_emits_only_two_modes(extension: str):
    """Across the planner's full extension surface, the emitted
    ``mode`` MUST be one of the two valid values. No legacy FAST
    leaks even for the extensions that used to map to it."""
    profile = _profile(extension=extension)
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.mode in {CompileMode.STANDARD, CompileMode.DEEP}


# ---- Contract 3: legacy mode="fast" payload tolerated -----------


def test_contract_3_legacy_fast_mode_payload_falls_back_to_standard():
    """A payload from before the two-mode refactor (carries
    ``mode="fast"``) MUST round-trip via ``from_payload`` and land
    on ``STANDARD``. The ``ValueError`` catch in ``from_payload`` is
    the contract surface — operators replaying historical artifacts
    from disk see no crash."""
    legacy_payload = {
        "schema_version": "1",
        "document_id": "doc-legacy",
        "mode": "fast",  # retired enum value
        "document_type": "plain_text",
        "complexity": "low",
        "confidence": 1.0,
        "required_capabilities": ["text_extraction"],
        "optional_capabilities": [],
        "risk_flags": [],
        "fallback_policy": "degrade_with_warning",
        "reason": "pre-two-mode artifact replayed from disk",
    }
    plan = AssessmentPlan.from_payload(legacy_payload)
    assert plan.mode == CompileMode.STANDARD, (
        f"legacy mode='fast' payload should fall back to STANDARD; "
        f"got {plan.mode!r}"
    )
    # The other fields round-trip unchanged.
    assert plan.document_id == "doc-legacy"
    assert plan.fallback_policy == FallbackPolicy.DEGRADE_WITH_WARNING
    assert Capability.TEXT_EXTRACTION in plan.required_capabilities


def test_contract_3_other_unknown_mode_values_also_fall_back_to_standard():
    """The catch is generic — any future or garbage mode string
    falls back to STANDARD. Pinned so the safety belt can't
    regress to silently raising."""
    for unknown in ("ultra", "lightning", "", "FAST", "Standard"):
        payload = {
            "schema_version": "1",
            "document_id": "d",
            "mode": unknown,
            "document_type": "pdf",
            "complexity": "medium",
            "confidence": 0.8,
            "required_capabilities": [],
            "optional_capabilities": [],
            "risk_flags": [],
            "fallback_policy": "degrade_with_warning",
            "reason": "",
        }
        plan = AssessmentPlan.from_payload(payload)
        # "Standard" with capital S is a ValueError too — StrEnum
        # is case-sensitive on the value.
        if unknown == "standard":
            assert plan.mode == CompileMode.STANDARD
        else:
            assert plan.mode == CompileMode.STANDARD, (
                f"unknown mode {unknown!r} should fall back to "
                f"STANDARD; got {plan.mode!r}"
            )


# ---- Contract 4: legacy recommended_path strings coerce safely --


@pytest.mark.parametrize("legacy_path_value", [
    "fast_text_compile",
    "multimodal_compile",
    "ocr_parse",
    "completely_unknown_value",
    "",
])
def test_contract_4_legacy_recommended_path_falls_back_to_standard_compile(
    legacy_path_value: str,
):
    """Pre-two-mode recommended_path strings + future unknown
    values both fall through to ``STANDARD_COMPILE``. The
    ``recommended_path`` is an operator-label field; the
    load-bearing ``mode`` field on the same payload determines
    actual compile behaviour."""
    payload = {
        "schema_version": "1",
        "document_id": "d",
        "mode": "standard",
        "document_type": "pdf",
        "complexity": "medium",
        "confidence": 0.8,
        "required_capabilities": [],
        "optional_capabilities": [],
        "risk_flags": [],
        "fallback_policy": "degrade_with_warning",
        "reason": "",
        "recommended_path": legacy_path_value,
    }
    plan = AssessmentPlan.from_payload(payload)
    assert plan.recommended_path == RecommendedProcessingPath.STANDARD_COMPILE


# ---- Contract 5: retry ladder is two-mode --------------------------


def test_contract_5_retry_ladder_only_escalates_standard_to_deep():
    """The compile-retry ladder MUST escalate STANDARD → DEEP →
    STOP. No FAST entry exists; STANDARD is the entry point."""
    assert next_compile_mode(CompileMode.STANDARD) == CompileMode.DEEP
    assert next_compile_mode(CompileMode.DEEP) is None


def test_contract_5_retry_ladder_does_not_carry_fast_entry():
    """Verify by introspection — the retry ladder dict has no key
    named ``FAST``. Pinned so a future commit adding the entry back
    fails this contract immediately."""
    from j1.processing.compile_retry import _RETRY_LADDER
    keys = {k.value for k in _RETRY_LADDER}
    assert keys == {"standard", "deep"}, (
        f"retry ladder regressed; expected {{'standard', 'deep'}}, "
        f"got {keys!r}"
    )


# ---- Contract 6: plan mapper is two-mode --------------------------


def test_contract_6_plan_mapper_mode_to_parse_method_is_two_mode():
    """The RAGAnything plan mapper's mode → parse_method dict has
    no ``FAST`` entry. Pinned so a future commit adding ``FAST →
    txt`` back fails immediately."""
    from j1.providers.raganything.plan_mapper import _MODE_TO_PARSE_METHOD
    keys = {k.value for k in _MODE_TO_PARSE_METHOD}
    assert keys == {"standard", "deep"}


# ---- Contract: workflow mode-to-parse-method helper is two-mode --


def test_workflow_mode_helper_resolves_only_standard_and_deep():
    """The workflow's ``_parse_method_for_mode`` helper resolves
    only STANDARD and DEEP. A pre-PR-03 payload carrying
    ``mode="fast"`` resolves to None (falls through the dict
    lookup) — operators see "unknown parse method" in diagnostics
    rather than the system silently picking the txt path."""
    from j1.orchestration.workflows.project_processing import (
        _parse_method_for_mode,
    )
    assert _parse_method_for_mode("standard") == "auto"
    assert _parse_method_for_mode("deep") == "auto"
    assert _parse_method_for_mode("fast") is None
    assert _parse_method_for_mode(None) is None
