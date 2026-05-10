"""Unit tests for `ProjectProcessingWorkflow._stage_enabled` — the
compile-result + post-compile-enrich-plan-aware stage gate.

Pure-method tests (no Temporal runtime). Build a workflow instance,
construct a `PostCompileEnrichPlan` + a synthetic compile result,
and assert the gate returns the expected `(enabled, reason, source)`.

There is intentionally NO IngestPlan parameter — gating is
compile-first. Pre-compile guesses do not influence enrich/graph/
index decisions.
"""

from __future__ import annotations

from j1.orchestration.activities.payloads import ArtifactActivityResult
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


def _ok_compile() -> ArtifactActivityResult:
    """Synthetic 'compile succeeded with chunks' result."""
    return ArtifactActivityResult(
        status="succeeded",
        artifact_ids=["a-c1"],
        kinds=("chunk",),
        compile_metrics={"chunks_count": 5, "extracted_text_chars": 1000},
    )


def _failed_compile() -> ArtifactActivityResult:
    return ArtifactActivityResult(
        status="failed",
        artifact_ids=[],
        error="parse failed",
        compile_metrics={},
    )


# ---- enrich stage --------------------------------------------------


def test_enrich_plan_skip_overrides_caller_intent():
    """SKIP is authoritative: even with `enricher_kind` set, the gate
    returns disabled with the blocking reason."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.SKIP,
        blocking_issues=("compile failed; nothing to enrich",),
    )
    enabled, reason, source = _wf()._stage_enabled(
        "enrich", "composite_enricher",
        compile_result=_ok_compile(),
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
        "enrich", "composite_enricher",
        compile_result=_ok_compile(),
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
        "enrich", "composite_enricher",
        compile_result=_ok_compile(),
        enrich_plan=plan,
    )
    assert enabled is True
    assert source == StepSource.PLANNER


def test_enrich_plan_optional_defers_to_caller():
    """OPTIONAL: enrich plan doesn't force a decision; the caller's
    `enricher_kind` runs the stage with CALLER source."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.OPTIONAL,
    )
    enabled, _, source = _wf()._stage_enabled(
        "enrich", "composite_enricher",
        compile_result=_ok_compile(),
        enrich_plan=plan,
    )
    assert enabled is True
    assert source == StepSource.CALLER


def test_no_enricher_kind_is_unrunnable_regardless_of_plan():
    """Caller didn't choose an enricher → unrunnable, regardless of
    what the enrich plan recommends. Caller intent gates first."""
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.RECOMMENDED,
    )
    enabled, reason, source = _wf()._stage_enabled(
        "enrich", request_kind=None,
        compile_result=_ok_compile(),
        enrich_plan=plan,
    )
    assert enabled is False
    assert "enrich_kind" in (reason or "")
    assert source == StepSource.CALLER


def test_no_enrich_plan_runs_with_caller_source():
    """Without an enrich_plan: caller intent + a successful compile
    is enough; runs with CALLER source."""
    enabled, reason, source = _wf()._stage_enabled(
        "enrich", "composite_enricher",
        compile_result=_ok_compile(),
    )
    assert enabled is True
    assert reason is None
    assert source == StepSource.CALLER


# ---- graph stage ---------------------------------------------------


def test_graph_skips_when_compile_failed():
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=_failed_compile(),
    )
    assert enabled is False
    assert "compile did not succeed" in (reason or "")
    assert source == StepSource.PLANNER


def test_graph_skips_when_final_quality_failed():
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=_ok_compile(),
        final_compile_quality="failed",
    )
    assert enabled is False
    assert "FAILED" in (reason or "")
    assert source == StepSource.PLANNER


def test_graph_skips_when_zero_chunks():
    cr = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=["a-c1"],
        kinds=(),  # no chunk kinds
        compile_metrics={"chunks_count": 0},
    )
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=cr,
    )
    assert enabled is False
    assert "zero chunks" in (reason or "")
    assert source == StepSource.PLANNER


def test_graph_skips_on_low_compile_quality():
    """Low parse quality → conservative skip. Graph extraction would
    amplify a degraded parse."""
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=_ok_compile(),
        final_compile_quality="low",
    )
    assert enabled is False
    assert "LOW" in (reason or "")
    assert source == StepSource.PLANNER


def test_graph_skips_on_enrich_plan_blocking_issues():
    plan = PostCompileEnrichPlan(
        overall_recommendation=EnrichRecommendation.SKIP,
        blocking_issues=("post-compile assessor: nothing usable",),
    )
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=_ok_compile(),
        enrich_plan=plan,
    )
    assert enabled is False
    assert "nothing usable" in (reason or "")
    assert source == StepSource.PLANNER


def test_graph_runs_when_compile_good_and_chunks_present():
    enabled, reason, source = _wf()._stage_enabled(
        "graph", "lightrag_graph",
        compile_result=_ok_compile(),
        final_compile_quality="good",
    )
    assert enabled is True
    assert reason is None
    assert source == StepSource.CALLER


def test_graph_skipped_when_no_graph_builder_kind():
    enabled, reason, source = _wf()._stage_enabled(
        "graph", request_kind=None,
        compile_result=_ok_compile(),
    )
    assert enabled is False
    assert "graph_kind" in (reason or "")
    assert source == StepSource.CALLER


# ---- index stage ---------------------------------------------------


def test_index_skips_when_compile_failed():
    enabled, reason, source = _wf()._stage_enabled(
        "index", "sqlite_search",
        compile_result=_failed_compile(),
    )
    assert enabled is False
    assert "compile did not succeed" in (reason or "")
    assert source == StepSource.PLANNER


def test_index_skips_when_zero_chunks():
    cr = ArtifactActivityResult(
        status="succeeded",
        artifact_ids=[],
        kinds=(),
        compile_metrics={"chunks_count": 0},
    )
    enabled, reason, source = _wf()._stage_enabled(
        "index", "sqlite_search",
        compile_result=cr,
    )
    assert enabled is False
    assert "zero chunks" in (reason or "")
    assert source == StepSource.PLANNER


def test_index_runs_when_compile_good():
    enabled, reason, source = _wf()._stage_enabled(
        "index", "sqlite_search",
        compile_result=_ok_compile(),
    )
    assert enabled is True
    assert reason is None
    assert source == StepSource.CALLER


def test_index_skipped_when_no_indexer_kind():
    enabled, reason, source = _wf()._stage_enabled(
        "index", request_kind=None,
        compile_result=_ok_compile(),
    )
    assert enabled is False
    assert "index_kind" in (reason or "")
    assert source == StepSource.CALLER


# ---- safety: no IngestPlan threaded through any of these ----------


def test_stage_enabled_signature_does_not_accept_ingest_plan():
    """Regression guard: ensure we don't reintroduce an `IngestPlan`
    parameter on `_stage_enabled` by accident. The new signature
    is `(stage, request_kind, *, compile_result, final_compile_quality,
    enrich_plan)` — no `plan` positional."""
    import inspect
    sig = inspect.signature(_wf()._stage_enabled)
    params = list(sig.parameters.keys())
    assert "plan" not in params
    assert params[0] == "stage"
    assert params[1] == "request_kind"
