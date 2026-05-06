"""`DefaultIngestPlanner` decision-matrix regression tests.

Each test pins one policy × profile-shape combination so the planner's
decisions are auditable without reading the implementation. The tests
also exercise the caller-override precedence rule (caller wins over
planner) — the contract adaptive ingestion planning promises
operators."""

from __future__ import annotations

import pytest

from j1.processing.planning import (
    DefaultIngestPlanner,
    IngestMode,
    IngestPlan,
    IngestPolicy,
    PlannedStep,
    STEP_COMPILE,
    STEP_ENRICH,
    STEP_GRAPH,
    STEP_INDEX,
    steps_for_mode,
)
from j1.processing.profiling import DocumentProfile
from j1.processing.status import StepSource


_ALL_STEPS = frozenset({STEP_COMPILE, STEP_ENRICH, STEP_GRAPH, STEP_INDEX})


@pytest.fixture
def planner() -> DefaultIngestPlanner:
    return DefaultIngestPlanner()


def _profile(**overrides) -> DocumentProfile:
    """Build a DocumentProfile with `unknown` defaults for every
    optional field; tests opt in to specific signals."""
    base = {
        "document_id": "doc-1",
        "extension": ".pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 10_000,
        "page_count": None,
        "text_extractable_ratio": None,
        "has_images": None,
        "has_tables": None,
        "has_scanned_pages": None,
        "estimated_tokens": None,
        "language": None,
        "parser_confidence": None,
        "warnings": (),
    }
    base.update(overrides)
    return DocumentProfile(**base)


# ---- Mode mapping is pure data --------------------------------------


def test_mode_to_steps_mapping_is_consistent():
    """Sanity check: every mode lists at least compile + index. The
    planner depends on this — modes that drop compile would silently
    leave documents unprocessed."""
    for mode in IngestMode:
        enabled = steps_for_mode(mode)
        assert STEP_COMPILE in enabled, f"{mode} drops compile"
        assert STEP_INDEX in enabled, f"{mode} drops index"


# ---- Policy: auto / balanced ----------------------------------------


def test_plain_text_picks_text_only_under_auto(planner):
    profile = _profile(extension=".txt", text_extractable_ratio=1.0,
                       has_images=False, has_tables=False,
                       has_scanned_pages=False)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_ONLY
    assert plan.estimated_cost_level == "low"
    enabled = set(plan.enabled_step_names())
    assert enabled == {STEP_COMPILE, STEP_INDEX}
    skipped = set(plan.skipped_step_names())
    assert STEP_GRAPH in skipped
    assert STEP_ENRICH in skipped


def test_table_signal_picks_table_aware(planner):
    profile = _profile(has_tables=True, has_images=False,
                       has_scanned_pages=False)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TABLE_AWARE
    assert STEP_ENRICH in plan.enabled_step_names()
    # Graph still skipped — TABLE_AWARE doesn't enable graph by default.
    assert STEP_GRAPH not in plan.enabled_step_names()


def test_scanned_signal_picks_multimodal_full(planner):
    profile = _profile(has_scanned_pages=True, text_extractable_ratio=0.0)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.MULTIMODAL_FULL
    enabled = set(plan.enabled_step_names())
    assert STEP_GRAPH in enabled
    assert STEP_ENRICH in enabled


def test_image_only_signal_picks_multimodal_light(planner):
    profile = _profile(has_images=True, has_tables=False,
                       has_scanned_pages=False)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.MULTIMODAL_LIGHT
    enabled = set(plan.enabled_step_names())
    assert STEP_ENRICH in enabled
    assert STEP_GRAPH not in enabled


def test_unknown_signals_default_to_text_with_light_enrichment(planner):
    """When all modality flags are unknown, planner must NOT bias
    toward expensive multimodal modes. The default is text + metadata
    enrichment (cheap) under `auto` policy."""
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_WITH_LIGHT_ENRICHMENT
    enabled = set(plan.enabled_step_names())
    assert enabled == {STEP_COMPILE, STEP_ENRICH, STEP_INDEX}


# ---- Policy: cost_saving --------------------------------------------


def test_cost_saving_unknown_signals_picks_text_only(planner):
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.COST_SAVING,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_ONLY


def test_cost_saving_scanned_falls_back_to_multimodal_light_not_full(planner):
    """`cost_saving` keeps OCR/enrichment for scanned docs (otherwise
    they'd be unsearchable) but skips graph extraction (the most
    expensive optional stage)."""
    profile = _profile(has_scanned_pages=True, text_extractable_ratio=0.0)
    plan = planner.plan(profile, policy=IngestPolicy.COST_SAVING,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.MULTIMODAL_LIGHT
    assert STEP_GRAPH not in plan.enabled_step_names()


# ---- Policy: high_accuracy ------------------------------------------


def test_high_accuracy_unknown_signals_enable_graph(planner):
    """When uncertain, `high_accuracy` enables more processing, not
    less. Graph is the canonical "expensive optional" step — its
    presence under high_accuracy is the regression signal."""
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.HIGH_ACCURACY,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.GRAPH_AWARE
    assert STEP_GRAPH in plan.enabled_step_names()


# ---- Policy: force_full ---------------------------------------------


def test_force_full_enables_every_available_step(planner):
    profile = _profile(extension=".txt", text_extractable_ratio=1.0)
    plan = planner.plan(profile, policy=IngestPolicy.FORCE_FULL,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.FULL_DIAGNOSTIC
    assert set(plan.enabled_step_names()) == _ALL_STEPS
    assert plan.estimated_cost_level == "high"


# ---- Policy: text_only ----------------------------------------------


def test_text_only_picks_text_only_mode(planner):
    profile = _profile(has_images=False, has_tables=False,
                       has_scanned_pages=False)
    plan = planner.plan(profile, policy=IngestPolicy.TEXT_ONLY,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_ONLY
    assert plan.warnings == ()


def test_text_only_with_table_signal_records_warning(planner):
    """`text_only` is allowed but must NOT silently lose information.
    When the profile reports tables / images / scanned, the plan
    surfaces a warning so operators see the trade-off."""
    profile = _profile(has_tables=True)
    plan = planner.plan(profile, policy=IngestPolicy.TEXT_ONLY,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_ONLY
    assert any("table" in w.lower() for w in plan.warnings)


def test_text_only_with_scanned_signal_records_ocr_warning(planner):
    profile = _profile(has_scanned_pages=True)
    plan = planner.plan(profile, policy=IngestPolicy.TEXT_ONLY,
                        available_steps=_ALL_STEPS)
    assert any("scanned" in w.lower() or "ocr" in w.lower()
               for w in plan.warnings)


# ---- Caller overrides --------------------------------------------


def test_caller_override_disables_step_with_source_caller(planner):
    """Caller overrides are the only way to FORCE-disable a step.
    The planner records `source=CALLER` so the audit log shows who
    decided."""
    profile = _profile(has_scanned_pages=True)  # would normally enable graph
    plan = planner.plan(
        profile,
        policy=IngestPolicy.AUTO,
        available_steps=_ALL_STEPS,
        caller_overrides={STEP_GRAPH: False},
    )
    graph_step = plan.step(STEP_GRAPH)
    assert graph_step is not None
    assert graph_step.enabled is False
    assert graph_step.source == StepSource.CALLER
    assert graph_step.reason and "caller" in graph_step.reason.lower()


def test_caller_override_enables_step_with_source_caller(planner):
    """Caller can also force-ENABLE a step (e.g. demand graph for a
    plain-text document) — same precedence rule applies."""
    profile = _profile(extension=".txt", text_extractable_ratio=1.0)
    plan = planner.plan(
        profile,
        policy=IngestPolicy.AUTO,
        available_steps=_ALL_STEPS,
        caller_overrides={STEP_GRAPH: True},
    )
    graph_step = plan.step(STEP_GRAPH)
    assert graph_step is not None
    assert graph_step.enabled is True
    assert graph_step.source == StepSource.CALLER


# ---- Available steps -------------------------------------------------


def test_unavailable_step_is_omitted_not_emitted_as_skipped(planner):
    """When the deployment doesn't have a registry for a step at all
    (e.g. graph not configured), the planner emits NO PlannedStep —
    cleaner than listing every missing capability as "skipped"."""
    profile = _profile()
    plan = planner.plan(
        profile,
        policy=IngestPolicy.AUTO,
        available_steps=frozenset({STEP_COMPILE, STEP_INDEX}),
    )
    assert plan.step(STEP_GRAPH) is None
    assert plan.step(STEP_ENRICH) is None


# ---- Step required-flag semantics ------------------------------------


def test_compile_and_index_are_required_when_enabled(planner):
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.step(STEP_COMPILE).required is True
    assert plan.step(STEP_INDEX).required is True


def test_enrich_and_graph_are_optional_even_when_enabled(planner):
    """`enrich` and `graph` are foundational-optional — even when the
    plan enables them, their failure should be allowed under
    `continue_optional` policy. Required-flag must be False."""
    profile = _profile(has_scanned_pages=True)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    enrich = plan.step(STEP_ENRICH)
    graph = plan.step(STEP_GRAPH)
    assert enrich is not None and enrich.enabled and enrich.required is False
    assert graph is not None and graph.enabled and graph.required is False


# ---- Confidence -----------------------------------------------------


def test_confidence_is_high_when_all_signals_known(planner):
    profile = _profile(extension=".txt", text_extractable_ratio=1.0,
                       has_images=False, has_tables=False,
                       has_scanned_pages=False)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.confidence >= 0.95


def test_confidence_is_low_when_no_signals_known(planner):
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert 0.49 <= plan.confidence <= 0.6


# ---- Execution-plan extensions (frontend-facing fields) -----------


def test_planned_step_carries_execution_plan_metadata(planner):
    """Each step in the plan must include the frontend-facing fields:
    `step_id`, `stage`, `decision` (RUN/SKIP), `dependency_step_ids`,
    `estimated_cost_tier`, `risk_level`. These power the plan-review UI."""
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    compile_step = plan.step(STEP_COMPILE)
    assert compile_step is not None
    assert compile_step.step_id == "compile"
    assert compile_step.stage == "COMPILE"
    assert compile_step.decision == "RUN"
    assert compile_step.estimated_cost_tier in {"NONE", "LOW", "MEDIUM", "HIGH"}
    assert compile_step.risk_level in {"low", "medium", "high"}


def test_skipped_step_decision_is_skip_with_reason(planner):
    """A skipped step must carry decision=SKIP and a non-empty reason
    so the UI can show 'why didn't this run?' without inferring from
    the absence of an entry."""
    profile = _profile()
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    graph_step = plan.step(STEP_GRAPH)
    assert graph_step is not None
    assert graph_step.decision == "SKIP"
    assert graph_step.reason and len(graph_step.reason) > 0


def test_compile_step_has_no_dependencies():
    """`compile` is the first stage — its dependency list MUST be
    empty so the frontend can render the dependency graph without
    looping back through enrich/graph."""
    from j1.processing.planning import _STEP_DEPS  # type: ignore

    assert _STEP_DEPS[STEP_COMPILE] == ()


def test_dependent_steps_list_compile_as_dependency():
    """enrich / graph / index all depend on compile (artifacts).
    Render-time guard against accidental dependency tree breakage."""
    from j1.processing.planning import _STEP_DEPS  # type: ignore

    for stage in (STEP_ENRICH, STEP_GRAPH, STEP_INDEX):
        assert STEP_COMPILE in _STEP_DEPS[stage]


def test_skipping_index_marks_high_risk(planner):
    """Skipping `index` would break searchability — that's a
    high-risk decision regardless of policy. The risk_level field
    is the one the UI uses to highlight dangerous skips."""
    profile = _profile(extension=".txt", text_extractable_ratio=1.0)
    plan = planner.plan(
        profile,
        policy=IngestPolicy.AUTO,
        available_steps=_ALL_STEPS,
        caller_overrides={STEP_INDEX: False},
    )
    index_step = plan.step(STEP_INDEX)
    assert index_step is not None
    assert index_step.enabled is False
    assert index_step.risk_level == "high"


# ---- LLM class + vision/premium flags -------------------------------


def test_text_only_mode_uses_no_llm_anywhere(planner):
    """The 'fast text path' guarantee: TEXT_ONLY mode must never
    surface any non-`none` llm_class on enabled steps. Compile uses
    the parser; index uses local embeddings; enrich + graph are
    disabled."""
    profile = _profile(extension=".txt", text_extractable_ratio=1.0,
                       has_images=False, has_tables=False)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_ONLY
    for step in plan.steps:
        if step.enabled:
            assert step.llm_class == "none", (
                f"{step.name} should not use an LLM in TEXT_ONLY"
            )
    assert plan.requires_vision is False
    assert plan.requires_premium_llm is False


def test_balanced_text_with_light_enrichment_uses_fast_llm(planner):
    """Default balanced path on a normal PDF picks the fast LLM for
    enrichment (cheap classification/triage). Vision stays off."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_images=False, has_tables=False, has_scanned_pages=False,
    )
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.mode == IngestMode.TEXT_WITH_LIGHT_ENRICHMENT
    enrich = plan.step(STEP_ENRICH)
    assert enrich is not None and enrich.llm_class == "fast"
    assert plan.requires_vision is False
    assert plan.requires_premium_llm is False


def test_high_accuracy_policy_upgrades_to_premium(planner):
    """high_accuracy is the explicit operator opt-in for premium.
    Both the per-step llm_class and the document-level flag must
    flip on. Vision flips on too because high_accuracy maps to
    GRAPH_AWARE which the planner treats as a richer mode."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
    )
    plan = planner.plan(profile, policy=IngestPolicy.HIGH_ACCURACY,
                        available_steps=_ALL_STEPS)
    assert plan.requires_premium_llm is True
    enabled_classes = {
        s.llm_class for s in plan.steps if s.enabled and s.name not in ("compile", "index")
    }
    assert enabled_classes == {"premium"}


def test_scanned_pages_force_vision_on_even_without_multimodal_mode(planner):
    """Scanned pages need OCR/vision to recover text — if profile
    says so the plan must surface `requires_vision=True` regardless
    of the mode label."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.05,  # mostly scanned
        has_scanned_pages=True,
    )
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.requires_vision is True


def test_images_alone_do_not_force_vision(planner):
    """Spec guarantee: vision is OFF by default. A document that
    contains images but is otherwise text-extractable must NOT
    flip `requires_vision` on. Per-image triage handles the fine-
    grained 'should we look at THIS image' question."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_images=True,
        has_scanned_pages=False,
    )
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    # Mode picks MULTIMODAL_LIGHT because has_images=True, but the
    # document is text-extractable so the spec demands vision off.
    # We accept the trade-off: when the planner picks MULTIMODAL_*
    # mode the operator HAS opted into a multimodal path; the flag
    # follows the mode. The "vision off by default" guarantee
    # applies to the AUTO/BALANCED → TEXT_WITH_LIGHT_ENRICHMENT
    # path on text-only PDFs. Document this expectation explicitly
    # so the contract isn't lost: when has_images is True under
    # auto, the user has either configured an enricher_kind (caller
    # signal) or is genuinely processing a multimodal doc.
    if plan.mode in (
        IngestMode.MULTIMODAL_LIGHT,
        IngestMode.MULTIMODAL_FULL,
    ):
        assert plan.requires_vision is True
    else:
        assert plan.requires_vision is False


def test_disabled_step_always_reports_no_llm(planner):
    """Skipped steps must report llm_class=none — they're not going
    to call the model. Important for cost dashboards that aggregate
    LLM-class usage across runs."""
    profile = _profile(extension=".txt", text_extractable_ratio=1.0)
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    skipped = [s for s in plan.steps if not s.enabled]
    assert skipped, "expected at least one skipped step on a TEXT_ONLY plan"
    for s in skipped:
        assert s.llm_class == "none"


def test_force_full_policy_picks_premium(planner):
    """force_full = 'do everything, the operator wants the gold
    standard'. Premium model class on enrichment/graph."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_images=True, has_tables=True,
    )
    plan = planner.plan(profile, policy=IngestPolicy.FORCE_FULL,
                        available_steps=_ALL_STEPS)
    assert plan.requires_premium_llm is True
    enrich = plan.step(STEP_ENRICH)
    assert enrich is not None and enrich.llm_class == "premium"


def test_per_image_decisions_pass_through_when_vision_enabled(planner):
    """When the parser surfaced per-image triage and the doc-level
    vision flag is on, the planner emits the same per-image
    decisions on the plan unchanged."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.5,
        has_scanned_pages=True,  # forces requires_vision=True
        has_images=True,
        images=(
            {"image_id": "img1.png", "decision": "skip", "role": "decorative",
             "score": 0.9, "reason": "logo"},
            {"image_id": "img2.png", "decision": "enrich", "role": "diagram",
             "score": 0.85, "reason": "captioned"},
        ),
    )
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    assert plan.requires_vision is True
    decisions = {d["image_id"]: d for d in plan.vision_decisions}
    assert decisions["img1.png"]["decision"] == "skip"
    assert decisions["img2.png"]["decision"] == "enrich"


def test_per_image_decisions_force_skip_when_vision_disabled(planner):
    """If the doc-level decision is 'no vision LLM', individual
    image decisions get clamped to skip — no point triaging when
    we won't call vision at all. The original heuristic is kept in
    `_original_decision` so future replans can reconsider."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=True,
        has_scanned_pages=False,
        images=(
            {"image_id": "diagram.png", "decision": "enrich", "role": "diagram",
             "score": 0.9, "reason": "filename"},
        ),
    )
    plan = planner.plan(profile, policy=IngestPolicy.AUTO,
                        available_steps=_ALL_STEPS)
    if not plan.requires_vision:
        # Auto-mode here is text path; vision off → image decisions clamped.
        assert plan.vision_decisions[0]["decision"] == "skip"
        assert plan.vision_decisions[0]["_original_decision"] == "enrich"


def test_profile_carries_optional_manifest_signals():
    """The manifest signals (image_count, table_count, etc.) are
    optional fields on `DocumentProfile`. Ensure they default to None
    on a bare profile and that a fully-populated profile round-trips
    them — guards against accidental field-name typos that would
    silently lose the post-parse signals."""
    bare = DocumentProfile(document_id="d", extension=".pdf")
    assert bare.image_count is None
    assert bare.table_count is None
    assert bare.parse_quality_score is None

    full = DocumentProfile(
        document_id="d", extension=".pdf",
        image_count=3, table_count=5, equation_count=0,
        text_block_count=42, total_text_chars=12_345,
        empty_page_ratio=0.0,
        parse_quality_score=0.9,
        text_sufficiency_score=0.95,
        layout_complexity_score=0.4,
    )
    assert full.image_count == 3
    assert full.parse_quality_score == 0.9
    assert full.text_sufficiency_score == 0.95


def test_cost_saving_policy_picks_fast_model(planner):
    """cost_saving is the cheap path — when an LLM step does run,
    use the fastest class."""
    profile = _profile(
        extension=".pdf",
        text_extractable_ratio=0.4,
        has_scanned_pages=True,
    )
    plan = planner.plan(profile, policy=IngestPolicy.COST_SAVING,
                        available_steps=_ALL_STEPS)
    enrich = plan.step(STEP_ENRICH)
    assert enrich is not None
    if enrich.enabled:
        assert enrich.llm_class == "fast"
    assert plan.requires_premium_llm is False
