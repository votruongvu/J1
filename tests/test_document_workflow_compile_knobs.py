"""Regression: `DocumentProcessingWorkflow` MUST configure the compile
activity with the same long-running-step safety net the project
workflow uses — bounded retry, generous start_to_close, heartbeat
liveness — so a long MinerU parse cannot trigger a 10-minute timeout
+ 5-attempt retry storm that re-spawns MinerU for the same document.

Pin the values structurally so a future "let me simplify the slim
workflow" refactor can't silently regress to DEFAULT_RETRY +
DEFAULT_ACTIVITY_TIMEOUT for the most expensive step in the pipeline.
"""

import asyncio
from datetime import timedelta

import pytest
from temporalio import workflow

from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    ProjectScope,
)
from j1.orchestration.temporal.retries import COMPILE_RETRY
from j1.orchestration.workflows.document_processing import (
    COMPILE_ACTIVITY_TIMEOUT,
    HEARTBEAT_TIMEOUT,
    DocumentProcessingRequest,
    DocumentProcessingWorkflow,
)


def _activity_name(method) -> str:
    return getattr(method, "__temporal_activity_definition", method).name


def test_compile_uses_compile_specific_timeout_and_retry(monkeypatch):
    captured: dict = {}

    async def _exec(method, payload=None, **kwargs):
        name = _activity_name(method)
        if name.endswith("compile"):
            captured.update(kwargs)
            return ArtifactActivityResult(
                status="succeeded", artifact_ids=["art-1"]
            )
        raise AssertionError(name)

    monkeypatch.setattr(workflow, "execute_activity_method", _exec)
    monkeypatch.setattr(workflow, "execute_activity", _exec)

    wf = DocumentProcessingWorkflow()
    request = DocumentProcessingRequest(
        scope=ProjectScope(tenant_id="acme", project_id="alpha"),
        document_id="doc-1",
        compiler_kind="c",
    )
    asyncio.run(wf.run(request))

    # Generous upper bound on a single attempt — must NOT be the
    # 10-minute default that fires mid-parse on real PDFs.
    assert captured["start_to_close_timeout"] == COMPILE_ACTIVITY_TIMEOUT
    assert captured["start_to_close_timeout"] >= timedelta(minutes=30)

    # Heartbeat liveness paired with the activity's 30-second ticker.
    assert captured["heartbeat_timeout"] == HEARTBEAT_TIMEOUT

    # Bounded retry: at most 2 attempts. A transient blip gets one
    # retry; deterministic failures don't multiply parse cost.
    retry = captured["retry_policy"]
    assert retry.maximum_attempts == COMPILE_RETRY.maximum_attempts == 2
    # Non-retryable error types include the J1 ingestion type and
    # parser-determinstic failure types so they fail fast.
    assert "J1_INGEST_REQUIRED_STEP_FAILED" in retry.non_retryable_error_types
    assert "UnsupportedFileType" in retry.non_retryable_error_types
