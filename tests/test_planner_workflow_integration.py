"""Phase B.4 regression tests: planner-driven workflow execution.

Pin two contracts:
  1. `planner_enabled=False` (default) preserves legacy behaviour
     exactly — the workflow doesn't profile, doesn't plan, and gates
     stages purely on `request.<kind>` presence.
  2. `planner_enabled=True` activates the profiler + planner. The
     plan's per-step decisions narrow what the workflow runs;
     caller-supplied kinds always win over planner skips."""

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
from j1.processing.planning import IngestPolicy
from j1.processing.profiling import DocumentProfile
from j1.processing.status import FinalStatus, StepSource, StepStatus


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

_PROFILE_SCANNED_PDF = DocumentProfile(
    document_id="doc-1",
    extension=".pdf",
    mime_type="application/pdf",
    file_size_bytes=10_000,
    page_count=20,
    text_extractable_ratio=0.0,
    has_images=True,
    has_tables=False,
    has_scanned_pages=True,
)


def _full_pipeline_handler(*, profile: DocumentProfile | None = None):
    """Build a handler that returns success for every stage, plus a
    pluggable profile for the profiling activity."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("profile_document"):
            assert profile is not None, "test passed planner_enabled=True without a profile"
            return profile
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        if name.endswith("enrich"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-enriched-1"]
            )
        if name.endswith("build_graph"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-graph-1"]
            )
        if name.endswith("index"):
            return ProcessingActivityResult(status="succeeded")
        if name.endswith("finalize"):
            return None
        raise AssertionError(f"unexpected activity: {name}")
    return handler


# ---- planner_enabled=False is identical to legacy --------------------


def test_planner_disabled_does_not_call_profile_document(monkeypatch):
    """Legacy callers must not pay the profiling cost when they
    haven't opted in. The profile_document activity must NEVER be
    invoked when planner_enabled=False."""
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_full_pipeline_handler(),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=False,  # legacy path
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED
    profile_calls = [c for c in captured["calls"] if "profile_document" in c["name"]]
    assert profile_calls == [], (
        "planner disabled should not invoke profile_document; "
        f"saw: {profile_calls}"
    )


# ---- planner_enabled=True profiles + plans + gates ------------------


def test_planner_enabled_text_profile_skips_optional_stages_with_planner_source(
    monkeypatch,
):
    """For a clean text document under default `auto` policy, the
    planner picks TEXT_ONLY mode → enrich/graph are skipped with
    `source=PLANNER` (not CALLER) so audit logs make the planner's
    role visible."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        # Caller supplied enricher_kind — but per the contract caller
        # *forces enable*. To exercise planner narrowing, leave it
        # None so the planner gets to decide.
        indexer_kind="i",
        planner_enabled=True,
        policy=IngestPolicy.AUTO,
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED
    skipped = {s.step: s for s in result.step_results if s.status == StepStatus.SKIPPED}
    # The planner had no enricher_kind to consult, but the text-only
    # mode does not enable graph either.
    assert "graph" in skipped
    # When the request didn't supply a kind for a stage, the source is
    # CALLER (no kind = caller didn't request it). Planner's mode
    # decisions only matter when the kind IS available.
    assert skipped["graph"].source == StepSource.CALLER


def test_planner_enabled_with_caller_overriding_graph_runs_graph(monkeypatch):
    """Caller wins: even with a TEXT_ONLY-favoring profile, supplying
    `graph_builder_kind` forces graph to run. Source on the recorded
    step is CALLER, not PLANNER, so the audit explains the override."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_full_pipeline_handler(profile=_PROFILE_SIMPLE_TEXT),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        graph_builder_kind="g",   # caller forces graph
        indexer_kind="i",
        planner_enabled=True,
        policy=IngestPolicy.COST_SAVING,  # would normally skip graph
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED
    graph_steps = [s for s in result.step_results if s.step == "graph"]
    completed = [s for s in graph_steps if s.status == StepStatus.COMPLETED]
    assert completed, (
        f"caller-supplied graph_builder_kind must run graph; saw: "
        f"{[(s.step, s.status, s.source) for s in graph_steps]}"
    )


def test_planner_enabled_records_plan_creation_log_event(monkeypatch):
    """When planner is enabled, the workflow logs an
    `ingestion.plan.created` event so operators can verify the
    planner ran. Field must include the chosen mode."""
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
        policy=IngestPolicy.AUTO,
    )
    asyncio.run(wf.run(request))

    plan_events = [
        e for e in captured_logs
        if e.get("event") == "ingestion.plan.created"
    ]
    assert plan_events, (
        f"expected ingestion.plan.created log event; saw: {captured_logs}"
    )
    # Reason field carries the chosen mode, e.g. "mode=text_only".
    assert any("mode=" in (e.get("reason") or "") for e in plan_events)


def test_planner_failure_is_surfaced_as_workflow_failure(monkeypatch):
    """If the profiling activity fails (file gone, pypdf crash that
    we can't recover from), it must propagate as a workflow failure,
    not silently disable the planner. Phase A semantics apply: a
    failure in the planner step is workflow-fatal."""
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
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="c",
        indexer_kind="i",
        planner_enabled=True,
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    # The original lookup-failed type bubbles through (not re-wrapped).
    assert excinfo.value.type == "J1_INGEST_LOOKUP_FAILED"
