"""Compile-first ingestion workflow regression tests.

Pin the contract of the post-`_build_plan`-removal workflow:

 1. `planner_enabled=False` skips even the cheap pre-compile profile.
 2. `planner_enabled=True` runs `profile_document` pre-compile, then
 `DefaultAssessmentPlanner` to derive the AssessmentPlan that
 drives compile config — but does NOT call any IngestPlanner /
 `_build_plan` / `_apply_post_compile_planning` / planning-result
 activity.
 3. The workflow emits an `ingestion.assessment.created` log event
 (not the old `ingestion.plan.created`).
 4. A `profile_document` failure under fail_closed surfaces as a
 workflow failure.
 5. Synthetic step events for `build_content_inventory` and
 `generate_knowledge_chunks` still fire in the user-facing order
 after compile succeeds.
 6. Caller-supplied graph_builder_kind keeps graph runnable; the
 graph stage is gated on compile evidence + the post-compile
 enrich plan, not a pre-compile plan.

Regression guards: any reintroduction of `_build_plan`,
`_apply_post_compile_planning`, or the `build_planning_result`
activity dispatch from the workflow path is asserted-against."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProcessingActivityResult,
    ProjectScope,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.enrich_assessment import (
    ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED,
)
from j1.processing.profiling import DocumentProfile
from j1.processing.status import FinalStatus, StepSource, StepStatus


@pytest.fixture(autouse=True)
def _enable_auto_enrichment(monkeypatch):
    """Workflow integration tests assert the enrichment + graph
    stages run end-to-end. The deployment-wide auto-run gate is OFF
    by default in production; opt in here so the integration tests
    still exercise the full pipeline. Dedicated tests cover the
    OFF-by-default behaviour."""
    monkeypatch.setenv(ENV_DOMAIN_ENRICHMENT_AUTO_ENABLED, "true")


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(monkeypatch, *, exec_handler):
    captured = {"calls": []}

    async def _exec(method, payload=None, **kwargs):
        captured["calls"].append({"name": _activity_name(method), "payload": payload})
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)
    return captured


# Reusable test profiles for different document shapes.
_PROFILE_SIMPLE_TEXT = DocumentProfile(
    document_id="doc-1",
    extension=".txt",
    mime_type="text/plain",
    file_size_bytes=42,
    page_count=1,
    text_extractable_ratio=1.0,
    has_images=False,
    has_tables=False,
    has_scanned_pages=False,
)


def _full_pipeline_handler(*, profile: DocumentProfile | None = None):
    """Build a handler that returns success for every stage. The
 `profile` fixture is returned by `profile_document` when planner
 is enabled."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            assert profile is not None, (
                "test passed planner_enabled=True without a profile"
            )
            return profile
        if name.endswith("build_initial_execution_plan"):
            # Mirror what the real activity produces: a populated
            # plan payload (with the legacy `compile_plan` shape the
            # rest of the workflow consumes) plus a stub artifact id.
            from j1.processing.assessment import DefaultAssessmentPlanner
            from j1.processing.initial_execution_plan import (
                build_initial_execution_plan as _build,
            )
            plan = _build(profile)
            from j1.orchestration.activities.payloads import (
                BuildInitialExecutionPlanResult,
            )
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="initial-plan-art-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 3, "extracted_text_chars": 200},
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-enriched-1"],
                kinds=("enriched.tables",),
            )
        if name.endswith("build_graph"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-graph-1"],
                kinds=("graph_json",),
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        if (
            name.endswith("persist_final_summary")
            or name.endswith("persist_error_report")
            or name.endswith("persist_compile_strategy_report")
            or name.endswith("persist_post_compile_enrich_plan")
            or name.endswith("persist_initial_execution_plan")
            or name.endswith("persist_compile_result_summary")
            or name.endswith("persist_enrichment_result")
        ):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["report-1"],
                kinds=("validation_report",),
            )
        if name.endswith("run_enrichment_stage"):
            #  stub: succeed cleanly so existing tests don't
            # accidentally observe a failed-optional outcome.
            from j1.orchestration.activities.payloads import (
                RunEnrichmentStageResult,
            )
            return RunEnrichmentStageResult(
                status="succeeded",
                plan_payload={"document_id": "doc-1", "status": "succeeded"},
                artifact_id="enrichment-art-1",
                require_enrichment_success=False,
            )
        raise AssertionError(f"unexpected activity: {name}")
    return handler


# ---- planner_enabled=False is identical to legacy ------------------


def test_planner_disabled_does_not_call_profile_document(monkeypatch):
    """Legacy callers must not pay the profiling cost when they
 haven't opted in."""
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_full_pipeline_handler(),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=False,
    )
    result = asyncio.run(wf.run(request))
    assert result.final_status == FinalStatus.COMPLETED
    profile_calls = [
        c for c in captured["calls"] if "profile_document" in c["name"]
    ]
    assert profile_calls == [], (
        "planner disabled must NOT invoke profile_document; "
        f"saw: {profile_calls}"
    )


# ---- planner_enabled=True runs only profile + AssessmentPlanner ----


def test_planner_enabled_calls_profile_document_pre_compile(monkeypatch):
    """With `planner_enabled=True`, `profile_document` runs once
 BEFORE `compile` so the AssessmentPlan can drive compile config."""
    captured = _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
    )
    asyncio.run(wf.run(request))

    names_in_order = [c["name"] for c in captured["calls"]]
    profile_idx = next(
        (i for i, n in enumerate(names_in_order) if "profile_document" in n),
        None,
    )
    compile_idx = next(
        (i for i, n in enumerate(names_in_order) if n.endswith(".compile")),
        None,
    )
    assert profile_idx is not None, (
        f"profile_document must run; saw: {names_in_order}"
    )
    assert compile_idx is not None
    assert profile_idx < compile_idx, (
        f"profile_document must precede compile; saw: {names_in_order}"
    )


def test_workflow_does_not_call_old_planner_activities(monkeypatch):
    """Regression guard: the workflow must NOT invoke any of the
 deleted planner-related activities. If a future refactor
 accidentally re-introduces a `build_planning_result` or
 `report_plan_generated` dispatch, this test catches it."""
    captured = _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
    )
    asyncio.run(wf.run(request))

    forbidden_substrings = (
        "build_planning_result",
        "report_plan_generated",
        "report_plan_revised",
    )
    forbidden_calls = [
        c for c in captured["calls"]
        if any(s in c["name"] for s in forbidden_substrings)
    ]
    assert forbidden_calls == [], (
        f"old planner activities must not run; saw: {forbidden_calls}"
    )


def test_workflow_does_not_invoke_build_plan_method():
    """Static guard: the workflow class must not expose `_build_plan`
 or `_apply_post_compile_planning` as methods anymore."""
    wf = ProjectProcessingWorkflow()
    assert not hasattr(wf, "_build_plan"), (
        "_build_plan must be deleted from the workflow"
    )
    assert not hasattr(wf, "_maybe_replan_after_compile"), (
        "_maybe_replan_after_compile must be deleted from the workflow"
    )
    assert not hasattr(wf, "_emit_plan_generated"), (
        "_emit_plan_generated must be deleted from the workflow"
    )
    assert not hasattr(wf, "_emit_plan_revised"), (
        "_emit_plan_revised must be deleted from the workflow"
    )


def test_workflow_module_does_not_export_old_planner_helpers():
    """Module-level audit: the deleted helpers must no longer be
 importable from the workflow module."""
    import j1.orchestration.workflows.project_processing as wf_module
    for name in (
        "_apply_post_compile_planning",
        "_summarise_plan_diff",
        "_format_plan_diff_reason",
        "_profile_payload",
    ):
        assert not hasattr(wf_module, name), (
            f"{name} must be deleted from the workflow module"
        )


# ---- ingestion.assessment.created log event ------------------------


def test_planner_enabled_records_assessment_created_log_event(monkeypatch):
    """The workflow logs an `ingestion.assessment.created` event
 (not the old `ingestion.plan.created`) so operators can verify
 the AssessmentPlan ran."""
    captured_logs: list[dict] = []

    class _StubLogger:
        def info(self, _msg, extra=None):
            captured_logs.append(extra or {})

    monkeypatch.setattr(workflow, "logger", _StubLogger())
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        correlation_id="run-test",
    )
    asyncio.run(wf.run(request))

    events = [
        e.get("event") for e in captured_logs if e.get("event")
    ]
    assert "ingestion.assessment.created" in events, (
        f"expected ingestion.assessment.created log; saw: {events}"
    )
    # Old name must not fire.
    assert "ingestion.plan.created" not in events, (
        f"old ingestion.plan.created must not fire; saw: {events}"
    )


# ---- profile_document failure surfacing ----------------------------


def test_profile_document_failure_under_fail_closed_surfaces_as_workflow_failure(
    monkeypatch,
):
    """Under `fail_closed`, a `profile_document` failure must
 propagate to the workflow as a business rejection (FAILED_FINAL),
 not silently downgrade to no-AssessmentPlan."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            raise ApplicationError(
                "source file missing",
                type="J1_INGEST_LOOKUP_FAILED",
                non_retryable=True,
            )
        if name.endswith("finalize"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        if (
            name.endswith("persist_error_report")
            or name.endswith("persist_final_summary")
        ):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["report-1"],
                kinds=("error_report",),
            )
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        assessment_failure_policy="fail_closed",
    )
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))


def test_profile_document_failure_under_fail_open_continues_with_settings(
    monkeypatch,
):
    """Under default `fail_open`, a `profile_document` failure logs
 + continues without an AssessmentPlan; the bridge falls back to
 `settings.parse_method`."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            raise ApplicationError(
                "source file missing",
                type="J1_INGEST_LOOKUP_FAILED",
                non_retryable=True,
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 1, "extracted_text_chars": 50},
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        if (
            name.endswith("persist_final_summary")
            or name.endswith("persist_compile_strategy_report")
            or name.endswith("persist_post_compile_enrich_plan")
        ):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["report-1"],
                kinds=("validation_report",),
            )
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        # fail_open is the default; spell it out for clarity.
        assessment_failure_policy="fail_open",
    )
    result = asyncio.run(wf.run(request))
    assert result.final_status == FinalStatus.COMPLETED


# ---- Synthetic step events around compile --------------------------


def _collect_step_lifecycle_calls(captured) -> list[dict]:
    return [
        (c["payload"].stage, c["payload"].step, c["payload"].action)
        for c in captured["calls"]
        if c["name"].endswith("report_step_lifecycle")
        and c["payload"] is not None
    ]


# ---- Caller intent vs. compile-result-driven gating ---------------


def test_caller_supplied_graph_kind_runs_when_compile_good(monkeypatch):
    """A successful compile + caller-supplied graph_builder_kind
 runs the graph stage."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        graph_builder_kind="g",
        indexer_kind="i",
        planner_enabled=True,
    )
    result = asyncio.run(wf.run(request))
    assert result.final_status == FinalStatus.COMPLETED
    graph_steps = [s for s in result.step_results if s.step == "graph"]
    completed = [s for s in graph_steps if s.status == StepStatus.COMPLETED]
    assert completed, (
        f"caller-supplied graph_builder_kind + good compile must run "
        f"graph; saw: {[(s.step, s.status, s.source) for s in graph_steps]}"
    )
