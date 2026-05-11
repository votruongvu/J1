""" tests: pre-compile `InitialExecutionPlan` + builder.

The plan is the cheap, deterministic, pre-compile decision the
workflow consumes BEFORE dispatching the compile activity. These
tests pin:

 * the plan stays cheap (no LLM / no OCR / no vision / no MinerU);
 * the domain layer is consumed via the registry interface only;
 * no-domain runs still produce a valid plan;
 * candidate enrichment modules come from the domain pack's
 `DomainEnrichmentPolicy`, not from hardcoded logic;
 * the plan does NOT make a final enrichment decision (that lives
 in `assess_post_compile_enrich`);
 * the plan does NOT gate graph / index (those are workflow
 `enricher_kind` / `indexer_kind` decisions);
 * no split-mode vocabulary leaks into the plan;
 * the wrapped `compile_plan` is the existing rule-based
 `AssessmentPlan` and remains intact.
"""

from __future__ import annotations

import importlib
import inspect
import sys

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import (
    DomainEnrichmentPolicy,
    DomainPack,
)
from j1.processing.assessment import AssessmentPlan, CompileMode
from j1.processing.initial_execution_plan import (
    COMPILE_ENGINE_RAGANYTHING,
    InitialExecutionPlan,
    build_initial_execution_plan,
)
from j1.processing.profiling import DocumentProfile


def _profile(**overrides) -> DocumentProfile:
    base = dict(
        document_id="doc-1",
        extension=".pdf",
        mime_type="application/pdf",
        file_size_bytes=50_000,
        page_count=8,
        total_text_chars=15_000,
        text_extractable_ratio=0.9,
    )
    base.update(overrides)
    return DocumentProfile(**base)


# ---- C1+C2: domain profile loads via interface; works without one ----


def test_plan_loads_civil_pack_via_domain_pack_interface():
    """Builder MUST consume the pack by interface — no hardcoded
 `civil_engineering` branches anywhere. We pass the pack in by
 reference; the builder reads `pack.enrichment_policy` and
 `pack.id` only."""
    pack = build_civil_engineering_pack()
    plan = build_initial_execution_plan(_profile(), domain_pack=pack)
    assert plan.domain_profile_id == "civil_engineering"
    assert plan.enrichment_policy == "always"


def test_plan_works_without_domain_pack():
    """Runs without a selected pack still produce a valid plan.
 `enrichment_policy=auto`, no candidate modules, no domain_id."""
    plan = build_initial_execution_plan(_profile())
    assert plan.domain_profile_id is None
    assert plan.enrichment_policy == "auto"
    assert plan.candidate_enrichment_modules == ()
    assert plan.run_compile is True


def test_plan_works_with_general_fallback_pack():
    """The general fallback pack carries auto policy + empty lists —
 indistinguishable from no-pack except for the recorded id."""
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_general_pack(),
    )
    assert plan.domain_profile_id == "general"
    assert plan.enrichment_policy == "auto"
    assert plan.candidate_enrichment_modules == ()


# ---- C3: enrichment_policy sourced from the domain pack --------------


def test_plan_emits_policy_always_for_civil_pack():
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_civil_engineering_pack(),
    )
    assert plan.enrichment_policy == "always"


def test_plan_emits_policy_never_when_pack_declares_it():
    pack = DomainPack(
        id="regulated",
        display_name="Regulated",
        version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(policy="never"),
    )
    plan = build_initial_execution_plan(_profile(), domain_pack=pack)
    assert plan.enrichment_policy == "never"


# ---- C4: candidate enrichment modules come from the policy -----------


def test_plan_emits_candidate_modules_from_force_and_optional_lists():
    pack = build_civil_engineering_pack()
    plan = build_initial_execution_plan(_profile(), domain_pack=pack)
    # Civil pack force-recommends requirement+risk extraction and
    # optionally suggests quality_assessment.
    assert "requirement_extraction" in plan.candidate_enrichment_modules
    assert "risk_extraction" in plan.candidate_enrichment_modules
    assert "quality_assessment" in plan.candidate_enrichment_modules


def test_plan_deduplicates_candidate_modules():
    """A pack that puts the same task in force AND optional shouldn't
 produce duplicates in the candidate list."""
    pack = DomainPack(
        id="dedupe_test",
        display_name="Dedupe",
        version="0.1",
        enrichment_policy=DomainEnrichmentPolicy(
            policy="auto",
            force_recommended_tasks=("requirement_extraction",),
            optional_tasks=("requirement_extraction", "table_enrichment"),
        ),
    )
    plan = build_initial_execution_plan(_profile(), domain_pack=pack)
    # Each task appears at most once.
    counts: dict[str, int] = {}
    for task in plan.candidate_enrichment_modules:
        counts[task] = counts.get(task, 0) + 1
    assert all(v == 1 for v in counts.values()), counts


# ---- C5: no LLM / OCR / vision / MinerU in the assessment path ------


def test_assessment_module_does_not_import_llm_clients():
    """The initial-execution-plan module must NOT depend on any
 LLM / OCR / vision / parser-vendor module. A regression here
 means an expensive client crept into the cheap path.

 Checks only IMPORT statements — docstrings / comments that
 mention vendor names (e.g. an explanatory note about RAGAnything)
 are fine. The signal we care about is symbol acquisition."""
    import ast
    plan_mod = sys.modules.get("j1.processing.initial_execution_plan")
    assessment_mod = sys.modules.get("j1.processing.assessment")
    profiling_mod = sys.modules.get("j1.processing.profiling")
    assert plan_mod is not None and assessment_mod is not None
    forbidden_prefixes = (
        "j1.llm",
        "j1.providers.raganything",
        "j1.providers.mineru",
        "openai",
        "anthropic",
    )
    for mod in (plan_mod, assessment_mod, profiling_mod):
        if mod is None:
            continue
        tree = ast.parse(inspect.getsource(mod))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            for prefix in forbidden_prefixes:
                assert not name.startswith(prefix), (
                    f"{mod.__name__} unexpectedly imports {name!r}; "
                    "the pre-compile assessment path must stay cheap."
                )


# ---- C6/C7: no split-mode / no graph-index gating in the plan -------


def test_plan_does_not_carry_split_mode_vocabulary():
    """Split-mode was removed. Pin that no field of the
 plan re-introduces it."""
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_civil_engineering_pack(),
    )
    payload = plan.to_payload()
    serialised = repr(payload).lower()
    for forbidden in ("split_mode", "split-mode", "insert_content"):
        assert forbidden not in serialised, (
            f"plan payload unexpectedly contains {forbidden!r}: {payload}"
        )


def test_plan_makes_no_graph_or_index_gating_decisions():
    """The plan must not carry graph_required / index_required /
 enrich_required boolean gates. Those decisions are workflow-
 level (`enricher_kind` / `graph_builder_kind` / `indexer_kind`
 on the request) + post-compile signals."""
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_civil_engineering_pack(),
    )
    payload = plan.to_payload()
    forbidden_keys = (
        "graph_required",
        "index_required",
        "enrich_required",
        "final_enrich_decision",
        "graph_decision",
        "index_decision",
    )
    for key in forbidden_keys:
        assert key not in payload, (
            f"plan unexpectedly carries gating key {key!r}: {payload}"
        )


def test_plan_does_not_make_final_enrichment_decision():
    """The candidate list is a SUGGESTION based on cheap signals +
 domain hints. The real per-document decision happens in
 `assess_post_compile_enrich` (post-compile). Verify the plan
 carries `candidate_enrichment_modules` (suggestion) and NOT
 `recommended_enrichment_tasks` / `final_*` (decision)."""
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_civil_engineering_pack(),
    )
    payload = plan.to_payload()
    assert "candidate_enrichment_modules" in payload
    for key in (
        "recommended_enrichment_tasks",
        "final_enrichment_recommendation",
        "enrich_recommendation",
    ):
        assert key not in payload


# ---- C8: cheap signals reflect the profile, never document content --


def test_cheap_signals_are_a_whitelisted_snapshot_of_profile():
    profile = _profile(
        has_images=True,
        has_tables=False,
        language="en",
        empty_page_ratio=0.05,
    )
    plan = build_initial_execution_plan(profile)
    sig = plan.cheap_signals
    assert sig["extension"] == ".pdf"
    assert sig["page_count"] == 8
    assert sig["has_images"] is True
    assert sig["has_tables"] is False
    assert sig["language"] == "en"
    assert sig["empty_page_ratio"] == 0.05
    # No accidental leakage of document content / heavy fields.
    for forbidden in ("content", "text", "body", "raw_text", "extracted_text"):
        assert forbidden not in sig, sig


# ---- Compile-plan integration ---------------------------------------


def test_plan_wraps_existing_assessment_plan():
    """The compile-stage detail (mode / capabilities / parse method)
 is the existing rule-based `AssessmentPlan` — unchanged contract
 so the RAGAnything adapter doesn't need to change."""
    plan = build_initial_execution_plan(_profile())
    assert isinstance(plan.compile_plan, AssessmentPlan)
    # Plain PDF with text → STANDARD compile mode.
    assert plan.compile_plan.mode == CompileMode.STANDARD


def test_plan_defaults_to_raganything_compile_engine():
    """RAGAnything is the default and only currently-wired engine.
 The field exists so a future engine swap is one line."""
    plan = build_initial_execution_plan(_profile())
    assert plan.compile_engine == COMPILE_ENGINE_RAGANYTHING


# ---- Round-trip + resource hints ------------------------------------


def test_plan_round_trips_through_to_payload_from_payload():
    pack = build_civil_engineering_pack()
    original = build_initial_execution_plan(_profile(), domain_pack=pack)
    payload = original.to_payload()
    restored = InitialExecutionPlan.from_payload(payload)
    assert restored.domain_profile_id == original.domain_profile_id
    assert restored.enrichment_policy == original.enrichment_policy
    assert restored.candidate_enrichment_modules == original.candidate_enrichment_modules
    assert restored.resource_hints == original.resource_hints
    assert restored.compile_plan is not None
    assert restored.compile_plan.mode == original.compile_plan.mode


def test_resource_hints_merge_policy_with_caller_overrides():
    """Caller-provided hints win over the policy default — typical
 use case is the deployment passing already-resolved env values."""
    pack = build_civil_engineering_pack()  # default_model_tier="fast"
    plan = build_initial_execution_plan(
        _profile(),
        domain_pack=pack,
        resource_hints={"vlm_concurrency": 2, "default_model_tier": "premium"},
    )
    assert plan.resource_hints["default_model_tier"] == "premium"
    assert plan.resource_hints["vlm_concurrency"] == 2


# ---- Determinism ----------------------------------------------------


def test_plan_is_deterministic_for_identical_inputs():
    """Same profile + same domain pack ⇒ same plan. Required for
 Temporal workflow replay."""
    pack = build_civil_engineering_pack()
    plan_a = build_initial_execution_plan(_profile(), domain_pack=pack)
    plan_b = build_initial_execution_plan(_profile(), domain_pack=pack)
    assert plan_a.to_payload() == plan_b.to_payload()


# ---- Reasons / warnings surface domain trail ------------------------


def test_reasons_include_domain_selection_trail():
    plan = build_initial_execution_plan(
        _profile(), domain_pack=build_civil_engineering_pack(),
    )
    joined = " ".join(plan.reasons)
    assert "civil_engineering" in joined
    assert "policy" in joined.lower()


def test_warnings_surface_scanned_pages_caveat():
    profile = _profile(has_scanned_pages=True)
    plan = build_initial_execution_plan(profile)
    assert any("scanned" in w.lower() for w in plan.warnings)


# ---- Eagerly load the module to support C5 source-introspection ----


@pytest.fixture(autouse=True, scope="module")
def _ensure_modules_loaded():
    importlib.import_module("j1.processing.initial_execution_plan")
    importlib.import_module("j1.processing.assessment")
    importlib.import_module("j1.processing.profiling")
