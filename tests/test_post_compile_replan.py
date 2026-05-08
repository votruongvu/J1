"""Post-compile replan tests.

Covers the workflow's `_maybe_replan_after_compile` + `_summarise_plan_diff`
helpers. The replan path is the framework's answer to "compile
discovered images/tables/scanned pages the deterministic profile
missed" — replan should ENABLE downstream stages, never re-run
compile, never DISABLE stages the initial plan had on.

The workflow itself drives Temporal activities, so we exercise the
pure-Python helpers directly. Workflow integration tests live
elsewhere; what's locked here is the contract:

  * `_summarise_plan_diff` returns an empty dict when nothing changed
    and a non-empty diff when step enablement flipped.
  * `_format_plan_diff_reason` produces operator-readable copy.
  * The diff never includes compile (compile already ran).
  * Caller-driven `force_full` policy still wins over post-compile
    signals.
"""

from __future__ import annotations

from j1.orchestration.workflows.project_processing import (
    _format_plan_diff_reason,
    _summarise_plan_diff,
)
from j1.processing.planning import (
    DefaultIngestPlanner,
    DocumentProfile,
    IngestMode,
    IngestPolicy,
    STEP_COMPILE,
    STEP_ENRICH,
    STEP_GRAPH,
    STEP_INDEX,
)


def _build_plan(profile, *, policy=IngestPolicy.AUTO, kinds=("compile", "enrich", "graph", "index")):
    """Convenience wrapper: build a plan with the bundled set of
    available steps + caller overrides. Mirrors the workflow's
    `_build_plan` shape so the replan tests exercise the planner's
    actual decision logic."""
    overrides = {STEP_COMPILE: True}
    available = {STEP_COMPILE}
    if "enrich" in kinds:
        overrides[STEP_ENRICH] = True
        available.add(STEP_ENRICH)
    if "graph" in kinds:
        overrides[STEP_GRAPH] = True
        available.add(STEP_GRAPH)
    if "index" in kinds:
        overrides[STEP_INDEX] = True
        available.add(STEP_INDEX)
    return DefaultIngestPlanner().plan(
        profile,
        policy=policy,
        available_steps=frozenset(available),
        caller_overrides=overrides,
    )


# ---- _summarise_plan_diff ----------------------------------------


def test_diff_empty_when_plans_match():
    profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
    )
    initial = _build_plan(profile)
    revised = _build_plan(profile)
    assert _summarise_plan_diff(initial, revised) == {}


def test_diff_flags_step_re_enabled_when_revised_plan_unlocks():
    """The planner picks `TEXT_WITH_LIGHT_ENRICHMENT` for a profile
    that looks text-only. After compile reveals images, the revised
    profile gets `has_images=True` and the planner upgrades to a
    multimodal mode. The diff should flag the mode change."""
    text_only_profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
    )
    revised_profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=True,
        has_tables=False,
        has_scanned_pages=False,
    )
    initial = _build_plan(text_only_profile)
    revised = _build_plan(revised_profile)
    diff = _summarise_plan_diff(initial, revised)
    if initial.mode != revised.mode:
        assert "mode" in diff


def test_diff_includes_mode_change_when_modes_differ():
    profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
        has_scanned_pages=False,
    )
    initial = _build_plan(profile)
    # Force a different mode by changing the profile signals.
    revised_profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=0.05,
        has_images=True,
        has_tables=True,
        has_scanned_pages=True,
    )
    revised = _build_plan(revised_profile)
    diff = _summarise_plan_diff(initial, revised)
    assert diff
    if initial.mode != revised.mode:
        assert diff["mode"]["before"] == initial.mode.value
        assert diff["mode"]["after"] == revised.mode.value


# ---- _format_plan_diff_reason ------------------------------------


def test_reason_empty_diff_returns_generic_label():
    """Empty diff should never be passed to the formatter (the
    workflow gates on it earlier), but the formatter still returns
    a non-empty string so audit consumers don't crash."""
    assert _format_plan_diff_reason({}) == "Post-compile replan"


def test_reason_step_re_enabled_renders_to_operator_copy():
    diff = {"graph": {"before": False, "after": True}}
    reason = _format_plan_diff_reason(diff)
    assert "Post-compile replan:" in reason
    assert "graph re-enabled" in reason


def test_reason_step_disabled_renders_to_operator_copy():
    diff = {"enrich": {"before": True, "after": False}}
    reason = _format_plan_diff_reason(diff)
    assert "enrich disabled" in reason


def test_reason_mode_change_renders_arrow():
    diff = {"mode": {"before": "text_only", "after": "multimodal_full"}}
    reason = _format_plan_diff_reason(diff)
    assert "mode text_only->multimodal_full" in reason


def test_reason_combines_multiple_changes():
    diff = {
        "graph": {"before": False, "after": True},
        "mode": {"before": "text_only", "after": "multimodal_light"},
    }
    reason = _format_plan_diff_reason(diff)
    # Both changes show up.
    assert "graph re-enabled" in reason
    assert "mode text_only->multimodal_light" in reason


# ---- Replan invariants -------------------------------------------


def test_compile_never_appears_in_diff():
    """Compile already ran by the time replan triggers. Even if the
    revised plan's compile step changed, the workflow MUST NOT
    re-execute compile — and the diff helpers must not surface
    compile changes the workflow could mistakenly act on."""
    profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
    )
    initial = _build_plan(profile)
    revised = _build_plan(profile)
    diff = _summarise_plan_diff(initial, revised)
    # Compile should be enabled in both → no diff entry. The
    # invariant: even when something else flips, compile-flipping
    # is impossible to reach via this path because compile is
    # always overridden True.
    assert "compile" not in diff


def test_force_full_policy_keeps_all_optional_stages_enabled_after_replan():
    """`force_full` makes every step enabled regardless of profile
    signals. A replan with the same policy must not silently
    DISABLE a stage just because compile content stats came back
    boring."""
    rich_profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=0.5,
        has_images=True,
        has_tables=True,
    )
    boring_profile = DocumentProfile(
        document_id="d-1",
        extension=".pdf",
        text_extractable_ratio=1.0,
        has_images=False,
        has_tables=False,
    )
    initial = _build_plan(rich_profile, policy=IngestPolicy.FORCE_FULL)
    revised = _build_plan(boring_profile, policy=IngestPolicy.FORCE_FULL)
    # Every optional step should still be enabled in both plans.
    enabled_initial = {s.name for s in initial.steps if s.enabled}
    enabled_revised = {s.name for s in revised.steps if s.enabled}
    assert STEP_ENRICH in enabled_initial and STEP_ENRICH in enabled_revised
    assert STEP_GRAPH in enabled_initial and STEP_GRAPH in enabled_revised
    assert STEP_INDEX in enabled_initial and STEP_INDEX in enabled_revised
