"""Phase A.4 regression tests: workflow / activity visibility surface.

Covers:
  * `workflow.logger` structured events at lifecycle transitions.
  * `workflow.upsert_search_attributes` calls at lifecycle transitions.
  * Activity heartbeats on long-running stages.

The Temporal SDK functions are monkeypatched so the tests can assert
on call arguments without spinning up a worker.
"""

import asyncio
from collections.abc import Callable

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProcessingActivityResult,
    ProjectScope,
    ValidateContextResult,
)
from j1.orchestration.activities.processing import _safe_heartbeat
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    HEARTBEAT_TIMEOUT,
)


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(
    monkeypatch,
    *,
    exec_handler: Callable[[object, object, dict], object] | None = None,
    wait_handler: Callable[[Callable[[], bool], dict], "asyncio.Future"] | None = None,
):
    captured = {"activity_calls": []}

    async def _exec(method, payload=None, **kwargs):
        captured["activity_calls"].append({
            "name": _activity_name(method),
            "kwargs": kwargs,
        })
        if exec_handler is None:
            return None
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        if wait_handler is not None:
            await wait_handler(predicate, kwargs)
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)
    return captured


def _capture_search_attributes(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    def _upsert(updates):
        # The typed API passes a list of update objects; serialize them
        # to a comparable form for assertions.
        for u in updates:
            captured.append({
                "key": getattr(u, "key", None) and getattr(u.key, "name", None),
                "value": getattr(u, "value", None),
            })

    monkeypatch.setattr(workflow, "upsert_search_attributes", _upsert)
    return captured


# ---- Heartbeats on long-running activities ----------------------------


def test_compile_activity_invocation_uses_heartbeat_timeout(monkeypatch):
    """The workflow must declare `heartbeat_timeout` on compile so a
    silent stall surfaces as a heartbeat-timeout retry, not a hang
    that consumes the full 10-minute start-to-close budget."""
    captured = _patch_workflow_runtime(
        monkeypatch,
        exec_handler=lambda m, p, k: (
            ValidateContextResult(valid=True)
            if _activity_name(m).endswith("validate_context")
            else (
                ["doc-1"]
                if _activity_name(m).endswith("list_pending_documents")
                else (
                    ArtifactActivityResult(status="succeeded", artifact_ids=["art-1"])
                    if _activity_name(m).endswith("compile")
                    else None
                )
            )
        ),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    compile_calls = [
        c for c in captured["activity_calls"] if c["name"].endswith("compile")
    ]
    assert compile_calls, "compile activity must be invoked"
    assert compile_calls[0]["kwargs"]["heartbeat_timeout"] == HEARTBEAT_TIMEOUT


def test_safe_heartbeat_silently_no_ops_outside_worker():
    """Outside a Temporal worker `activity.heartbeat` raises. Our
    helper must swallow that — heartbeats are observability, not
    correctness, and we don't want unit tests to need a Temporal
    runtime just to call activity methods directly."""
    # Should not raise even though there is no activity context.
    _safe_heartbeat({"stage": "compile"})


# ---- Search attributes -----------------------------------------------


def test_workflow_sets_search_attribute_on_completion(monkeypatch):
    """The workflow must announce its lifecycle stage via
    `upsert_search_attributes` so operators can filter active /
    failed / completed workflows in the Temporal UI."""
    captured_sa = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=lambda m, p, k: (
            ValidateContextResult(valid=True)
            if _activity_name(m).endswith("validate_context")
            else (
                ["doc-1"]
                if _activity_name(m).endswith("list_pending_documents")
                else (
                    ArtifactActivityResult(status="succeeded", artifact_ids=["art-1"])
                    if _activity_name(m).endswith("compile")
                    else None
                )
            )
        ),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    asyncio.run(wf.run(request))

    keys_set = {sa["key"] for sa in captured_sa if sa["key"]}
    assert "J1IngestStage" in keys_set, (
        f"workflow must set J1IngestStage; saw {keys_set}"
    )


def test_workflow_sets_search_attribute_on_failure(monkeypatch):
    """Failure must update the search attribute too — a workflow
    that fails silently (no stage update) defeats the visibility
    contract Phase A.4 introduces."""
    captured_sa = _capture_search_attributes(monkeypatch)
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=lambda m, p, k: (
            ValidateContextResult(valid=False, message="bad scope")
            if _activity_name(m).endswith("validate_context")
            else None
        ),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    with pytest.raises(ApplicationError):
        asyncio.run(wf.run(request))

    failure_signals = [
        sa for sa in captured_sa
        if sa["key"] == "J1IngestStage" and "fail" in (sa.get("value") or "").lower()
    ]
    assert failure_signals, (
        f"failure path must update J1IngestStage to a failed value; saw {captured_sa}"
    )


def test_search_attribute_upsert_failure_does_not_block_workflow(monkeypatch):
    """If the namespace doesn't have the search attributes registered,
    the upsert raises. The workflow MUST tolerate this — visibility
    is best-effort."""
    def _bad_upsert(_updates):
        raise RuntimeError("search attribute 'J1IngestStage' is not registered")

    monkeypatch.setattr(workflow, "upsert_search_attributes", _bad_upsert)
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=lambda m, p, k: (
            ValidateContextResult(valid=True)
            if _activity_name(m).endswith("validate_context")
            else (
                ["doc-1"]
                if _activity_name(m).endswith("list_pending_documents")
                else (
                    ArtifactActivityResult(status="succeeded", artifact_ids=["art-1"])
                    if _activity_name(m).endswith("compile")
                    else None
                )
            )
        ),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(scope=_scope(), compiler_kind="c")
    # Should NOT raise — workflow continues even though every search-
    # attribute upsert fails.
    result = asyncio.run(wf.run(request))
    assert result.state == "completed"


# ---- Structured-log fields ------------------------------------------


def test_log_step_uses_safe_fields_only(monkeypatch):
    """Structured logs must never carry document content, file paths,
    prompts, or LLM responses. Validate by inspecting the `extra`
    dict passed to the logger."""
    captured_logs: list[dict] = []

    class _StubLogger:
        def info(self, _msg, extra=None):
            captured_logs.append(extra or {})

    monkeypatch.setattr(workflow, "logger", _StubLogger())
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=lambda m, p, k: (
            ValidateContextResult(valid=True)
            if _activity_name(m).endswith("validate_context")
            else (
                ["doc-1"]
                if _activity_name(m).endswith("list_pending_documents")
                else (
                    ArtifactActivityResult(status="succeeded", artifact_ids=["art-1"])
                    if _activity_name(m).endswith("compile")
                    else None
                )
            )
        ),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        correlation_id="corr-123",
    )
    asyncio.run(wf.run(request))

    assert captured_logs, "expected structured log entries"
    # Every entry carries the safe operational context.
    for entry in captured_logs:
        assert "tenant_id" in entry
        assert "project_id" in entry
        assert "compiler_kind" in entry
        # Forbidden fields must never appear:
        for forbidden in ("document_content", "file_path", "raw_text", "prompt"):
            assert forbidden not in entry, (
                f"forbidden field {forbidden!r} leaked into structured logs"
            )
    # The lifecycle events were emitted.
    events = {e["event"] for e in captured_logs if "event" in e}
    assert "ingestion.workflow.started" in events
    assert "ingestion.workflow.completed" in events
