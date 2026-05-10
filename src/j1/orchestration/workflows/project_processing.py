from dataclasses import dataclass, field, replace as _replace_request
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

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
        InsertContentActivityInput,
        PersistErrorReportInput,
        PersistFinalSummaryInput,
        PersistValidationReportInput,
        ProcessingActivityResult,
        ProjectScope,
        SetDocumentStatusInput,
        SpendSummary,
        ValidateContextResult,
    )
    from j1.orchestration.activities.planning import (
        ACTIVITY_BUILD_PLANNING_RESULT,
        BuildPlanningResultInput,
        BuildPlanningResultOutput,
        PlanningActivities,
    )
    from j1.orchestration.activities.processing import ProcessingActivities
    from j1.orchestration.activities.profiling import (
        ProfileDocumentInput,
        ProfilingActivities,
    )
    from j1.orchestration.activities.project import ProjectActivities
    from j1.orchestration.activities.runs import (
        ReportPlanGeneratedInput,
        ReportPlanRevisedInput,
        ReportRunTerminalInput,
        ReportStepLifecycleInput,
        ReportStepSkippedInput,
        RunsActivities,
        StepSummaryEntry,
    )
    from j1.orchestration.errors import (
        ERROR_TYPE_REQUIRED_STEP_FAILED,
        ERROR_TYPE_UNEXPECTED_ERROR,
    )
    from j1.orchestration.temporal.retries import COMPILE_RETRY, DEFAULT_RETRY
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
    from j1.jobs.status import ProcessingStatus
    from j1._serialization import to_jsonable


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
# Long-running activities (compile, enrich, build_graph, index) wrap
# the synchronous work in a background heartbeat ticker (see
# `j1.orchestration.activities.processing._heartbeating`) that emits
# `activity.heartbeat` every 30 s. `HEARTBEAT_TIMEOUT` is therefore
# the LIVENESS budget — the worker has to ping at least once per
# this window or Temporal fails the attempt and the retry policy
# kicks in. We tune it generously enough to absorb GIL contention,
# brief network hiccups, and a slow-loading model, while still
# detecting genuine worker death within minutes.
HEARTBEAT_TIMEOUT = timedelta(minutes=5)
# Compile-stage timeout. Real PDFs through MinerU + raganything can
# legitimately take many minutes. Generous ceiling so the activity
# isn't killed mid-parse on the worst documents — the heartbeat
# ticker is the real liveness check; this is the absolute upper
# bound on a single attempt.
COMPILE_ACTIVITY_TIMEOUT = timedelta(hours=1)

OPERATION_VALIDATE = "validate"
OPERATION_LIST_DOCUMENTS = "list_documents"
OPERATION_COMPILE = "compile"
OPERATION_INSERT_CONTENT = "insert_content"
OPERATION_ENRICH = "enrich"
OPERATION_BUILD_GRAPH = "build_graph"
OPERATION_INDEX = "index"
OPERATION_FINALIZE = "finalize"
OPERATION_BUDGET_CHECK = "budget_check"
OPERATION_REVIEW_GATE = "review_gate"

# Pipeline mode values mirrored from `RAGAnythingSettings`. The
# workflow code branches on these strings to decide whether to invoke
# the split-mode `insert_content` activity. Hard-coding the literals
# avoids pulling the raganything-specific module into a workflow
# that should stay provider-agnostic.
PIPELINE_MODE_COMPLETE = "complete"
PIPELINE_MODE_SPLIT_PARSE_INSERT = "split_parse_insert"

# Temporal search-attribute names. Must match the registrations in
# `deploy/dev/docker-compose.yml` (temporal-init service) — writing
# an unregistered attribute crashes workflow activation when
# `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true`.
SEARCH_ATTR_INGEST_STAGE = "J1IngestStage"
SEARCH_ATTR_INGEST_MODE = "J1IngestMode"
SEARCH_ATTR_DOCUMENT_ID = "J1DocumentId"
SEARCH_ATTR_WORKSPACE_ID = "J1WorkspaceId"
SEARCH_ATTR_PARSER_NAME = "J1ParserName"
SEARCH_ATTR_REQUIRES_VISION = "J1RequiresVision"
SEARCH_ATTR_REQUIRES_PREMIUM_LLM = "J1RequiresPremiumLLM"

# Values written into `SEARCH_ATTR_INGEST_STAGE`. Operators filter
# Temporal histories on these — keep in sync with the values quoted
# in deployment runbooks.
INGEST_STAGE_STARTING = "starting"
INGEST_STAGE_CANCELLED = "cancelled"
INGEST_STAGE_COMPLETED = "completed"
INGEST_STAGE_FAILED = "failed"

# Workflow signal names. Must match the `@workflow.signal`-decorated
# method names on `ProjectProcessingWorkflow` since Temporal infers
# the signal name from the Python identifier. Senders elsewhere in
# the codebase (`integration/services.py`) MUST use these constants
# rather than re-spelling the strings — a typo silently sends the
# signal to nowhere.
SIGNAL_PAUSE = "pause"
SIGNAL_RESUME = "resume"
SIGNAL_CANCEL = "cancel"
SIGNAL_APPROVE_BUDGET = "approve_budget"
SIGNAL_REJECT_BUDGET = "reject_budget"
SIGNAL_APPROVE_REVIEW = "approve_review"
SIGNAL_REJECT_REVIEW = "reject_review"


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
    # Restrict the workflow to specific document IDs. When non-empty,
    # the workflow processes ONLY these documents and skips
    # `list_pending_documents` entirely. The user-facing
    # `POST /ingestion-runs` flow uses this to scope each upload to
    # the document that was just registered, instead of re-processing
    # every PENDING document in the project. Empty (default) keeps
    # the legacy bulk-job behaviour.
    target_document_ids: tuple[str, ...] = ()
    # How the workflow reacts to step failures. Defaults to fail_fast,
    # which preserves the historical "any failure fails the workflow"
    # behaviour. `continue_optional` permits PARTIAL_COMPLETED when
    # optional steps fail; `best_effort` permits it for required steps.
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    # Adaptive ingestion planning toggle. When False (default), the
    # workflow uses the "kind is None → skip" gate logic and behaves
    # exactly as it did before adaptive planning landed. When True,
    # each document is compiled first (compile is always required),
    # then profiled and run through `DefaultIngestPlanner` using the
    # parser's content signals; the resulting `IngestPlan` decides
    # which of the LLM-expensive stages (enrich / graph / index) to
    # actually attempt. Caller-supplied kinds always override planner
    # decisions (caller wins).
    planner_enabled: bool = False
    # Policy fed to the planner. Only consulted when `planner_enabled`.
    policy: IngestPolicy = IngestPolicy.AUTO
    # Temporal search-attribute upserts. Default OFF because the
    # cluster rejects upserts for attributes that aren't registered
    # with the namespace, and the rejection happens at workflow-
    # activation completion (server-side) — the SDK's exception
    # surfaces AFTER the workflow code returns, so a try/except in
    # the workflow can't catch it. Operators who want this signal
    # must (1) register the attributes via
    # `temporal operator search-attribute create --name J1IngestStage
    # --type Keyword` (and the same for J1IngestMode), and (2) flip
    # this flag to True. Until then the workflow silently skips the
    # upsert calls.
    search_attributes_enabled: bool = False
    # Continue-as-new control. Both default to 0 (disabled).
    continue_as_new_after_documents: int = 0
    history_event_threshold: int = 0
    # Carried state across continue-as-new boundaries. Empty = fresh run.
    completed_operations: tuple[str, ...] = ()
    produced_artifact_ids: tuple[str, ...] = ()
    documents_completed: int = 0
    workflow_run_id: str | None = None
    # Domain pack selection.
    #   `domain_override` — operator's per-upload choice. None
    #   means "no override; let the workflow apply the workspace
    #   default + auto-detect chain". `workspace_default_domain`
    #   carries the workspace/project default. Both are validated
    #   against the deployment's allow-list inside the planning
    #   activity; an unrecognised value falls back to `general`
    #   with a warning recorded on `domain_context`.
    domain_override: str | None = None
    workspace_default_domain: str | None = None
    # Pipeline mode for the compile/insert split. Mirrors
    # `RAGAnythingSettings.pipeline_mode` and is propagated by the
    # REST adapter from raganything settings. Two values are
    # recognised:
    #   * "complete" (default) — the compile activity runs the
    #     legacy single-shot `process_document_complete` path; no
    #     separate insert step; chunk+graph artifacts are produced
    #     by compile itself.
    #   * "split_parse_insert" — the compile activity parses ONLY
    #     and registers a `parsed_source` artifact; the workflow
    #     then runs `insert_content` (RAGAnything.insert_content_list)
    #     after the post-compile planning step. Chunk artifacts come
    #     from insert; the upstream `parsed_source` is what unlocks
    #     the Content Inventory tab.
    # Defaults to "complete" so existing tests + deployments keep
    # working unchanged. Production opts into split mode via
    # `J1_RAGANYTHING_PIPELINE_MODE=split_parse_insert`.
    pipeline_mode: str = "complete"
    # Resume-from-checkpoint context. Empty (default) = fresh run.
    # When set, the workflow honours the carry-forward artifact lists
    # at startup and skips activities for steps named in
    # `resume_completed_steps` (limited to the LLM-cost stages —
    # see `RESUMABLE_STAGES`). The new run gets its own correlation_id;
    # `resume_from_run_id` only points back to the prior attempt for
    # audit / FE-rendering of the relationship.
    resume_from_run_id: str | None = None
    resume_completed_steps: tuple[str, ...] = ()
    resume_artifact_ids: tuple[str, ...] = ()
    resume_artifact_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectProcessingResult:
    state: str
    artifact_ids: list[str] = field(default_factory=list)
    documents_total: int = 0
    documents_completed: int = 0
    error: str | None = None
    # `final_status` is the workflow's outcome from an operator's
    # point of view and is the field tests should assert on (`state`
    # reflects the lower-level `WorkflowState`). Both are populated;
    # they're consistent but not redundant — `state` may be
    # `failed_final` (terminal-business) vs `failed_recoverable`
    # (unexpected exception), while `final_status` collapses both to
    # `FinalStatus.FAILED`. `step_results` is the per-stage audit:
    # what ran, what was skipped (with reason + source), what failed.
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
    # Per-stage records visible to the get_status query so
    # `GET /ingestion-jobs/{id}` can surface "what ran / was skipped /
    # failed" without waiting for workflow completion.
    step_results: list[StepResult] = field(default_factory=list)
    final_status: FinalStatus | None = None


class _BusinessRejection(Exception):
    """Internal sentinel for terminal business failures (rejected approvals, validation, activity errors).

    Why: lets the workflow distinguish business rejections (FAILED_FINAL) from unexpected
    exceptions (FAILED_RECOVERABLE) without exposing the distinction to callers.
    """


def _merge_compile_signals(
    profile: DocumentProfile, signals: dict
) -> DocumentProfile:
    """Return a new `DocumentProfile` with parser-observed signals
    overlaid on the deterministic profile.

    Compile-time signals are authoritative because the parser inspects
    block-level structure; the deterministic profiler only saw the
    file from the outside. Keys recognised: `has_images`, `has_tables`,
    `has_scanned_pages`, `page_count`, `text_extractable_ratio`. Any
    other keys are ignored — `_artifact_result` already filters the
    activity payload, but we re-filter here so a stale audit log or
    a third-party processor with a richer schema doesn't leak fields
    into the profile."""
    overrides: dict = {}
    if "has_images" in signals:
        overrides["has_images"] = bool(signals["has_images"])
    if "has_tables" in signals:
        overrides["has_tables"] = bool(signals["has_tables"])
    if "has_scanned_pages" in signals:
        overrides["has_scanned_pages"] = bool(signals["has_scanned_pages"])
    if "page_count" in signals and signals["page_count"] is not None:
        overrides["page_count"] = int(signals["page_count"])
    if (
        "text_extractable_ratio" in signals
        and signals["text_extractable_ratio"] is not None
    ):
        overrides["text_extractable_ratio"] = float(
            signals["text_extractable_ratio"]
        )
    # Manifest signals — post-parse counts + quality scores. Each is
    # optional so a parser that doesn't surface them leaves the
    # corresponding field None on the profile (the planner already
    # treats None as "I don't know").
    for int_key in (
        "image_count", "table_count", "equation_count",
        "text_block_count", "total_text_chars",
    ):
        if int_key in signals and signals[int_key] is not None:
            overrides[int_key] = int(signals[int_key])
    for float_key in (
        "empty_page_ratio",
        "parse_quality_score",
        "text_sufficiency_score",
        "layout_complexity_score",
    ):
        if float_key in signals and signals[float_key] is not None:
            overrides[float_key] = float(signals[float_key])
    # Per-image list — coerce to tuple so the frozen dataclass can
    # hold it. Each entry stays a dict (Temporal-data-converter
    # serialisable) so we don't need a parallel dataclass.
    images = signals.get("images")
    if isinstance(images, list):
        overrides["images"] = tuple(
            dict(item) for item in images if isinstance(item, dict)
        )
    if not overrides:
        return profile
    return _replace_request(profile, **overrides)


def _summarise_plan_diff(
    initial: "IngestPlan", revised: "IngestPlan",
) -> dict[str, Any]:
    """Compute a minimal step-enablement diff between two plans.

    Returns an empty dict when no step's enabled flag changed —
    callers treat that as "no observable change, skip the audit
    event". Otherwise returns a dict keyed by step name with
    `{before, after}` booleans, plus the new mode if it shifted.

    The diff intentionally ignores fields that don't change
    pipeline behaviour (confidence numbers, vision_decisions detail,
    reasons): we want a coarse "does this revision unlock a step?"
    signal, not a deep equality check.
    """
    out: dict[str, Any] = {}
    initial_steps = {s.name: s for s in initial.steps}
    revised_steps = {s.name: s for s in revised.steps}
    for name in sorted(set(initial_steps) | set(revised_steps)):
        before = initial_steps.get(name)
        after = revised_steps.get(name)
        before_enabled = bool(before and before.enabled)
        after_enabled = bool(after and after.enabled)
        if before_enabled != after_enabled:
            out[name] = {"before": before_enabled, "after": after_enabled}
    if initial.mode != revised.mode:
        out["mode"] = {"before": initial.mode.value, "after": revised.mode.value}
    return out


def _profile_payload(profile: "DocumentProfile | None") -> dict[str, Any] | None:
    """Serialise a `DocumentProfile` to a Temporal-data-converter
    friendly dict for the planning activity. Lossy on tuple fields
    (e.g. `images`, `warnings`) — the planning activity only reads
    scalar signals, so we keep this compact."""
    if profile is None:
        return None
    return {
        "document_id": profile.document_id,
        "extension": profile.extension,
        "mime_type": profile.mime_type,
        "file_size_bytes": profile.file_size_bytes,
        "page_count": profile.page_count,
        "text_extractable_ratio": profile.text_extractable_ratio,
        "has_images": profile.has_images,
        "has_tables": profile.has_tables,
        "has_scanned_pages": profile.has_scanned_pages,
        "estimated_tokens": profile.estimated_tokens,
        "language": profile.language,
        "parser_confidence": profile.parser_confidence,
        "parse_quality_score": profile.parse_quality_score,
        "text_sufficiency_score": profile.text_sufficiency_score,
        "layout_complexity_score": profile.layout_complexity_score,
    }


def _apply_post_compile_planning(
    plan: "IngestPlan", planning: "BuildPlanningResultOutput",
) -> tuple["IngestPlan", dict[str, Any]]:
    """Overlay the post-compile planning result onto the existing
    `IngestPlan` and return the updated plan plus a diff dict.

    The post-compile planning result speaks `execution_plan.steps`
    (rich shape with reasons / scopes) but the workflow's gating logic
    only consults `IngestPlan.steps[*].enabled`. Translate the rich
    decisions into per-step enable/disable overrides — this is the
    minimal safe wiring that lets the downstream gates honor the
    planner's recommendations without rewriting `_stage_enabled`.

    Mapping (post-compile → workflow step):
      * `enrich` is enabled when ANY of `table_enrichment`,
        `vision_enrichment`, `image_captioning`,
        `requirement_extraction`, `risk_extraction`,
        `quality_assessment` is enabled. Disabled when all of them
        are disabled — saves the cost of a no-op enrich call.
      * `graph` follows `graph_extraction.enabled` directly.
      * `index` follows `indexing.enabled` (defaults True).

    Returns `(new_plan, diff)`. `diff` is empty when the post-compile
    plan agrees with the initial plan; non-empty diff is suitable for
    `_format_plan_diff_reason`."""
    # Defensive: `execution_plan.steps` is contractually a dict
    # keyed by step name. LLM-emitted plans occasionally emit it as
    # a list of `{name, enabled, ...}` objects instead. Without this
    # guard the next `.get()` call raises
    # `AttributeError: 'list' object has no attribute 'get'` and the
    # whole workflow fails with `J1_INGEST_UNEXPECTED_ERROR` —
    # exactly the BUILD_CONTENT_INVENTORY-stage crash operators have
    # hit. Coerce a list to its dict equivalent (keyed by `name` /
    # `step_id`) so downstream lookups work; fall back to an empty
    # dict when the shape is genuinely unrecognised.
    raw_steps = (planning.execution_plan or {}).get("steps")
    if isinstance(raw_steps, dict):
        steps = raw_steps
    elif isinstance(raw_steps, list):
        steps = {}
        for entry in raw_steps:
            if not isinstance(entry, dict):
                continue
            key = (
                entry.get("name")
                or entry.get("step_id")
                or entry.get("id")
            )
            if key:
                steps[str(key)] = entry
    else:
        steps = {}
    enrich_drivers = (
        "table_enrichment", "vision_enrichment", "image_captioning",
        "requirement_extraction", "risk_extraction", "quality_assessment",
    )
    enrich_should_run = any(
        bool((steps.get(name) or {}).get("enabled"))
        for name in enrich_drivers
    )
    graph_entry = steps.get("graph_extraction") or {}
    graph_should_run = bool(graph_entry.get("enabled"))
    index_entry = steps.get("indexing") or {}
    index_should_run = bool(index_entry.get("enabled", True))

    desired = {
        STEP_ENRICH: enrich_should_run,
        STEP_GRAPH: graph_should_run,
        STEP_INDEX: index_should_run,
    }

    diff: dict[str, Any] = {}
    new_steps: list = []
    for step in plan.steps:
        if step.name in desired:
            target = desired[step.name]
            # Caller-supplied kinds become forced-enables in the
            # initial plan (`_build_plan` sets `overrides[name]=True`,
            # which marks `step.source=CALLER` + `step.enabled=True`).
            # The post-compile overlay must NOT silently flip those
            # back to skipped — the operator's per-run intent wins
            # over the planner's rule-based recommendation.
            if step.source == StepSource.CALLER and step.enabled and not target:
                # Caller wants this step; the planner wanted to
                # skip it. Honor the caller. The Light-/RAG-Anything
                # graph build is the canonical example — when
                # `graph_builder_kind` is set, the operator wants
                # graph artifacts surfaced even on a document the
                # rule-based planner classified as a non-graph
                # candidate.
                new_steps.append(step)
                continue
            if step.enabled != target:
                diff[step.name] = {"before": step.enabled, "after": target}
                new_steps.append(_replace_request(
                    step,
                    enabled=target,
                    required=step.required and target,
                    decision=("RUN" if target else "SKIP"),
                    reason=(
                        step.reason if target
                        else (
                            (steps.get(step.name) or {}).get("reason")
                            or step.reason
                        )
                    ),
                ))
                continue
        new_steps.append(step)

    if not diff:
        return plan, {}

    return _replace_request(plan, steps=tuple(new_steps)), diff


def _format_plan_diff_reason(diff: dict[str, Any]) -> str:
    """Render a `_summarise_plan_diff` result as a one-line reason.

    Operator-readable; lands in the audit payload's `reason` field
    so the FE timeline shows e.g.
    `Post-compile signals: graph re-enabled, mode multimodal_light`
    without bespoke FE rendering.
    """
    bits: list[str] = []
    for key, change in sorted(diff.items()):
        if not isinstance(change, dict):
            continue
        before = change.get("before")
        after = change.get("after")
        if key == "mode":
            bits.append(f"mode {before}->{after}")
        elif before is False and after is True:
            bits.append(f"{key} re-enabled")
        elif before is True and after is False:
            bits.append(f"{key} disabled")
    return "Post-compile replan: " + ", ".join(bits) if bits else "Post-compile replan"


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
        # Mirror of `_produced_artifact_ids` carrying the artifact
        # KIND for each id in the same order. Populated in lockstep
        # with every `extend` call. Used by `_validate_completion` to
        # enforce per-stage required outputs (graph step that
        # "completed" without producing a graph_json is a contract
        # violation, not a SUCCEEDED state).
        self._produced_artifact_kinds: list[str] = []
        self._error: str | None = None
        # Per-stage records aggregated into the workflow's final
        # result so operators / status endpoints / audit logs can
        # answer "what ran, what was skipped, what failed, why" without
        # re-reading workflow history. Recording sites are colocated
        # with each stage call.
        self._step_results: list[StepResult] = []
        # Cached scope identifiers so structured-log lines /
        # search-attribute updates don't have to dig into `request`
        # every call. Populated on first `_log_step()` and reused.
        self._scope_log_context: dict[str, str] = {}
        # Mirrors `request.search_attributes_enabled` once `run()` is
        # called. Default False matches the request default; only
        # flips True when the operator has registered the attributes
        # with the Temporal namespace AND explicitly opted in via the
        # request flag.
        self._search_attributes_enabled: bool = False

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
        self._search_attributes_enabled = request.search_attributes_enabled

        # Resume-from-checkpoint carry-forward. Seed the produced-
        # artifact mirrors so downstream `extend` calls layer cleanly
        # on top of the prior run's outputs and `_validate_completion`
        # sees the full kind set. The carry-forward IDs reference
        # artifacts still tagged to the prior run; the resume endpoint
        # also persists them on the new run's metadata so the FE can
        # render the lineage without walking workflow state.
        if request.resume_from_run_id and request.resume_artifact_ids:
            self._produced_artifact_ids.extend(
                list(request.resume_artifact_ids)
            )
            self._produced_artifact_kinds.extend(
                list(request.resume_artifact_kinds)
            )

        # Announce workflow start with the operationally interesting
        # context — what's enabled, who asked. Lets operators filter
        # logs / Temporal UI without opening the workflow input
        # payload.
        self._log_step(
            request,
            event="ingestion.workflow.started",
            stage="workflow",
            status="running",
        )
        self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_STARTING)

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
                try:
                    await self._process_document(request, doc_id)
                except BaseException:
                    # The document failed mid-pipeline. Flip its
                    # registry status to FAILED so a subsequent
                    # project-wide job doesn't re-pick it (otherwise
                    # the same document loops forever — registry
                    # status stays PENDING and `list_pending_documents`
                    # keeps surfacing it). Best-effort: registry
                    # writes never block the workflow's failure
                    # surface.
                    await self._mark_document_status(
                        request, doc_id, ProcessingStatus.FAILED,
                    )
                    raise
                # Successful per-document path. Mark as SUCCEEDED so
                # the next bulk job won't re-pick it. Per-stage
                # warnings/skips are recorded separately in
                # `step_results`; the document itself is "done" once
                # `_process_document` returns without raising.
                await self._mark_document_status(
                    request, doc_id, ProcessingStatus.SUCCEEDED,
                )
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
                await self._emit_step_skipped(
                    request, stage="INDEX", step="index",
                    reason="indexer_kind not provided in request",
                    source="caller",
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
                self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_CANCELLED)
                await self._emit_run_terminal(
                    request, final_status="cancelled",
                )
            else:
                # Completion validation: catch the case where the
                # workflow reached the end without any failure being
                # raised, but the required artifacts aren't actually
                # present (compile reported success but produced
                # nothing, a required step's StepResult was never
                # recorded, etc.). Without this gate the workflow
                # would mark SUCCEEDED on a degenerate run.
                validation_errors = self._validate_completion(request)
                # Persist the validation_report artifact regardless of
                # outcome — the FE artifact-listing surface needs the
                # snapshot for both success (proof of validation) and
                # failure (error detail) paths.
                await self._persist_validation_report(
                    request,
                    passed=not validation_errors,
                    errors=list(validation_errors),
                    rules_evaluated=[
                        "at_least_one_artifact_produced",
                        "required_steps_completed_or_skipped",
                        "indexer_kind_set_implies_index_step_ran",
                        "graph_step_completed_implies_graph_json_artifact",
                        "chunks_step_completed_implies_chunk_artifact",
                    ],
                )
                if validation_errors:
                    raise _BusinessRejection(
                        "completion validation failed: "
                        + "; ".join(validation_errors)
                    )
                self._state = WorkflowState.COMPLETED
                self._log_step(
                    request,
                    event="ingestion.workflow.completed",
                    stage="workflow",
                    status="completed",
                )
                self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_COMPLETED)
                # `final_status` distinguishes succeeded vs.
                # succeeded_with_warnings using the recorded
                # `step_results` warning_count semantic. Today the
                # workflow raises on any failure, so warning_count
                # is 0 in the success path; deployments adopting
                # `continue_optional` policy will populate this.
                final_status = "succeeded_with_warnings" if self._warning_count() > 0 else "succeeded"
                # Persist the final_summary artifact at successful
                # terminal — single canonical artifact summarising
                # the run for the FE / operators.
                await self._persist_final_summary(
                    request,
                    final_status=final_status,
                    warning_count=self._warning_count(),
                )
                await self._emit_run_terminal(
                    request, final_status=final_status,
                    warning_count=self._warning_count(),
                )
        except _BusinessRejection as exc:
            # Terminal business failure (validation, rejected approval,
            # required-step failure, etc.). Record the recoverable state
            # for `get_status` queries, run finalization for cleanup,
            # then raise so Temporal sees the workflow as Failed (not
            # Completed). Earlier versions of this branch *returned* a
            # result with `state="failed_final"`, leaving Temporal UI
            # showing "Completed" for a workflow that internally
            # failed — the false-success bug this raise fixes.
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
            self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_FAILED)
            # Persist the failure-path `error_report` artifact so the
            # FE artifact-listing surface carries the failure detail
            # under the run, alongside whatever partial artifacts the
            # earlier stages produced. Best-effort — any persistence
            # error is logged inside the activity and we proceed
            # regardless.
            await self._persist_error_report(
                request,
                failure_code=ERROR_TYPE_REQUIRED_STEP_FAILED,
                failure_message=self._error,
            )
            # Failed runs also get a `final_summary` artifact so the
            # FE has a single canonical run-outcome artifact for
            # both success and failure paths.
            await self._persist_final_summary(
                request,
                final_status="failed",
                failure_code=ERROR_TYPE_REQUIRED_STEP_FAILED,
                failure_message=self._error,
            )
            await self._safe_finalize(request)
            await self._emit_run_terminal(
                request, final_status="failed",
                failure_code=ERROR_TYPE_REQUIRED_STEP_FAILED,
                failure_message=self._error,
            )
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
            self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_FAILED)
            await self._safe_finalize(request)
            await self._emit_run_terminal(
                request, final_status="failed",
                failure_code=getattr(exc, "type", None) or "ApplicationError",
                failure_message=self._error,
            )
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
            self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_FAILED)
            await self._safe_finalize(request)
            await self._emit_run_terminal(
                request, final_status="failed",
                failure_code=ERROR_TYPE_UNEXPECTED_ERROR,
                failure_message=self._error,
            )
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
        `FinalStatus`. PARTIAL_COMPLETED is returned when (a) all
        required steps succeeded AND (b) at least one optional step
        is recorded as FAILED — same semantic as `_warning_count()`.

        Today the workflow is `fail_fast` everywhere so the optional
        path mostly stays unused, BUT activities / planner-skipped
        stages can still record `StepStatus.FAILED` on optional
        steps without aborting the workflow. When that happens, the
        run completes successfully overall but with warnings — and
        PARTIAL_COMPLETED is the correct external label so the FE
        can flip the run header to SUCCEEDED_WITH_WARNINGS."""
        if self._state == WorkflowState.COMPLETED:
            if self._warning_count() > 0:
                return FinalStatus.PARTIAL_COMPLETED
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
        self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, op)

    def _complete(self, op: str) -> None:
        self._completed_operations.append(op)
        self._current_operation = None

    # ---- Structured logging + Temporal search attributes -------------

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

        Source defaults: caller-supplied kinds → `CALLER`; defaults
        from capabilities → `DEFAULT`; config-disabled → `CONFIG`.
        When the planner is enabled, `_stage_enabled` substitutes
        `PLANNER` / `POLICY` for stages whose decision was made by
        the plan rather than the caller — the helper signature is
        unchanged."""
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

    # ---- Run-terminal progress events (via activity) ----------------

    def _warning_count(self) -> int:
        """Count step results in WARNING / FAILED-but-non-fatal state.

        Today the workflow is `fail_fast` everywhere, so the count is
        always 0 when the workflow reaches the success path — but the
        helper exists so deployments adopting `continue_optional`
        policy can populate it without a workflow signature change."""
        count = 0
        for r in self._step_results:
            if r.status == StepStatus.FAILED and not r.required:
                count += 1
        return count

    def _validate_completion(
        self, request: ProjectProcessingRequest,
    ) -> list[str]:
        """Last-mile gate: don't mark SUCCEEDED unless the required
        artifacts are actually present.

        The workflow's per-step error handling already raises on a
        failed required step (the `fail_fast` policy), so most paths
        never reach this validator. It catches the degenerate cases:
          * compile reported success but produced ZERO artifacts (the
            parser may have silently no-oped on an unsupported MIME);
          * a required step's `StepResult` was never recorded because
            of a coding-level miss in a new branch;
          * the workflow drained all documents but produced nothing
            indexable;
          * `indexer_kind` was set, artifacts were produced, but no
            index `StepResult` exists — the document would be reported
            SUCCEEDED while remaining unsearchable.

        Returns a list of human-readable validation errors. An empty
        list = OK, callers proceed to the SUCCEEDED transition. Any
        entries cause the caller to raise `_BusinessRejection` and
        the workflow is marked FAILED with `J1_INGEST_COMPLETION_VALIDATION_FAILED`.

        Cheap to call; no I/O — pure inspection of in-memory state."""
        errors: list[str] = []
        # Required = at least one artifact got produced. Catches the
        # compile-no-op-and-falls-through case.
        if not self._produced_artifact_ids:
            errors.append(
                "no artifacts were produced; the workflow ran but "
                "compile/enrich/graph/index returned nothing indexable"
            )
        # Required steps recorded as anything other than COMPLETED at
        # this point are a contract bug — fail_fast should have raised
        # earlier. Surface explicitly so the operator sees what slipped.
        for r in self._step_results:
            if r.required and r.status not in (
                StepStatus.COMPLETED, StepStatus.SKIPPED,
            ):
                errors.append(
                    f"required step {r.step!r} ended in status "
                    f"{r.status.value!r} without aborting the workflow"
                )
        # When INDEX was requested AND artifacts were produced, the
        # workflow MUST have recorded an index step. Without this the
        # job exits SUCCEEDED but the search index never received the
        # artifacts — the run looks green but search returns nothing.
        if request.indexer_kind and self._produced_artifact_ids:
            saw_index = any(r.step == "index" for r in self._step_results)
            if not saw_index:
                errors.append(
                    "indexer_kind is set and artifacts were produced, "
                    "but no index step ran; the run would be reported "
                    "SUCCEEDED while remaining unsearchable"
                )

        # Per-stage required-output rules. These catch the "step
        # reported COMPLETED but produced no canonical artifact" case
        # — a regression that the fail-fast handler can't see because
        # the activity returned status="succeeded" with an empty
        # `artifact_ids` list. We compare against
        # `_produced_artifact_kinds` (a strict mirror of
        # `_produced_artifact_ids` populated in lockstep with each
        # `extend()` call) so the rules don't need a registry query.
        kinds = set(self._produced_artifact_kinds)
        for r in self._step_results:
            if r.status != StepStatus.COMPLETED:
                continue
            if r.step == "graph" and "graph_json" not in kinds:
                # graph step said it succeeded but no graph_json
                # artifact landed — the canonical graph output is
                # missing. Surface so the run fails closed instead of
                # being reported as a working graph build.
                errors.append(
                    "graph step recorded as completed but no "
                    "`graph_json` artifact was produced"
                )
            elif r.step == "generate_knowledge_chunks" and "chunk" not in kinds:
                # In split_parse_insert mode, the chunks step is the
                # real boundary; in legacy `complete` mode chunks are
                # produced by compile and labelled as a synthetic
                # generate_knowledge_chunks step (skip the rule via
                # metadata.synthetic). The check honours that escape
                # hatch so we don't false-flag legacy runs.
                synthetic = bool(
                    isinstance(r.metadata, dict)
                    and r.metadata.get("synthetic")
                )
                if not synthetic:
                    errors.append(
                        "generate_knowledge_chunks step recorded as "
                        "completed but no `chunk` artifact was produced"
                    )
        return errors

    def _step_summary_payload(self) -> tuple[StepSummaryEntry, ...]:
        """Compact summary embedded in `run.completed` / `run.failed`
        events so the frontend can render a "what ran" recap without
        re-fetching `/events`."""
        return tuple(
            StepSummaryEntry(
                step=r.step,
                status=r.status.value,
                required=r.required,
                source=r.source.value,
                reason=r.reason,
                artifact_count=r.artifact_count,
            )
            for r in self._step_results
        )

    async def _emit_run_terminal(
        self,
        request: ProjectProcessingRequest,
        *,
        final_status: str,
        warning_count: int = 0,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """Schedule the `j1.runs.report_terminal` activity. Best-effort
        — telemetry must not block the workflow's exit. Skipped when
        the request didn't supply a `correlation_id` (which by
        convention is the run_id; without it the reporter has nothing
        to correlate against)."""
        if not request.correlation_id:
            return
        # Build the resume snapshot for FAILED / SUCCEEDED transitions
        # only — cancelled runs aren't a useful resume point (the
        # operator explicitly stopped them) and unknown-terminal
        # paths shouldn't pretend to be resumable. The snapshot
        # captures settings + completed-step set + carry-forward
        # artifact IDs so a later resume request can validate
        # compatibility and skip the LLM-cost stages that finished.
        resume_snapshot: dict | None = None
        if final_status in (
            "succeeded", "succeeded_with_warnings",
            "partial_completed", "failed", "timed_out",
        ):
            try:
                from j1.runs.resume import build_resume_snapshot
                step_results_payload: list[dict] = []
                for r in self._step_results:
                    entry: dict = {
                        "step": r.step,
                        "status": (
                            r.status.value if hasattr(r.status, "value")
                            else str(r.status)
                        ),
                        "required": bool(r.required),
                        "artifact_count": int(r.artifact_count or 0),
                    }
                    step_results_payload.append(entry)
                resume_snapshot = build_resume_snapshot(
                    request=request,
                    step_results_payload=step_results_payload,
                    produced_artifact_ids=self._produced_artifact_ids,
                    produced_artifact_kinds=self._produced_artifact_kinds,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    snapshot_at=workflow.now(),
                )
            except Exception:  # noqa: BLE001 — snapshot build never blocks exit
                resume_snapshot = None
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_run_terminal,
                ReportRunTerminalInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    final_status=final_status,
                    warning_count=warning_count,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    actor=request.actor,
                    step_summary=self._step_summary_payload(),
                    resume_snapshot=resume_snapshot,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow exit
            pass

    async def _persist_error_report(
        self,
        request: ProjectProcessingRequest,
        *,
        failure_code: str,
        failure_message: str,
    ) -> None:
        """Schedule `ProcessingActivities.persist_error_report` so the
        FE artifact-listing surface picks up an `error_report` artifact
        under the failed run.

        Best-effort like the other emit helpers — any persistence
        error (activity timeout, registry write failure) is logged
        inside the activity and we proceed regardless. Skipped when
        no `correlation_id` is set (the resolver has nothing to
        attach the artifact to)."""
        if not request.correlation_id:
            return
        # Snapshot the current step_results into a Temporal-data-
        # converter-friendly list of dicts. The workflow's
        # `_step_results` is a list of frozen dataclasses; convert
        # at the boundary.
        step_results_payload: list[dict] = []
        for r in self._step_results:
            try:
                entry = {
                    "step": r.step,
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "required": bool(r.required),
                    "source": r.source.value if hasattr(r.source, "value") else str(r.source),
                    "reason": r.reason,
                    "artifact_count": int(r.artifact_count or 0),
                }
                if r.error is not None:
                    entry["error_type"] = r.error.type
                    entry["error_message"] = r.error.message
                step_results_payload.append(entry)
            except Exception:  # noqa: BLE001 — defensive snapshot
                continue
        # Last-known stage / step come from the most recent FAILED
        # entry if present; otherwise from the last recorded entry.
        stage_hint: str | None = None
        step_hint: str | None = None
        for r in reversed(self._step_results):
            if (r.status.value if hasattr(r.status, "value") else str(r.status)) == "failed":
                step_hint = r.step
                stage_hint = r.step
                break
        if step_hint is None and self._step_results:
            step_hint = self._step_results[-1].step
            stage_hint = self._step_results[-1].step
        # Document id from the most recently-recorded step's
        # metadata (workflow records `metadata={"document_id": ...}`
        # on per-document steps).
        document_id: str | None = None
        for r in reversed(self._step_results):
            if isinstance(r.metadata, dict):
                doc = r.metadata.get("document_id")
                if doc:
                    document_id = str(doc)
                    break
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_error_report,
                PersistErrorReportInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    stage=stage_hint,
                    step=step_hint,
                    step_results=step_results_payload,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow exit
            pass

    async def _persist_validation_report(
        self,
        request: ProjectProcessingRequest,
        *,
        passed: bool,
        errors: list[str],
        rules_evaluated: list[str] | None = None,
    ) -> None:
        """Persist `validation_report.json` summarising the outcome of
        `_validate_completion`. Called at every terminal transition
        (success or failure) so operators can see WHICH rules ran
        and which ones tripped without re-running validation. Best-
        effort — never blocks the terminal transition."""
        if not request.correlation_id:
            return
        document_id: str | None = None
        for r in reversed(self._step_results):
            if isinstance(r.metadata, dict):
                doc = r.metadata.get("document_id")
                if doc:
                    document_id = str(doc)
                    break
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_validation_report,
                PersistValidationReportInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    passed=passed,
                    errors=list(errors or []),
                    rules_evaluated=list(rules_evaluated or []),
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow exit
            pass

    async def _persist_final_summary(
        self,
        request: ProjectProcessingRequest,
        *,
        final_status: str,
        warning_count: int = 0,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """Persist `final_summary.json` at terminal state. Carries the
        at-a-glance run outcome so the FE has a single canonical
        artifact to summarise the run without assembling state from
        separate endpoints."""
        if not request.correlation_id:
            return
        document_id: str | None = None
        for r in reversed(self._step_results):
            if isinstance(r.metadata, dict):
                doc = r.metadata.get("document_id")
                if doc:
                    document_id = str(doc)
                    break
        # Snapshot the per-step status table at terminal time. Same
        # shape as the `error_report` artifact's step_results for
        # cross-referencing.
        executed_steps: list[dict] = []
        for r in self._step_results:
            try:
                entry = {
                    "step": r.step,
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "required": bool(r.required),
                    "source": r.source.value if hasattr(r.source, "value") else str(r.source),
                    "artifact_count": int(r.artifact_count or 0),
                }
                if r.reason:
                    entry["reason"] = r.reason
                executed_steps.append(entry)
            except Exception:  # noqa: BLE001
                continue
        # Aggregate artifact counts by kind from the workflow's
        # in-memory tracker.
        kind_counts: dict[str, int] = {}
        for k in self._produced_artifact_kinds:
            kind_counts[k] = kind_counts.get(k, 0) + 1
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_final_summary,
                PersistFinalSummaryInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    final_status=final_status,
                    executed_steps=executed_steps,
                    artifact_kind_counts=kind_counts,
                    warning_count=warning_count,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks workflow exit
            pass

    async def _emit_step_skipped(
        self,
        request: ProjectProcessingRequest,
        *,
        stage: str,
        step: str,
        reason: str,
        source: str = "planner",
    ) -> None:
        """Emit a `step.skipped` progress event from inside the
        workflow. Goes through an activity because the reporter call
        needs to happen in non-deterministic context."""
        if not request.correlation_id:
            return
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_step_skipped,
                ReportStepSkippedInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    stage=stage, step=step, reason=reason,
                    source=source, actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _emit_step_lifecycle(
        self,
        request: ProjectProcessingRequest,
        *,
        stage: str,
        step: str,
        action: str,
        artifact_count: int = 0,
        engine: str | None = None,
    ) -> None:
        """Synthesise a `step.started` / `step.completed` event for
        a user-facing sub-step that doesn't run as a standalone
        activity.

        Used for `build_content_inventory` and `generate_knowledge_chunks`
        — both happen inside the compile activity but the FE
        renders them as separate steps. Best-effort like every
        emit helper; failure never blocks the workflow."""
        if not request.correlation_id:
            return
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_step_lifecycle,
                ReportStepLifecycleInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    stage=stage,
                    step=step,
                    action=action,
                    artifact_count=artifact_count,
                    engine=engine,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _emit_plan_generated(
        self,
        request: ProjectProcessingRequest,
        document_id: str,
        plan: IngestPlan,
    ) -> None:
        """Persist the plan into the audit log via an activity.

        The planner runs in workflow code (deterministic, no I/O), so
        the audit-log write that backs `GET /ingestion-runs/{id}/plan`
        has to be an activity call. Best-effort like the other
        emit helpers — failure to record the plan must not block the
        rest of the pipeline."""
        if not request.correlation_id:
            return
        # `to_jsonable` recursively converts dataclasses + enums (the
        # `IngestPlan` tree includes `PlannedStep` / `IngestMode` /
        # `IngestPolicy` / `DocumentProfile`) into Temporal-data-
        # converter-safe dicts.
        plan_payload = to_jsonable(plan)
        # The REST `_read_run_plan` reads `payload["plan"]["document_id"]`
        # etc. directly, so make sure the payload has `document_id`
        # at top level (matches `IngestPlan.document_id`).
        if isinstance(plan_payload, dict):
            plan_payload.setdefault("document_id", document_id)
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_plan_generated,
                ReportPlanGeneratedInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    plan_payload=plan_payload,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _emit_plan_revised(
        self,
        request: ProjectProcessingRequest,
        plan: IngestPlan,
        *,
        diff: dict[str, Any],
    ) -> None:
        """Persist the revised plan via `j1.progress.plan.revised`.

        Same best-effort contract as `_emit_plan_generated`. The
        `diff` summary is folded into the audit payload's `reason`
        so operators reading the timeline see exactly what changed
        between initial and revised plans (e.g. "graph step
        re-enabled by post-compile signals: image_count=8").
        """
        if not request.correlation_id:
            return
        plan_payload = to_jsonable(plan)
        if isinstance(plan_payload, dict):
            plan_payload.setdefault("document_id", plan.document_id)
        reason = _format_plan_diff_reason(diff)
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_plan_revised,
                ReportPlanRevisedInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    plan_payload=plan_payload,
                    reason=reason,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001
            pass

    def _set_search_attribute(self, name: str, value: str) -> None:
        """Opt-in keyword search-attribute upsert.

        Default OFF (`request.search_attributes_enabled=False`). The
        Temporal cluster rejects upserts for attributes that aren't
        registered with the namespace, and the rejection happens at
        workflow-activation completion — the SDK's exception surfaces
        AFTER this method returns, so a try/except here can't catch
        it. The clean alternative is to NOT issue the upsert unless
        the operator has explicitly registered the attributes and
        flipped `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true` (which
        the deployment passes through to `request.search_attributes_enabled`).

        Uses the typed `SearchAttributeKey` API (the dict form is
        deprecated as of Temporal Python SDK 1.x). Inner try/except
        is kept as a final guardrail for unit tests and other
        non-Temporal-runtime scenarios where the upsert call itself
        raises synchronously."""
        if not self._search_attributes_enabled:
            return
        try:
            from temporalio.common import SearchAttributeKey
            key = SearchAttributeKey.for_keyword(name)
            workflow.upsert_search_attributes([key.value_set(value)])
        except Exception:  # noqa: BLE001 — synchronous failures are non-fatal
            # Intentionally silent. Server-side rejection (unregistered
            # attribute) bypasses this handler — that's why the opt-in
            # flag above exists.
            pass

    def _set_search_attribute_int(self, name: str, value: int) -> None:
        """Opt-in int-typed search-attribute upsert. Same gating
        rules as `_set_search_attribute`; separate method because
        Temporal's typed-key API distinguishes Keyword from Int at
        the SDK level."""
        if not self._search_attributes_enabled:
            return
        try:
            from temporalio.common import SearchAttributeKey
            key = SearchAttributeKey.for_int(name)
            workflow.upsert_search_attributes([key.value_set(int(value))])
        except Exception:  # noqa: BLE001
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
        # `target_document_ids` lets the user-facing flow scope the
        # workflow to a specific document (the one just uploaded)
        # without re-processing every PENDING document in the project.
        # When unset, the legacy bulk behaviour kicks in.
        if request.target_document_ids:
            documents = list(request.target_document_ids)
        else:
            documents = await workflow.execute_activity_method(
                ProjectActivities.list_pending_documents,
                request.scope,
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        self._complete(OPERATION_LIST_DOCUMENTS)
        return documents

    async def _mark_document_status(
        self,
        request: ProjectProcessingRequest,
        document_id: str,
        status: ProcessingStatus,
    ) -> None:
        """Best-effort registry status update.

        Telemetry-grade: failures are swallowed so they can't block
        the workflow's outcome. The activity itself logs missing
        documents; transport-level failures here just mean the
        registry stays at PENDING for that doc — the next bulk job
        will re-pick it, which is the previous behaviour."""
        try:
            await workflow.execute_activity_method(
                ProjectActivities.set_document_status,
                SetDocumentStatusInput(
                    scope=request.scope,
                    document_id=document_id,
                    status=status.value,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — registry status is non-critical telemetry
            pass

    async def _build_plan(
        self,
        request: ProjectProcessingRequest,
        document_id: str,
        *,
        compile_content_stats: dict | None = None,
    ) -> IngestPlan:
        """Run the profiling activity and feed the result through the
        planner. Pure side-effect-free planning happens in-workflow;
        only the I/O-bound profile call goes through an activity.

        Caller-supplied kinds become caller_overrides — the planner
        sees them as forced-enable for that step (the legacy
        "kind is set" semantics). Stages without a kind on the
        request are left to the planner's mode-driven decision.

        `compile_content_stats` (when supplied — typically by the
        post-compile call site) carries observed signals from the
        parser. Keys override the deterministic profile so a 1-page
        PDF that contains only a diagram (which the file-system-only
        profiler classifies as text-only) gets `has_images=True`
        post-compile and the planner picks an image-aware mode."""
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
        if compile_content_stats:
            profile = _merge_compile_signals(profile, compile_content_stats)
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
        # compile (mandatory in the legacy model). A future change can
        # pass this in via `ProcessingCapabilities` so the planner
        # sees the full registered set, not just what the caller
        # picked.
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

    async def _maybe_replan_after_compile(
        self,
        request: ProjectProcessingRequest,
        document_id: str,
        *,
        initial_plan: IngestPlan,
        compile_content_stats: dict,
    ) -> IngestPlan | None:
        """Re-run the planner with parser-derived stats overlaid on
        the deterministic profile. Returns the revised plan when it
        differs from `initial_plan`, otherwise None.

        The revised plan can ONLY affect downstream optional stages
        (enrich / graph / index): compile already ran. We never
        revisit a step the initial plan reported as completed.
        Caller-supplied kinds keep winning via `caller_overrides`.

        Persists the revision via `j1.progress.plan.revised` so the
        FE plan card swaps from initial → revised, and operators can
        see the reason in the audit timeline.
        """
        try:
            revised = await self._build_plan(
                request,
                document_id,
                compile_content_stats=compile_content_stats,
            )
        except Exception as exc:  # noqa: BLE001 — replan is best-effort
            workflow.logger.warning(
                "post-compile replan failed; keeping initial plan: %s", exc,
            )
            return None

        diff = _summarise_plan_diff(initial_plan, revised)
        if not diff:
            # No effective change in step enablement — keep the
            # initial plan and skip the audit event noise.
            return None

        await self._emit_plan_revised(request, revised, diff=diff)
        return revised

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
        # Surface document / workspace identity on every workflow as
        # search attributes so operators can find the workflow that
        # processed a given document via the Temporal UI without
        # paging through histories. Gated on
        # `search_attributes_enabled` like every other upsert; the
        # `temporal-init` service registers all of these at cluster
        # boot for the dev stack.
        self._set_search_attribute(SEARCH_ATTR_DOCUMENT_ID, document_id)
        self._set_search_attribute(SEARCH_ATTR_WORKSPACE_ID, request.scope.project_id)
        if request.compiler_kind:
            self._set_search_attribute(SEARCH_ATTR_PARSER_NAME, request.compiler_kind)

        # Build the plan BEFORE compile so the FE's plan card resolves
        # within seconds of upload — operators see "what will run" up
        # front, not at workflow exit.
        #
        # We trade off content-stat refinement for visibility: the
        # planner sees only the deterministic profile (file size,
        # extension), not the parser-derived signals (has_images /
        # has_tables / text_extractable_ratio). For most documents the
        # extension-based profile is sufficient — e.g. a `.pdf` is
        # already known to potentially carry images and tables, so the
        # planner picks an image-aware mode regardless. The remaining
        # edge case (a `.txt` that secretly contains a base64-encoded
        # diagram) is rare enough that earlier visibility wins.
        #
        # Compile is always force-enabled — the planner only gates the
        # LLM-expensive stages (enrich / graph / index) — so an
        # upfront plan never short-circuits the parsing path.
        plan: IngestPlan | None = None
        if request.planner_enabled:
            plan = await self._build_plan(request, document_id)
            self._set_search_attribute(SEARCH_ATTR_INGEST_MODE, plan.mode.value)
            # Surface the LLM/vision policy decisions as search
            # attributes so operators can filter Temporal histories
            # for "all runs that needed the premium model" without
            # re-reading the audit log. Both gated on the same
            # `search_attributes_enabled` flag as the existing upserts.
            self._set_search_attribute(
                SEARCH_ATTR_REQUIRES_VISION,
                "true" if plan.requires_vision else "false",
            )
            self._set_search_attribute(
                SEARCH_ATTR_REQUIRES_PREMIUM_LLM,
                "true" if plan.requires_premium_llm else "false",
            )
            self._log_step(
                request,
                event="ingestion.plan.created",
                stage="plan",
                status="completed",
                document_id=document_id,
                reason=(
                    f"mode={plan.mode.value} policy={plan.policy.value} "
                    f"requires_vision={plan.requires_vision} "
                    f"requires_premium_llm={plan.requires_premium_llm}"
                ),
            )
            # Persist the plan into the audit log so the FE's
            # `GET /ingestion-runs/{id}/plan` endpoint can serve it.
            # Without this the run-detail page sits on "Generating
            # plan…" forever — the plan only exists in workflow
            # memory until this activity writes it through the
            # progress reporter.
            await self._emit_plan_generated(request, document_id, plan)

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
                # Compile is the most expensive activity (MinerU parse
                # is minutes per real PDF). Wider timeout absorbs
                # worst-case docs; bounded retry (`COMPILE_RETRY` =
                # 2 attempts) keeps a transient infrastructure blip
                # from multiplying parse cost. The activity ticker
                # heartbeats every 30 s, so `HEARTBEAT_TIMEOUT` is
                # the real liveness check.
                start_to_close_timeout=COMPILE_ACTIVITY_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=COMPILE_RETRY.to_temporal(),
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
        self._produced_artifact_kinds.extend(compile_result.kinds)
        self._record_step(
            step="compile",
            status=StepStatus.COMPLETED,
            required=True,
            source=StepSource.CALLER,
            artifact_count=len(compile_result.artifact_ids),
            metadata={"document_id": document_id},
        )
        self._complete(compile_op)

        # ── Synthetic step: Build Content Inventory ──────────────
        # The compile activity bundles parse + chunk in one shot
        # (RAGAnything's `process_document_complete` is one call),
        # but the user-facing flow lists Build Content Inventory as
        # an independent step. We synthesise its lifecycle events
        # here — immediately after compile.completed — so the
        # timeline shows the right ordering even though the work
        # was actually done inside compile. The Content Inventory
        # tab unlocks on the underlying parsed-content manifest
        # artifact (which is already present at this point).
        await self._emit_step_lifecycle(
            request, stage="BUILD_CONTENT_INVENTORY",
            step="build_content_inventory", action="started",
        )
        self._record_step(
            step="build_content_inventory",
            status=StepStatus.COMPLETED,
            required=True,
            source=StepSource.CALLER,
            artifact_count=len(compile_result.artifact_ids),
            metadata={
                "document_id": document_id,
                "synthetic": True,
                "synthesised_from": "compile",
            },
        )
        await self._emit_step_lifecycle(
            request, stage="BUILD_CONTENT_INVENTORY",
            step="build_content_inventory", action="completed",
            artifact_count=len(compile_result.artifact_ids),
        )

        # Post-compile replan. The compile activity returns
        # `content_stats` with parser-derived signals (image/table/
        # equation counts, page count, scanned-page hints). Re-feed
        # them into the planner so a misclassified document (e.g. a
        # PDF whose deterministic profile said `has_images=False`
        # but compile actually found embedded figures) can trigger
        # a downstream stage that the initial plan skipped. The
        # revised plan is allowed to ENABLE optional stages only —
        # compile already ran and is not re-evaluated.
        if (
            request.planner_enabled
            and plan is not None
            and compile_result.content_stats
        ):
            revised = await self._maybe_replan_after_compile(
                request,
                document_id,
                initial_plan=plan,
                compile_content_stats=compile_result.content_stats,
            )
            if revised is not None:
                plan = revised

        # Post-compile Processing Plan. Reads the parsed-content
        # manifest the compile activity emitted, runs Document
        # Understanding + Lightweight Content Digest + Rule-based
        # Post-Compile Assessment, optionally consults a planner LLM,
        # and persists `planning_result.json`. Best-effort: when the
        # activity fails or no manifest is available the workflow
        # falls back to the existing IngestPlan unchanged.
        if plan is not None:
            try:
                planning_result: "BuildPlanningResultOutput | None" = (
                    await workflow.execute_activity_method(
                        PlanningActivities.build_planning_result,
                        BuildPlanningResultInput(
                            scope=request.scope,
                            run_id=request.correlation_id or "",
                            document_id=document_id,
                            profile_payload=_profile_payload(plan.profile),
                            domain_override=request.domain_override,
                            workspace_default_domain=request.workspace_default_domain,
                        ),
                        start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                        retry_policy=DEFAULT_RETRY.to_temporal(),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — planning is best-effort
                workflow.logger.warning(
                    "post-compile planning activity failed; keeping existing plan: %s",
                    exc,
                )
                planning_result = None

            if planning_result is not None:
                updated, diff = _apply_post_compile_planning(plan, planning_result)
                if diff:
                    plan = updated
                # Always re-emit `plan.revised` after a successful
                # post-compile planning activity — even when the
                # overlay didn't flip an enable bit. Two reasons:
                # (a) the activity's planning_result.json artifact
                #     just landed; the audit-log event is the FE's
                #     trigger to refresh the run summary so the new
                #     Planning Report tab unlocks promptly.
                # (b) when no enable bits flipped but scope/pages
                #     did (e.g. a domain-pack overlay narrowed
                #     `vision_enrichment.pages`), `/plan` should
                #     still surface the latest canonical IngestPlan
                #     so the plan card stays accurate.
                await self._emit_plan_revised(
                    request, plan,
                    diff={
                        **diff,
                        "post_compile_source": {
                            "after": planning_result.source,
                        },
                        "post_compile_domain": {
                            "after": planning_result.selected_domain,
                        },
                    },
                )

        # ── Generate Knowledge Chunks ────────────────────────────
        # Two paths:
        #
        #   * `pipeline_mode == "complete"` (legacy default):
        #     compile already produced chunk artifacts; emit
        #     synthetic step.* events so the user-facing ordering
        #     reads "Plan → Chunks" even though chunks landed at
        #     compile-time. The Chunks tab has been unlocked since
        #     compile.completed; the events here only set the
        #     timeline ordering.
        #
        #   * `pipeline_mode == "split_parse_insert"`:
        #     compile parsed the source and registered a
        #     `parsed_source` artifact only — no chunks yet. The
        #     workflow now runs the `insert_content` activity which
        #     calls `RAGAnything.insert_content_list` and produces
        #     real chunk drafts. Step.* events wrap the activity
        #     execution so the timeline shows actual work, not a
        #     synthetic.
        if (
            request.pipeline_mode == PIPELINE_MODE_SPLIT_PARSE_INSERT
            and compile_result.parsed_source_artifact_id
        ):
            insert_op = f"{OPERATION_INSERT_CONTENT}:{document_id}"
            self._begin(insert_op)
            try:
                insert_result: ArtifactActivityResult = (
                    await workflow.execute_activity_method(
                        ProcessingActivities.insert_content,
                        InsertContentActivityInput(
                            scope=request.scope,
                            document_id=document_id,
                            processor_kind=request.compiler_kind,
                            parsed_source_artifact_id=(
                                compile_result.parsed_source_artifact_id
                            ),
                            actor=request.actor,
                            correlation_id=request.correlation_id,
                        ),
                        # Insert drives chunking + LightRAG graph
                        # storage; matches compile's heartbeat
                        # ceiling because it can be similarly long
                        # on large docs.
                        start_to_close_timeout=COMPILE_ACTIVITY_TIMEOUT,
                        heartbeat_timeout=HEARTBEAT_TIMEOUT,
                        retry_policy=DEFAULT_RETRY.to_temporal(),
                    )
                )
            except Exception as exc:
                self._record_step(
                    step="generate_knowledge_chunks",
                    status=StepStatus.FAILED,
                    required=True,
                    source=StepSource.CALLER,
                    reason=str(exc),
                    error=StepError(
                        type=type(exc).__name__,
                        message=str(exc),
                        retryable=False,
                    ),
                    metadata={"document_id": document_id},
                )
                raise
            if insert_result.status != "succeeded":
                self._record_step(
                    step="generate_knowledge_chunks",
                    status=StepStatus.FAILED,
                    required=True,
                    source=StepSource.CALLER,
                    reason=insert_result.error or "insert_content activity returned non-succeeded status",
                    error=StepError(
                        type="ActivityFailure",
                        message=insert_result.error or "unspecified",
                        retryable=False,
                    ),
                    metadata={"document_id": document_id},
                )
                raise _BusinessRejection(
                    f"insert_content failed for {document_id}: {insert_result.error}"
                )
            self._produced_artifact_ids.extend(insert_result.artifact_ids)
            self._produced_artifact_kinds.extend(insert_result.kinds)
            self._record_step(
                step="generate_knowledge_chunks",
                status=StepStatus.COMPLETED,
                required=True,
                source=StepSource.CALLER,
                artifact_count=len(insert_result.artifact_ids),
                metadata={
                    "document_id": document_id,
                    "synthetic": False,
                    "pipeline_mode": PIPELINE_MODE_SPLIT_PARSE_INSERT,
                },
            )
            self._complete(insert_op)
        else:
            # Legacy synthetic path. Keep behaviour identical to
            # before split mode existed so deployments running
            # `complete` mode see no timeline change.
            await self._emit_step_lifecycle(
                request, stage="GENERATE_KNOWLEDGE_CHUNKS",
                step="generate_knowledge_chunks", action="started",
            )
            self._record_step(
                step="generate_knowledge_chunks",
                status=StepStatus.COMPLETED,
                required=True,
                source=StepSource.CALLER,
                artifact_count=len(compile_result.artifact_ids),
                metadata={
                    "document_id": document_id,
                    "synthetic": True,
                    "synthesised_from": "compile",
                },
            )
            await self._emit_step_lifecycle(
                request, stage="GENERATE_KNOWLEDGE_CHUNKS",
                step="generate_knowledge_chunks", action="completed",
                artifact_count=len(compile_result.artifact_ids),
            )

        await self._maybe_review(request, GATE_AFTER_COMPILE)
        if self._cancelled:
            return

        # Stage gate: planner (if enabled) and request kind together
        # decide whether enrich runs. `_stage_enabled` codifies the
        # precedence rules (caller > planner > default).
        enrich_enabled, enrich_reason, enrich_source = self._stage_enabled(
            plan, "enrich", request.enricher_kind,
        )
        # Resume short-circuit: if the prior run already completed
        # enrich, skip the activity dispatch and record a SKIPPED
        # step with a clear reason. The carry-forward artifact IDs
        # were already seeded into `_produced_artifact_ids` at
        # workflow start so downstream stages see the same artifact
        # set the prior run produced. Set both flags so the existing
        # `if not enrich_enabled` block below doesn't double-record.
        resumed_skip_enrich = (
            enrich_enabled
            and bool(request.resume_from_run_id)
            and "enrich" in request.resume_completed_steps
        )
        if resumed_skip_enrich:
            self._record_step(
                step="enrich",
                status=StepStatus.SKIPPED,
                required=False,
                source=StepSource.POLICY,
                reason=(
                    f"resumed from run {request.resume_from_run_id} — "
                    "enrich already completed"
                ),
                metadata={"document_id": document_id, "resumed": True},
            )
            await self._emit_step_skipped(
                request, stage="ENRICH", step="enrich",
                reason=f"resumed from {request.resume_from_run_id}",
                source=StepSource.POLICY.value,
            )
            enrich_enabled = False
        if not enrich_enabled and not resumed_skip_enrich:
            self._record_step(
                step="enrich",
                status=StepStatus.SKIPPED,
                required=False,
                source=enrich_source,
                reason=enrich_reason,
                metadata={"document_id": document_id},
            )
            await self._emit_step_skipped(
                request, stage="ENRICH", step="enrich",
                reason=enrich_reason or "skipped",
                source=enrich_source.value,
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
                        # Long enrich children — vision / table-LLM
                        # paths — routinely run minutes per artifact.
                        # Without a heartbeat budget, Temporal has no
                        # liveness check; a hung worker would blow the
                        # 10-minute total timeout instead of failing
                        # fast on a missed heartbeat. Compile and
                        # build_graph already pair their timeouts the
                        # same way; enrich was the outlier.
                        heartbeat_timeout=HEARTBEAT_TIMEOUT,
                        retry_policy=DEFAULT_RETRY.to_temporal(),
                    )
                )
                if enrich_result.status != "succeeded":
                    self._record_step(
                        step="enrich",
                        status=StepStatus.FAILED,
                        # Caller asked for this enricher → required.
                        # (A future planner-driven mode may emit
                        # `required=False` for planner-enabled enrich
                        # so `continue_optional` can let it fail.)
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
                self._produced_artifact_kinds.extend(enrich_result.kinds)
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
        # Resume short-circuit: same shape as the enrich one above.
        # The `graph` step name matches what the planner / status
        # tracker uses; `build_graph` is the activity name (not a
        # step name).
        resumed_skip_graph = (
            graph_enabled
            and bool(request.resume_from_run_id)
            and "graph" in request.resume_completed_steps
        )
        if resumed_skip_graph:
            self._record_step(
                step="graph",
                status=StepStatus.SKIPPED,
                required=False,
                source=StepSource.POLICY,
                reason=(
                    f"resumed from run {request.resume_from_run_id} — "
                    "graph already completed"
                ),
                metadata={"document_id": document_id, "resumed": True},
            )
            await self._emit_step_skipped(
                request, stage="GRAPH", step="graph",
                reason=f"resumed from {request.resume_from_run_id}",
                source=StepSource.POLICY.value,
            )
            graph_enabled = False
        if not graph_enabled and not resumed_skip_graph:
            self._record_step(
                step="graph",
                status=StepStatus.SKIPPED,
                required=False,
                source=graph_source,
                reason=graph_reason,
                metadata={"document_id": document_id},
            )
            await self._emit_step_skipped(
                request, stage="GRAPH", step="graph",
                reason=graph_reason or "skipped",
                source=graph_source.value,
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
            self._produced_artifact_kinds.extend(graph_result.kinds)
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
        # Surface step_results so the status endpoint can show
        # "what ran / was skipped / failed" without waiting for
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
