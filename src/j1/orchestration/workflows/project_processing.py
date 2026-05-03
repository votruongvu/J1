from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from j1.orchestration.activities.payloads import (
        ArtifactActivityResult,
        CompileActivityInput,
        EnrichActivityInput,
        FinalizeInput,
        GraphActivityInput,
        IndexActivityInput,
        ProcessingActivityResult,
        ProjectScope,
        SpendSummary,
        ValidateContextResult,
    )
    from j1.orchestration.activities.processing import ProcessingActivities
    from j1.orchestration.activities.project import ProjectActivities
    from j1.orchestration.temporal.retries import DEFAULT_RETRY


class WorkflowState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_BUDGET_APPROVAL = "waiting_for_budget_approval"
    WAITING_FOR_REVIEW = "waiting_for_review"
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_FINAL = "failed_final"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


GATE_AFTER_COMPILE = "after_compile"
GATE_AFTER_ENRICH = "after_enrich"
GATE_AFTER_GRAPH = "after_graph"
GATE_AFTER_INDEX = "after_index"

DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=10)
SHORT_ACTIVITY_TIMEOUT = timedelta(seconds=30)

OPERATION_VALIDATE = "validate"
OPERATION_LIST_DOCUMENTS = "list_documents"
OPERATION_COMPILE = "compile"
OPERATION_ENRICH = "enrich"
OPERATION_BUILD_GRAPH = "build_graph"
OPERATION_INDEX = "index"
OPERATION_FINALIZE = "finalize"
OPERATION_BUDGET_CHECK = "budget_check"
OPERATION_REVIEW_GATE = "review_gate"


@dataclass(frozen=True)
class ProjectProcessingRequest:
    scope: ProjectScope
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: tuple[str, ...] = ()
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class ProjectProcessingResult:
    state: str
    artifact_ids: list[str] = field(default_factory=list)
    documents_total: int = 0
    documents_completed: int = 0
    error: str | None = None


@dataclass(frozen=True)
class WorkflowStatus:
    state: str
    current_operation: str | None = None
    pending_operation: str | None = None
    completed_operations: list[str] = field(default_factory=list)
    documents_total: int = 0
    documents_completed: int = 0
    produced_artifact_ids: list[str] = field(default_factory=list)
    review_required: bool = False
    review_gate: str | None = None
    budget_approval_required: bool = False
    error: str | None = None


class _BusinessRejection(Exception):
    """Internal sentinel for terminal business failures (rejected approvals, validation, activity errors).

    Why: lets the workflow distinguish business rejections (FAILED_FINAL) from unexpected
    exceptions (FAILED_RECOVERABLE) without exposing the distinction to callers.
    """


@workflow.defn
class ProjectProcessingWorkflow:
    def __init__(self) -> None:
        self._state: WorkflowState = WorkflowState.RUNNING
        self._paused: bool = False
        self._cancelled: bool = False
        self._budget_approved: bool | None = None
        self._review_approved: bool | None = None
        self._review_gate: str | None = None
        self._review_required: bool = False
        self._budget_approval_required: bool = False
        self._current_operation: str | None = None
        self._pending_operation: str | None = None
        self._completed_operations: list[str] = []
        self._documents_total: int = 0
        self._documents_completed: int = 0
        self._produced_artifact_ids: list[str] = []
        self._error: str | None = None

    @workflow.run
    async def run(
        self, request: ProjectProcessingRequest
    ) -> ProjectProcessingResult:
        try:
            await self._validate(request)
            documents = await self._list_documents(request)
            self._documents_total = len(documents)

            for doc_id in documents:
                self._set_pending(f"{OPERATION_COMPILE}:{doc_id}")
                if await self._should_stop():
                    break
                await self._process_document(request, doc_id)
                self._documents_completed += 1

            if (
                not self._cancelled
                and request.indexer_kind
                and self._produced_artifact_ids
            ):
                self._set_pending(OPERATION_INDEX)
                if not await self._should_stop():
                    await self._index_all(request)
                    await self._maybe_review(request, GATE_AFTER_INDEX)

            await self._finalize(request)

            if self._cancelled:
                self._state = WorkflowState.CANCELLED
            else:
                self._state = WorkflowState.COMPLETED
        except _BusinessRejection as exc:
            self._state = WorkflowState.FAILED_FINAL
            self._error = str(exc)
            await self._safe_finalize(request)
        except Exception as exc:
            self._state = WorkflowState.FAILED_RECOVERABLE
            self._error = f"{type(exc).__name__}: {exc}"
            await self._safe_finalize(request)

        return ProjectProcessingResult(
            state=self._state.value,
            artifact_ids=list(self._produced_artifact_ids),
            documents_total=self._documents_total,
            documents_completed=self._documents_completed,
            error=self._error,
        )

    # ---- Operation lifecycle helpers ---------------------------------------

    def _set_pending(self, op: str) -> None:
        self._pending_operation = op

    def _begin(self, op: str) -> None:
        self._current_operation = op
        self._pending_operation = None

    def _complete(self, op: str) -> None:
        self._completed_operations.append(op)
        self._current_operation = None

    # ---- Pipeline phases ---------------------------------------------------

    async def _validate(self, request: ProjectProcessingRequest) -> None:
        self._begin(OPERATION_VALIDATE)
        result: ValidateContextResult = await workflow.execute_activity_method(
            ProjectActivities.validate_context,
            request.scope,
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY.to_temporal(),
        )
        if not result.valid:
            raise _BusinessRejection(
                f"invalid project context: {result.message or 'unspecified'}"
            )
        self._complete(OPERATION_VALIDATE)

    async def _list_documents(
        self, request: ProjectProcessingRequest
    ) -> list[str]:
        self._begin(OPERATION_LIST_DOCUMENTS)
        documents = await workflow.execute_activity_method(
            ProjectActivities.list_pending_documents,
            request.scope,
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY.to_temporal(),
        )
        self._complete(OPERATION_LIST_DOCUMENTS)
        return documents

    async def _process_document(
        self, request: ProjectProcessingRequest, document_id: str
    ) -> None:
        compile_op = f"{OPERATION_COMPILE}:{document_id}"
        if await self._gate_before_expensive(request, compile_op):
            return

        self._begin(compile_op)
        compile_result: ArtifactActivityResult = (
            await workflow.execute_activity_method(
                ProcessingActivities.compile,
                CompileActivityInput(
                    scope=request.scope,
                    document_id=document_id,
                    processor_kind=request.compiler_kind,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                ),
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        )
        if compile_result.status != "succeeded":
            raise _BusinessRejection(
                f"compile failed for {document_id}: {compile_result.error}"
            )
        self._produced_artifact_ids.extend(compile_result.artifact_ids)
        self._complete(compile_op)

        await self._maybe_review(request, GATE_AFTER_COMPILE)
        if self._cancelled:
            return

        if request.enricher_kind:
            for artifact_id in list(compile_result.artifact_ids):
                enrich_op = f"{OPERATION_ENRICH}:{artifact_id}"
                if await self._gate_before_expensive(request, enrich_op):
                    return
                self._begin(enrich_op)
                enrich_result: ArtifactActivityResult = (
                    await workflow.execute_activity_method(
                        ProcessingActivities.enrich,
                        EnrichActivityInput(
                            scope=request.scope,
                            artifact_id=artifact_id,
                            processor_kind=request.enricher_kind,
                            actor=request.actor,
                            correlation_id=request.correlation_id,
                        ),
                        start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                        retry_policy=DEFAULT_RETRY.to_temporal(),
                    )
                )
                if enrich_result.status != "succeeded":
                    raise _BusinessRejection(
                        f"enrich failed for {artifact_id}: {enrich_result.error}"
                    )
                self._produced_artifact_ids.extend(enrich_result.artifact_ids)
                self._complete(enrich_op)
            await self._maybe_review(request, GATE_AFTER_ENRICH)
            if self._cancelled:
                return

        if request.graph_builder_kind:
            graph_op = OPERATION_BUILD_GRAPH
            if await self._gate_before_expensive(request, graph_op):
                return
            self._begin(graph_op)
            graph_result: ArtifactActivityResult = (
                await workflow.execute_activity_method(
                    ProcessingActivities.build_graph,
                    GraphActivityInput(
                        scope=request.scope,
                        artifact_ids=list(self._produced_artifact_ids),
                        processor_kind=request.graph_builder_kind,
                        actor=request.actor,
                        correlation_id=request.correlation_id,
                    ),
                    start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                    retry_policy=DEFAULT_RETRY.to_temporal(),
                )
            )
            if graph_result.status != "succeeded":
                raise _BusinessRejection(
                    f"build_graph failed: {graph_result.error}"
                )
            self._produced_artifact_ids.extend(graph_result.artifact_ids)
            self._complete(graph_op)
            await self._maybe_review(request, GATE_AFTER_GRAPH)

    async def _index_all(self, request: ProjectProcessingRequest) -> None:
        # Indexing is treated as cheap (no LLM), so no budget check.
        self._begin(OPERATION_INDEX)
        index_result: ProcessingActivityResult = (
            await workflow.execute_activity_method(
                ProcessingActivities.index,
                IndexActivityInput(
                    scope=request.scope,
                    artifact_ids=list(self._produced_artifact_ids),
                    processor_kind=request.indexer_kind,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                ),
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        )
        if index_result.status != "succeeded":
            raise _BusinessRejection(f"index failed: {index_result.error}")
        self._complete(OPERATION_INDEX)

    async def _finalize(self, request: ProjectProcessingRequest) -> None:
        self._begin(OPERATION_FINALIZE)
        await workflow.execute_activity_method(
            ProjectActivities.finalize,
            FinalizeInput(
                scope=request.scope,
                state=self._state.value,
                artifact_ids=list(self._produced_artifact_ids),
                error=self._error,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY.to_temporal(),
        )
        self._complete(OPERATION_FINALIZE)

    async def _safe_finalize(self, request: ProjectProcessingRequest) -> None:
        try:
            await self._finalize(request)
        except Exception:
            # Finalization is best-effort during failure handling — never let it
            # mask the original error.
            pass

    # ---- Gates -------------------------------------------------------------

    async def _gate_before_expensive(
        self, request: ProjectProcessingRequest, next_operation: str
    ) -> bool:
        """Run pause + budget gates before an expensive operation.

        Returns True when the workflow should stop (cancelled).
        """
        self._set_pending(next_operation)
        await self._await_pause_or_cancel()
        if self._cancelled:
            return True
        await self._budget_checkpoint(request)
        if self._cancelled:
            return True
        return False

    async def _await_pause_or_cancel(self) -> None:
        if self._cancelled or not self._paused:
            return
        previous_state = self._state
        self._state = WorkflowState.PAUSED
        await workflow.wait_condition(
            lambda: not self._paused or self._cancelled
        )
        if not self._cancelled:
            self._state = (
                previous_state
                if previous_state != WorkflowState.PAUSED
                else WorkflowState.RUNNING
            )

    async def _budget_checkpoint(
        self, request: ProjectProcessingRequest
    ) -> None:
        if request.budget_limit_amount is None:
            return
        previous_operation = self._current_operation
        self._current_operation = OPERATION_BUDGET_CHECK
        spend: SpendSummary = await workflow.execute_activity_method(
            ProjectActivities.compute_spend,
            request.scope,
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY.to_temporal(),
        )
        if Decimal(spend.total_amount) < Decimal(request.budget_limit_amount):
            self._current_operation = previous_operation
            return
        self._budget_approved = None
        self._budget_approval_required = True
        self._state = WorkflowState.WAITING_FOR_BUDGET_APPROVAL
        await workflow.wait_condition(
            lambda: self._budget_approved is not None or self._cancelled
        )
        self._budget_approval_required = False
        self._current_operation = previous_operation
        if self._cancelled:
            return
        if not self._budget_approved:
            raise _BusinessRejection(
                f"budget rejected at spend={spend.total_amount} {spend.currency} "
                f"limit={request.budget_limit_amount} {request.budget_currency}"
            )
        self._state = WorkflowState.RUNNING

    async def _maybe_review(
        self, request: ProjectProcessingRequest, gate: str
    ) -> None:
        if gate not in request.review_after:
            return
        previous_operation = self._current_operation
        self._review_approved = None
        self._review_gate = gate
        self._review_required = True
        self._current_operation = f"{OPERATION_REVIEW_GATE}:{gate}"
        self._state = WorkflowState.WAITING_FOR_REVIEW
        await workflow.wait_condition(
            lambda: self._review_approved is not None or self._cancelled
        )
        self._review_required = False
        self._current_operation = previous_operation
        if self._cancelled:
            self._review_gate = None
            return
        if not self._review_approved:
            raise _BusinessRejection(f"review rejected at gate {gate}")
        self._review_gate = None
        self._state = WorkflowState.RUNNING

    async def _should_stop(self) -> bool:
        await self._await_pause_or_cancel()
        return self._cancelled

    # ---- Signals -----------------------------------------------------------

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.signal
    def approve_budget(self) -> None:
        self._budget_approved = True

    @workflow.signal
    def reject_budget(self) -> None:
        self._budget_approved = False

    @workflow.signal
    def approve_review(self) -> None:
        self._review_approved = True

    @workflow.signal
    def reject_review(self) -> None:
        self._review_approved = False

    # ---- Query -------------------------------------------------------------

    @workflow.query
    def get_status(self) -> WorkflowStatus:
        return WorkflowStatus(
            state=self._state.value,
            current_operation=self._current_operation,
            pending_operation=self._pending_operation,
            completed_operations=list(self._completed_operations),
            documents_total=self._documents_total,
            documents_completed=self._documents_completed,
            produced_artifact_ids=list(self._produced_artifact_ids),
            review_required=self._review_required,
            review_gate=self._review_gate,
            budget_approval_required=self._budget_approval_required,
            error=self._error,
        )
