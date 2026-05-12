"""Unit tests for the post-compile verification gate.

Covers the pure helper (`verify_compile_output_health`) at the
contract boundary, the workflow signal handler
(`SIGNAL_TRIGGER_COMPILE` / `trigger_compile`), and the new
request-shape fields (`two_phase_compile`, `verify_after_compile`).
The full workflow integration (gate parking + verification dispatch)
is exercised end-to-end in `test_project_processing_workflow.py`
once the Temporal harness picks up the new payloads — these tests
keep the unit boundary tight so a regression on reason-code labels
or the signal-handler attribute is caught immediately.
"""

from __future__ import annotations

from j1.orchestration.activities.payloads import (
    ProjectScope,
    VerifyCompileActivityResult,
    VerifyCompileInput,
)
from j1.orchestration.workflows.project_processing import (
    SIGNAL_TRIGGER_COMPILE,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    WorkflowState,
)
from j1.processing.compile_verification import verify_compile_output_health
from j1.runs.models import (
    FAILURE_CODE_CHUNK_FAILED,
    FAILURE_CODE_INDEX_FAILED,
    FAILURE_CODE_VERIFICATION_FAILED,
    RunStatus,
)


# ---- verify_compile_output_health (pure helper) -----------------


def test_health_passes_when_chunk_count_meets_minimum():
    passed, reason, msg, n = verify_compile_output_health(
        artifact_kinds=("chunk", "chunk", "chunk"),
        min_chunks=2,
    )
    assert passed
    assert reason is None
    assert n == 3


def test_health_rejects_zero_chunks_with_chunk_failed():
    passed, reason, _msg, n = verify_compile_output_health(
        artifact_kinds=("parsed_source", "parsed_content_manifest"),
        min_chunks=1,
    )
    assert not passed
    assert reason == FAILURE_CODE_CHUNK_FAILED
    assert n == 0


def test_health_rejects_too_few_chunks_with_chunk_failed():
    passed, reason, _msg, n = verify_compile_output_health(
        artifact_kinds=("chunk",),
        min_chunks=3,
    )
    assert not passed
    assert reason == FAILURE_CODE_CHUNK_FAILED
    assert n == 1


def test_health_rejects_missing_index_manifest_when_required():
    passed, reason, _msg, _n = verify_compile_output_health(
        artifact_kinds=("chunk", "chunk"),
        min_chunks=1,
        require_index_manifest=True,
    )
    assert not passed
    assert reason == FAILURE_CODE_INDEX_FAILED


def test_health_passes_with_index_manifest_when_required():
    passed, reason, _msg, _n = verify_compile_output_health(
        artifact_kinds=("chunk", "index_manifest"),
        min_chunks=1,
        require_index_manifest=True,
    )
    assert passed
    assert reason is None


def test_health_min_chunks_zero_disables_chunk_check():
    """An empty-document compile may legitimately produce zero
 chunks. `min_chunks=0` is the opt-out — verification should pass."""
    passed, reason, _msg, n = verify_compile_output_health(
        artifact_kinds=("parsed_source",),
        min_chunks=0,
    )
    assert passed
    assert reason is None
    assert n == 0


# ---- Failure code vocabulary ------------------------------------


def test_failure_codes_are_stable_string_constants():
    """The reason codes are part of the wire/audit contract — the FE
 and ops-grade audit consumers branch on these exact strings. Pin
 them so a rename here is intentional and traceable."""
    assert FAILURE_CODE_CHUNK_FAILED == "CHUNK_FAILED"
    assert FAILURE_CODE_INDEX_FAILED == "INDEX_FAILED"
    assert FAILURE_CODE_VERIFICATION_FAILED == "VERIFICATION_FAILED"


# ---- Run status + workflow state ---------------------------------


def test_run_status_includes_two_phase_states():
    assert RunStatus.COMPILE_PENDING.value == "compile_pending"
    assert RunStatus.VERIFYING.value == "verifying"


def test_workflow_state_includes_compile_trigger_and_verifying():
    assert (
        WorkflowState.WAITING_FOR_COMPILE_TRIGGER.value
        == "waiting_for_compile_trigger"
    )
    assert WorkflowState.VERIFYING.value == "verifying"


# ---- Signal handler ---------------------------------------------


def test_trigger_compile_signal_constant_matches_handler_name():
    """Temporal infers the signal name from the Python identifier of
 the `@workflow.signal`-decorated method. The constant the REST
 handler / dev wiring uses MUST match the method name verbatim;
 a drift here silently sends the signal to nowhere."""
    assert SIGNAL_TRIGGER_COMPILE == "trigger_compile"
    assert hasattr(ProjectProcessingWorkflow, "trigger_compile")


def test_trigger_compile_signal_flips_internal_flag():
    """The signal sets `_compile_triggered=True` so the gate's
 `wait_condition` releases. Idempotent — calling twice keeps it
 True (the gate consumes-and-resets on entry)."""
    wf = ProjectProcessingWorkflow()
    assert wf._compile_triggered is False
    wf.trigger_compile()
    assert wf._compile_triggered is True
    wf.trigger_compile()
    assert wf._compile_triggered is True


# ---- Request shape ----------------------------------------------


def test_two_phase_compile_and_verify_after_compile_default_off():
    """Backward-compat: legacy callers that don't opt in MUST see
 the pre- behaviour — no gate, no verification. Both
 flags default to False."""
    request = ProjectProcessingRequest(
        scope=ProjectScope(tenant_id="t", project_id="p"),
        compiler_kind="raganything",
    )
    assert request.two_phase_compile is False
    assert request.verify_after_compile is False


def test_request_accepts_two_phase_compile_and_verify_after_compile():
    request = ProjectProcessingRequest(
        scope=ProjectScope(tenant_id="t", project_id="p"),
        compiler_kind="raganything",
        two_phase_compile=True,
        verify_after_compile=True,
    )
    assert request.two_phase_compile is True
    assert request.verify_after_compile is True


# ---- VerifyCompileActivityResult shape --------------------------


def test_verify_compile_result_default_shape_indicates_pass():
    """A `passed=True` result must not require an explicit
 reason_code so callers can construct successful results
 without specifying every optional field."""
    r = VerifyCompileActivityResult(passed=True)
    assert r.passed is True
    assert r.reason_code is None
    assert r.errors == []


def test_verify_compile_input_defaults_to_min_chunks_one():
    """Default policy: every non-empty compile must produce at
 least one chunk. Callers opt out by passing `min_chunks=0`."""
    inp = VerifyCompileInput(
        scope=ProjectScope(tenant_id="t", project_id="p"),
        run_id="run-1",
        document_id="doc-1",
    )
    assert inp.min_chunks == 1
    assert inp.require_index_manifest is False
