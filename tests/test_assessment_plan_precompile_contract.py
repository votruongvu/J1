"""Pre-compile contract regression — Assessment Plan ↔ Compile boundary.

Pin three invariants:

1. The `initial_execution_plan` artifact MUST be built (and persisted)
   before any `compile` activity is invoked. The AssessmentPlan lives
   inside that artifact under `compile_plan`; if compile ran first,
   the FE would have nothing to render until compile completed — the
   bug operators flagged.

2. The InitialExecutionPlan payload MUST NOT carry post-compile-only
   fields (chunks_count, detected_images, detected_tables, extracted
   text counts, graph artifact ids, etc.). Those belong on
   `compile_strategy_report` and friends.

3. The AssessmentPlan handed to the compile activity MUST be the
   plan built by `build_initial_execution_plan` — round-tripping
   through to_payload + from_payload preserves every relevant field.

These tests cover ONLY the boundary. They don't exercise the rest of
the workflow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from temporalio import workflow

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    BuildInitialExecutionPlanResult,
    ProjectScope,
    StageValidationActivityResult,
    ValidateContextResult,
    VerifyCompileActivityResult,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.assessment import AssessmentPlan, DefaultAssessmentPlanner
from j1.processing.profiling import DocumentProfile


# ---- Test harness (mirrors test_workflow_failure_semantics) -------


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(
    monkeypatch,
    *,
    exec_handler: Callable[[object, object, dict], object] | None = None,
):
    async def _exec(method, payload=None, **kwargs):
        if exec_handler is None:
            return None
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)


def _make_plan_payload() -> dict:
    """A realistic InitialExecutionPlan payload with the AssessmentPlan
 attached. Mirrors what build_initial_execution_plan returns —
 lets the workflow follow the non-fallback branch."""
    plan = DefaultAssessmentPlanner().assess(DocumentProfile(
        document_id="doc-1",
        extension="pdf",
        file_size_bytes=10_000,
    ))
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "run_compile": True,
        "compile_engine": "raganything",
        "domain_profile_id": "general",
        "enrichment_policy": "auto",
        "candidate_enrichment_modules": [],
        "cheap_signals": {},
        "resource_hints": {},
        "reasons": [],
        "warnings": [],
        "compile_plan": plan.to_payload(),
    }


# ---- 1. Order-of-calls invariant ----------------------------------


def test_initial_execution_plan_persisted_before_compile_invocation(
    monkeypatch,
):
    """`build_initial_execution_plan` (which persists the artifact
 inside the activity) MUST resolve before any `compile` activity
 is invoked. Operators rely on this so the Assessment Plan panel
 can render the moment the workflow starts — not after compile."""
    captured: list[str] = []
    plan_payload = _make_plan_payload()

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        captured.append(name)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("build_initial_execution_plan"):
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan_payload,
                artifact_id="art-initial-1",
                domain_profile_id="general",
            )
        if name == "j1.processing.compile":
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-compile-1"],
            )
        if name.endswith("verify_compile_output"):
            return VerifyCompileActivityResult(
                passed=True, chunk_count=1, artifact_count=1,
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="passed",
                passed=True,
                check_count=1,
            )
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        correlation_id="run-precompile-order",
        planner_enabled=True,  # gates the pre-compile plan-build path
    )
    asyncio.run(wf.run(request))

    # Find the call indices.
    build_indices = [
        i for i, n in enumerate(captured)
        if n.endswith("build_initial_execution_plan")
    ]
    compile_indices = [
        i for i, n in enumerate(captured)
        if n == "j1.processing.compile"
    ]
    assert build_indices, (
        "build_initial_execution_plan was never invoked — pre-compile "
        "plan artifact would be missing"
    )
    assert compile_indices, "compile activity was never invoked"
    # Every compile call must come AFTER the first plan-build call.
    assert compile_indices[0] > build_indices[0], (
        f"compile (idx {compile_indices[0]}) ran before "
        f"build_initial_execution_plan (idx {build_indices[0]}) — "
        "the Assessment Plan would only be visible post-compile"
    )


# ---- 2. Payload-shape invariant -----------------------------------


_POST_COMPILE_ONLY_KEYS = frozenset({
    # Compile-result projection
    "chunks_count", "extracted_text_chars", "page_count",
    "detected_tables", "detected_images", "retry_attempts",
    "final_quality", "raw_artifact_refs",
    # Graph / index
    "graph_artifact_ids", "index_status",
    # Final report
    "final_summary", "final_ingestion_report",
    # Compile strategy report
    "compile_strategy_report", "attempts", "attempts_count",
    "final_compile_quality", "final_mode", "final_warnings",
    "extraction_evidence", "unhandled_capabilities",
})


def test_initial_execution_plan_payload_carries_no_postcompile_fields():
    """The pre-compile artifact must be a clean pre-compile contract.
 Detecting any post-compile field means somebody started leaking
 compile observations into the planning surface — exactly the
 bug this audit pinned."""
    plan = _make_plan_payload()
    # Top-level
    leaks = sorted(set(plan.keys()) & _POST_COMPILE_ONLY_KEYS)
    assert leaks == [], (
        f"InitialExecutionPlan payload leaks post-compile keys at top "
        f"level: {leaks}"
    )
    # compile_plan (the AssessmentPlan sub-payload)
    compile_plan = plan.get("compile_plan") or {}
    sub_leaks = sorted(set(compile_plan.keys()) & _POST_COMPILE_ONLY_KEYS)
    assert sub_leaks == [], (
        f"AssessmentPlan sub-payload leaks post-compile keys: {sub_leaks}"
    )


def test_assessment_plan_to_payload_carries_no_postcompile_fields():
    """Same check at the AssessmentPlan level — direct, not through
 the InitialExecutionPlan wrapper. Pin the boundary even when
 someone reaches for `AssessmentPlan.to_payload()` directly."""
    plan = DefaultAssessmentPlanner().assess(DocumentProfile(
        document_id="doc-x",
        extension="pdf",
        file_size_bytes=10_000,
    ))
    payload = plan.to_payload()
    leaks = sorted(set(payload.keys()) & _POST_COMPILE_ONLY_KEYS)
    assert leaks == [], (
        f"AssessmentPlan.to_payload leaks post-compile keys: {leaks}"
    )


# ---- 3. Plan passed into compile unchanged ------------------------


def test_assessor_tolerates_missing_extension():
    """`DocumentProfile.extension` is typed `str` but a `None` can
 sneak through when the dataclass round-trips through Temporal's
 JSON data converter and a producer omitted the field. The planner
 must produce a valid AssessmentPlan in that case — not crash with
 `AttributeError: 'NoneType' object has no attribute 'lstrip'`,
 which is the failure mode that hid behind the
 `j1.processing.build_initial_execution_plan` NotFoundError before
 the activity-registration fix landed."""
    profile = DocumentProfile(
        document_id="doc-noext",
        extension=None,  # type: ignore[arg-type]
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.document_type == "unknown"
    assert plan.mode is not None  # planner still produced a verdict


def test_assessor_tolerates_empty_extension():
    """Counter-test for the path above — empty extension is the
 normal case the deterministic profiler emits for unrecognised
 files. Both empty and None should map to the same fallback."""
    profile = DocumentProfile(
        document_id="doc-emptyext",
        extension="",
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    assert plan.document_type == "unknown"
    assert plan.mode is not None


def test_assessment_plan_round_trips_through_payload_unchanged():
    """The workflow serialises the AssessmentPlan to a payload and the
 compile activity reconstructs it via `from_payload`. The round-
 trip must preserve every field — otherwise the plan compile sees
 differs silently from the plan persisted to disk."""
    original = DefaultAssessmentPlanner().assess(DocumentProfile(
        document_id="doc-rt",
        extension="pdf",
        file_size_bytes=50_000,
        page_count=12,
        has_images=True,
    ))
    payload = original.to_payload()
    reconstructed = AssessmentPlan.from_payload(payload)
    assert reconstructed.mode == original.mode
    assert reconstructed.confidence == original.confidence
    assert reconstructed.document_type == original.document_type
    assert reconstructed.complexity == original.complexity
    assert reconstructed.fallback_policy == original.fallback_policy
    assert reconstructed.reason == original.reason
    assert (
        tuple(reconstructed.required_capabilities)
        == tuple(original.required_capabilities)
    )
    assert (
        tuple(reconstructed.optional_capabilities)
        == tuple(original.optional_capabilities)
    )
    assert tuple(reconstructed.risk_flags) == tuple(original.risk_flags)


def test_workflow_threads_built_assessment_plan_into_compile(monkeypatch):
    """The compile activity receives the AssessmentPlan that
 `build_initial_execution_plan` produced — not a fresh one built
 inline, not the empty default. Pins that the workflow doesn't
 silently regenerate the plan between the persistence step and the
 compile call."""
    built = _make_plan_payload()
    captured_compile_payloads: list[object] = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("build_initial_execution_plan"):
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=built,
                artifact_id="art-initial-1",
                domain_profile_id="general",
            )
        if name == "j1.processing.compile":
            captured_compile_payloads.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-compile-1"],
            )
        if name.endswith("verify_compile_output"):
            return VerifyCompileActivityResult(
                passed=True, chunk_count=1, artifact_count=1,
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="passed",
                passed=True,
                check_count=1,
            )
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        correlation_id="run-thread-plan",
        planner_enabled=True,
    )
    asyncio.run(wf.run(request))

    assert captured_compile_payloads, "compile activity never invoked"
    compile_input = captured_compile_payloads[0]
    threaded = getattr(compile_input, "assessment_plan_payload", None)
    assert threaded is not None, (
        "compile activity received no assessment_plan_payload — the "
        "plan built pre-compile was dropped"
    )
    # The threaded payload IS the compile_plan sub-payload (not the
    # outer InitialExecutionPlan envelope).
    assert threaded == built["compile_plan"], (
        "compile activity received an AssessmentPlan that differs from "
        "the one built by build_initial_execution_plan"
    )
