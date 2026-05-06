"""`TemporalJobControlService` is the integration-layer entry point for
"start a project workflow". This test pins down the round-2 contract:

  * The default `_default_workflow_id` factory is intentionally
    non-deterministic (uuid4 suffix) — every bulk-job invocation is a
    fresh run.
  * `make_per_document_workflow_id` produces a stable id that
    callers driving per-document starts can use.
  * `id_conflict_policy` is forwarded to `client.start_workflow` only
    when explicitly configured — keeps backward compatibility with
    deployments that rely on Temporal's default (FAIL) behaviour.
"""

import asyncio
from dataclasses import dataclass

from temporalio.common import WorkflowIDConflictPolicy

from j1.integration import ProjectIngestionRequestDTO
from j1.integration.services import (
    TemporalJobControlService,
    _default_workflow_id,
    make_per_document_workflow_id,
)
from j1.projects.context import ProjectContext


@dataclass
class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def start_workflow(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return None


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _request() -> ProjectIngestionRequestDTO:
    return ProjectIngestionRequestDTO(compiler_kind="mock", actor="tester")


def test_default_workflow_id_is_unique_per_invocation():
    ctx = _ctx()
    a = _default_workflow_id(ctx)
    b = _default_workflow_id(ctx)
    assert a != b, "bulk-job factory must produce a fresh id each call"
    assert a.startswith("j1-acme-alpha-")
    assert b.startswith("j1-acme-alpha-")


def test_make_per_document_workflow_id_is_deterministic():
    ctx = _ctx()
    assert (
        make_per_document_workflow_id(ctx, "doc-42")
        == "j1-acme-alpha-doc-42"
    )
    assert (
        make_per_document_workflow_id(ctx, "doc-42")
        == make_per_document_workflow_id(ctx, "doc-42")
    )


def test_service_omits_id_conflict_policy_by_default():
    """Backward compatibility: deployments that don't pass a policy
    keep getting Temporal's default (FAIL) behaviour."""
    client = _RecordingClient()
    service = TemporalJobControlService(
        client_provider=lambda: client, task_queue="j1-default",
    )
    asyncio.run(service.start_project_job(_ctx(), _request()))
    assert "id_conflict_policy" not in client.calls[0]["kwargs"]


def test_service_forwards_id_conflict_policy_when_set():
    client = _RecordingClient()
    service = TemporalJobControlService(
        client_provider=lambda: client,
        task_queue="j1-default",
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    asyncio.run(service.start_project_job(_ctx(), _request()))
    assert (
        client.calls[0]["kwargs"]["id_conflict_policy"]
        == WorkflowIDConflictPolicy.USE_EXISTING
    )


def test_service_uses_supplied_workflow_id_factory():
    client = _RecordingClient()
    service = TemporalJobControlService(
        client_provider=lambda: client,
        task_queue="j1-default",
        workflow_id_factory=lambda ctx: f"stable-{ctx.project_id}",
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    result = asyncio.run(service.start_project_job(_ctx(), _request()))
    assert result.job_id == "stable-alpha"
    assert client.calls[0]["kwargs"]["id"] == "stable-alpha"
