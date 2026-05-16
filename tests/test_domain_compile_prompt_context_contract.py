"""Contract — domain-pack `compile_prompt_context` + per-document-
type focus snippets.

Pins the new domain-pack surface added 2026-05-16:

  * `DomainPack.compile_prompt_context` — domain-wide system addon
    surfaced to LightRAG's entity / relationship extraction call.
    Hard contract: NEVER replaces the vendor's prompt, NEVER fires
    a new LLM call, NEVER triggers assessment / enrichment.
  * `DomainPack.compile_prompt_focus` — per-document-type focus
    snippets appended to the domain-level addon when a document
    type is detected.
  * `resolve_compile_prompt_addon(pack, document_type)` — combines
    the two into the final string the bridge prepends.
  * `AssessmentPlan.compile_prompt_addon` — round-trips through
    `to_payload()` so the compile activity can read it.
  * The bridge's `_make_text_callable(system_addon=...)` prepends
    the addon to LightRAG's `system_prompt` argument without
    replacing it.

Smoke test only — no LightRAG, no MinerU. Verifies the prepend
seam directly by exercising the callable with a fake text client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    DomainAssessmentCapabilityHint,
    DomainAssessmentCapabilityHints,
    DomainCompilePromptContext,
    DomainPack,
    resolve_compile_prompt_addon,
)
from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
)


# ---- Domain pack data presence ----------------------------------


def test_civil_pack_carries_assessment_capability_hints():
    """The civil_engineering YAML ships at least the BOQ /
    construction_drawing / structural_calculation buckets."""
    pack = build_civil_engineering_pack()
    keys = set(pack.assessment_capability_hints.keys())
    assert {
        "boq", "construction_drawing", "structural_calculation",
        "inspection_report", "test_report", "specification",
    }.issubset(keys)


def test_civil_boq_recommends_table_processing_high():
    pack = build_civil_engineering_pack()
    hint = pack.assessment_capability_hints["boq"].process_tables
    assert hint.recommended is True
    assert hint.confidence == "high"
    assert "BOQ" in hint.reason or "table" in hint.reason


def test_civil_structural_calc_recommends_equation_processing_high():
    pack = build_civil_engineering_pack()
    hint = pack.assessment_capability_hints[
        "structural_calculation"
    ].process_equations
    assert hint.recommended is True
    assert hint.confidence == "high"
    assert hint.reason  # non-empty operator copy


def test_civil_construction_drawing_recommends_image_high():
    pack = build_civil_engineering_pack()
    hint = pack.assessment_capability_hints[
        "construction_drawing"
    ].process_images
    assert hint.recommended is True
    assert hint.confidence == "high"


def test_civil_pack_compile_prompt_context_is_enabled():
    pack = build_civil_engineering_pack()
    ctx = pack.compile_prompt_context
    assert ctx is not None
    assert ctx.enabled is True
    assert "Civil Engineering" in ctx.system_addon
    assert ctx.max_tokens_budget_hint > 0
    assert "entity_extraction" in ctx.apply_to


def test_civil_pack_has_per_document_type_focus():
    pack = build_civil_engineering_pack()
    focus_keys = set(pack.compile_prompt_focus.keys())
    assert {"boq", "construction_drawing", "structural_calculation"}.issubset(
        focus_keys,
    )
    # Each focus tuple carries one-or-more non-empty lines.
    for snippets in pack.compile_prompt_focus.values():
        assert snippets
        for line in snippets:
            assert line.strip()


def test_general_pack_has_no_compile_prompt_addon():
    """`general` stays minimal — no domain-specific assumptions
    bleed into generic indexing."""
    pack = build_general_pack()
    assert pack.compile_prompt_context is None
    assert pack.compile_prompt_focus == {}
    assert pack.assessment_capability_hints == {}


# ---- resolve_compile_prompt_addon helper ------------------------


def test_resolve_returns_none_when_pack_is_none():
    assert resolve_compile_prompt_addon(None, document_type="boq") is None


def test_resolve_returns_none_for_general_pack():
    pack = build_general_pack()
    assert resolve_compile_prompt_addon(pack, document_type="boq") is None


def test_resolve_returns_addon_with_focus_for_known_type():
    pack = build_civil_engineering_pack()
    addon = resolve_compile_prompt_addon(pack, document_type="boq")
    assert addon is not None
    assert "Civil Engineering" in addon
    # Per-document-type focus block is appended.
    assert "Document-type focus" in addon
    assert "BOQ item" in addon


def test_resolve_returns_addon_without_focus_for_unknown_type():
    """An unknown document type falls back to the domain-level
    addon without raising."""
    pack = build_civil_engineering_pack()
    addon = resolve_compile_prompt_addon(pack, document_type="not_a_real_type")
    assert addon is not None
    assert "Civil Engineering" in addon
    # No per-document-type focus appended.
    assert "Document-type focus" not in addon


def test_resolve_returns_none_when_context_disabled():
    """An operator can flip the master switch off without removing
    the YAML — the resolver short-circuits."""
    ctx = DomainCompilePromptContext(
        enabled=False, system_addon="ignored",
    )
    pack = DomainPack(
        id="test", display_name="Test", version="0",
        compile_prompt_context=ctx,
        compile_prompt_focus={"boq": ("focus line",)},
    )
    assert resolve_compile_prompt_addon(pack, document_type="boq") is None


def test_resolve_returns_none_when_addon_empty_string():
    """`enabled=True` but empty `system_addon` is still a no-op —
    `has_addon` checks both flag + content."""
    ctx = DomainCompilePromptContext(enabled=True, system_addon="   ")
    pack = DomainPack(
        id="test", display_name="Test", version="0",
        compile_prompt_context=ctx,
    )
    assert resolve_compile_prompt_addon(pack, document_type=None) is None


# ---- AssessmentPlan compile_prompt_addon round-trip ------------


def _basic_plan(**overrides) -> AssessmentPlan:
    return AssessmentPlan(
        document_id=overrides.get("document_id", "d1"),
        mode=overrides.get("mode", CompileMode.STANDARD),
        document_type=overrides.get("document_type", "boq"),
        complexity=overrides.get("complexity", Complexity.MEDIUM),
        confidence=overrides.get("confidence", 0.7),
        compile_prompt_addon=overrides.get("compile_prompt_addon"),
    )


def test_assessment_plan_round_trips_compile_prompt_addon():
    addon = "You are indexing a Civil Engineering document.\n\nPrioritise X."
    plan = _basic_plan(compile_prompt_addon=addon)
    payload = plan.to_payload()
    assert payload["compile_prompt_addon"] == addon
    reconstructed = AssessmentPlan.from_payload(payload)
    assert reconstructed.compile_prompt_addon == addon


def test_assessment_plan_legacy_payload_tolerates_missing_addon():
    """Replayed payloads that pre-date this field (no
    `compile_prompt_addon` key) must reconstruct as `None`."""
    plan = _basic_plan()
    payload = plan.to_payload()
    del payload["compile_prompt_addon"]
    reconstructed = AssessmentPlan.from_payload(payload)
    assert reconstructed.compile_prompt_addon is None


def test_assessment_plan_empty_addon_string_coerces_to_none():
    """An empty / whitespace-only string is the same as no addon —
    avoids LightRAG seeing the noisy double-newline."""
    plan = _basic_plan(compile_prompt_addon="")
    payload = plan.to_payload()
    payload["compile_prompt_addon"] = "   "
    reconstructed = AssessmentPlan.from_payload(payload)
    assert reconstructed.compile_prompt_addon is None


# ---- Bridge `_make_text_callable` prepend semantics --------------


@dataclass
class _FakeUsage:
    provider: str = "stub"
    model: str = "stub-model"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class _FakeTextClient:
    """Captures the prompt + system_prompt passed by the bridge so
    the test can assert the prepend happened correctly."""

    provider = "stub"
    model = "stub-model"

    def __init__(self) -> None:
        self.last_prompt: str = ""
        self.last_system_prompt: str | None = None

    def generate(self, prompt, *, system_prompt=None, **_kw):
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        return "ok", _FakeUsage()


def _run(coro):
    return asyncio.run(coro)


def test_bridge_text_callable_prepends_addon_to_system_prompt():
    """The addon must appear BEFORE LightRAG's system prompt so the
    extraction template still drives the call."""
    from j1.providers.raganything._bridge import _make_text_callable

    addon = "DOMAIN ADDON HEADER"
    client = _FakeTextClient()
    callable_ = _make_text_callable(client, system_addon=addon)
    _run(callable_("user-prompt", system_prompt="LIGHTRAG-EXTRACTION-TEMPLATE"))
    assert client.last_system_prompt is not None
    # Both fragments present; addon comes first.
    addon_idx = client.last_system_prompt.find(addon)
    lightrag_idx = client.last_system_prompt.find(
        "LIGHTRAG-EXTRACTION-TEMPLATE",
    )
    assert addon_idx >= 0
    assert lightrag_idx > addon_idx


def test_bridge_text_callable_unchanged_when_addon_none():
    from j1.providers.raganything._bridge import _make_text_callable

    client = _FakeTextClient()
    callable_ = _make_text_callable(client, system_addon=None)
    _run(callable_("user-prompt", system_prompt="LIGHTRAG-EXTRACTION-TEMPLATE"))
    assert client.last_system_prompt is not None
    # No "DOMAIN ADDON" or similar prefix; the existing /no_think
    # injection is the only thing the bridge adds.
    assert "DOMAIN" not in client.last_system_prompt
    assert "LIGHTRAG-EXTRACTION-TEMPLATE" in client.last_system_prompt


def test_bridge_text_callable_empty_addon_string_is_noop():
    """Whitespace-only addon is identical to ``None`` — no spurious
    blank-line prefix injected."""
    from j1.providers.raganything._bridge import _make_text_callable

    client = _FakeTextClient()
    callable_ = _make_text_callable(client, system_addon="   ")
    _run(callable_("user-prompt", system_prompt="LIGHTRAG-EXTRACTION-TEMPLATE"))
    # The system prompt starts with `/no_think\n\n` then LIGHTRAG's
    # prompt — no double newline gap from an empty addon.
    sp = client.last_system_prompt or ""
    # Allow leading `/no_think\n\n` from the existing injection, but
    # no other blank-line prefix.
    after_no_think = sp.split("/no_think", 1)[-1].lstrip("\n").strip()
    assert after_no_think.startswith("LIGHTRAG-EXTRACTION-TEMPLATE")


# ---- Recommender folds domain capability hints ------------------


def test_recommender_high_confidence_hint_forces_high_alone():
    """A `recommended=True && confidence=high` domain hint is
    authoritative — alone it elevates the capability to high+
    recommended, even when no other signal fires."""
    from j1.processing.execution_profile import (
        recommend_capabilities_from_assessment,
    )

    hints = DomainAssessmentCapabilityHints(
        process_tables=DomainAssessmentCapabilityHint(
            recommended=True, confidence="high",
            reason="Domain says: this type is table-dense.",
        ),
    )
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="quiet.pdf", sample_text=None,
        domain_capability_hints=hints,
    )
    assert recs.table_processing.recommended is True
    assert recs.table_processing.confidence == "high"
    assert "domain_type_hint" in recs.table_processing.sources
    assert "Domain says" in " ".join(recs.table_processing.reasons)


def test_recommender_medium_hint_counts_as_one_source():
    """Medium + recommended hints add one source to the ladder,
    not enough alone to force high."""
    from j1.processing.execution_profile import (
        recommend_capabilities_from_assessment,
    )

    hints = DomainAssessmentCapabilityHints(
        process_tables=DomainAssessmentCapabilityHint(
            recommended=True, confidence="medium",
            reason="Domain suspects tables.",
        ),
    )
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="quiet.pdf", sample_text=None,
        domain_capability_hints=hints,
    )
    # One source → medium, not high.
    assert recs.table_processing.recommended is True
    assert recs.table_processing.confidence == "medium"


def test_recommender_low_hint_is_silent():
    """`recommended=False` or `confidence=low` hints do not add
    sources or reasons — they're informational only."""
    from j1.processing.execution_profile import (
        recommend_capabilities_from_assessment,
    )

    hints = DomainAssessmentCapabilityHints(
        process_tables=DomainAssessmentCapabilityHint(
            recommended=False, confidence="low",
            reason="Domain doesn't strongly recommend.",
        ),
    )
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="quiet.pdf", sample_text=None,
        domain_capability_hints=hints,
    )
    # No signals fired AND the low/not-recommended hint is silent
    # → low / not-recommended.
    assert recs.table_processing.recommended is False
    assert recs.table_processing.confidence == "low"


def test_recommender_hint_does_not_suppress_filename_signal():
    """A negative domain hint must NOT suppress an otherwise
    positive filename signal. User intent (a filename like
    `boq.pdf`) outranks the domain's softer "BOQ images aren't
    usually needed" hint for that capability."""
    from j1.processing.execution_profile import (
        recommend_capabilities_from_assessment,
    )

    pack = build_civil_engineering_pack()
    hints = pack.assessment_capability_hints["boq"]
    # BOQ hints have `process_images.recommended=False, confidence=low`.
    # But filename clearly says `figure.pdf` → image filename hint fires.
    recs = recommend_capabilities_from_assessment(
        has_images=False, has_tables=False, has_scanned_pages=False,
        text_extractable_ratio=0.9, page_count=10,
        filename="annual-figure-report.pdf", sample_text=None,
        domain_capability_hints=hints,
    )
    # Filename signal alone → medium-confidence recommended, not
    # suppressed by the negative domain hint.
    assert recs.image_processing.recommended is True


# ---- No-LLM regression guard ------------------------------------


def test_compile_prompt_module_has_no_llm_imports():
    """The new resolver MUST stay deterministic — no provider
    coupling. A future refactor that pulls an LLM client in here
    is a hard regression."""
    import importlib
    import inspect

    # The helper lives in `j1.domains.models`; check its source
    # imports for LLM provider modules.
    mod = importlib.import_module("j1.domains.models")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "raganything", "lightrag",
        "LLMClient", "llm_call",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked, (
        f"domains/models.py imports/mentions LLM modules — "
        f"the new compile-prompt-context surface must stay "
        f"deterministic. Leaks: {leaked}"
    )
