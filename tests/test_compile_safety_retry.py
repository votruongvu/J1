"""Compile-safety retry: quality evaluator + workflow orchestration.

Three layers:

 1. **`evaluate_compile_quality`** (pure, no I/O) — gold-bucket
 decisions for retry vs. no-retry vs. failed.
 2. **Settings env loader** — `CompileRetrySettings` reads env
 correctly + degrades on bad values.
 3. **Workflow integration** — the retry loop dispatches the
 compile activity, escalates the AssessmentPlan's mode on
 low-quality results, persists the per-attempt audit trail,
 and respects `max_compile_attempts`.

The workflow tests use the same activity-monkeypatch pattern as
`test_project_processing_workflow.py`. Each test asserts on the
sequence of activity invocations + the final
`compile_strategy_report` payload the workflow assembled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from temporalio import workflow

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    CompileActivityInput,
    PersistCompileStrategyReportInput,
    ProjectScope,
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
from j1.processing.compile_quality import (
    QUALITY_FAILED,
    QUALITY_GOOD,
    QUALITY_LOW,
    RETRY_REASON_LOW_TEXT,
    RETRY_REASON_OCR_LIKELY_NEEDED,
    RETRY_REASON_RECOVERABLE_FAILURE,
    RETRY_REASON_ZERO_CHUNKS,
    evaluate_compile_quality,
)
from j1.processing.compile_retry import (
    CompileRetrySettings,
    DEFAULT_MAX_ATTEMPTS,
    next_compile_mode,
    load_compile_retry_settings,
)
from j1.processing.profiling import DocumentProfile
from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ArtifactProcessingResult,
    ResultStatus,
)


# ---- evaluate_compile_quality (unit) ------------------------------


def _result(
    *, status: ResultStatus = ResultStatus.SUCCEEDED,
    drafts: list = (), metadata: dict | None = None,
    error: str | None = None, message: str | None = None,
) -> ArtifactProcessingResult:
    return ArtifactProcessingResult(
        status=status, drafts=list(drafts), error=error, message=message,
        metadata=dict(metadata or {}),
    )


def test_quality_good_when_chunks_and_text_present():
    """Happy path: chunk drafts + chars above threshold → GOOD,
 no retry."""
    from j1.processing.results import ArtifactDraft
    drafts = [
        ArtifactDraft(kind=ARTIFACT_KIND_CHUNK, content=b"x", suggested_extension=".json")
        for _ in range(3)
    ]
    verdict = evaluate_compile_quality(
        _result(drafts=drafts, metadata={"total_text_chars": 5000}),
        min_text_chars=200, min_chunks=1,
    )
    assert verdict.quality == QUALITY_GOOD
    assert not verdict.should_retry()


def test_quality_zero_chunks_triggers_retry():
    verdict = evaluate_compile_quality(
        _result(drafts=[], metadata={"total_text_chars": 5000}),
        min_text_chars=200, min_chunks=1,
    )
    assert verdict.quality == QUALITY_LOW
    assert verdict.retry_reason == RETRY_REASON_ZERO_CHUNKS
    assert verdict.signals["chunks_count"] == 0


def test_quality_low_text_triggers_retry():
    """Chunks present but extracted_text_chars below threshold —
 `low_text_chars` retry-reason."""
    from j1.processing.results import ArtifactDraft
    drafts = [ArtifactDraft(kind=ARTIFACT_KIND_CHUNK, content=b"x", suggested_extension=".json")]
    verdict = evaluate_compile_quality(
        _result(drafts=drafts, metadata={"total_text_chars": 50}),
        min_text_chars=200, min_chunks=1,
    )
    assert verdict.quality == QUALITY_LOW
    assert verdict.retry_reason == RETRY_REASON_LOW_TEXT


def test_quality_low_text_with_plan_required_ocr_uses_ocr_reason():
    """Same low-text condition but the plan required OCR + the
 parse_method didn't fire OCR → distinct `ocr_likely_needed`
 reason so the retry layer escalates to a mode that DOES enable
 OCR."""
    from j1.processing.results import ArtifactDraft
    drafts = [ArtifactDraft(kind=ARTIFACT_KIND_CHUNK, content=b"x", suggested_extension=".json")]
    verdict = evaluate_compile_quality(
        _result(drafts=drafts, metadata={"total_text_chars": 50}),
        min_text_chars=200, min_chunks=1,
        plan_required_ocr=True, parse_method_used="auto",
    )
    assert verdict.retry_reason == RETRY_REASON_OCR_LIKELY_NEEDED


def test_quality_failure_with_recoverable_pattern_triggers_retry():
    verdict = evaluate_compile_quality(
        _result(
            status=ResultStatus.FAILED,
            error="Parsing failed: No content was extracted from PDF",
        ),
    )
    assert verdict.quality == QUALITY_FAILED
    assert verdict.retry_reason == RETRY_REASON_RECOVERABLE_FAILURE


def test_quality_failure_with_unrecoverable_pattern_does_not_retry():
    """Hard failures (file-not-found, license errors, vendor crashes
 that don't match the recoverable patterns) don't retry — burning
 another expensive compile attempt won't change the outcome."""
    verdict = evaluate_compile_quality(
        _result(
            status=ResultStatus.FAILED,
            error="FileNotFoundError: source file is gone",
        ),
    )
    assert verdict.quality == QUALITY_FAILED
    assert verdict.retry_reason is None


def test_quality_unknown_text_chars_skips_text_rule():
    """When `total_text_chars` is missing from metadata, the
 chars-below-threshold rule must NOT fire defensively.
 Unknown ≠ 'pretend it's 0'."""
    from j1.processing.results import ArtifactDraft
    drafts = [ArtifactDraft(kind=ARTIFACT_KIND_CHUNK, content=b"x", suggested_extension=".json")]
    verdict = evaluate_compile_quality(
        _result(drafts=drafts, metadata={}),
        min_text_chars=200, min_chunks=1,
    )
    assert verdict.quality == QUALITY_GOOD
    assert verdict.signals["extracted_text_chars"] is None


# ---- Mode escalation ladder --------------------------------------


def test_next_compile_mode_ladder():
    assert next_compile_mode(CompileMode.FAST) == CompileMode.STANDARD
    assert next_compile_mode(CompileMode.STANDARD) == CompileMode.DEEP
    assert next_compile_mode(CompileMode.DEEP) is None


# ---- Settings env loader -----------------------------------------


def test_load_retry_settings_defaults():
    s = load_compile_retry_settings(env={})
    assert s.enabled is True
    assert s.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert s.min_text_chars == 200
    assert s.min_chunks == 1


def test_load_retry_settings_disable_via_env():
    s = load_compile_retry_settings(env={
        "J1_COMPILE_RETRY_ENABLED": "false",
        "J1_COMPILE_MAX_ATTEMPTS": "1",
    })
    assert s.enabled is False
    assert s.max_attempts == 1


def test_load_retry_settings_clamps_to_minimum():
    """`max_attempts=0` is nonsensical (would never run). Clamp to 1."""
    s = load_compile_retry_settings(env={"J1_COMPILE_MAX_ATTEMPTS": "0"})
    assert s.max_attempts == 1


def test_load_retry_settings_garbage_value_falls_back_to_default():
    s = load_compile_retry_settings(env={
        "J1_COMPILE_MAX_ATTEMPTS": "very-many",
    })
    assert s.max_attempts == DEFAULT_MAX_ATTEMPTS


# ---- Workflow integration: retry orchestration -------------------


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


def _scanned_profile() -> DocumentProfile:
    """Profile that the planner routes to DEEP mode."""
    return DocumentProfile(
        document_id="doc-scan",
        extension=".pdf",
        text_extractable_ratio=0.0,
        has_scanned_pages=True,
        page_count=3,
    )


def _readable_profile() -> DocumentProfile:
    return DocumentProfile(
        document_id="doc-read",
        extension=".pdf",
        text_extractable_ratio=0.95,
        has_scanned_pages=False,
        page_count=3,
    )


def _plain_text_profile() -> DocumentProfile:
    """Plain-text profile → FAST mode."""
    return DocumentProfile(
        document_id="doc-fast",
        extension=".txt",
        text_extractable_ratio=1.0,
        page_count=1,
    )


def test_standard_compile_with_zero_chunks_retries_to_deep(monkeypatch):
    """Two-mode model: planner emits STANDARD for plain-text
 profiles (the bridge takes the plaintext bypass at runtime).
 First compile attempt returns ZERO chunks → quality LOW with
 `zero_chunks` retry reason → workflow escalates to DEEP and
 re-dispatches. Second attempt returns chunks; final quality
 GOOD."""
    compile_payloads: list[CompileActivityInput] = []
    strategy_report_payload: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-standard"]
        if name.endswith("profile_document"):
            return _plain_text_profile()
        if name.endswith("build_initial_execution_plan"):
            from j1.processing.initial_execution_plan import (
                build_initial_execution_plan as _build,
            )
            from j1.orchestration.activities.payloads import (
                BuildInitialExecutionPlanResult,
            )
            plan = _build(_plain_text_profile())
            return BuildInitialExecutionPlanResult(
                status="succeeded",
                plan_payload=plan.to_payload(),
                artifact_id="initial-plan-art",
            )
        if name.endswith("persist_initial_execution_plan"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["ip-1"],
                kinds=("initial_execution_plan",),
            )
        if name.endswith("compile"):
            compile_payloads.append(payload)
            mode = (payload.assessment_plan_payload or {}).get("mode")
            if mode == "standard":
                # First attempt: zero chunks, low text.
                return ArtifactActivityResult(
                    status="succeeded", artifact_ids=[],
                    kinds=(),
                    compile_metrics={"chunks_count": 0, "extracted_text_chars": 0},
                )
            # Deep attempt: succeeds with chunks + text.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c-1"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 1, "extracted_text_chars": 5000,
                },
            )
        if name.endswith("persist_compile_strategy_report"):
            strategy_report_payload.update(payload.payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr-1"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
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
        correlation_id="run-standard-retry",
        compile_max_attempts=2,
    )
    result = asyncio.run(wf.run(request))

    assert result.state == "completed"
    assert len(compile_payloads) == 2, (
        "expected 1 retry → 2 compile attempts"
    )
    # First attempt was standard; second was deep (two-mode ladder).
    first_mode = (compile_payloads[0].assessment_plan_payload or {}).get("mode")
    second_mode = (compile_payloads[1].assessment_plan_payload or {}).get("mode")
    assert first_mode == "standard"
    assert second_mode == "deep"
    assert "fast" not in {first_mode, second_mode}, (
        "FAST must not appear in either attempt's mode under the "
        "two-mode model"
    )

    # Strategy report carries the full audit trail. New fields:
    # `selected_compile_mode` (canonical) + back-compat aliases.
    assert strategy_report_payload["selected_compile_mode"] == "deep"
    assert strategy_report_payload["initial_compile_mode"] == "standard"
    assert strategy_report_payload["final_compile_mode"] == "deep"
    # Back-compat aliases still populated for older FE consumers.
    assert strategy_report_payload["initial_mode"] == "standard"
    assert strategy_report_payload["final_mode"] == "deep"
    assert strategy_report_payload["retry_used"] is True
    assert strategy_report_payload["attempts_count"] == 2
    attempts = strategy_report_payload["attempts"]
    assert attempts[0]["mode"] == "standard"
    assert attempts[0]["retry_reason"] == "zero_chunks"
    assert attempts[0]["status"] == "retried"
    assert attempts[1]["mode"] == "deep"
    assert attempts[1]["status"] == "succeeded"
    assert strategy_report_payload["final_compile_quality"] == "good"
    # Escalation reason is now surfaced explicitly.
    assert strategy_report_payload["escalation_reason"]
    assert "standard" in strategy_report_payload["escalation_reason"]
    assert "deep" in strategy_report_payload["escalation_reason"]
    assert "zero_chunks" in strategy_report_payload["escalation_reason"]


def test_standard_compile_with_low_text_and_ocr_required_retries_to_deep(
    monkeypatch,
):
    """Standard compile returns chunks but very few chars; the
 planner had OCR in `required_capabilities` (because the profile
 looked scanned-ish). Retry must escalate to DEEP and force OCR
 on the retry payload."""
    compile_payloads: list[CompileActivityInput] = []
    strategy_payload: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-mixed"]
        if name.endswith("profile_document"):
            # Mixed profile — not strict scanned, but planner
            # still bias toward standard with possible OCR need.
            return DocumentProfile(
                document_id="doc-mixed",
                extension=".pdf",
                text_extractable_ratio=0.3,
                has_scanned_pages=None,
                page_count=5,
            )
        if name.endswith("compile"):
            compile_payloads.append(payload)
            ap = payload.assessment_plan_payload or {}
            mode = ap.get("mode")
            if mode == "standard":
                # Inject OCR requirement on the standard plan so the
                # evaluator's `ocr_likely_needed` branch fires.
                if "ocr" not in (ap.get("required_capabilities") or ()):
                    # The default planner doesn't require OCR for this
                    # profile → simulate a deployment that does. Test
                    # patches the payload's required_capabilities
                    # below by mutating the plan.
                    pass
                # Returns barely any text → low_text_chars; combined
                # with OCR-required → ocr_likely_needed.
                return ArtifactActivityResult(
                    status="succeeded", artifact_ids=["c-1"],
                    kinds=("chunk",),
                    compile_metrics={
                        "chunks_count": 1, "extracted_text_chars": 30,
                    },
                )
            # Deep retry: succeeds with substantial text.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c-2"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 4, "extracted_text_chars": 12000,
                },
            )
        if name.endswith("persist_compile_strategy_report"):
            strategy_payload.update(payload.payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r"],
                kinds=("validation_report",),
            )
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        return None

    # Force the AssessmentPlanner to mark OCR as required on this
    # profile. We patch only `assess` to keep everything else honest.
    real_planner_assess = DefaultAssessmentPlanner.assess

    def _assess_with_ocr(self, profile, *, document_type=None):
        plan = real_planner_assess(self, profile, document_type=document_type)
        # Force OCR to be required so the retry layer's
        # ocr_likely_needed reason path fires.
        return AssessmentPlan(
            document_id=plan.document_id,
            mode=CompileMode.STANDARD,
            document_type=plan.document_type,
            complexity=plan.complexity,
            confidence=plan.confidence,
            required_capabilities=plan.required_capabilities | {Capability.OCR},
            optional_capabilities=plan.optional_capabilities,
            risk_flags=plan.risk_flags,
            fallback_policy=plan.fallback_policy,
            reason=plan.reason,
        )
    monkeypatch.setattr(DefaultAssessmentPlanner, "assess", _assess_with_ocr)

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        planner_enabled=True,
        correlation_id="run-std-deep",
        compile_max_attempts=3,
    )
    result = asyncio.run(wf.run(request))

    assert result.state == "completed"
    assert len(compile_payloads) == 2
    second_payload = compile_payloads[1].assessment_plan_payload or {}
    assert second_payload.get("mode") == "deep"
    # OCR augmentation: the retry layer adds OCR to required_capabilities
    # specifically for ocr_likely_needed reason. Verify it stuck.
    assert "ocr" in (second_payload.get("required_capabilities") or ())

    assert strategy_payload["final_mode"] == "deep"
    assert strategy_payload["retry_used"] is True
    attempts = strategy_payload["attempts"]
    assert attempts[0]["retry_reason"] == "ocr_likely_needed"


def test_deep_failure_does_not_retry_endlessly(monkeypatch):
    """Plan starts at DEEP (scanned profile). Compile returns LOW
 quality. Retry layer evaluates `next_compile_mode(deep)=None` →
 no escalation → workflow stops at one attempt + records the
 final low quality with explanation."""
    compile_payloads: list[CompileActivityInput] = []
    strategy_payload: dict = {}

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-scan"]
        if name.endswith("profile_document"):
            return _scanned_profile()
        if name.endswith("compile"):
            compile_payloads.append(payload)
            # Return a stub artifact so the workflow's final
            # `_validate_completion` check (which requires at least
            # one produced artifact) passes — but with chunks_count=0
            # so the retry-evaluator still classifies as LOW quality.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["compile-out"],
                kinds=("chunk",),
                compile_metrics={
                    "chunks_count": 0, "extracted_text_chars": 0,
                },
            )
        if name.endswith("persist_compile_strategy_report"):
            strategy_payload.update(payload.payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r"],
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
        correlation_id="run-deep-low",
        compile_max_attempts=3,
    )
    result = asyncio.run(wf.run(request))

    # Even with max_compile_attempts=3, deep can't escalate further.
    assert result.state == "completed"
    assert len(compile_payloads) == 1
    assert strategy_payload["initial_mode"] == "deep"
    assert strategy_payload["final_mode"] == "deep"
    assert strategy_payload["retry_used"] is False
    assert strategy_payload["final_compile_quality"] == "low"
    assert strategy_payload["final_retry_reason"] == "zero_chunks"


def test_max_attempts_respected(monkeypatch):
    """`max_compile_attempts=1` disables retry — even if the first
 attempt is low-quality, no second attempt fires."""
    compile_payloads: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _plain_text_profile()
        if name.endswith("compile"):
            compile_payloads.append(payload)
            # Stub artifact id — same rationale as
            # `test_deep_failure_does_not_retry_endlessly`: keeps
            # `_validate_completion`'s at-least-one-artifact rule
            # happy while the retry-evaluator still sees 0 chunks.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["compile-out"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 0, "extracted_text_chars": 0},
            )
        if name.endswith("persist_compile_strategy_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r"],
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
        correlation_id="run-no-retry",
        compile_max_attempts=1,
    )
    asyncio.run(wf.run(request))
    assert len(compile_payloads) == 1


def test_retry_disabled_skips_evaluator_entirely(monkeypatch):
    """`compile_retry_enabled=False` — even with zero chunks, no
 second attempt fires. Operators that want to gate retry off
 in production keep the legacy single-shot behaviour."""
    compile_payloads: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _plain_text_profile()
        if name.endswith("compile"):
            compile_payloads.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 0, "extracted_text_chars": 0},
            )
        if name.endswith("persist_compile_strategy_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r"],
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
        correlation_id="run-no-retry-flag",
        compile_retry_enabled=False,
        compile_max_attempts=3,  # ignored when enabled=False
    )
    asyncio.run(wf.run(request))
    assert len(compile_payloads) == 1


def test_retry_does_not_double_write_chunks(monkeypatch):
    """Each attempt's mode is part of the activity-side cache key
 (`_compile_cache_key_parts` includes mode). Escalating from
 fast→standard creates a NEW cache row + NEW compile invocation,
 but the prior attempt's artifacts are NOT re-emitted by the
 workflow — only the latest attempt's `compile_result.artifact_ids`
 feed `_produced_artifact_ids`. We verify by counting the
 artifact_ids the workflow extends after retry."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            return _plain_text_profile()
        if name.endswith("compile"):
            mode = (payload.assessment_plan_payload or {}).get("mode")
            if mode == "fast":
                # First attempt: produces a chunk artifact that
                # SHOULD NOT be carried forward (low-quality, retry).
                return ArtifactActivityResult(
                    status="succeeded", artifact_ids=["fast-chunk-A"],
                    kinds=("chunk",),
                    compile_metrics={"chunks_count": 1, "extracted_text_chars": 0},
                )
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["standard-chunk-B"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 1, "extracted_text_chars": 5000},
            )
        if name.endswith("persist_compile_strategy_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["sr"],
                kinds=("compile_strategy_report",),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("persist_final_summary") or name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["r"],
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
        correlation_id="run-no-double-write",
        compile_max_attempts=2,
    )
    result = asyncio.run(wf.run(request))
    assert result.state == "completed"
    # ONLY the final-attempt's chunk landed on _produced_artifact_ids.
    # The fast-attempt's chunk-A was discarded (not carried forward).
    assert "fast-chunk-A" not in wf._produced_artifact_ids
    assert "standard-chunk-B" in wf._produced_artifact_ids


def test_legacy_path_no_assessment_plan_skips_retry_loop(monkeypatch):
    """`planner_enabled=False` (legacy bulk-job path): no
 AssessmentPlan is built → retry loop runs once with
 `assessment_plan_payload=None` and the bridge falls back to
 settings.parse_method. Backward compat guard."""
    compile_payloads: list = []

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            compile_payloads.append(payload)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["c"],
                kinds=("chunk",),
                compile_metrics={"chunks_count": 0, "extracted_text_chars": 0},
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        return None

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(), compiler_kind="c",
        planner_enabled=False,
    )
    asyncio.run(wf.run(request))
    assert len(compile_payloads) == 1
    assert compile_payloads[0].assessment_plan_payload is None
