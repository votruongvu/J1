"""Regression: the dev API's per-document `JobStarter` builds a
deterministic workflow id and forwards `id_conflict_policy=USE_EXISTING`
so a re-uploaded file (same checksum → same `document_id` → same
workflow id) doesn't spawn a parallel parse.

This is the application-level half of the duplicate-prevention
contract; the activity-level half is `ProcessingResultCache`."""

import asyncio
from dataclasses import dataclass

from temporalio.common import WorkflowIDConflictPolicy

from deploy.dev.api import make_per_document_starter
from j1.projects.context import ProjectContext


@dataclass
class _Body:
    compiler_kind: str = "mock"
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    actor: str = "tester"
    correlation_id: str | None = "run-1"


class _RecordingClient:
    """Captures `start_workflow` arguments without talking to Temporal."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def start_workflow(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return None


def test_starter_uses_deterministic_workflow_id_and_use_existing_conflict_policy():
    client = _RecordingClient()
    starter = make_per_document_starter(
        client_provider=lambda: client,
        task_queue="j1-default",
        planner_enabled=True,
    )

    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    workflow_id = asyncio.run(starter(ctx, "doc-XYZ", _Body()))

    assert workflow_id == "j1-acme-alpha-doc-XYZ"
    assert len(client.calls) == 1
    kwargs = client.calls[0]["kwargs"]
    assert kwargs["id"] == "j1-acme-alpha-doc-XYZ"
    assert kwargs["task_queue"] == "j1-default"
    # The whole point of this test: re-uploads of the same physical
    # file must NOT spawn a parallel workflow.
    assert kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


def test_starter_returns_same_id_for_repeated_uploads_of_same_document():
    """Two starts with the same `document_id` produce the same workflow
    id. Combined with `id_conflict_policy=USE_EXISTING`, the second
    call returns the existing handle instead of starting a new run."""
    client = _RecordingClient()
    starter = make_per_document_starter(
        client_provider=lambda: client,
        task_queue="j1-default",
        planner_enabled=False,
    )

    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    first = asyncio.run(starter(ctx, "doc-1", _Body()))
    second = asyncio.run(starter(ctx, "doc-1", _Body()))

    assert first == second == "j1-acme-alpha-doc-1"
    assert client.calls[0]["kwargs"]["id"] == client.calls[1]["kwargs"]["id"]
    for call in client.calls:
        assert (
            call["kwargs"]["id_conflict_policy"]
            == WorkflowIDConflictPolicy.USE_EXISTING
        )
