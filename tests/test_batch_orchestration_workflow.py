"""Unit tests for `BatchOrchestrationWorkflow`.

Covers the sequential dispatch contract, failure-policy semantics
(halt vs continue), cancel propagation, and the aggregate result
shape. Mirrors the patching strategy in
`test_project_processing_workflow.py`: monkeypatch
`workflow.execute_child_workflow` so the test can observe dispatch
order without standing up a Temporal server.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError, CancelledError

from j1.orchestration.activities.payloads import ProjectScope
from j1.orchestration.workflows.batch_orchestration import (
    BATCH_FAILURE_POLICY_CONTINUE,
    BATCH_FAILURE_POLICY_HALT,
    BatchChildSpec,
    BatchOrchestrationRequest,
    BatchOrchestrationResult,
    BatchOrchestrationWorkflow,
)


def _scope() -> ProjectScope:
    return ProjectScope(tenant_id="acme", project_id="alpha")


def _spec(*, document_id: str, run_id: str, **overrides) -> BatchChildSpec:
    """Build a child spec with sane defaults so each test can focus
    on the one or two fields it cares about."""
    return BatchChildSpec(
        workflow_id=f"j1-acme-alpha-{document_id}",
        document_id=document_id,
        correlation_id=run_id,
        compiler_kind=overrides.pop("compiler_kind", "raganything"),
        enricher_kind=overrides.pop("enricher_kind", None),
        graph_builder_kind=overrides.pop("graph_builder_kind", None),
        indexer_kind=overrides.pop("indexer_kind", "sqlite_search"),
        actor=overrides.pop("actor", "ops@example.com"),
        planner_enabled=overrides.pop("planner_enabled", True),
    )


def _patch_child_dispatch(monkeypatch, *, side_effect=None):
    """Replace `workflow.execute_child_workflow` with a recorder.
    Returns the call list — each entry is a `(workflow_id,
    correlation_id, document_id)` tuple. `side_effect` is an
    optional callable invoked with the same args before recording;
    it can raise (e.g. `ApplicationError`) to simulate child failure.

    Also patches `workflow.logger` to a stdlib logger because the
    real `workflow.logger` raises outside a Temporal runtime — and
    the workflow's failure-handling path logs warnings."""
    import logging
    calls: list[tuple[str, str, str]] = []

    async def _exec(workflow_run_method, child_request, **kwargs):
        wf_id = kwargs.get("id", "<no-id>")
        calls.append((
            wf_id,
            child_request.correlation_id or "",
            (child_request.target_document_ids or ("",))[0],
        ))
        if side_effect is not None:
            side_effect(wf_id, child_request)

    monkeypatch.setattr(workflow, "execute_child_workflow", _exec)
    monkeypatch.setattr(
        workflow, "logger", logging.getLogger("test.batch_orchestration"),
    )
    return calls


def test_dispatches_children_sequentially_in_listed_order(monkeypatch):
    """Three child specs → three dispatches in the same order. The
    awaited `execute_child_workflow` enforces sequential semantics
    (next child waits for the previous one's terminal state)."""
    calls = _patch_child_dispatch(monkeypatch)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
            _spec(document_id="doc-C", run_id="run-C"),
        ),
    )
    result = asyncio.run(wf.run(request))
    assert [c[2] for c in calls] == ["doc-A", "doc-B", "doc-C"]
    # Each dispatch used the deterministic per-document workflow_id
    # the spec carried — guards against a refactor that synthesises
    # ids inside the parent (which would break USE_EXISTING re-attach).
    assert [c[0] for c in calls] == [
        "j1-acme-alpha-doc-A",
        "j1-acme-alpha-doc-B",
        "j1-acme-alpha-doc-C",
    ]
    assert result == BatchOrchestrationResult(
        batch_run_id="batch-1",
        file_count=3,
        succeeded_count=3,
        failed_count=0,
        cancelled=False,
        failed_run_ids=[],
        final_status="completed",
    )


def test_continue_policy_keeps_dispatching_after_child_failure(monkeypatch):
    """Default `failure_policy="continue"` lets a single flaky
    document fail without blocking the rest of the batch. The
    failed run id shows up in `failed_run_ids`; remaining children
    still launch."""
    def _maybe_fail(wf_id: str, child_request) -> None:
        if child_request.correlation_id == "run-B":
            raise ApplicationError("simulated child failure", non_retryable=True)

    calls = _patch_child_dispatch(monkeypatch, side_effect=_maybe_fail)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        failure_policy=BATCH_FAILURE_POLICY_CONTINUE,
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
            _spec(document_id="doc-C", run_id="run-C"),
        ),
    )
    result = asyncio.run(wf.run(request))
    # All three children dispatched even though B failed.
    assert [c[2] for c in calls] == ["doc-A", "doc-B", "doc-C"]
    assert result.succeeded_count == 2
    assert result.failed_count == 1
    assert result.failed_run_ids == ["run-B"]
    assert result.final_status == "partial_completed"


def test_halt_policy_stops_after_first_failure(monkeypatch):
    """`failure_policy="halt"` aborts the batch on the first child
    failure. Used when every document is required (e.g. a multi-part
    upload that only makes sense as a unit)."""
    def _fail_b(wf_id: str, child_request) -> None:
        if child_request.correlation_id == "run-B":
            raise ApplicationError("hard fail", non_retryable=True)

    calls = _patch_child_dispatch(monkeypatch, side_effect=_fail_b)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        failure_policy=BATCH_FAILURE_POLICY_HALT,
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
            _spec(document_id="doc-C", run_id="run-C"),
        ),
    )
    result = asyncio.run(wf.run(request))
    # Only A and B dispatched — C was never started.
    assert [c[2] for c in calls] == ["doc-A", "doc-B"]
    assert result.succeeded_count == 1
    assert result.failed_count == 1
    assert result.failed_run_ids == ["run-B"]
    # All-failed-after-first-success vs no-success matters for the
    # final status mapping; partial_completed is correct here
    # because A succeeded.
    assert result.final_status == "partial_completed"


def test_all_failed_reports_failed_status(monkeypatch):
    """When zero children succeed and at least one failed, the
    batch is `failed` (not `partial_completed`)."""
    def _always_fail(wf_id: str, child_request) -> None:
        raise ApplicationError("everything fails", non_retryable=True)

    _patch_child_dispatch(monkeypatch, side_effect=_always_fail)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        failure_policy=BATCH_FAILURE_POLICY_CONTINUE,
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
        ),
    )
    result = asyncio.run(wf.run(request))
    assert result.succeeded_count == 0
    assert result.failed_count == 2
    assert result.final_status == "failed"


def test_cancel_signal_stops_further_dispatch(monkeypatch):
    """Cancelling the parent sets `_cancelled`; subsequent iterations
    of the dispatch loop break out before launching the next child.
    The `cancelled` boolean + `cancelled` final_status surface so
    operators can distinguish "operator stopped this" from "batch
    failed organically."""
    wf = BatchOrchestrationWorkflow()

    def _cancel_after_first(wf_id: str, child_request) -> None:
        # Cancel mid-batch: after dispatching A, fire the cancel
        # signal so the next iteration sees `_cancelled=True`.
        if child_request.correlation_id == "run-A":
            wf.cancel()

    calls = _patch_child_dispatch(monkeypatch, side_effect=_cancel_after_first)
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
            _spec(document_id="doc-C", run_id="run-C"),
        ),
    )
    result = asyncio.run(wf.run(request))
    assert [c[2] for c in calls] == ["doc-A"]  # only A launched
    assert result.cancelled is True
    assert result.succeeded_count == 1
    assert result.final_status == "cancelled"


def test_cancellation_during_in_flight_child_marks_batch_cancelled(monkeypatch):
    """If Temporal cancels the parent while a child is in flight,
    the dispatch await raises `CancelledError`. The parent flips
    `_cancelled`, breaks out of the loop, and reports `cancelled`."""
    def _raise_cancel(wf_id: str, child_request) -> None:
        if child_request.correlation_id == "run-A":
            raise CancelledError("parent cancelled")

    _patch_child_dispatch(monkeypatch, side_effect=_raise_cancel)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(),
        batch_run_id="batch-1",
        child_specs=(
            _spec(document_id="doc-A", run_id="run-A"),
            _spec(document_id="doc-B", run_id="run-B"),
        ),
    )
    result = asyncio.run(wf.run(request))
    assert result.cancelled is True
    assert result.final_status == "cancelled"


def test_empty_batch_returns_completed_with_zero_counts(monkeypatch):
    """Defensive: an empty `child_specs` is a degenerate case
    (the REST endpoint rejects this with 400 BEFORE dispatching the
    parent), but the workflow MUST still terminate cleanly if the
    spec list is empty — never hang waiting for nothing."""
    calls = _patch_child_dispatch(monkeypatch)
    wf = BatchOrchestrationWorkflow()
    request = BatchOrchestrationRequest(
        scope=_scope(), batch_run_id="batch-empty", child_specs=(),
    )
    result = asyncio.run(wf.run(request))
    assert calls == []
    assert result.file_count == 0
    assert result.final_status == "completed"


def test_workflow_registration_is_temporal_compatible():
    """Sanity check — the workflow class carries the temporal
    decorator marker so `WorkerSpec(workflows=[...])` accepts it."""
    assert hasattr(
        BatchOrchestrationWorkflow, "__temporal_workflow_definition",
    )
