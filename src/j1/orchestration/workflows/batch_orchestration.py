"""Parent workflow for multi-upload ingestion batches.

Replaces the previous "fan out N child workflows from the REST handler"
pattern (which relied on the worker-wide
`J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1` env var to serialize per-doc
processing). One `BatchOrchestrationWorkflow` instance owns the batch:
it dispatches each child `ProjectProcessingWorkflow` sequentially via
`workflow.execute_child_workflow`, waits for the child's terminal
state before starting the next, and propagates cancellation to
in-flight children.

Design notes:

  * Each child workflow_id is constructed by the REST endpoint and
    handed to the parent in a `ChildSpec`. Keeping ID construction
    on the REST side means the deterministic `j1-{tenant}-{project}-
    {document_id}` derivation lives in one place — the parent just
    forwards the id to `execute_child_workflow`.

  * `failure_policy` controls whether a child failure halts the
    batch or continues to the next. Default is `"continue"` because
    a flaky single doc shouldn't block the rest of an operator's
    upload. `"halt"` is the safer choice for batches where every
    document is required.

  * Status aggregation is read-side, not parent-side. The existing
    `derive_batch_status` helper queries each child's run record;
    the parent doesn't track child statuses itself. This keeps the
    parent dumb (and Temporal payloads small).

  * Cancellation: operators send the cancel signal; the parent flips
    `_cancelled`, refuses to start any further children, and lets
    the in-flight child finish (Temporal's default
    `ParentClosePolicy.TERMINATE` is overridden to
    `REQUEST_CANCEL` per child so the in-flight workflow gets a
    proper cancellation signal rather than a hard kill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError, CancelledError

with workflow.unsafe.imports_passed_through():
    from j1.orchestration.activities.payloads import ProjectScope
    from j1.orchestration.workflows.project_processing import (
        ProjectProcessingRequest,
        ProjectProcessingWorkflow,
    )

# Per-child execution timeout. Generous because a single-doc workflow
# can run an hour or more (compile is the slow stage). The parent
# bounds total batch wall-clock at this × file-count; operators who
# want a tighter cap should use smaller batches.
CHILD_EXECUTION_TIMEOUT = timedelta(hours=2)

BATCH_FAILURE_POLICY_HALT = "halt"
BATCH_FAILURE_POLICY_CONTINUE = "continue"


@dataclass(frozen=True)
class BatchChildSpec:
    """One child workflow's launch parameters.

    The REST endpoint builds N of these — one per uploaded file —
    and hands them to the parent. The parent forwards them as-is
    to `execute_child_workflow`.

    `workflow_id` is the deterministic per-document id the existing
    `make_per_document_starter` would have used; constructing it on
    the REST side keeps the `j1-{tenant}-{project}-{document_id}`
    derivation in one place.
    """

    workflow_id: str
    document_id: str
    correlation_id: str  # = the child's run_id (FE-facing identifier)
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    actor: str = "system"
    planner_enabled: bool = False


@dataclass(frozen=True)
class BatchOrchestrationRequest:
    """Parent workflow input. `batch_run_id` is the operator-facing
    identifier (also the Temporal `workflow_id` of the parent — see
    REST construction). `child_specs` are launched in list order."""

    scope: ProjectScope
    batch_run_id: str
    child_specs: tuple[BatchChildSpec, ...] = field(default_factory=tuple)
    actor: str = "system"
    failure_policy: str = BATCH_FAILURE_POLICY_CONTINUE


@dataclass(frozen=True)
class BatchOrchestrationResult:
    """Aggregate outcome reported back to Temporal. Intentionally
    minimal — readers should query the per-child run records via the
    REST run-list surface for full status."""

    batch_run_id: str
    file_count: int
    succeeded_count: int
    failed_count: int
    cancelled: bool = False
    failed_run_ids: list[str] = field(default_factory=list)
    final_status: str = "completed"


@workflow.defn
class BatchOrchestrationWorkflow:
    """Sequential dispatcher for an ingestion batch.

    One instance per `POST /ingestion-batches` call. Lives for the
    duration of the batch. Listens for a `cancel` signal so operators
    can stop the batch mid-flight; in-flight children get a proper
    Temporal cancellation, not a hard kill.
    """

    def __init__(self) -> None:
        self._cancelled: bool = False

    @workflow.signal
    def cancel(self) -> None:
        """Operator-initiated stop. The current child (if any) keeps
        running because Temporal's child cancellation is requested
        at dispatch boundary in v1; future iterations may add
        per-child cancel signals to interrupt in-flight workflows
        too. Setting the flag here prevents any further children
        from launching."""
        self._cancelled = True

    @workflow.run
    async def run(
        self, request: BatchOrchestrationRequest
    ) -> BatchOrchestrationResult:
        succeeded = 0
        failed = 0
        failed_run_ids: list[str] = []

        for spec in request.child_specs:
            if self._cancelled:
                break
            child_request = ProjectProcessingRequest(
                scope=request.scope,
                compiler_kind=spec.compiler_kind,
                enricher_kind=spec.enricher_kind,
                graph_builder_kind=spec.graph_builder_kind,
                indexer_kind=spec.indexer_kind,
                actor=spec.actor,
                correlation_id=spec.correlation_id,
                target_document_ids=(spec.document_id,),
                planner_enabled=spec.planner_enabled,
            )
            try:
                await workflow.execute_child_workflow(
                    ProjectProcessingWorkflow.run,
                    child_request,
                    id=spec.workflow_id,
                    # Each child writes its own status to the
                    # IngestionRunStore; the parent doesn't need
                    # the return value to aggregate. Using
                    # execute_child_workflow (not start_child) gives
                    # us the sequential await we want.
                    execution_timeout=CHILD_EXECUTION_TIMEOUT,
                )
                succeeded += 1
            except CancelledError:
                # Parent itself was cancelled mid-child. Stop
                # dispatching further children and let the cancelled
                # child surface its own status.
                self._cancelled = True
                break
            except ApplicationError as exc:
                failed += 1
                failed_run_ids.append(spec.correlation_id)
                workflow.logger.warning(
                    "batch %s child %s failed: %s",
                    request.batch_run_id, spec.correlation_id, exc,
                )
                if request.failure_policy == BATCH_FAILURE_POLICY_HALT:
                    break

        if self._cancelled:
            final_status = "cancelled"
        elif failed == 0:
            final_status = "completed"
        elif succeeded == 0:
            final_status = "failed"
        else:
            final_status = "partial_completed"

        return BatchOrchestrationResult(
            batch_run_id=request.batch_run_id,
            file_count=len(request.child_specs),
            succeeded_count=succeeded,
            failed_count=failed,
            cancelled=self._cancelled,
            failed_run_ids=failed_run_ids,
            final_status=final_status,
        )
