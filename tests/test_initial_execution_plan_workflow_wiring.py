"""Wave 3 workflow-integration tests.

Pins:
  1. The workflow's planner-enabled path dispatches the new
     `build_initial_execution_plan` activity AFTER `profile_document`
     and BEFORE `compile`.
  2. The activity's `plan_payload` flows into the compile activity
     via the legacy `assessment_plan_payload` shape (back-compat
     with the per-attempt compile retry loop).
  3. The activity's `domain_profile_id` is surfaced on the
     `ingestion.assessment.created` log event.
  4. When the activity returns None / missing payload, the workflow
     falls back to the in-workflow `DefaultAssessmentPlanner` and
     still completes.

These tests use the same `_patch_workflow_runtime` shape as
`test_planner_workflow_integration.py` — synchronous handler that
intercepts every `execute_activity_method` dispatch.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    BuildInitialExecutionPlanResult,
    ProjectScope,
    ProcessingActivityResult,
    StageValidationActivityResult,
    ValidateContextResult,
)
from temporalio import workflow as temporal_workflow_mod
from j1.orchestration.workflows import project_processing as workflow_mod
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.initial_execution_plan import build_initial_execution_plan
from j1.processing.profiling import DocumentProfile


_PROFILE = DocumentProfile(
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


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _activity_name(method) -> str:
    return (
        getattr(method, "__temporal_activity_definition", None)
        and method.__temporal_activity_definition.name
        or getattr(method, "__name__", str(method))
    )


def _patch_workflow_runtime(monkeypatch, *, exec_handler):
    captured = {"calls": []}

    async def _exec(method, payload=None, **kwargs):
        captured["calls"].append({
            "name": _activity_name(method), "payload": payload,
        })
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow_mod.workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow_mod.workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow_mod.workflow, "wait_condition", _wait)
    monkeypatch.setattr(
        workflow_mod.workflow, "continue_as_new", lambda *_a, **_k: None,
    )
    return captured


def _make_handler(
    *,
    profile: DocumentProfile = _PROFILE,
    build_result: BuildInitialExecutionPlanResult | None = None,
    compile_captured: list[Any] | None = None,
):
    """Default-success handler that returns valid results for every
    activity we expect to see. `build_result` lets a test inject a
    specific BuildInitialExecutionPlanResult; None ⇒ build a default
    from the real builder."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return profile
        if name.endswith("build_initial_execution_plan"):
            if build_result is not None:
                return build_result
            plan = build_initial_execution_plan(profile)
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="initial-plan-art-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            if compile_captured is not None:
                compile_captured.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3, "extracted_text_chars": 200,
                },
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=getattr(payload, "stage_name", "compile"),
                validation_status="passed", passed=True,
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        if name.endswith("report_step_skipped"):
            return None
        if name.endswith("set_document_status"):
            return None
        if name.endswith("report_terminal"):
            return None
        if (
            name.endswith("persist_validation_report")
            or name.endswith("persist_final_summary")
            or name.endswith("persist_error_report")
            or name.endswith("persist_compile_strategy_report")
            or name.endswith("persist_post_compile_enrich_plan")
            or name.endswith("persist_initial_execution_plan")
        ):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r-1"],
                kinds=("validation_report",),
            )
        if name.endswith("fast_llm_consult_enrich"):
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        # Catch-all: never raise (matches the existing patch style),
        # just return None for unrecognised activities.
        return None

    return handler


# ---- 1. activity is dispatched in the right order -----------------


def test_build_initial_execution_plan_runs_after_profile_before_compile(
    monkeypatch,
):
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_make_handler(),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
        planner_enabled=True, correlation_id="run-test",
    )
    asyncio.run(wf.run(request))
    names = [c["name"] for c in captured["calls"]]
    # profile_document precedes build_initial_execution_plan
    profile_idx = next(
        i for i, n in enumerate(names) if "profile_document" in n
    )
    build_idx = next(
        i for i, n in enumerate(names)
        if n.endswith("build_initial_execution_plan")
    )
    compile_idx = next(
        i for i, n in enumerate(names)
        if n.endswith("j1.processing.compile")
    )
    assert profile_idx < build_idx < compile_idx, (
        f"expected profile → build → compile order; saw: {names}"
    )


# ---- 2. compile activity receives the compile_plan from the new payload


def test_build_payload_threads_compile_plan_into_compile_activity(
    monkeypatch,
):
    """The compile activity's `assessment_plan_payload` must be the
    `compile_plan` slice of the InitialExecutionPlan payload. A
    regression here means downstream compile-config mapping uses
    stale data."""
    compile_calls: list[Any] = []
    handler = _make_handler(compile_captured=compile_calls)
    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
        planner_enabled=True, correlation_id="run-test",
    )
    asyncio.run(wf.run(request))
    assert len(compile_calls) == 1
    assessment_payload = compile_calls[0].assessment_plan_payload
    # The legacy compile_plan keys are present (mode, recommended_path).
    assert assessment_payload is not None
    assert assessment_payload.get("mode") in (
        "standard", "deep",
    ), f"unexpected mode: {assessment_payload.get('mode')}"
    assert "recommended_path" in assessment_payload


# ---- 3. domain_profile_id flows into assessment log event ---------


def test_domain_profile_id_surfaces_on_assessment_created_log(monkeypatch):
    captured_logs: list[dict] = []

    class _StubLogger:
        def info(self, _msg, extra=None):
            captured_logs.append(extra or {})

    monkeypatch.setattr(temporal_workflow_mod, "logger", _StubLogger())

    # Build a custom handler that returns a payload with a non-None
    # domain id so we can verify it lands in the log.
    plan = build_initial_execution_plan(_PROFILE)
    payload = plan.to_payload()
    payload["domain_profile_id"] = "civil_engineering"
    custom_result = BuildInitialExecutionPlanResult(
        status="succeeded",
        plan_payload=payload,
        artifact_id="init-1",
        domain_profile_id="civil_engineering",
    )
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_make_handler(build_result=custom_result),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
        planner_enabled=True, correlation_id="run-test",
    )
    asyncio.run(wf.run(request))
    # Find the assessment.created event and check the reason carries
    # the domain id (the log line format: "mode=X confidence=Y domain=Z").
    assessment_events = [
        e for e in captured_logs
        if e.get("event") == "ingestion.assessment.created"
    ]
    assert assessment_events, (
        f"expected ingestion.assessment.created log; got events: "
        f"{[e.get('event') for e in captured_logs]}"
    )
    reasons = " ".join(str(e.get("reason", "")) for e in assessment_events)
    assert "civil_engineering" in reasons


# ---- 4. fallback when build activity returns None -----------------


def test_workflow_falls_back_when_build_activity_returns_none(monkeypatch):
    """Test harnesses (and pre-Wave-3 deployments) that don't wire
    the new activity see it return None. The workflow must continue
    via the in-workflow `DefaultAssessmentPlanner` and finish."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _PROFILE
        if name.endswith("build_initial_execution_plan"):
            return None  # mimic an unregistered activity
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 3, "extracted_text_chars": 200,
                },
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=getattr(payload, "stage_name", "compile"),
                validation_status="passed", passed=True,
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("fast_llm_consult_enrich"):
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c", indexer_kind="i",
        planner_enabled=True, correlation_id="run-test",
    )
    result = asyncio.run(wf.run(request))
    assert result.state == "completed", (
        "workflow must complete even when the build activity returns None"
    )
