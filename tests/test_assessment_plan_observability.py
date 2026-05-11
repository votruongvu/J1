"""Observability hardening for AssessmentPlan integration:

 1. Plan/config warnings persist on `ArtifactProcessingResult.metadata
 ["plan_warnings"]` (visible beyond the log line).
 2. `metadata["unhandled_capabilities"]` lists the required
 capabilities the parser couldn't honour at the per-switch level.
 3. `J1_ASSESSMENT_FAILURE_POLICY=fail_open` (default) lets the
 ingest continue when planner construction itself fails.
 4. `J1_ASSESSMENT_FAILURE_POLICY=fail_closed` blocks ingest with a
 clear error when planner construction fails.
 5. Existing ingest behaviour stays backward-compatible — no plan,
 no plan_warnings key surprises (we always set the key, but
 legacy callers who don't pass a plan see empty values).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

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
    ERROR_TYPE_REQUIRED_STEP_FAILED,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.assessment import (
    ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED,
    ASSESSMENT_FAILURE_POLICY_FAIL_OPEN,
    DEFAULT_ASSESSMENT_FAILURE_POLICY,
    ENV_ASSESSMENT_FAILURE_POLICY,
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    DefaultAssessmentPlanner,
    FallbackPolicy,
    load_assessment_failure_policy,
)
from j1.processing.profiling import DocumentProfile
from j1.processing.status import StepStatus
from j1.providers.raganything.plan_mapper import (
    map_assessment_to_raganything_config,
)
from j1.providers.raganything.settings import RAGAnythingSettings


# ---- (1) plan warnings + (2) unhandled capabilities in metadata ----


def test_compile_result_metadata_surfaces_warnings_for_disabled_capability(
    monkeypatch, tmp_path,
):
    """End-to-end: deployment marks `supports_equation=False`, plan
 requires FORMULA_EXTRACTION, default fallback policy. Expect
 `result.metadata["plan_warnings"]` carries the mapper's
 deployment-disabled-equation string AND
 `unhandled_capabilities` lists `formula_extraction`."""
    import sys
    import types
    from j1.providers.raganything._bridge import default_compile
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.projects.context import ProjectContext

    # Stub RAGAnything + RAGAnythingConfig. The fake config DOES
    # expose the per-capability attributes so the bridge's setattr
    # path succeeds for image/table — only `supports_equation=False`
    # routes formula_extraction into the warning path.
    class _FakeConfig:
        def __init__(self, **_kwargs):
            self.enable_image_processing = True
            self.enable_table_processing = True
            self.enable_equation_processing = True

    class _FakeRAG:
        def __init__(self, **_kwargs): pass

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method, **_extra,
        ):
            outdir = Path(output_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "out.md").write_text("ok", encoding="utf-8")

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)

    # Drop the fast-text-pdf shortcut so the stub `process_document_complete`
    # is actually invoked.
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._is_text_extractable_pdf",
        lambda _path: False,
    )

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "doc-1.pdf").write_bytes(b"%PDF-fake")

    plan = AssessmentPlan(
        document_id="doc-1",
        mode=CompileMode.STANDARD,
        document_type="pdf",
        complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.FORMULA_EXTRACTION,  # disabled in settings below
        }),
    )
    settings = RAGAnythingSettings(
        workdir=str(tmp_path / "rag-workdir"),
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
        supports_equation=False,  # the only capability now in the warning path
    )
    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=None,
        vision_client=None,
        embedding_client=None,
        assessment_plan=plan,
    )

    result = default_compile(request)

    assert result.status.value == "succeeded", (result.error, result.message)
    assert "plan_warnings" in result.metadata
    warnings = result.metadata["plan_warnings"]
    assert any(
        "formula_extraction" in w and "unsupported" in w.lower()
        for w in warnings
    ), warnings
    unhandled = result.metadata["unhandled_capabilities"]
    assert "formula_extraction" in unhandled
    # image_extraction NOT in unhandled — config layer applied it.
    assert result.metadata.get("assessment_mode") == "standard"


def test_compile_result_metadata_has_empty_keys_when_no_plan(
    monkeypatch, tmp_path,
):
    """Backward compat: legacy caller (no assessment_plan) still gets
 `plan_warnings` and `unhandled_capabilities` keys but with empty
 lists. Downstream consumers can rely on the keys existing."""
    import sys
    import types
    from j1.providers.raganything._bridge import default_compile
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    from j1.projects.context import ProjectContext

    class _FakeRAG:
        def __init__(self, **_kwargs): pass

        async def _ensure_lightrag_initialized(self):
            return {"success": True}

        async def process_document_complete(
            self, *, file_path, output_dir, parse_method, **_extra,
        ):
            outdir = Path(output_dir)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "out.md").write_text("ok", encoding="utf-8")

    class _FakeConfig:
        def __init__(self, **_kwargs): pass

    fake_mod = types.ModuleType("raganything")
    fake_mod.RAGAnything = _FakeRAG
    fake_mod.RAGAnythingConfig = _FakeConfig
    monkeypatch.setitem(sys.modules, "raganything", fake_mod)
    monkeypatch.setattr(
        "j1.providers.raganything._bridge._is_text_extractable_pdf",
        lambda _path: False,
    )

    monkeypatch.setenv("J1_DATA_ROOT", str(tmp_path))
    raw_dir = tmp_path / "tenants" / "acme" / "projects" / "alpha" / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "doc-1.pdf").write_bytes(b"%PDF-fake")

    settings = RAGAnythingSettings(
        workdir=str(tmp_path / "rag-workdir"),
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
    )
    request = RAGAnythingCompileRequest(
        ctx=ProjectContext(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        settings=settings,
        text_client=None,
        vision_client=None,
        embedding_client=None,
        # no assessment_plan
    )

    result = default_compile(request)
    assert result.status.value == "succeeded", (result.error, result.message)
    assert result.metadata["plan_warnings"] == []
    assert result.metadata["unhandled_capabilities"] == []
    assert "assessment_mode" not in result.metadata


def test_mapper_unhandled_capabilities_empty_when_deployment_supports_all():
    """Mapper unit: when settings advertise every capability as
 supported, `unhandled_capabilities` is empty. Image / table /
 formula now flow through `RAGAnythingConfig` via
 `to_config_overrides` instead of being treated as drop-throughs."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.LAYOUT_DETECTION,
            Capability.IMAGE_EXTRACTION,
            Capability.TABLE_EXTRACTION,
        }),
    )
    settings = RAGAnythingSettings(
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
    )
    config = map_assessment_to_raganything_config(plan, settings)
    assert config.unhandled_capabilities == ()
    # And the config_overrides slice carries the per-capability
    # toggles for the bridge to apply on RAGAnythingConfig.
    overrides = config.to_config_overrides()
    assert overrides["enable_image_processing"] is True
    assert overrides["enable_table_processing"] is True


def test_mapper_unhandled_capabilities_lists_explicitly_disabled_caps():
    """When the deployment advertises a capability as unsupported
 (`supports_equation=False`), and the plan requires it, the
 mapper records BOTH a warning string AND the capability name
 in `unhandled_capabilities`."""
    plan = AssessmentPlan(
        document_id="d", mode=CompileMode.STANDARD,
        document_type="pdf", complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION,
            Capability.FORMULA_EXTRACTION,
        }),
    )
    settings = RAGAnythingSettings(
        parse_method="auto", backend="vlm-http-client",
        vlm_http_server_url="http://stub:1/v1",
        supports_equation=False,
    )
    config = map_assessment_to_raganything_config(plan, settings)
    assert "formula_extraction" in config.unhandled_capabilities
    assert any("formula_extraction" in w for w in config.warnings)


# ---- (3)+(4) fail_open vs fail_closed policy ----------------------


def test_load_assessment_failure_policy_defaults_to_fail_open():
    assert load_assessment_failure_policy(env={}) == \
        ASSESSMENT_FAILURE_POLICY_FAIL_OPEN
    assert DEFAULT_ASSESSMENT_FAILURE_POLICY == "fail_open"


def test_load_assessment_failure_policy_accepts_fail_closed():
    assert load_assessment_failure_policy(env={
        ENV_ASSESSMENT_FAILURE_POLICY: "fail_closed",
    }) == ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED


def test_load_assessment_failure_policy_unknown_value_falls_back():
    """A typo or future-policy-name in the env quietly downgrades to
 fail_open. The assessment plan exists for cost optimisation,
 not as a correctness gate — a typo shouldn't kill ingest."""
    assert load_assessment_failure_policy(env={
        ENV_ASSESSMENT_FAILURE_POLICY: "fail-closed-typo",
    }) == ASSESSMENT_FAILURE_POLICY_FAIL_OPEN


# ---- Workflow integration: fail_open vs fail_closed ---------------


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


def _broken_profile() -> DocumentProfile:
    """A profile shape that survives Temporal serialisation but trips
 `DefaultAssessmentPlanner` if we monkeypatch the planner to raise.
 Used by the policy tests below."""
    return DocumentProfile(
        document_id="doc-broken",
        extension=".pdf", page_count=1,
        text_extractable_ratio=1.0,
    )


def test_fail_open_lets_workflow_complete_when_assessment_planner_raises(
    monkeypatch,
):
    """Default `fail_open`: planner failure logs + sets
 `assessment_plan_payload=None`. Bridge falls back to
 `settings.parse_method`. Run completes."""
    # Force the planner to raise.
    def _raise(*a, **k):
        raise RuntimeError("planner exploded")
    monkeypatch.setattr(
        "j1.orchestration.workflows.project_processing.DefaultAssessmentPlanner",
        type("_Bad", (), {"__init__": lambda self: None, "assess": _raise}),
    )

    captured: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-broken"]
        if name.endswith("profile_document"):
            return _broken_profile()
        if name.endswith("compile"):
            captured["compile_payload"] = payload
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["a-1"], kinds=("chunk",),
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
        planner_enabled=True,
        correlation_id="run-fail-open",
        assessment_failure_policy=ASSESSMENT_FAILURE_POLICY_FAIL_OPEN,
    )
    result = asyncio.run(wf.run(request))
    assert result.state == "completed"
    # Compile activity was invoked with no plan (fail-open path).
    payload: CompileActivityInput = captured["compile_payload"]
    assert payload.assessment_plan_payload is None


def test_fail_closed_marks_compile_failed_when_assessment_planner_raises(
    monkeypatch,
):
    """`fail_closed`: planner failure → workflow records compile
 FAILED + raises. Operator gets a clear error about the missing
 plan rather than a silent fallback."""
    def _raise(*a, **k):
        raise RuntimeError("planner exploded under fail_closed")
    monkeypatch.setattr(
        "j1.orchestration.workflows.project_processing.DefaultAssessmentPlanner",
        type("_Bad", (), {"__init__": lambda self: None, "assess": _raise}),
    )

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-broken"]
        if name.endswith("profile_document"):
            return _broken_profile()
        if name.endswith("compile"):
            raise AssertionError(
                "compile activity must NOT run under fail_closed when "
                "AssessmentPlan build failed"
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        if name.endswith("persist_validation_report") or name.endswith("persist_final_summary"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r-1"],
                kinds=("validation_report",),
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
        planner_enabled=True,
        correlation_id="run-fail-closed",
        assessment_failure_policy=ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED,
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert excinfo.value.type == ERROR_TYPE_REQUIRED_STEP_FAILED
    # Compile step recorded as FAILED with the assessment-failed reason.
    compile_failures = [
        r for r in wf._step_results
        if r.step == "compile" and r.status == StepStatus.FAILED
    ]
    assert len(compile_failures) == 1
    assert "AssessmentPlan" in (compile_failures[0].reason or "")


# ---- (5) backward compatibility ---------------------------------


def test_request_assessment_failure_policy_defaults_to_fail_open():
    """Existing callers that build `ProjectProcessingRequest` without
 the new field get `fail_open` automatically. No production
 behaviour change for legacy code paths."""
    req = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c",
    )
    assert req.assessment_failure_policy == "fail_open"
