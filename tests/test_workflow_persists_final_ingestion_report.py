"""Wave 10 — workflow integration tests.

Pins:
  1. Success terminal dispatches `persist_final_ingestion_report`.
  2. Failure terminal (compile failure) dispatches the activity too —
     best-effort report persistence is the contract.
  3. The activity input carries the framework final status,
     failure code, document id, and run timing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    BuildInitialExecutionPlanResult,
    PersistFinalIngestionReportInput,
    ProcessingActivityResult,
    ProjectScope,
    RunEnrichmentStageResult,
    StageValidationActivityResult,
    ValidateContextResult,
)
from j1.orchestration.workflows import project_processing as workflow_mod
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.initial_execution_plan import build_initial_execution_plan
from j1.processing.profiling import DocumentProfile


_PROFILE = DocumentProfile(
    document_id="doc-1",
    extension=".pdf",
    page_count=10,
    total_text_chars=15_000,
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
            "name": _activity_name(method),
            "payload": payload,
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


def _success_handler():
    """Handler that returns success for every activity. Mirrors the
    Wave 8 / 9A workflow test fixture shape."""

    def handler(method, payload, _kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _PROFILE
        if name.endswith("build_initial_execution_plan"):
            plan = build_initial_execution_plan(_PROFILE)
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="art-init-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["compile-1"],
                kinds=("parsed_content_manifest",),
                compile_metrics={
                    "chunks_count": 1,
                    "extracted_text_chars": 15000,
                },
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=getattr(payload, "stage_name", "compile"),
                validation_status="passed",
                passed=True,
            )
        if name.endswith("run_enrichment_stage"):
            return RunEnrichmentStageResult(
                status="succeeded",
                plan_payload={"document_id": "doc-1", "status": "succeeded"},
                artifact_id="art-enr-1",
                require_enrichment_success=False,
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("fast_llm_consult_enrich"):
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        if "persist_" in name:
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["a-1"], kinds=("art",),
            )
        return None
    return handler


def _compile_failure_handler():
    def handler(method, payload, _kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _PROFILE
        if name.endswith("build_initial_execution_plan"):
            plan = build_initial_execution_plan(_PROFILE)
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="art-init-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            # Trigger the failure path — compile returns
            # non-succeeded which lands a _BusinessRejection in the
            # workflow.
            return ArtifactActivityResult(
                status="failed",
                error="compile activity returned failed",
            )
        if "persist_" in name:
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["a-1"], kinds=("art",),
            )
        return None
    return handler


def _request(**overrides) -> ProjectProcessingRequest:
    base: dict[str, Any] = dict(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        correlation_id="run-test",
    )
    base.update(overrides)
    return ProjectProcessingRequest(**base)


# ---- 1. Success terminal -------------------------------------------


def test_workflow_dispatches_persist_final_ingestion_report_on_success(
    monkeypatch,
):
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_success_handler(),
    )
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))
    names = [c["name"] for c in captured["calls"]]
    assert any(n.endswith("persist_final_ingestion_report") for n in names), (
        f"workflow must dispatch persist_final_ingestion_report; "
        f"saw: {names}"
    )


def test_persist_final_ingestion_report_input_carries_terminal_state(
    monkeypatch,
):
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_success_handler(),
    )
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))
    payload = next(
        c["payload"] for c in captured["calls"]
        if c["name"].endswith("persist_final_ingestion_report")
    )
    assert isinstance(payload, PersistFinalIngestionReportInput)
    assert payload.run_id == "run-test"
    assert payload.framework_final_status in ("completed", "partial_completed")
    assert payload.failure_code is None
    assert payload.completed_at is not None


# ---- 2. Failure terminal -------------------------------------------


def test_workflow_dispatches_persist_final_ingestion_report_on_compile_failure(
    monkeypatch,
):
    """Compile failure is the canonical test for the best-effort
    failure-path report write — the workflow MUST still attempt the
    report persist even when the run is failing."""
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_compile_failure_handler(),
    )
    wf = ProjectProcessingWorkflow()
    from temporalio.exceptions import ApplicationError
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(_request()))
    names = [c["name"] for c in captured["calls"]]
    assert any(n.endswith("persist_final_ingestion_report") for n in names), (
        f"failure path must still dispatch the report persist; saw: {names}"
    )


def test_persist_final_ingestion_report_failure_input_carries_failure_state(
    monkeypatch,
):
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_compile_failure_handler(),
    )
    wf = ProjectProcessingWorkflow()
    from temporalio.exceptions import ApplicationError
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(_request()))
    payload = next(
        c["payload"] for c in captured["calls"]
        if c["name"].endswith("persist_final_ingestion_report")
    )
    assert isinstance(payload, PersistFinalIngestionReportInput)
    assert payload.framework_final_status == "failed"
    assert payload.failure_code is not None
    assert payload.failure_message is not None
