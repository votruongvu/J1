"""Workflow tests for the RAGAnything split-pipeline mode.

Pin two contracts:

  1. `pipeline_mode="split_parse_insert"` invokes the dedicated
     `insert_content` activity after compile + post-compile planning,
     using the `parsed_source_artifact_id` returned by compile.
  2. `pipeline_mode="complete"` (default) does NOT invoke
     `insert_content` — the legacy single-shot compile path is
     preserved exactly for deployments that haven't migrated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    InsertContentActivityInput,
    ProcessingActivityResult,
    ProjectScope,
    ValidateContextResult,
)
from j1.orchestration.workflows.project_processing import (
    PIPELINE_MODE_COMPLETE,
    PIPELINE_MODE_SPLIT_PARSE_INSERT,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
)
from j1.processing.status import FinalStatus, StepStatus


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _patch_workflow_runtime(monkeypatch, *, exec_handler):
    captured = {"calls": []}

    async def _exec(method, payload=None, **kwargs):
        captured["calls"].append(
            {"name": _activity_name(method), "payload": payload}
        )
        return exec_handler(method, payload, kwargs)

    async def _wait(predicate, **kwargs):
        return None

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)
    monkeypatch.setattr(workflow, "wait_condition", _wait)
    monkeypatch.setattr(workflow, "continue_as_new", lambda *_a, **_k: None)
    return captured


def _split_handler(*, insert_artifacts: list[str] | None = None):
    """Build a handler that handles every activity the workflow
    normally invokes during a split-mode run, returning success
    everywhere. The compile result includes a parsed_source artifact
    id so the workflow can dispatch to insert_content."""

    insert_artifacts = insert_artifacts if insert_artifacts is not None else [
        "chunk-1", "chunk-2",
    ]

    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["parsed-1", "manifest-1", "compiled-text-1"],
                parsed_source_artifact_id="parsed-1",
                kinds=("parsed_source", "parsed_content_manifest", "compiled.text"),
            )
        if name.endswith("insert_content"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=list(insert_artifacts),
                kinds=tuple("chunk" for _ in insert_artifacts),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_plan_revised"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    return handler


def test_split_mode_invokes_insert_content_activity(monkeypatch):
    """`pipeline_mode=split_parse_insert` must dispatch to the
    `insert_content` activity, passing the `parsed_source_artifact_id`
    the compile activity returned. Chunk artifact ids from the insert
    activity feed into the workflow's produced artifacts."""
    captured = _patch_workflow_runtime(
        monkeypatch, exec_handler=_split_handler(),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="raganything",
        pipeline_mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED, result.error
    insert_calls = [
        c for c in captured["calls"] if c["name"].endswith("insert_content")
    ]
    assert len(insert_calls) == 1, (
        f"expected exactly one insert_content invocation, got {insert_calls}"
    )
    payload = insert_calls[0]["payload"]
    assert isinstance(payload, InsertContentActivityInput)
    assert payload.parsed_source_artifact_id == "parsed-1"
    assert payload.processor_kind == "raganything"
    assert payload.document_id == "doc-1"
    # Chunk artifacts produced by insert flow into the workflow's
    # produced_artifact_ids so downstream stages (enrich, graph, index)
    # see them.
    assert "chunk-1" in result.artifact_ids
    assert "chunk-2" in result.artifact_ids


def test_split_mode_records_real_generate_knowledge_chunks_step(monkeypatch):
    """In split mode, `generate_knowledge_chunks` is no longer a
    synthetic step — it wraps the real `insert_content` activity. The
    step result must carry `synthetic=False` and the actual chunk
    artifact count, so the FE timeline reads truthfully."""
    _patch_workflow_runtime(
        monkeypatch,
        exec_handler=_split_handler(insert_artifacts=["chunk-a", "chunk-b", "chunk-c"]),
    )
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="raganything",
        pipeline_mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
    )
    result = asyncio.run(wf.run(request))

    chunk_steps = [
        s for s in result.step_results if s.step == "generate_knowledge_chunks"
    ]
    assert chunk_steps, "generate_knowledge_chunks step not recorded"
    chunk_step = chunk_steps[-1]
    assert chunk_step.status == StepStatus.COMPLETED
    assert chunk_step.artifact_count == 3
    assert chunk_step.metadata.get("synthetic") is False
    assert (
        chunk_step.metadata.get("pipeline_mode")
        == PIPELINE_MODE_SPLIT_PARSE_INSERT
    )


def test_complete_mode_does_not_invoke_insert_content(monkeypatch):
    """`pipeline_mode=complete` (the default) preserves the legacy
    single-shot compile path. The workflow must NOT invoke the
    `insert_content` activity — even if the compile result happens to
    carry a `parsed_source_artifact_id` (defensive: a future processor
    might surface one for diagnostic reasons)."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["art-1"],
                parsed_source_artifact_id="parsed-1",
            )
        if name.endswith("insert_content"):
            raise AssertionError(
                "insert_content must NOT be invoked in complete mode"
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_plan_revised"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    captured = _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="raganything",
        pipeline_mode=PIPELINE_MODE_COMPLETE,
    )
    result = asyncio.run(wf.run(request))

    assert result.final_status == FinalStatus.COMPLETED
    insert_calls = [
        c for c in captured["calls"] if c["name"].endswith("insert_content")
    ]
    assert insert_calls == [], (
        "insert_content must not run in complete mode; "
        f"saw: {insert_calls}"
    )
    # Synthetic generate_knowledge_chunks still recorded in complete mode.
    chunk_steps = [
        s for s in result.step_results if s.step == "generate_knowledge_chunks"
    ]
    assert chunk_steps, "generate_knowledge_chunks step missing in complete mode"
    assert chunk_steps[-1].metadata.get("synthetic") is True


def test_split_mode_propagates_insert_failure(monkeypatch):
    """An insert activity that returns a non-succeeded status must
    fail the workflow with `_BusinessRejection` semantics, recording
    the failure on the `generate_knowledge_chunks` step. Without this
    pin the workflow could silently complete with a parsed-but-not-
    chunked document — exactly the failure mode split mode is meant
    to make visible."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["parsed-1"],
                parsed_source_artifact_id="parsed-1",
            )
        if name.endswith("insert_content"):
            return ArtifactActivityResult(
                status="failed", error="insert blew up",
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_plan_revised"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="raganything",
        pipeline_mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))

    assert "insert blew up" in str(excinfo.value)
    # The step result is captured on the workflow's recorded state
    # before the raise — assert via `get_status()` so failures still
    # surface to operators via the FE.
    status = wf.get_status()
    chunk_steps = [
        s for s in status.step_results if s.step == "generate_knowledge_chunks"
    ]
    assert chunk_steps, "generate_knowledge_chunks step not recorded on failure"
    assert chunk_steps[-1].status == StepStatus.FAILED
    assert "insert blew up" in (chunk_steps[-1].reason or "")


def test_completion_validation_fails_when_chunks_step_completed_without_artifact(
    monkeypatch,
):
    """Per-stage required-output rule: in split mode the
    `generate_knowledge_chunks` step is the real boundary; a step
    that's recorded as COMPLETED without producing any `chunk`
    artifact is a contract violation, not a SUCCEEDED state. The
    workflow must reject the run via `_validate_completion` rather
    than mark it COMPLETED with an empty chunk store."""
    def handler(method, payload, kwargs):
        name = _activity_name(method)
        if name.endswith("validate_context"):
            return ValidateContextResult(valid=True)
        if name.endswith("list_pending_documents"):
            return ["doc-1"]
        if name.endswith("compile"):
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=["parsed-1"],
                parsed_source_artifact_id="parsed-1",
                kinds=("parsed_source",),
            )
        if name.endswith("insert_content"):
            # Activity reports SUCCEEDED but produces NO chunk artifact
            # — the bug class this rule catches. Without the rule the
            # workflow would mark COMPLETED.
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=[],
                kinds=(),
            )
        if name.endswith("set_document_status"):
            return None
        if name.endswith("finalize"):
            return None
        if name.endswith("build_planning_result"):
            return None
        if name.endswith("report_plan_revised"):
            return None
        if name.endswith("report_step_lifecycle"):
            return None
        if name.endswith("persist_error_report"):
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["err-1"],
                kinds=("error_report",),
            )
        raise AssertionError(f"unexpected activity: {name}")

    _patch_workflow_runtime(monkeypatch, exec_handler=handler)
    wf = ProjectProcessingWorkflow()
    request = ProjectProcessingRequest(
        scope=_scope(),
        compiler_kind="raganything",
        pipeline_mode=PIPELINE_MODE_SPLIT_PARSE_INSERT,
    )
    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(wf.run(request))
    assert "chunk" in str(excinfo.value).lower()
