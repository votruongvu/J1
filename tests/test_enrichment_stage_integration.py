""" integration tests — workflow wiring + activity behavior +
require_enrichment_success enforcement.

Pins the contract surface:

1. Workflow dispatches `run_enrichment_stage` activity AFTER
 `_run_post_compile_enrich_assessment` and BEFORE `finalize`.
2. Skipped enrichment (should_enrich=False) produces a typed
 `status="skipped"` overlay record.
3. `require_enrichment_success=True` + `failed` enrichment →
 `_BusinessRejection` with `FAILURE_CODE_ENRICHMENT_REQUIRED`.
4. `require_enrichment_success=False` + `failed` enrichment →
 run continues; final status surfaces warnings.
5. Optional module failure doesn't destroy compile artifacts.
6. Raw compile result preserved.
7. Provenance preserved.
8. Prompt resolver helper precedence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from j1.domains.civil_engineering.pack import build_civil_engineering_pack
from j1.domains.general import build_general_pack
from j1.domains.models import DomainEnrichmentPolicy, DomainPack
from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
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
from j1.processing.enrichment_modules import (
    build_skipped_enrichment_result,
    resolve_module_prompt,
)
from j1.processing.enrichment_overlay import EnrichmentResult
from j1.processing.initial_execution_plan import build_initial_execution_plan
from j1.processing.profiling import DocumentProfile
from j1.processing.status import FinalStatus, StepStatus
from j1.runs.models import FAILURE_CODE_ENRICHMENT_REQUIRED


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


def _build_handler(
    *,
    enrichment_result: RunEnrichmentStageResult | None = None,
):
    """Build a handler that supplies all the activities a planner-
 enabled run touches. `enrichment_result` lets each test inject
 the specific outcome it wants from `run_enrichment_stage`."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _PROFILE
        if name.endswith("build_initial_execution_plan"):
            plan = build_initial_execution_plan(_PROFILE)
            from j1.orchestration.activities.payloads import (
                BuildInitialExecutionPlanResult,
            )
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="initial-plan-1",
                domain_profile_id=plan.domain_profile_id,
            )
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["compile-1", "chunk-1"],
                kinds=("parsed_content_manifest", "chunk"),
                compile_metrics={
                    "chunks_count": 1,
                    "extracted_text_chars": 15_000,
                },
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=getattr(payload, "stage_name", "compile"),
                validation_status="passed",
                passed=True,
            )
        if name.endswith("run_enrichment_stage"):
            if enrichment_result is not None:
                return enrichment_result
            return RunEnrichmentStageResult(
                status="succeeded",
                plan_payload={"document_id": "doc-1", "status": "succeeded"},
                artifact_id="enrichment-art-1",
                require_enrichment_success=False,
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        if name.endswith("fast_llm_consult_enrich"):
            return ArtifactActivityResult(status="succeeded", artifact_ids=[])
        if (
            name.endswith("persist_validation_report")
            or name.endswith("persist_final_summary")
            or name.endswith("persist_error_report")
            or name.endswith("persist_compile_strategy_report")
            or name.endswith("persist_post_compile_enrich_plan")
            or name.endswith("persist_initial_execution_plan")
            or name.endswith("persist_compile_result_summary")
            or name.endswith("persist_enrichment_result")
        ):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r-1"],
                kinds=("validation_report",),
            )
        return None
    return handler


def _request(**overrides) -> ProjectProcessingRequest:
    base = dict(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
        correlation_id="run-test",
    )
    base.update(overrides)
    return ProjectProcessingRequest(**base)


# ---- 1. Workflow dispatches the stage after the assessor ---------


def test_workflow_dispatches_run_enrichment_stage_after_assessment(monkeypatch):
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_build_handler(),
    )
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))
    names = [c["name"] for c in captured["calls"]]
    assessment_idx = next(
        i for i, n in enumerate(names)
        if "persist_post_compile_enrich_plan" in n
    )
    enrich_stage_idx = next(
        i for i, n in enumerate(names)
        if n.endswith("run_enrichment_stage")
    )
    assert assessment_idx < enrich_stage_idx, (
        f"enrichment stage must run after post-compile assessment; "
        f"saw order: {names}"
    )


def test_workflow_records_enrich_stage_step_on_success(monkeypatch):
    _patch_workflow_runtime(
        monkeypatch, exec_handler=_build_handler(),
    )
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    enrich_steps = [r for r in result.step_results if r.step == "enrich_stage"]
    assert enrich_steps
    assert enrich_steps[0].status == StepStatus.COMPLETED
    assert enrich_steps[0].required is False


# ---- 2. Skipped enrichment is explicit ---------------------------


def test_workflow_records_enrich_stage_skipped_when_activity_reports_skipped(
    monkeypatch,
):
    """The activity decides to skip (e.g. post-compile says SKIP).
 The workflow surfaces `enrich_stage` as SKIPPED + the run
 completes cleanly without warnings."""
    skipped = build_skipped_enrichment_result(
        document_id="doc-1",
        reason="compile failed; nothing to enrich",
    )
    result = _build_handler(enrichment_result=RunEnrichmentStageResult(
        status="skipped",
        plan_payload=skipped.to_payload(),
        artifact_id="enrichment-art-skipped",
        require_enrichment_success=False,
    ))
    _patch_workflow_runtime(monkeypatch, exec_handler=result)
    wf = ProjectProcessingWorkflow()
    outcome = asyncio.run(wf.run(_request()))
    enrich_steps = [r for r in outcome.step_results if r.step == "enrich_stage"]
    assert enrich_steps
    assert enrich_steps[0].status == StepStatus.SKIPPED


def test_skipped_enrichment_does_not_lift_final_status_to_warnings(
    monkeypatch,
):
    """Skipped enrichment is not a warning condition — the run
 should land at COMPLETED, not PARTIAL_COMPLETED."""
    skipped = build_skipped_enrichment_result(
        document_id="doc-1", reason="domain policy=never",
    )
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_build_handler(enrichment_result=RunEnrichmentStageResult(
            status="skipped",
            plan_payload=skipped.to_payload(),
            artifact_id="art-1",
            require_enrichment_success=False,
        )),
    )
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    assert result.final_status == FinalStatus.COMPLETED


# ---- 3. require_enrichment_success=True + failed → fail run ------


def test_required_enrichment_failure_fails_run_with_enrichment_required_code(
    monkeypatch,
):
    """The workflow raises when enrichment fails and the policy
 requires success. The failure code is recorded on the
 enrich_stage step's metadata (read via `get_status`) — the
 outer ApplicationError's message wraps the rejection's text,
 but the step metadata carries the structured code."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_build_handler(enrichment_result=RunEnrichmentStageResult(
            status="failed",
            plan_payload={"document_id": "doc-1", "status": "failed"},
            artifact_id="art-1",
            require_enrichment_success=True,
        )),
    )
    wf = ProjectProcessingWorkflow()
    with pytest.raises(Exception):
        asyncio.run(wf.run(_request()))
    status = wf.get_status()
    enrich_steps = [
        r for r in status.step_results if r.step == "enrich_stage"
    ]
    assert enrich_steps
    failed_required = [
        r for r in enrich_steps
        if r.status == StepStatus.FAILED and r.required
    ]
    assert failed_required
    assert failed_required[0].metadata.get("failure_code") == (
        FAILURE_CODE_ENRICHMENT_REQUIRED
    )


def test_required_enrichment_failure_records_failed_required_step(
    monkeypatch,
):
    """The enrich_stage step record carries the failure-code so the
 final-summary artifact + audit log surface it."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_build_handler(enrichment_result=RunEnrichmentStageResult(
            status="failed",
            plan_payload={"document_id": "doc-1", "status": "failed"},
            artifact_id="art-1",
            require_enrichment_success=True,
        )),
    )
    wf = ProjectProcessingWorkflow()
    with pytest.raises(Exception):
        asyncio.run(wf.run(_request()))
    # `wf` retains step_results even after the raise (get_status
    # query returns them).
    status = wf.get_status()
    enrich_steps = [
        r for r in status.step_results if r.step == "enrich_stage"
    ]
    assert enrich_steps
    assert enrich_steps[0].status == StepStatus.FAILED
    assert enrich_steps[0].required is True
    assert enrich_steps[0].metadata.get("failure_code") == FAILURE_CODE_ENRICHMENT_REQUIRED


# ---- 4. require_enrichment_success=False + failed → warnings ------


def test_optional_enrichment_failure_keeps_run_completed(monkeypatch):
    """Optional enrichment failure → run continues. Compile output
 + index complete; final status surfaces warnings."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_build_handler(enrichment_result=RunEnrichmentStageResult(
            status="failed",
            plan_payload={"document_id": "doc-1", "status": "failed"},
            artifact_id="art-1",
            require_enrichment_success=False,
        )),
    )
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    # Run completed (not failed); warnings present.
    assert result.final_status == FinalStatus.PARTIAL_COMPLETED
    # Compile + index steps must remain COMPLETED — raw output
    # preserved.
    compile_steps = [r for r in result.step_results if r.step == "compile"]
    index_steps = [r for r in result.step_results if r.step == "index"]
    assert compile_steps[0].status == StepStatus.COMPLETED
    assert index_steps[0].status == StepStatus.COMPLETED


def test_optional_enrichment_warnings_lift_final_status(monkeypatch):
    """succeeded_with_warnings from enrichment lifts the run to
 PARTIAL_COMPLETED (which the FE renders as SUCCEEDED_WITH_WARNINGS)."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_build_handler(enrichment_result=RunEnrichmentStageResult(
            status="succeeded_with_warnings",
            plan_payload={"document_id": "doc-1", "status": "succeeded_with_warnings"},
            artifact_id="art-1",
            require_enrichment_success=False,
        )),
    )
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    assert result.final_status == FinalStatus.PARTIAL_COMPLETED


# ---- 5. Activity-level failure handling --------------------------


def test_activity_raise_records_optional_failed_step(monkeypatch):
    """If `run_enrichment_stage` activity raises (worker crash,
 sandbox issue, etc.) AND the in-memory enrich plan doesn't
 require success, the workflow records the failure as an
 optional FAILED step but completes the run with warnings."""

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("run_enrichment_stage"):
            raise RuntimeError("worker crashed")
        return _build_handler()(method, payload, kwargs)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    # Optional failure → run completes with warnings (lifts to
    # PARTIAL_COMPLETED via `_warning_count`).
    assert result.final_status == FinalStatus.PARTIAL_COMPLETED
    enrich_steps = [
        r for r in result.step_results if r.step == "enrich_stage"
    ]
    assert enrich_steps
    assert enrich_steps[0].status == StepStatus.FAILED
    assert enrich_steps[0].required is False
    assert "raised" in enrich_steps[0].reason


# ---- 6. Compile result not mutated by enrichment ----------------


def test_compile_artifacts_preserved_when_enrichment_runs(monkeypatch):
    """The enrich_stage runs AFTER compile and validate_stage. The
 compile step record + compile artifacts must remain unchanged
 regardless of enrichment outcome."""
    _patch_workflow_runtime(
        monkeypatch, exec_handler=_build_handler(),
    )
    wf = ProjectProcessingWorkflow()
    result = asyncio.run(wf.run(_request()))
    compile_steps = [r for r in result.step_results if r.step == "compile"]
    assert compile_steps
    assert compile_steps[0].status == StepStatus.COMPLETED
    assert compile_steps[0].artifact_count >= 1
    # And compile precedes enrichment in the step-results order
    step_names = [r.step for r in result.step_results]
    assert step_names.index("compile") < step_names.index("enrich_stage")


# ---- 7. Prompt resolver precedence -------------------------------


def test_prompt_resolver_uses_pack_override_when_present():
    pack = build_civil_engineering_pack()
    # Civil pack overrides `table_enrichment_prompt`.
    prompt = resolve_module_prompt(
        domain_pack=pack,
        prompt_field="table_enrichment_prompt",
        builtin_default="DEFAULT",
    )
    # Pack override won — civil-specific text wins over the default.
    assert "DEFAULT" not in prompt
    assert "BOQ" in prompt or "civil" in prompt.lower()


def test_prompt_resolver_falls_back_to_builtin_default():
    pack = build_civil_engineering_pack()
    # Civil pack does NOT override `text_enrichment_prompt`.
    prompt = resolve_module_prompt(
        domain_pack=pack,
        prompt_field="text_enrichment_prompt",
        builtin_default="DEFAULT TEXT PROMPT",
    )
    assert "DEFAULT TEXT PROMPT" in prompt


def test_prompt_resolver_prepends_pack_addon_when_present():
    pack = build_civil_engineering_pack()
    prompt = resolve_module_prompt(
        domain_pack=pack,
        prompt_field="text_enrichment_prompt",
        builtin_default="BUILTIN",
    )
    # Addon precedes the default.
    addon_idx = prompt.find("Civil Engineering")
    default_idx = prompt.find("BUILTIN")
    assert 0 <= addon_idx < default_idx, prompt


def test_prompt_resolver_returns_default_only_without_pack():
    prompt = resolve_module_prompt(
        domain_pack=None,
        prompt_field="text_enrichment_prompt",
        builtin_default="ONLY DEFAULT",
    )
    assert prompt == "ONLY DEFAULT"


def test_prompt_resolver_falls_through_for_unknown_field():
    pack = build_civil_engineering_pack()
    prompt = resolve_module_prompt(
        domain_pack=pack,
        prompt_field="nonexistent_prompt",
        builtin_default="FALLBACK",
    )
    assert "FALLBACK" in prompt


# ---- 8. Legacy regression checks --------------------------------


def test_workflow_does_not_call_legacy_planner(monkeypatch):
    """Regression: the workflow must NOT dispatch any of the
 deleted legacy planner activities."""
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_build_handler(),
    )
    wf = ProjectProcessingWorkflow()
    asyncio.run(wf.run(_request()))
    names = " ".join(c["name"] for c in captured["calls"])
    forbidden = (
        "build_planning_result",
        "planning_llm",
        "_build_plan",
        "_apply_post_compile_planning",
    )
    for f in forbidden:
        assert f not in names, (
            f"legacy reference reappeared in workflow dispatch: {f}"
        )


def test_enrichment_overlay_does_not_carry_split_mode_vocabulary():
    """The serialized EnrichmentResult must not carry any
 split-mode vocabulary on the wire."""
    from j1.processing.enrichment_modules import (
        build_skipped_enrichment_result,
    )
    skipped = build_skipped_enrichment_result(
        document_id="doc-1", reason="test",
    )
    payload = skipped.to_payload()
    assert "split_mode" not in repr(payload)
    assert "insert_content" not in repr(payload)
