"""Unit tests for `ProjectProcessingWorkflow._stage_enabled` — the
post-compile-enrich-plan-aware stage gate.

Pure-method tests (no Temporal runtime). Build a workflow instance,
construct a `PostCompileEnrichPlan`, and assert the gate returns
the expected `(enabled, reason, source)` tuple.

Covers the contract added in Tier B-1:
  * SKIP recommendation with blocking_issues → skip with PLANNER source.
  * RECOMMENDED / REQUIRED → run with PLANNER source.
  * OPTIONAL → defers to caller (request_kind set means run, source=CALLER).
  * Stage other than `enrich` is unchanged by enrich_plan.
"""

from __future__ import annotations

from j1.orchestration.workflows.project_processing import (
    ProjectProcessingWorkflow,
)
from j1.processing.enrich_assessment import (
    EnrichRecommendation,
    PostCompileEnrichPlan,
)
from j1.processing.status import StepSource


def _wf() -> ProjectProcessingWorkflow:
    """Workflow instance for `_stage_enabled` testing. The constructor
    runs without a Temporal runtime; we never invoke `run`."""
    return ProjectProcessingWorkflow()


def test_enrich_plan_skip_overrides_caller_intent():
    """SKIP is authoritative: even with `enricher_kind` set, the gate
    returns disabled with the blocking reason."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.SKIP,
        blocking_issues=("compile failed; nothing to enrich",),
    )
    enabled, reason, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
        enrich_plan=plan,
    )
    assert enabled is False
    assert "compile failed" in (reason or "")
    assert source == StepSource.PLANNER


def test_enrich_plan_recommended_runs_with_planner_source():
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.RECOMMENDED,
        recommended_tasks=("table_enrichment",),
    )
    enabled, reason, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
        enrich_plan=plan,
    )
    assert enabled is True
    assert reason is None
    assert source == StepSource.PLANNER


def test_enrich_plan_required_runs_with_planner_source():
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.REQUIRED,
    )
    enabled, _, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
        enrich_plan=plan,
    )
    assert enabled is True
    assert source == StepSource.PLANNER


def test_enrich_plan_optional_defers_to_caller():
    """OPTIONAL: enrich plan doesn't force a decision; if the IngestPlan
    is None and the caller supplied `enricher_kind`, run with CALLER
    source (legacy behaviour preserved)."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
    )
    enabled, _, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
        enrich_plan=plan,
    )
    assert enabled is True
    assert source == StepSource.CALLER


def test_enrich_plan_does_not_affect_graph_stage():
    """Enrich plan is scoped to the enrich stage; graph still uses
    the IngestPlan + caller precedence unchanged."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.SKIP,
        blocking_issues=("compile failed",),
    )
    enabled, _, source = _wf()._stage_enabled(
        None, "graph", request_kind="lightrag_graph",
        enrich_plan=plan,
    )
    # Graph gate sees plan=None + request_kind set → run with CALLER.
    assert enabled is True
    assert source == StepSource.CALLER


def test_enrich_plan_skip_with_no_blocking_issues_uses_default_reason():
    """SKIP without explicit blocking_issues still returns a reason
    so audit logs always have something to render."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.SKIP,
    )
    enabled, reason, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
        enrich_plan=plan,
    )
    assert enabled is False
    assert reason  # non-empty fallback string
    assert source == StepSource.PLANNER


def test_no_enricher_kind_is_unrunnable_regardless_of_plan():
    """Caller didn't choose an enricher → unrunnable, regardless of
    what the enrich plan recommends. Caller intent gates first."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.RECOMMENDED,
    )
    enabled, reason, source = _wf()._stage_enabled(
        None, "enrich", request_kind=None,
        enrich_plan=plan,
    )
    assert enabled is False
    assert "enrich_kind" in (reason or "")
    assert source == StepSource.CALLER


def test_no_enrich_plan_falls_back_to_legacy_behaviour():
    """Without an enrich_plan the gate behaves exactly as before:
    plan=None + request_kind set → run with CALLER source."""
    enabled, reason, source = _wf()._stage_enabled(
        None, "enrich", request_kind="composite_enricher",
    )
    assert enabled is True
    assert reason is None
    assert source == StepSource.CALLER
