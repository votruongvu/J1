"""End-to-end wiring tests for AssessmentPlan through the workflow,
the compile activity, the ProcessingService, and the RAGAnything
compiler/bridge.

Covers the user-spec scenarios for the wiring step:
  1. Workflow builds AssessmentPlan before compile.
  2. RAGAnythingCompileRequest receives assessment_plan.
  3. Existing fallback path still works when assessment_plan is missing.
  4. Fast/standard/deep plans reach the adapter mapper (i.e. mapper
     produces the right parse_method).
  5. Unsupported capabilities surface as warnings (default policy).
  6. Settings flags are loaded from env (supports_image/table/equation,
     allowed_parse_methods).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    CompileActivityInput,
    ProcessingActivityResult,
    ProjectScope,
    StageValidationActivityResult,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    WorkflowState,
)
from j1.processing.assessment import (
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    DefaultAssessmentPlanner,
    FallbackPolicy,
)
from j1.processing.profiling import DocumentProfile
from j1.providers.raganything.plan_mapper import (
    map_assessment_to_raganything_config,
)
from j1.providers.raganything.settings import (
    RAGAnythingSettings,
    load_raganything_settings,
)


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _activity_name(method) -> str:
    defn = getattr(method, "__temporal_activity_definition", None)
    return defn.name if defn else method.__name__


def _patch_workflow_runtime(monkeypatch, *, exec_handler: Callable):
    async def _exec(method, payload=None, **kwargs):
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)


def _profile(extension: str, **overrides) -> DocumentProfile:
    base = dict(
        document_id="doc-1", extension=extension, mime_type=None,
        file_size_bytes=10_000, page_count=10,
    )
    base.update(overrides)
    return DocumentProfile(**base)


# ---- (1)+(2): workflow builds AssessmentPlan + activity sees it ----


def test_workflow_threads_assessment_plan_payload_into_compile_activity(
    monkeypatch,
):
    """End-to-end: workflow profiles → builds AssessmentPlan →
    serialises to dict → compile activity receives it. Asserts on
    the `CompileActivityInput.assessment_plan_payload` field at the
    activity boundary."""
    captured: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            # Scanned PDF → planner picks DEEP.
            return _profile(
                ".pdf", text_extractable_ratio=0.0, has_scanned_pages=True,
            )
        if name.endswith("compile"):
            captured["compile_payload"] = payload
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"], kinds=("chunk",),
            )
        if name.endswith("validate_stage"):
            return StageValidationActivityResult(
                stage_name=payload.stage_name,
                validation_status="passed", passed=True,
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_validation_report") or name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r-1"], kinds=("validation_report",),
            )
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        # planner_enabled=True is the trigger for assessment build.
        planner_enabled=True,
        correlation_id="run-1",
    )
    asyncio.run(wf.run(request))

    assert "compile_payload" in captured, (
        "compile activity must be invoked when profiling succeeds"
    )
    payload: CompileActivityInput = captured["compile_payload"]
    assert payload.assessment_plan_payload is not None, (
        "workflow must build AssessmentPlan and thread its payload "
        "into CompileActivityInput when planner_enabled=True"
    )
    plan_dict = payload.assessment_plan_payload
    # Scanned profile → DEEP mode + OCR required.
    assert plan_dict["mode"] == "deep"
    assert "ocr" in plan_dict["required_capabilities"]
    # Round-trip check: serialised payload reconstructs to a real plan.
    rebuilt = AssessmentPlan.from_payload(plan_dict)
    assert rebuilt.mode == CompileMode.DEEP
    assert rebuilt.requires(Capability.OCR)


def test_workflow_skips_assessment_when_planner_disabled(monkeypatch):
    """Legacy bulk-job path: planner_enabled=False → no profile, no
    AssessmentPlan, payload field stays None. The bridge falls back
    to settings.parse_method — backward-compatible."""
    captured: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            captured["compile_payload"] = payload
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"], kinds=("chunk",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        planner_enabled=False,  # legacy path
    )
    asyncio.run(wf.run(request))

    assert captured["compile_payload"].assessment_plan_payload is None, (
        "legacy path must not synthesise an AssessmentPlan; "
        "bridge falls back to settings.parse_method"
    )


# ---- (3): compiler signature accepts assessment_plan kwarg ---------


def test_raganything_compiler_compile_signature_accepts_assessment_plan():
    """The compiler's `compile` signature must include
    `assessment_plan` so `ProcessingService.compile`'s introspection
    threads it through. Locks the integration contract."""
    from j1.providers.raganything.compiler import RAGAnythingCompiler
    sig = inspect.signature(RAGAnythingCompiler.compile)
    assert "assessment_plan" in sig.parameters


def test_processing_service_compile_threads_assessment_plan_to_compiler(
    artifact_registry, audit_recorder, cost_recorder, ctx, workspace,
):
    """When `assessment_plan` is supplied, ProcessingService.compile
    forwards it to compilers that accept the kwarg. Mock compilers
    that don't accept it (legacy interface) stay working — the
    introspection guard skips the kwarg silently."""
    from j1.documents.models import DocumentRecord
    from j1.jobs.status import ProcessingStatus
    from j1.processing.results import ArtifactProcessingResult, ResultStatus
    from j1.processing.service import ProcessingService

    svc = ProcessingService(
        workspace=workspace, artifact_registry=artifact_registry,
        audit=audit_recorder, cost=cost_recorder,
    )
    captured: dict = {}

    class _PlanAwareCompiler:
        kind = "plan_aware"

        def compile(self, ctx, document_id, *, assessment_plan=None):
            captured["plan_received"] = assessment_plan
            return ArtifactProcessingResult(status=ResultStatus.SUCCEEDED, drafts=[])

    class _LegacyCompiler:
        kind = "legacy"

        def compile(self, ctx, document_id):
            captured["legacy_called"] = True
            return ArtifactProcessingResult(status=ResultStatus.SUCCEEDED, drafts=[])

    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    doc = DocumentRecord(
        document_id="d", project=ctx,
        original_filename="d.pdf", stored_filename="d.pdf",
        mime_type="application/pdf", file_size=10, checksum="h",
        status=ProcessingStatus.PENDING,
        created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    svc.compile(ctx, _PlanAwareCompiler(), doc, assessment_plan=plan)
    assert captured["plan_received"] is plan

    captured.clear()
    svc.compile(ctx, _LegacyCompiler(), doc, assessment_plan=plan)
    # Legacy compiler doesn't crash; the introspection guard ate the kwarg.
    assert captured.get("legacy_called") is True


# ---- (4): fast/standard/deep plans reach the mapper correctly -----


@pytest.mark.parametrize(
    "mode,expected_parse_method",
    [
        # Legacy FAST → txt is preserved as a read-path safety net
        # for legacy plans replayed from history. The planner never
        # emits FAST any more (see two-mode model in assessment.py).
        (CompileMode.FAST, "txt"),
        (CompileMode.STANDARD, "auto"),
        (CompileMode.DEEP, "auto"),  # no OCR required
    ],
)
def test_each_mode_resolves_to_expected_parse_method(mode, expected_parse_method):
    plan = AssessmentPlan(
        document_id="d", mode=mode,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({Capability.TEXT_EXTRACTION}),
    )
    settings = RAGAnythingSettings(
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
    )
    config = map_assessment_to_raganything_config(plan, settings)
    assert config.parse_method == expected_parse_method


# ---- (5): unsupported capability → warning (default policy) -------


def test_unsupported_capability_in_settings_records_warning():
    """When `supports_image=False` is loaded from env (real settings
    field, not a monkey patch), and the plan requires
    IMAGE_EXTRACTION, the mapper records a warning under the default
    `degrade_with_warning` policy."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.IMAGE_EXTRACTION,
        }),
    )
    settings = RAGAnythingSettings(
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
        supports_image=False,
    )
    config = map_assessment_to_raganything_config(plan, settings)
    assert any(
        "image" in w.lower() and "unsupported" in w.lower()
        for w in config.warnings
    ), config.warnings


# ---- (6): settings flags loaded from env --------------------------


def test_supports_flags_load_from_env_defaults_to_true():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
    })
    assert s.supports_image is True
    assert s.supports_table is True
    assert s.supports_equation is True


def test_supports_flags_can_be_disabled_via_env():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
        "J1_RAGANYTHING_SUPPORTS_IMAGE": "false",
        "J1_RAGANYTHING_SUPPORTS_TABLE": "0",
        "J1_RAGANYTHING_SUPPORTS_EQUATION": "no",
    })
    assert s.supports_image is False
    assert s.supports_table is False
    assert s.supports_equation is False


def test_allowed_parse_methods_from_env_constrains_mapper():
    """Operator restricts the deployment to {auto, txt} via env;
    the mapper degrades a plan-requested 'ocr' to the deployment
    default 'auto' with a warning."""
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
        "J1_RAGANYTHING_ALLOWED_PARSE_METHODS": "auto,txt",
    })
    assert s.allowed_parse_methods == ("auto", "txt")
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.DEEP,
        document_type="pdf", complexity=Complexity.HIGH,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.OCR,
        }),
    )
    config = map_assessment_to_raganything_config(plan, s)
    # OCR not in allow-list → falls back to env default ("auto").
    assert config.parse_method == "auto"
    assert any("allow-list" in w or "falling back" in w for w in config.warnings)


def test_allowed_parse_methods_default_is_unrestricted():
    s = load_raganything_settings(env={
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL": "http://x:1/v1",
    })
    assert s.allowed_parse_methods == ()


# ---- Bonus: round-trip and fallback ------------------------------


def test_assessment_plan_payload_round_trip_is_lossless():
    """Workflow → activity passes the plan as a dict via Temporal's
    data converter. Round-trip via `to_payload` / `from_payload`
    must preserve every operationally-meaningful field."""
    profile = DocumentProfile(
        document_id="doc-1", extension=".pdf",
        text_extractable_ratio=0.0, has_scanned_pages=True,
        has_images=True,
    )
    plan = DefaultAssessmentPlanner().assess(profile)
    rebuilt = AssessmentPlan.from_payload(plan.to_payload())
    assert rebuilt.document_id == plan.document_id
    assert rebuilt.mode == plan.mode
    assert rebuilt.complexity == plan.complexity
    assert rebuilt.required_capabilities == plan.required_capabilities
    assert rebuilt.optional_capabilities == plan.optional_capabilities
    assert rebuilt.fallback_policy == plan.fallback_policy
    assert rebuilt.risk_flags == plan.risk_flags
