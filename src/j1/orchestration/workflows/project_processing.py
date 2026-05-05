from dataclasses import dataclass, field, replace as _replace_request
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from temporalio import workflow
from temporalio.exceptions import ApplicationError

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
    from j1.orchestration.activities.profiling import (
        ProfileDocumentInput,
        ProfilingActivities,
    )
    from j1.orchestration.activities.project import ProjectActivities
    from j1.orchestration.errors import (
        ERROR_TYPE_REQUIRED_STEP_FAILED,
        ERROR_TYPE_UNEXPECTED_ERROR,
    )
    from j1.orchestration.temporal.retries import DEFAULT_RETRY
    from j1.processing.planning import (
        STEP_COMPILE,
        STEP_ENRICH,
        STEP_GRAPH,
        STEP_INDEX,
        DefaultIngestPlanner,
        IngestPlan,
        IngestPlanner,
        IngestPolicy,
    )
    from j1.processing.profiling import DocumentProfile
    from j1.processing.status import (
        FailurePolicy,
        FinalStatus,
        StepSource,
        StepStatus,
    )
    from j1.processing.step_result import StepError, StepResult


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
# Phase A.4: long-running activities (compile, build_graph) emit a
# heartbeat at start and finish. The activity is considered hung if
# no heartbeat arrives within this window — Temporal then fails the
# attempt with a heartbeat-timeout, retried per the activity's retry
# policy. Generous compared to the default heartbeat cadence so a
# slow but progressing activity (e.g. mineru loading 2.4 GB of model
# weights on first call) doesn't get killed.
HEARTBEAT_TIMEOUT = timedelta(minutes=2)

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
    # How the workflow reacts to step failures. Defaults to fail_fast,
    # which preserves the historical "any failure fails the workflow"
    # behaviour. `continue_optional` permits PARTIAL_COMPLETED when
    # optional steps fail; `best_effort` permits it for required steps.
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    # Phase B: adaptive planning. When False (default), the workflow
    # uses the legacy "kind is None → skip" gate logic and behaves
    # exactly as before Phase B. When True, each document is profiled
    # and run through `DefaultIngestPlanner`; the resulting `IngestPlan`
    # decides which configured stages to actually attempt. Caller-
    # supplied kinds always override planner decisions (caller wins).
    planner_enabled: bool = False
    # Policy fed to the planner. Only consulted when `planner_enabled`.
    policy: IngestPolicy = IngestPolicy.AUTO
    # Continue-as-new control. Both default to 0 (disabled).
    continue_as_new_after_documents: int = 0
    history_event_threshold: int = 0
    # Carried state across continue-as-new boundaries. Empty = fresh run.
    completed_operations: tuple[str, ...] = ()
    produced_artifact_ids: tuple[str, ...] = ()
    documents_completed: int = 0
    workflow_run_id: str | None = None


@dataclass(frozen=True)
class ProjectProcessingResult:
    state: str
    artifact_ids: list[str] = field(default_factory=list)
    documents_total: int = 0
    documents_completed: int = 0
    error: str | None = None
    # Phase A additions. `final_status` is the workflow's outcome from
    # an operator's point of view and is the field tests should assert
    # on (`state` reflects the lower-level `WorkflowState`). Both are
    # populated; they're consistent but not redundant — `state` may be
    # `failed_final` (terminal-business) vs `failed_recoverable`
    # (unexpected exception), while `final_status` collapses both to
    # `FinalStatus.FAILED`.
    final_status: FinalStatus = FinalStatus.FAILED
    step_results: list[StepResult] = field(default_factory=list)


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
    # Phase A.5: per-stage records visible to the get_status query so
    # `GET /ingestion-jobs/{id}` can surface "what ran / was skipped /
    # failed" without waiting for workflow completion.
    step_results: list[StepResult] = field(default_factory=list)
    final_status: FinalStatus | None = None


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
        # Phase A: per-stage records aggregated into the workflow's
        # final result so operators / status endpoints / audit logs can
        # answer "what ran, what was skipped, what failed, why" without
        # re-reading workflow history. Recording sites live next to
        # each stage call (Phase A.5); A.1 only declares the slot.
        self._step_results: list[StepResult] = []
        # Phase A.4: cached scope identifiers so structured-log lines /
        # search-attribute updates don't have to dig into `request`
        # every call. Populated on first `_log_step()` and reused.
        self._scope_log_context: dict[str, str] = {}

    @workflow.run
    async def run(
        self, request: ProjectProcessingRequest
    ) -> ProjectProcessingResult:
        # Restore carried state when this is a continuation. Empty defaults
        # mean a fresh run.
        is_continuation = (
            bool(request.completed_operations)
            or request.documents_completed > 0
            or bool(request.produced_artifact_ids)
        )
        self._completed_operations = list(request.completed_operations)
        self._produced_artifact_ids = list(request.produced_artifact_ids)
        self._documents_completed = request.documents_completed

        # Phase A.4: announce workflow start with the operationally
        # interesting context — what's enabled, who asked. Lets
        # operators filter logs / Temporal UI without opening the
        # workflow input payload.
        self._log_step(
            request,
            event="ingestion.workflow.started",
            stage="workflow",
            status="running",
        )
        self._set_search_attribute("J1IngestStage", "starting")

        try:
            if not is_continuation:
                await self._validate(request)
            documents = await self._list_documents(request)
            self._documents_total = len(documents)

            # Skip documents already processed in a prior run.
            for doc_id in documents[self._documents_completed:]:
                self._set_pending(f"{OPERATION_COMPILE}:{doc_id}")
                if await self._should_stop():
                    break
                await self._process_document(request, doc_id)
                self._documents_completed += 1

                if self._should_continue_as_new(request):
                    # In real Temporal, ContinueAsNewError (a BaseException
                    # subclass) is raised here and bypasses the except clauses
                    # below; the workflow restarts with the new request.
                    workflow.continue_as_new(self._build_continuation(request))

            # Index runs once at job-end across all produced artifacts.
            # Re-use the same precedence helper used per-document:
            # caller-supplied indexer_kind always enables; planner
            # decisions only narrow when caller didn't specify.
            # (We don't have a per-document plan here; index is
            # job-scope. The planner's index decision is made per
            # document but the workflow currently runs index across
            # all artifacts in one shot, so we treat "any document's
            # plan that enabled index" as a global enable.)
            index_enabled = bool(request.indexer_kind) and bool(self._produced_artifact_ids)
            if not self._cancelled and index_enabled:
                self._set_pending(OPERATION_INDEX)
                if not await self._should_stop():
                    await self._index_all(request)
                    await self._maybe_review(request, GATE_AFTER_INDEX)
            elif not request.indexer_kind and not self._cancelled:
                self._record_step(
                    step="index",
                    status=StepStatus.SKIPPED,
                    required=False,
                    source=StepSource.CALLER,
                    reason="indexer_kind not provided in request",
                    artifact_count=len(self._produced_artifact_ids),
                )

            await self._finalize(request)

            if self._cancelled:
                self._state = WorkflowState.CANCELLED
                self._log_step(
                    request,
                    event="ingestion.workflow.cancelled",
                    stage="workflow",
                    status="cancelled",
                )
                self._set_search_attribute("J1IngestStage", "cancelled")
            else:
                self._state = WorkflowState.COMPLETED
                self._log_step(
                    request,
                    event="ingestion.workflow.completed",
                    stage="workflow",
                    status="completed",
                )
                self._set_search_attribute("J1IngestStage", "completed")
        except _BusinessRejection as exc:
            # Terminal business failure (validation, rejected approval,
            # required-step failure, etc.). Record the recoverable state
            # for `get_status` queries, run finalization for cleanup,
            # then raise so Temporal sees the workflow as Failed (not
            # Completed). Phase A core fix: previously this branch
            # *returned* a result, leaving Temporal UI showing
            # "Completed" for a workflow that internally failed.
            self._state = WorkflowState.FAILED_FINAL
            self._error = str(exc)
            self._log_step(
                request,
                event="ingestion.workflow.failed",
                stage="workflow",
                status="failed",
                reason=self._error,
                error_type=ERROR_TYPE_REQUIRED_STEP_FAILED,
            )
            self._set_search_attribute("J1IngestStage", "failed")
            await self._safe_finalize(request)
            raise ApplicationError(
                self._error,
                type=ERROR_TYPE_REQUIRED_STEP_FAILED,
                non_retryable=True,
            ) from exc
        except ApplicationError as exc:
            # Already a typed Temporal failure (e.g. raised by an
            # activity or by a deeper helper) — record state, finalize,
            # and re-raise unchanged so the original `type` /
            # `non_retryable` survive.
            self._state = WorkflowState.FAILED_FINAL
            self._error = str(exc)
            self._log_step(
                request,
                event="ingestion.workflow.failed",
                stage="workflow",
                status="failed",
                reason=self._error,
                error_type=getattr(exc, "type", None) or "ApplicationError",
            )
            self._set_search_attribute("J1IngestStage", "failed")
            await self._safe_finalize(request)
            raise
        except Exception as exc:
            # Unexpected exception — wrap in ApplicationError so
            # Temporal's failure rendering shows a clean type, and so
            # callers / status endpoints / search queries can
            # distinguish ingestion failures from infrastructure noise.
            # `non_retryable=False` means the workflow ITSELF won't be
            # auto-retried by a parent (we don't have one), but the
            # failing activity's retry policy still applies before the
            # exception reaches here.
            self._state = WorkflowState.FAILED_RECOVERABLE
            self._error = f"{type(exc).__name__}: {exc}"
            self._log_step(
                request,
                event="ingestion.workflow.failed",
                stage="workflow",
                status="failed",
                reason=self._error,
                error_type=ERROR_TYPE_UNEXPECTED_ERROR,
            )
            self._set_search_attribute("J1IngestStage", "failed")
            await self._safe_finalize(request)
            raise ApplicationError(
                self._error,
                type=ERROR_TYPE_UNEXPECTED_ERROR,
                non_retryable=False,
            ) from exc

        return ProjectProcessingResult(
            state=self._state.value,
            artifact_ids=list(self._produced_artifact_ids),
            documents_total=self._documents_total,
            documents_completed=self._documents_completed,
            error=self._error,
            final_status=self._compute_final_status(),
            step_results=list(self._step_results),
        )

    def _compute_final_status(self) -> FinalStatus:
        """Map the internal `WorkflowState` to the operator-facing
        `FinalStatus`. PARTIAL_COMPLETED requires *both* (a) all
        required steps succeeded and (b) at least one optional step
        failed — neither condition can be true today (no per-step
        required/optional taxonomy yet, no step-level recording yet),
        so the mapping is straightforward in Phase A.1. Phase A.2
        wires the optional-failure path."""
        if self._state == WorkflowState.COMPLETED:
            return FinalStatus.COMPLETED
        if self._state == WorkflowState.CANCELLED:
            return FinalStatus.CANCELLED
        # FAILED_FINAL and FAILED_RECOVERABLE both collapse to FAILED
        # at the operator boundary. The internal distinction (business
        # vs. unexpected) stays in `state` for callers that care.
        return FinalStatus.FAILED

    # ---- Operation lifecycle helpers ---------------------------------------

    def _set_pending(self, op: str) -> None:
        self._pending_operation = op

    def _begin(self, op: str) -> None:
        self._current_operation = op
        self._pending_operation = None
        # Surface "currently running" state via Temporal search
        # attributes so the UI can group/filter active workflows by
        # stage. Best-effort — fails silently if the attribute isn't
        # registered with the namespace.
        self._set_search_attribute("J1IngestStage", op)

    def _complete(self, op: str) -> None:
        self._completed_operations.append(op)
        self._current_operation = None

    # ---- Phase A.4: structured logging + search attributes ------------

    def _scope_context(self, request: ProjectProcessingRequest) -> dict[str, str]:
        """Build the standard log-context dict for this run. Cached after
        first call so each log line doesn't re-derive it. Only operationally
        safe fields — never document content."""
        if not self._scope_log_context:
            self._scope_log_context = {
                "tenant_id": request.scope.tenant_id,
                "project_id": request.scope.project_id,
                "compiler_kind": request.compiler_kind,
                "enricher_kind": request.enricher_kind or "",
                "graph_builder_kind": request.graph_builder_kind or "",
                "indexer_kind": request.indexer_kind or "",
                "correlation_id": request.correlation_id or "",
            }
        return self._scope_log_context

    def _log_step(
        self,
        request: ProjectProcessingRequest,
        *,
        event: str,
        stage: str,
        status: str,
        document_id: str | None = None,
        reason: str | None = None,
        error_type: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Emit a single structured workflow log line.

        `event` is the canonical name (e.g. `ingestion.step.started`)
        operators / log aggregators filter on. The rest go in `extra`
        so JSON-encoding loggers pick them up as top-level fields.

        Field hygiene: never log document content, file paths, prompts,
        or LLM responses here. Stage / kind / id / reason / error type
        are all operationally safe."""
        ctx = self._scope_context(request)
        payload: dict[str, object] = {
            "event": event,
            "stage": stage,
            "status": status,
            **ctx,
        }
        if document_id is not None:
            payload["document_id"] = document_id
        if reason is not None:
            payload["reason"] = reason
        if error_type is not None:
            payload["error_type"] = error_type
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        # `workflow.logger` is replay-safe (it deduplicates lines on
        # replay) and routes through the user-supplied LoggerAdapter,
        # so a deployment-side JSON formatter sees the `extra` keys
        # as native fields. Wrapped in try/except because the logger's
        # `isEnabledFor` consults the workflow runtime — outside a
        # real Temporal worker (e.g. unit tests driving `run()`
        # directly via `asyncio.run`) that runtime isn't available.
        # Logging is observability, never correctness; silently
        # degrade rather than fail.
        try:
            workflow.logger.info(event, extra=payload)
        except Exception:  # noqa: BLE001 — observability must not block ingest
            pass

    def _record_step(
        self,
        *,
        step: str,
        status: StepStatus,
        required: bool,
        source: StepSource,
        reason: str | None = None,
        error: StepError | None = None,
        artifact_count: int = 0,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Append a StepResult to the workflow's per-stage record.

        Source defaults follow Phase A semantics (caller-supplied
        kinds → `CALLER`; defaults from capabilities → `DEFAULT`;
        config-disabled → `CONFIG`). Phase B will add `PLANNER`/
        `POLICY` sources without changing this helper's signature."""
        try:
            now = workflow.now()
        except Exception:  # noqa: BLE001 — outside Temporal runtime
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
        self._step_results.append(StepResult(
            step=step,
            status=status,
            required=required,
            source=source,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            reason=reason,
            error=error,
            artifact_count=artifact_count,
            metadata=metadata or {},
        ))

    def _set_search_attribute(self, name: str, value: str) -> None:
        """Best-effort search-attribute upsert.

        Search attributes must be pre-registered with the Temporal
        namespace (via `temporal operator search-attribute create`).
        If they're not — the typical state in fresh dev clusters — the
        upsert raises. Catch and continue: visibility is a nice-to-have,
        not a correctness requirement.

        Uses the typed `SearchAttributeKey` API (the dict form is
        deprecated as of Temporal Python SDK 1.x). All J1 ingestion
        attributes are `keyword` type (lower-cardinality, exact-match
        filterable in the UI)."""
        try:
            from temporalio.common import SearchAttributeKey
            key = SearchAttributeKey.for_keyword(name)
            workflow.upsert_search_attributes([key.value_set(value)])
        except Exception:  # noqa: BLE001 — visibility never blocks ingest
            # Intentionally silent. Operators who want this signal can
            # register the attributes; everyone else gets no spam.
            pass

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

    async def _build_plan(
        self,
        request: ProjectProcessingRequest,
        document_id: str,
    ) -> IngestPlan:
        """Run the profiling activity and feed the result through the
        planner. Pure side-effect-free planning happens in-workflow;
        only the I/O-bound profile call goes through an activity.

        Caller-supplied kinds become caller_overrides — the planner
        sees them as forced-enable for that step (the legacy
        "kind is set" semantics). Stages without a kind on the
        request are left to the planner's mode-driven decision."""
        profile: DocumentProfile = await workflow.execute_activity_method(
            ProfilingActivities.profile_document,
            ProfileDocumentInput(
                scope=request.scope,
                document_id=document_id,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY.to_temporal(),
        )
        # Caller-supplied kinds → forced-enable. Compile is treated
        # as always set (request.compiler_kind has a default fallback
        # at the REST adapter layer).
        overrides: dict[str, bool] = {STEP_COMPILE: True}
        if request.enricher_kind:
            overrides[STEP_ENRICH] = True
        if request.graph_builder_kind:
            overrides[STEP_GRAPH] = True
        if request.indexer_kind:
            overrides[STEP_INDEX] = True

        # Available steps reflect what the deployment has registered;
        # for now infer from caller-supplied kinds + always include
        # compile (mandatory in the legacy model). Phase B+ may pass
        # this in via ProcessingCapabilities.
        available_steps = frozenset({STEP_COMPILE})
        if request.enricher_kind:
            available_steps |= {STEP_ENRICH}
        if request.graph_builder_kind:
            available_steps |= {STEP_GRAPH}
        if request.indexer_kind:
            available_steps |= {STEP_INDEX}

        planner = DefaultIngestPlanner()
        return planner.plan(
            profile,
            policy=request.policy,
            available_steps=available_steps,
            caller_overrides=overrides,
        )

    def _stage_enabled(
        self, plan: IngestPlan | None, stage: str, request_kind: str | None,
    ) -> tuple[bool, str | None, StepSource]:
        """Resolve "should this stage run?" with consistent precedence.

        Returns `(enabled, skip_reason, source)`. The reason is None
        when enabled; populated when skipped so the workflow can pass
        it to `_record_step`.

        Order of precedence (highest first):
          1. No `request_kind` → stage is unrunnable (no adapter chosen)
             → skip with `source=CALLER`.
          2. Plan is None (planner disabled) → run if request_kind set.
          3. Plan says step is enabled → run; source=PLANNER (or CALLER
             if caller-overridden).
          4. Plan says step is skipped → skip; source carried from
             plan.step.source (typically PLANNER)."""
        if not request_kind:
            return False, f"{stage}_kind not provided in request", StepSource.CALLER
        if plan is None:
            return True, None, StepSource.CALLER
        step = plan.step(stage)
        if step is None:
            # Stage isn't in the plan — defaults to caller-driven.
            return True, None, StepSource.CALLER
        if step.enabled:
            return True, None, step.source
        return False, step.reason, step.source

    async def _process_document(
        self, request: ProjectProcessingRequest, document_id: str
    ) -> None:
        # Phase B: when planner is enabled, build the plan up-front so
        # every stage gate consults the same authoritative decision.
        plan: IngestPlan | None = None
        if request.planner_enabled:
            plan = await self._build_plan(request, document_id)
            self._set_search_attribute("J1IngestMode", plan.mode.value)
            self._log_step(
                request,
                event="ingestion.plan.created",
                stage="plan",
                status="completed",
                document_id=document_id,
                reason=f"mode={plan.mode.value} policy={plan.policy.value}",
            )

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
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        )
        if compile_result.status != "succeeded":
            self._record_step(
                step="compile",
                status=StepStatus.FAILED,
                required=True,
                source=StepSource.CALLER,
                reason=compile_result.error or "compile activity returned non-succeeded status",
                error=StepError(
                    type="ActivityFailure",
                    message=compile_result.error or "unspecified",
                    retryable=False,
                ),
                metadata={"document_id": document_id},
            )
            raise _BusinessRejection(
                f"compile failed for {document_id}: {compile_result.error}"
            )
        self._produced_artifact_ids.extend(compile_result.artifact_ids)
        self._record_step(
            step="compile",
            status=StepStatus.COMPLETED,
            required=True,
            source=StepSource.CALLER,
            artifact_count=len(compile_result.artifact_ids),
            metadata={"document_id": document_id},
        )
        self._complete(compile_op)

        await self._maybe_review(request, GATE_AFTER_COMPILE)
        if self._cancelled:
            return

        # Stage gate: planner (if enabled) and request kind together
        # decide whether enrich runs. `_stage_enabled` codifies the
        # precedence rules (caller > planner > default).
        enrich_enabled, enrich_reason, enrich_source = self._stage_enabled(
            plan, "enrich", request.enricher_kind,
        )
        if not enrich_enabled:
            self._record_step(
                step="enrich",
                status=StepStatus.SKIPPED,
                required=False,
                source=enrich_source,
                reason=enrich_reason,
                metadata={"document_id": document_id},
            )

        if enrich_enabled:
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
                    self._record_step(
                        step="enrich",
                        status=StepStatus.FAILED,
                        # Caller asked for this enricher → required
                        # (Phase B may relax for planner-driven optional).
                        required=True,
                        source=StepSource.CALLER,
                        reason=enrich_result.error or "enrich activity returned non-succeeded status",
                        error=StepError(
                            type="ActivityFailure",
                            message=enrich_result.error or "unspecified",
                            retryable=False,
                        ),
                        metadata={"artifact_id": artifact_id},
                    )
                    raise _BusinessRejection(
                        f"enrich failed for {artifact_id}: {enrich_result.error}"
                    )
                self._produced_artifact_ids.extend(enrich_result.artifact_ids)
                self._record_step(
                    step="enrich",
                    status=StepStatus.COMPLETED,
                    required=True,
                    source=StepSource.CALLER,
                    artifact_count=len(enrich_result.artifact_ids),
                    metadata={"artifact_id": artifact_id},
                )
                self._complete(enrich_op)
            await self._maybe_review(request, GATE_AFTER_ENRICH)
            if self._cancelled:
                return

        graph_enabled, graph_reason, graph_source = self._stage_enabled(
            plan, "graph", request.graph_builder_kind,
        )
        if not graph_enabled:
            self._record_step(
                step="graph",
                status=StepStatus.SKIPPED,
                required=False,
                source=graph_source,
                reason=graph_reason,
                metadata={"document_id": document_id},
            )

        if graph_enabled:
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
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=DEFAULT_RETRY.to_temporal(),
                )
            )
            if graph_result.status != "succeeded":
                self._record_step(
                    step="graph",
                    status=StepStatus.FAILED,
                    required=True,
                    source=StepSource.CALLER,
                    reason=graph_result.error or "build_graph activity returned non-succeeded status",
                    error=StepError(
                        type="ActivityFailure",
                        message=graph_result.error or "unspecified",
                        retryable=False,
                    ),
                )
                raise _BusinessRejection(
                    f"build_graph failed: {graph_result.error}"
                )
            self._produced_artifact_ids.extend(graph_result.artifact_ids)
            self._record_step(
                step="graph",
                status=StepStatus.COMPLETED,
                required=True,
                source=StepSource.CALLER,
                artifact_count=len(graph_result.artifact_ids),
            )
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
            self._record_step(
                step="index",
                status=StepStatus.FAILED,
                required=True,
                source=StepSource.CALLER,
                reason=index_result.error or "index activity returned non-succeeded status",
                error=StepError(
                    type="ActivityFailure",
                    message=index_result.error or "unspecified",
                    retryable=False,
                ),
                artifact_count=len(self._produced_artifact_ids),
            )
            raise _BusinessRejection(f"index failed: {index_result.error}")
        self._record_step(
            step="index",
            status=StepStatus.COMPLETED,
            required=True,
            source=StepSource.CALLER,
            artifact_count=len(self._produced_artifact_ids),
        )
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

    # ---- Continue-as-new --------------------------------------------------

    def _should_continue_as_new(
        self, request: ProjectProcessingRequest
    ) -> bool:
        """Return True if the workflow should continue-as-new now.

        Two thresholds (both opt-in):
          * `continue_as_new_after_documents`: trigger every N documents.
          * `history_event_threshold`: trigger when Temporal's recorded history
            length crosses N events. Falls back to False outside a workflow
            runtime (e.g., direct unit tests).
        """
        if (
            request.continue_as_new_after_documents > 0
            and self._documents_completed > 0
            and self._documents_completed % request.continue_as_new_after_documents == 0
        ):
            return True
        if request.history_event_threshold > 0:
            try:
                history_length = workflow.info().get_current_history_length()
            except Exception:
                # `workflow.info()` raises outside a workflow event loop
                # (e.g., direct unit tests). Threshold is unreachable then.
                return False
            return history_length >= request.history_event_threshold
        return False

    def _build_continuation(
        self, request: ProjectProcessingRequest
    ) -> ProjectProcessingRequest:
        """Compact carry-forward state for `workflow.continue_as_new`.

        Carries IDs, counters, and flags only — never artifact bytes or
        document content. Big payloads stay in J1 storage and are referenced
        by ID after restart.
        """
        return _replace_request(
            request,
            completed_operations=tuple(self._completed_operations),
            produced_artifact_ids=tuple(self._produced_artifact_ids),
            documents_completed=self._documents_completed,
            workflow_run_id=self._continuation_run_id(request),
        )

    def _continuation_run_id(
        self, request: ProjectProcessingRequest
    ) -> str | None:
        if request.workflow_run_id:
            return request.workflow_run_id
        try:
            return workflow.info().workflow_id
        except Exception:
            # Outside a workflow event loop — fall back to correlation_id.
            return request.correlation_id

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
        # Phase A.5: surface step_results so the status endpoint can
        # show "what ran / was skipped / failed" without waiting for
        # workflow completion. `final_status` is None while the
        # workflow is in progress; populated only on terminal exit.
        final_status = (
            None if self._state == WorkflowState.RUNNING
            else self._compute_final_status()
        )
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
            step_results=list(self._step_results),
            final_status=final_status,
        )
