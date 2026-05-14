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
        BuildInitialExecutionPlanInput,
        BuildInitialExecutionPlanResult,
        CompileActivityInput,
        EnrichActivityInput,
        FastLLMConsultEnrichInput,
        FinalizeInput,
        GraphActivityInput,
        IndexActivityInput,
        PersistCompileResultSummaryInput,
        PersistCompileStrategyReportInput,
        PersistErrorReportInput,
        PersistFinalIngestionReportInput,
        PersistFinalSummaryInput,
        PersistInitialExecutionPlanInput,
        PersistPostCompileEnrichPlanInput,
        RunEnrichmentStageInput,
        RunEnrichmentStageResult,
        ProcessingActivityResult,
        ProjectScope,
        SetDocumentStatusInput,
        SpendSummary,
        ValidateContextResult,
    )
    from j1.orchestration.activities.processing import ProcessingActivities
    from j1.orchestration.activities.profiling import (
        ProfileDocumentInput,
        ProfilingActivities,
    )
    from j1.orchestration.activities.project import ProjectActivities
    from j1.orchestration.activities.runs import (
        ReportAttemptInput,
        ReportRunTerminalInput,
        ReportStepLifecycleInput,
        ReportStepSkippedInput,
        RunsActivities,
        StepSummaryEntry,
    )
    from j1.processing.diagnostics import (
        EVENT_COMPILE_ATTEMPT_COMPLETED,
        EVENT_COMPILE_ATTEMPT_STARTED,
        EVENT_COMPILE_RETRY_SCHEDULED,
        EVENT_ENRICHMENT_ATTEMPT_COMPLETED,
        EVENT_ENRICHMENT_ATTEMPT_STARTED,
    )
    from j1.orchestration.errors import (
        ERROR_TYPE_REQUIRED_STEP_FAILED,
        ERROR_TYPE_UNEXPECTED_ERROR,
    )
    from j1.orchestration.temporal.retries import COMPILE_RETRY, DEFAULT_RETRY
    from j1.processing.assessment import (
        ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED,
        AssessmentPlan,
        Capability,
        CompileMode,
        DefaultAssessmentPlanner,
        load_assessment_failure_policy,
    )
    from j1.processing.enrich_assessment import (
        EnrichRecommendation,
        FastLLMRefinement,
        PostCompileEnrichPlan,
        apply_fast_llm_refinement,
        assess_post_compile_enrich,
        build_signals_from_compile_metrics,
        is_consult_warranted,
    )
    from j1.processing.compile_quality import (
        QUALITY_FAILED,
        QUALITY_GOOD,
        QUALITY_LOW,
        QualityVerdict,
        evaluate_compile_quality,
    )
    from j1.processing.compile_retry import (
        CompileAttemptRecord,
        CompileRetrySettings,
        DEFAULT_MAX_ATTEMPTS,
        next_compile_mode,
    )
    from j1.processing.compile_result import (
        NormalizedCompileResult,
        normalize_compile_result,
    )
    from j1.runs.models import (
        FAILURE_CODE_ENRICHMENT_REQUIRED,
        FAILURE_CODE_FINALIZATION_FAILED,
    )
    from j1.processing.initial_execution_plan import (
        InitialExecutionPlan,
        build_initial_execution_plan,
    )
    from j1.processing.results import ArtifactProcessingResult, ResultStatus
    from j1.processing.profiling import DocumentProfile
    from j1.processing.status import (
        FailurePolicy,
        FinalStatus,
        StepSource,
        StepStatus,
    )
    from j1.processing.step_result import StepError, StepResult
    from j1.jobs.status import ProcessingStatus


class WorkflowState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_BUDGET_APPROVAL = "waiting_for_budget_approval"
    WAITING_FOR_REVIEW = "waiting_for_review"
    # Two-phase compile gate. The workflow parks here after the
    # assessment plan is built but before the (expensive) compile
    # activity dispatches; `trigger_compile` advances back to RUNNING.
    # Only reached when the request opted in (`two_phase_compile=True`).
    WAITING_FOR_COMPILE_TRIGGER = "waiting_for_compile_trigger"
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
OPERATION_POST_COMPILE_ASSESS = "post_compile_assess"
OPERATION_ENRICH = "enrich"
OPERATION_BUILD_GRAPH = "build_graph"
OPERATION_INDEX = "index"
OPERATION_FINALIZE = "finalize"
OPERATION_BUDGET_CHECK = "budget_check"
OPERATION_REVIEW_GATE = "review_gate"

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
# Temporal search attributes for the new pipeline shape.
# `J1DomainProfileId` lets ops filter on the active domain pack.
# `J1EnrichmentPolicy` reflects the resolved policy literal
# (`auto` / `always` / `never`) so dashboards group by policy.
# `J1RequireEnrichmentSuccess` is the resolved boolean policy
# flag. `J1FinalStatus` carries the final-status projection
# string at run terminal. Retry counts let ops aggregate by
# stage-attempt cost. All best-effort writes; missing attribute
# registrations silently noop.
SEARCH_ATTR_DOMAIN_PROFILE_ID = "J1DomainProfileId"
SEARCH_ATTR_ENRICHMENT_POLICY = "J1EnrichmentPolicy"
SEARCH_ATTR_REQUIRE_ENRICHMENT_SUCCESS = "J1RequireEnrichmentSuccess"
SEARCH_ATTR_FINAL_STATUS = "J1FinalStatus"
SEARCH_ATTR_COMPILE_RETRY_COUNT = "J1CompileRetryCount"
SEARCH_ATTR_ENRICHMENT_RETRY_COUNT = "J1EnrichmentRetryCount"

# Values written into `SEARCH_ATTR_INGEST_STAGE`. Operators filter
# Temporal histories on these — keep in sync with the values quoted
# in deployment runbooks.
#
# Macro-stage vocabulary: the workflow writes one of these
# stable values as it moves between high-level phases, rather than
# the raw per-operation string (`compile:doc-1`, `enrich:doc-2`,
# etc.). The reduced cardinality lets ops dashboards group by stage
# instead of carrying a long-tail of per-doc values. Mapping from
# the workflow's internal `op` strings happens in
# `_macro_ingest_stage` below.
INGEST_STAGE_RECEIVED = "received"
INGEST_STAGE_ASSESSING = "assessing"
INGEST_STAGE_ASSESSMENT_READY = "assessment_ready"
INGEST_STAGE_COMPILE_PENDING = "compile_pending"
INGEST_STAGE_COMPILING = "compiling"
INGEST_STAGE_VERIFYING = "verifying"
# Catch-all for non-macro ops (build_graph, index, enrich, finalize,
# budget_check, review_gate). Workflow consumers that need the
# specific stage read `current_operation` off the workflow's status
# query instead — search attributes are for filtering, not per-event
# detail.
INGEST_STAGE_RUNNING = "running"
# Legacy starting value. Kept as an alias of RECEIVED so dashboards
# filtering on "starting" still match new runs during migration; the
# workflow writes RECEIVED on new runs.
INGEST_STAGE_STARTING = "starting"
INGEST_STAGE_CANCELLED = "cancelled"
INGEST_STAGE_COMPLETED = "completed"
INGEST_STAGE_FAILED = "failed"


# Per-op → macro-stage projection. `op` is the per-doc workflow
# operation string written by `_begin` (e.g. `compile:doc-1`,
# `assess_compile_strategy:doc-1`, `validate`). The macro stage is
# the coarse stage the run is in; this lets dashboards group by
# stage without enumerating every per-doc op.
#
# Strip the `:doc-id` suffix before lookup so `compile:doc-1` and
# `compile:doc-2` both map to `compiling`. Unknown ops fall back to
# `running` so a new op accidentally bypassing the table doesn't
# crash the upsert.
_OP_TO_MACRO_INGEST_STAGE: dict[str, str] = {
    "validate": INGEST_STAGE_RECEIVED,
    "list_documents": INGEST_STAGE_RECEIVED,
    "assess_compile_strategy": INGEST_STAGE_ASSESSING,
    "compile_pending": INGEST_STAGE_COMPILE_PENDING,
    "compile": INGEST_STAGE_COMPILING,
    "verify_compile": INGEST_STAGE_VERIFYING,
    "post_compile_assess": INGEST_STAGE_VERIFYING,
    "assess_enrichment": INGEST_STAGE_VERIFYING,
}


def _macro_ingest_stage(op: str | None) -> str:
    """Return the macro-stage value to write into
 `SEARCH_ATTR_INGEST_STAGE` for the given workflow operation.

 Pure / deterministic. Strips an optional `:doc-id` suffix before
 table lookup so per-doc operations collapse onto one stage value.
 Unknown ops fall back to `INGEST_STAGE_RUNNING` — operators get a
 coarse "something's happening" signal even when the op string
 isn't in the table."""
    if not op:
        return INGEST_STAGE_RUNNING
    base, _, _ = op.partition(":")
    return _OP_TO_MACRO_INGEST_STAGE.get(base, INGEST_STAGE_RUNNING)

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
# Signal that releases the workflow from the
# `WAITING_FOR_COMPILE_TRIGGER` state into the compile retry loop.
# Sent by `POST /ingestion-runs/{run_id}/compile` (via the REST
# adapter's `compile_handler`). No-op when the workflow isn't parked
# (the signal flips a flag the gate awaits on; the gate doesn't fire
# at all when `two_phase_compile=False`).
SIGNAL_TRIGGER_COMPILE = "trigger_compile"


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
    # Cheap deterministic profile + AssessmentPlan toggle. When True
    # (production default), the workflow runs the `profile_document`
    # activity pre-compile and feeds the resulting profile into
    # `DefaultAssessmentPlanner` to derive compile config (parse_method,
    # per-capability toggles). The IngestPlanner is gone entirely —
    # downstream stage gating uses compile evidence + the
    # post-compile enrich plan, not a pre-compile decision tree.
    # Caller-supplied stage kinds remain authoritative for whether
    # an enricher / graph builder / indexer is even runnable.
    planner_enabled: bool = False
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
    #  `domain_override` — operator's per-upload choice. None
    #  means "no override; let the workflow apply the workspace
    #  default + auto-detect chain". `workspace_default_domain`
    #  carries the workspace/project default. Both are validated
    #  against the deployment's allow-list inside the planning
    #  activity; an unrecognised value falls back to `general`
    #  with a warning recorded on `domain_context`.
    domain_override: str | None = None
    workspace_default_domain: str | None = None
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
    # Rebuild-index-only mode. When True, the workflow skips the
    # per-document loop entirely (no compile / chunks / enrich /
    # graph) and runs ONLY the `index` activity against the
    # carry-forward artifact IDs in `resume_artifact_ids`. Used by
    # `POST /ingestion-runs/{id}/rebuild-index` when chunks already
    # exist + are valid but the retrieval index is stale (vector
    # store cleared, embedding model upgrade, index corruption).
    # Requires `indexer_kind` to be set; rejects at workflow start
    # otherwise.
    rebuild_index_only: bool = False
    # How the workflow reacts when AssessmentPlan construction
    # itself fails (planner raised, profile incomplete). Read from
    # `J1_ASSESSMENT_FAILURE_POLICY` at request-build time (REST
    # adapter / dev wiring), NOT inside the workflow — Temporal
    # sandbox forbids reading os.environ from workflow code.
    #
    # See `j1.processing.assessment.ASSESSMENT_FAILURE_POLICY_*`
    # for the value vocabulary. Default `fail_open` keeps ingest
    # robust to a degenerate profile.
    assessment_failure_policy: str = "fail_open"
    # Compile-safety-retry knobs. Read once at REST/dev-wiring
    # boundary via `load_compile_retry_settings` and threaded
    # through; the workflow rebuilds a `CompileRetrySettings`
    # from these fields for the evaluator. Defaults match
    # `compile_retry.DEFAULT_*`.
    compile_retry_enabled: bool = True
    compile_max_attempts: int = 2
    compile_retry_min_text_chars: int = 200
    compile_retry_min_chunks: int = 1
    # Two-phase compile control. When True, the workflow pauses
    # AFTER the assessment plan is built and BEFORE the compile
    # retry loop dispatches, parking in
    # `WorkflowState.WAITING_FOR_COMPILE_TRIGGER` and surfacing
    # `RunStatus.COMPILE_PENDING` on the IngestionRun record. The
    # REST adapter's `POST /ingestion-runs/{id}/compile` endpoint
    # sends `SIGNAL_TRIGGER_COMPILE` to advance the workflow. When
    # False (default), compile dispatches inline as before.
    two_phase_compile: bool = False
    # Phase 9: up-front snapshot allocation. The REST adapter
    # allocates a candidate ``DocumentSnapshot`` at the same time
    # it creates the IngestionRun and threads the snapshot_id
    # through here so every downstream activity can address its
    # output paths under
    # ``{workdir}/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}/``
    # without round-tripping through ``get_or_create_for_run``.
    # ``None`` only for the legacy bulk-job path that pre-dates
    # snapshot-centered processing; new flows always supply this.
    target_snapshot_id: str | None = None


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

 `failure_code` is an optional override for the outer-handler-default
 `ERROR_TYPE_REQUIRED_STEP_FAILED`. Verification rejections set this
 to one of the `FAILURE_CODE_*` strings from `j1.runs.models` so the
 final `error_report` artifact + `IngestionRun.failure_code` carry
 the verification-specific reason (CHUNK_FAILED / INDEX_FAILED /
 VERIFICATION_FAILED) instead of the generic step-failed label.
 """

    def __init__(self, message: str, *, failure_code: str | None = None) -> None:
        super().__init__(message)
        self.failure_code = failure_code


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




def _compile_saw_images(compile_result) -> bool:
    """True iff the compile activity's `content_stats` reports actual
 image content. Used to set the `REQUIRES_VISION` search attribute
 from compile evidence rather than pre-compile guesses. Defensive
 against missing `content_stats` (legacy compilers / test fakes
 that don't populate it) — returns False in that case so the
 search attribute defaults to a conservative `false`."""
    if compile_result is None:
        return False
    stats = getattr(compile_result, "content_stats", None)
    if not isinstance(stats, dict):
        return False
    if stats.get("has_images") is True:
        return True
    image_count = stats.get("image_count")
    return isinstance(image_count, int) and image_count > 0


def _enrich_plan_needs_premium_llm(enrich_plan) -> bool:
    """True iff the post-compile enrich plan recommends or requires a
 vision-aware enrichment task. Drives the `REQUIRES_PREMIUM_LLM`
 search attribute from assessment output rather than pre-compile
 heuristics. None plan → False (no premium-LLM signal yet)."""
    if enrich_plan is None:
        return False
    rec = getattr(enrich_plan, "overall_recommendation", None)
    if rec is None:
        return False
    rec_value = getattr(rec, "value", str(rec))
    if rec_value not in {"recommended", "required"}:
        return False
    recommended = tuple(
        getattr(enrich_plan, "recommended_tasks", ()) or ()
    )
    vision_aware = {"image_captioning", "vision_enrichment"}
    return any(t in vision_aware for t in recommended)


def _escalation_reason(
    initial_mode: str | None,
    final_mode: str | None,
    attempts: list[dict],
    final_retry_reason: str | None,
) -> str | None:
    """Render a one-line operator-readable reason for compile-mode
 escalation, or None when no escalation occurred.

 Inputs:
 * `initial_mode` / `final_mode` — when they differ, escalation
 DID happen. The attempt records carry the per-attempt
 retry_reason ("zero_chunks", "ocr_likely_needed", etc.)
 that triggered each escalation; this helper joins them
 into a single audit string.
 * `attempts` — the per-attempt audit list from the workflow's
 retry loop.
 * `final_retry_reason` — the verdict the LAST attempt would
 have escalated on if the ladder had more rungs (None when
 the final attempt succeeded cleanly).

 Returns None when initial_mode == final_mode (single-attempt
 success) so the FE knows to suppress the escalation callout."""
    if not initial_mode or not final_mode or initial_mode == final_mode:
        return None
    triggers: list[str] = []
    for entry in attempts:
        reason = entry.get("retry_reason") if isinstance(entry, dict) else None
        if reason:
            triggers.append(str(reason))
    if final_retry_reason and final_retry_reason not in triggers:
        triggers.append(final_retry_reason)
    if triggers:
        joined = ", ".join(triggers)
        return (
            f"compile escalated {initial_mode} → {final_mode} "
            f"after: {joined}"
        )
    return f"compile escalated {initial_mode} → {final_mode}"


def _build_extraction_evidence(compile_result) -> dict[str, Any]:
    """Produce the `extraction_evidence` block for the
 `compile_strategy_report` payload.

 The block describes what the PARSER extracted — independent of
 whether downstream chunking / indexing actually happened. The FE
 renders this distinctly from chunking status so operators can
 tell at a glance: "parsing worked, but no chunks landed" vs
 "parsing failed entirely" vs "everything is green".

 Fields:
 * `parser` — adapter that produced the result.
 * `parser_method` — adapter-level method (txt / auto /
 ocr / vlm-* depending on backend).
 * `text_char_count` — characters of extracted text.
 * `content_block_count` — text blocks the parser emitted.
 * `detected_content_types` — sorted list of content types the
 parser actually saw (text /
 images / tables / equations).
 * `page_count` — pages observed (None when unknown).
 * `chunking_status` — always `pending_verification` here.
 Chunk evidence is verified later
 via the per-stage validation gate
 + the chunk artifact registry; this
 block intentionally never claims a
 chunk_count.

 Defensive on every field: missing `compile_result` → empty
 block; missing keys on the result → field omitted rather than
 zero (the FE distinguishes "unknown" from "zero")."""
    if compile_result is None:
        return {
            "parser": "raganything",
            "parser_method": None,
            "text_char_count": None,
            "content_block_count": None,
            "detected_content_types": [],
            "page_count": None,
            "chunking_status": "pending_verification",
        }
    stats = getattr(compile_result, "content_stats", None) or {}
    metrics = getattr(compile_result, "compile_metrics", None) or {}

    # Parse method comes from the bridge's manifest (parser_engine
    # field carries the resolved method); the workflow tracks the
    # mapped method per attempt under `mapped_compile_config`.
    parser_method = None
    if isinstance(stats.get("parser_engine"), str):
        parser_method = stats["parser_engine"]
    elif isinstance(metrics.get("parser_method"), str):
        parser_method = metrics["parser_method"]
    elif isinstance(stats.get("parse_method"), str):
        parser_method = stats["parse_method"]

    text_chars = stats.get("total_text_chars")
    if not isinstance(text_chars, int):
        text_chars = metrics.get("extracted_text_chars")
        if not isinstance(text_chars, int):
            text_chars = None

    content_blocks = stats.get("text_block_count")
    if not isinstance(content_blocks, int):
        content_blocks = None

    page_count = stats.get("page_count")
    if not isinstance(page_count, int):
        page_count = None

    detected: list[str] = []
    if stats.get("has_text") is True or (
        isinstance(content_blocks, int) and content_blocks > 0
    ) or (isinstance(text_chars, int) and text_chars > 0):
        detected.append("text")
    if stats.get("has_images") is True or (
        isinstance(stats.get("image_count"), int) and stats["image_count"] > 0
    ):
        detected.append("images")
    if stats.get("has_tables") is True or (
        isinstance(stats.get("table_count"), int) and stats["table_count"] > 0
    ):
        detected.append("tables")
    if stats.get("has_equations") is True or (
        isinstance(stats.get("equation_count"), int) and stats["equation_count"] > 0
    ):
        detected.append("equations")
    if stats.get("has_scanned_pages") is True:
        detected.append("scanned_pages")
    return {
        "parser": stats.get("provider") or "raganything",
        "parser_method": parser_method,
        "text_char_count": text_chars,
        "content_block_count": content_blocks,
        "detected_content_types": detected,
        "page_count": page_count,
        # CHUNKS ARE VERIFIED LATER — never claimed here.
        # Operators reading this should treat extraction evidence
        # as "what the probe saw" and check the chunks-tab / index
        # status for actual chunk verification.
        "chunking_status": "pending_verification",
    }


def _safe_now_iso() -> str:
    """Return an ISO-8601 timestamp string. Uses `workflow.now`
 when running inside Temporal so replay sees the same value;
 falls back to `datetime.now(timezone.utc)` when called outside
 a workflow event loop (e.g. unit tests that monkeypatch
 `execute_activity_method` but not `workflow.now`)."""
    try:
        return workflow.now().isoformat()
    except Exception:  # noqa: BLE001 — outside workflow runtime
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


def _safe_now():
    """Datetime variant of :func:`_safe_now_iso`. Same fallback
 rationale: callers (e.g. duration math around compile / enrich
 attempts) need a datetime in both production replay paths AND in
 unit tests that drive the workflow without a real Temporal
 event loop."""
    try:
        return workflow.now()
    except Exception:  # noqa: BLE001 — outside workflow runtime
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


def _parse_method_for_mode(mode: str | None) -> str | None:
    """Mirror the RAGAnything mapper's mode→parse_method table at the
 workflow layer. Used only for the per-attempt audit record so
 the FE can render `parse_method=txt|auto|ocr` without re-running
 the mapper. Vendor-neutral string IO — the workflow doesn't import
 the mapper directly to avoid circular dep with the providers
 layer."""
    if mode is None:
        return None
    return {"fast": "txt", "standard": "auto", "deep": "auto"}.get(mode)


def _project_search_attr_final_status(
    *,
    framework_final_status: str,
    step_results: list,
    failure_code: str | None = None,
) -> str:
    """Build the `J1FinalStatus` search-attribute value at run
 terminal.

 Uses `project_final_status` from `j1.processing.final_status`
 to map the framework status + recorded step outcomes onto the
 operator-facing vocabulary. Reads the most recent
 `enrich_stage` step's `enrichment_outcome` metadata to
 distinguish completed-without-enrichment from
 completed-with-enrichment etc. Returns the projected status
 string — never None.

 Pure / no I/O — safe to call from workflow code."""
    from j1.processing.final_status import project_final_status

    enrichment_outcome: str | None = None
    enrichment_skipped_reason: str | None = None
    enrichment_required = False
    enrichment_status_for_projector: str | None = None
    for record in reversed(step_results):
        if record.step != "enrich_stage":
            continue
        meta = record.metadata or {}
        enrichment_outcome = meta.get("enrichment_outcome")
        enrichment_skipped_reason = meta.get("enrichment_skipped_reason")
        if meta.get("failure_code") == "ENRICHMENT_REQUIRED":
            enrichment_required = True
        # Project the workflow's per-step outcome label onto the
        # value the final-status projector expects.
        if enrichment_outcome == "skipped":
            enrichment_status_for_projector = "skipped"
        elif enrichment_outcome == "failed_required":
            enrichment_status_for_projector = "failed"
            enrichment_required = True
        elif enrichment_outcome == "failed_optional":
            enrichment_status_for_projector = "failed"
        elif enrichment_outcome == "completed_with_warnings":
            enrichment_status_for_projector = "succeeded_with_warnings"
        elif enrichment_outcome == "completed":
            enrichment_status_for_projector = "succeeded"
        break

    projection = project_final_status(
        framework_final_status=framework_final_status,
        failure_code=failure_code,
        enrichment_status=enrichment_status_for_projector,
        enrichment_required=enrichment_required,
        enrichment_skipped_reason=enrichment_skipped_reason,
    )
    return projection.status


def _enrichment_outcome_label(
    *,
    enrichment_status: str,
    require_success: bool,
) -> str:
    """Project the activity's `EnrichmentResult.status` literal +
 the resolved `require_enrichment_success` flag onto the 
 fine-grained outcome vocabulary.

 Values: `completed` / `completed_with_warnings` /
 `failed_optional` / `failed_required` / `skipped`. The
 workflow stores this on step metadata + emits it in the
 structured log so the final-status projector + FE branch on a
 single label."""
    if enrichment_status == "skipped":
        return "skipped"
    if enrichment_status == "failed":
        return "failed_required" if require_success else "failed_optional"
    if enrichment_status == "succeeded_with_warnings":
        return "completed_with_warnings"
    return "completed"







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
        # Two-phase compile trigger flag — flipped by
        # `SIGNAL_TRIGGER_COMPILE`. The compile gate consumes the
        # flag (reads then resets to False) so a second document in
        # a multi-doc run gates independently. None means "not yet
        # signalled"; we use bool here because the gate's loop is
        # `wait_condition(self._compile_triggered or self._cancelled)`
        # and a fresh False at gate-entry means "wait again".
        self._compile_triggered: bool = False
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
        # every call. Populated on first `_log_step` and reused.
        self._scope_log_context: dict[str, str] = {}
        # Mirrors `request.search_attributes_enabled` once `run` is
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
        # RECEIVED is the canonical first-stage value. The
        # legacy "starting" alias is retained as a constant for
        # operators whose dashboards filter on it; new runs write
        # the canonical name and `_macro_ingest_stage` translates
        # subsequent transitions consistently.
        self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_RECEIVED)

        # capture the workflow's start timestamp so the
        # `final_ingestion_report` carries duration_ms at terminal.
        # Sandbox-safe via `workflow.now`.
        self._workflow_started_at_iso = _safe_now_iso()

        try:
            if not is_continuation:
                await self._validate(request)

            # Rebuild-index-only mode: skip the documents loop
            # entirely and emit synthetic SKIPPED step records for
            # the upstream stages so the FE timeline still shows the
            # full pipeline shape (with explicit "skipped: rebuild
            # index only" reasons). Requires indexer_kind + at least
            # one carry-forward artifact id; otherwise we can't index
            # anything.
            if request.rebuild_index_only:
                if not request.indexer_kind:
                    raise _BusinessRejection(
                        "rebuild_index_only=True requires indexer_kind"
                    )
                if not self._produced_artifact_ids:
                    raise _BusinessRejection(
                        "rebuild_index_only=True requires at least one "
                        "carry-forward artifact id (none provided in "
                        "resume_artifact_ids)"
                    )
                self._documents_total = 1
                reason = (
                    f"rebuild index only — chunks reused from "
                    f"{request.resume_from_run_id or 'prior run'}"
                )
                for skipped_step in ("compile", "enrich", "graph"):
                    self._record_step(
                        step=skipped_step,
                        status=StepStatus.SKIPPED,
                        required=False,
                        source=StepSource.POLICY,
                        reason=reason,
                        metadata={"rebuild_index_only": True},
                    )
                # Fall through to the index-enabled branch below.
                documents = []
            else:
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

            try:
                await self._finalize(request)
            except Exception as finalize_exc:  # noqa: BLE001
                # Finalize raised after a successful compile/enrichment
                # pipeline. Surface as a typed `FINALIZATION_FAILED`
                # ApplicationError so the existing failed-path handler
                # below persists the failure artifact with the right
                # failure_code — the projector then maps this to
                # `failed_finalization`. Compile/enrichment artifacts
                # already in `_produced_artifact_ids` remain preserved.
                self._error = (
                    f"finalize step failed: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}"
                )
                raise ApplicationError(
                    self._error,
                    type=FAILURE_CODE_FINALIZATION_FAILED,
                    non_retryable=True,
                ) from finalize_exc

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
                # surface the resolved final status on the
                # search-attribute surface. The string mirrors the
                # `IngestionFinalStatusProjection` vocabulary so ops
                # can filter on operator-facing labels directly.
                self._set_search_attribute(
                    SEARCH_ATTR_FINAL_STATUS,
                    _project_search_attr_final_status(
                        framework_final_status=(
                            "partial_completed" if final_status == "succeeded_with_warnings"
                            else "completed"
                        ),
                        step_results=list(self._step_results),
                    ),
                )
                # Persist the final_summary artifact at successful
                # terminal — single canonical artifact summarising
                # the run for the FE / operators.
                await self._persist_final_summary(
                    request,
                    final_status=final_status,
                    warning_count=self._warning_count(),
                )
                # persist the aggregated
                # final_ingestion_report after final_summary so the
                # builder picks up the just-written summary artifact.
                await self._persist_final_ingestion_report(
                    request,
                    framework_final_status=(
                        "partial_completed"
                        if final_status == "succeeded_with_warnings"
                        else "completed"
                    ),
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
            # Verification rejections carry a stable `failure_code` on
            # the exception. Other business rejections fall back to
            # the generic `ERROR_TYPE_REQUIRED_STEP_FAILED` label.
            business_failure_code = (
                exc.failure_code or ERROR_TYPE_REQUIRED_STEP_FAILED
            )
            self._log_step(
                request,
                event="ingestion.workflow.failed",
                stage="workflow",
                status="failed",
                reason=self._error,
                error_type=business_failure_code,
            )
            self._set_search_attribute(SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_FAILED)
            # final-status search attr at terminal so ops
            # can filter `J1FinalStatus=failed_enrichment_required`
            # / `failed_compile` / etc.
            self._set_search_attribute(
                SEARCH_ATTR_FINAL_STATUS,
                _project_search_attr_final_status(
                    framework_final_status="failed",
                    step_results=list(self._step_results),
                    failure_code=business_failure_code,
                ),
            )
            # Persist the failure-path `error_report` artifact so the
            # FE artifact-listing surface carries the failure detail
            # under the run, alongside whatever partial artifacts the
            # earlier stages produced. Best-effort — any persistence
            # error is logged inside the activity and we proceed
            # regardless.
            await self._persist_error_report(
                request,
                failure_code=business_failure_code,
                failure_message=self._error,
            )
            # Failed runs also get a `final_summary` artifact so the
            # FE has a single canonical run-outcome artifact for
            # both success and failure paths.
            await self._persist_final_summary(
                request,
                final_status="failed",
                failure_code=business_failure_code,
                failure_message=self._error,
            )
            # even on failure, persist the aggregated
            # report so the FE renders the (A–F) failure breakdown
            # with the partial-stage state intact.
            await self._persist_final_ingestion_report(
                request,
                framework_final_status="failed",
                warning_count=self._warning_count(),
                failure_code=business_failure_code,
                failure_message=self._error,
            )
            await self._safe_finalize(request)
            await self._emit_run_terminal(
                request, final_status="failed",
                failure_code=business_failure_code,
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
            # best-effort report persistence on the
            # ApplicationError-propagation path. Same observability
            # gate as the _BusinessRejection branch above.
            await self._persist_final_ingestion_report(
                request,
                framework_final_status="failed",
                warning_count=self._warning_count(),
                failure_code=getattr(exc, "type", None) or "ApplicationError",
                failure_message=self._error,
            )
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
            # final-effort report persistence on the
            # unexpected-exception path.
            await self._persist_final_ingestion_report(
                request,
                framework_final_status="failed",
                warning_count=self._warning_count(),
                failure_code=ERROR_TYPE_UNEXPECTED_ERROR,
                failure_message=self._error,
            )
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
 is recorded as FAILED — same semantic as `_warning_count`.

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
        # stage. Project the per-op string onto a macro-stage value
        #  so the search-attribute cardinality stays bounded
        # — `compile:doc-1` and `compile:doc-2` both become
        # `compiling`. Best-effort: fails silently if the attribute
        # isn't registered with the namespace.
        self._set_search_attribute(
            SEARCH_ATTR_INGEST_STAGE, _macro_ingest_stage(op),
        )

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
        # real Temporal worker (e.g. unit tests driving `run`
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
        # `extend` call) so the rules don't need a registry query.
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
        # metadata (workflow records `metadata={"document_id":...}`
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

    async def _persist_compile_strategy_report(
        self,
        request: ProjectProcessingRequest,
        *,
        document_id: str,
        initial_assessment: dict | None,
        final_assessment: dict | None,
        initial_mode: str | None,
        final_mode: str | None,
        attempts: list[dict],
        final_quality: str,
        final_retry_reason: str | None,
        final_warnings: list[str],
        unhandled_capabilities: list[str],
        plan_warnings: list[str],
        compile_result: "ArtifactActivityResult | None" = None,
    ) -> None:
        """Schedule the `persist_compile_strategy_report` activity.
 Best-effort — any persistence error inside the activity is
 swallowed there, and a Temporal-side failure here is also
 swallowed so observability never blocks ingest.

 When `compile_result` is supplied, the payload also carries an
 `extraction_evidence` block surfacing what the parser actually
 extracted (parser name, parse method, char/block counts,
 detected content types). The FE renders this distinctly from
 chunking status — extraction evidence is "what the probe saw",
 chunking is "what was indexed", and the two MUST stay
 separately verifiable."""
        if not request.correlation_id:
            return
        retry_used = bool(
            initial_mode is not None
            and final_mode is not None
            and initial_mode != final_mode
        )
        payload = {
            "schema_version": "1",
            "run_id": request.correlation_id,
            "document_id": document_id,
            # Two-mode model. `selected_compile_mode` is the
            # canonical mode of the FINAL successful (or
            # last-attempted) compile; `initial_mode` / `final_mode`
            # are kept for back-compat with existing FE consumers.
            "selected_compile_mode": final_mode or initial_mode,
            "initial_compile_mode": initial_mode,
            "final_compile_mode": final_mode,
            "initial_mode": initial_mode,  # legacy alias
            "final_mode": final_mode,      # legacy alias
            "retry_used": retry_used,
            # Operator-readable escalation reason when the retry
            # layer escalated mode mid-flight. Empty when no
            # escalation occurred (single-attempt success / no
            # retry needed). Drives the FE's "compile-safety retry
            # escalated mode" callout.
            "escalation_reason": _escalation_reason(
                initial_mode, final_mode, attempts, final_retry_reason,
            ),
            "attempts_count": len(attempts),
            "attempts": list(attempts),
            "final_compile_quality": final_quality,
            "final_retry_reason": final_retry_reason,
            "final_warnings": list(final_warnings),
            "assessment_plan": dict(final_assessment or initial_assessment or {}),
            "initial_assessment_plan": dict(initial_assessment or {}),
            "plan_warnings": list(plan_warnings),
            "unhandled_capabilities": list(unhandled_capabilities),
            "extraction_evidence": _build_extraction_evidence(compile_result),
            # Verification status block. Compile-stage success is
            # NOT enough to mark the run complete — chunks + index
            # must verify separately. This block carries the
            # known-at-compile-time state; downstream stage gates
            # update it as they run.
            "verification_status": {
                "compile": (
                    "succeeded"
                    if (final_quality not in {"failed"})
                    else "failed"
                ),
                "chunks": "pending_verification",
                "index": "pending_verification",
            },
        }
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_compile_strategy_report,
                PersistCompileStrategyReportInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    payload=payload,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — never block compile success on telemetry
            pass

    async def _persist_compile_result_summary(
        self,
        request: ProjectProcessingRequest,
        *,
        document_id: str,
        compile_result: "ArtifactActivityResult",
        retry_attempts: list[dict],
        final_quality_verdict: str | None,
    ) -> None:
        """Build the typed `NormalizedCompileResult` and persist it
 as a `compile_result_summary` artifact.

 Pure projection — the builder runs inside the workflow
 (sandbox-safe, no I/O). Best-effort persistence: a write
 failure logs at the activity layer; the run completes
 regardless because the durable signal for downstream stages
 is the inline `compile_result` the workflow already holds."""
        normalized = normalize_compile_result(
            compile_result,
            document_id=document_id,
            retry_attempts=retry_attempts,
            final_quality_verdict=final_quality_verdict,
        )
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_compile_result_summary,
                PersistCompileResultSummaryInput(
                    scope=request.scope,
                    run_id=request.correlation_id or "",
                    document_id=document_id,
                    payload=normalized.to_payload(),
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — never block compile success on telemetry
            pass

    async def _run_enrichment_stage(
        self,
        request: ProjectProcessingRequest,
        *,
        document_id: str,
        compile_result: "ArtifactActivityResult",
        enrich_plan: "PostCompileEnrichPlan",
        initial_plan_payload: dict | None,
    ) -> None:
        """Dispatch the typed enrichment overlay stage.

 Always invoked when both the initial plan + enrich plan are
 available — the activity itself decides whether to run the
 runner or produce a typed `skipped` overlay.

 Enforces `require_enrichment_success`: a `failed` enrichment
 when the policy requires success raises a
 `_BusinessRejection` with
 `FAILURE_CODE_ENRICHMENT_REQUIRED`, so the run lands at
 FAILED_FINAL with the dedicated reason code. Raw compile
 artifacts remain in the workspace either way.

 Optional-failure handling: a `failed` enrichment when the
 policy doesn't require success is logged + warning_count
 bumped, but the workflow continues to graph/index/finalize.
 Partial / warnings-only outcomes bump `_warning_count` so
 the final status surfaces as `succeeded_with_warnings`.
 """
        # Build the typed `NormalizedCompileResult.to_payload`
        # dict to thread through to the activity. The activity
        # reconstructs the typed dataclass on its side.
        normalized = normalize_compile_result(
            compile_result, document_id=document_id,
        )
        compile_payload = normalized.to_payload()
        await self._emit_step_lifecycle(
            request, stage="ENRICH",
            step="enrich_stage", action="started",
        )
        try:
            result = await workflow.execute_activity_method(
                ProcessingActivities.run_enrichment_stage,
                RunEnrichmentStageInput(
                    scope=request.scope,
                    run_id=request.correlation_id or "",
                    document_id=document_id,
                    compile_result_payload=compile_payload,
                    post_compile_enrich_plan_payload=enrich_plan.to_payload(),
                    initial_plan_payload=initial_plan_payload,
                    domain_override=request.domain_override,
                    workspace_default_domain=request.workspace_default_domain,
                    allowed_domain_overrides=(),
                    actor=request.actor,
                ),
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception as exc:  # noqa: BLE001 — surface as step failure
            await self._emit_step_lifecycle(
                request, stage="ENRICH",
                step="enrich_stage", action="failed",
            )
            self._log_step(
                request,
                event="ingestion.enrichment.stage_failed",
                stage="enrichment",
                status="failed",
                document_id=document_id,
                reason=f"activity raised: {type(exc).__name__}: {exc}",
            )
            # Treat an activity-level failure like a failed
            # enrichment for require_enrichment_success purposes.
            if enrich_plan.require_enrichment_success:
                self._record_step(
                    step="enrich_stage",
                    status=StepStatus.FAILED,
                    required=True,
                    source=StepSource.CALLER,
                    reason=f"enrichment activity raised: {exc}",
                    error=StepError(
                        type="EnrichmentRequired",
                        message=str(exc),
                        retryable=False,
                    ),
                    metadata={
                        "document_id": document_id,
                        "failure_code": FAILURE_CODE_ENRICHMENT_REQUIRED,
                    },
                )
                raise _BusinessRejection(
                    "enrichment activity failed and "
                    f"require_enrichment_success=True: {exc}",
                    failure_code=FAILURE_CODE_ENRICHMENT_REQUIRED,
                ) from exc
            self._record_step(
                step="enrich_stage",
                status=StepStatus.FAILED,
                required=False,
                source=StepSource.CALLER,
                reason=f"enrichment activity raised (optional): {exc}",
                metadata={"document_id": document_id},
            )
            return

        # Defensive: test stubs (or an unwired worker) may return
        # None. Treat as a no-op skip so the workflow continues
        # without recording a misleading outcome.
        if result is None:
            await self._emit_step_lifecycle(
                request, stage="ENRICH",
                step="enrich_stage", action="skipped",
            )
            return
        status = (
            getattr(result, "status", "succeeded") or "succeeded"
        )
        action = (
            "completed"
            if status in ("succeeded", "succeeded_with_warnings", "skipped")
            else "failed"
        )
        await self._emit_step_lifecycle(
            request, stage="ENRICH",
            step="enrich_stage", action=action,
        )
        #  fine-grained outcome label. Carried on the
        # structured log + the step_results metadata so the FE +
        # final-status projector branch on the same vocabulary.
        # Values: completed / completed_with_warnings / failed_optional
        # / failed_required / skipped.
        enrichment_outcome = _enrichment_outcome_label(
            enrichment_status=status,
            require_success=result.require_enrichment_success,
        )
        # surface require_success on the search-attribute
        # surface so ops can filter "what runs were governed by
        # require_success?" without parsing the audit log.
        self._set_search_attribute(
            SEARCH_ATTR_REQUIRE_ENRICHMENT_SUCCESS,
            "true" if result.require_enrichment_success else "false",
        )
        # write the enrichment retry count. Reserved
        # for future limiter-driven module retries; current
        # runner emits 0. Operators dashboard-aggregate this with
        # `J1CompileRetryCount` to see total per-run retry cost.
        self._set_search_attribute_int(
            SEARCH_ATTR_ENRICHMENT_RETRY_COUNT,
            int(getattr(result, "retry_count", 0) or 0),
        )
        self._log_step(
            request,
            event="ingestion.enrichment.stage_completed",
            stage="enrichment",
            status=enrichment_outcome,
            document_id=document_id,
            reason=(
                f"enrichment_outcome={enrichment_outcome} "
                f"enrichment_status={status} "
                f"require_success={result.require_enrichment_success} "
                f"persist_error={result.persist_error or 'none'}"
            ),
        )

        # Record the enrichment step on the workflow's step_results
        # list so the final-summary / status surface reflects the
        # outcome. The existing `_warning_count` method counts
        # FAILED-but-not-required step results — recording an
        # optional-failed step here bumps the count, lifting the
        # final status to `succeeded_with_warnings`.
        if status == "failed":
            if result.require_enrichment_success:
                self._record_step(
                    step="enrich_stage",
                    status=StepStatus.FAILED,
                    required=True,
                    source=StepSource.CALLER,
                    reason=(
                        "enrichment failed and "
                        "require_enrichment_success=True"
                    ),
                    error=StepError(
                        type="EnrichmentRequired",
                        message=(
                            f"enrichment failed for {document_id}"
                        ),
                        retryable=False,
                    ),
                    metadata={
                        "document_id": document_id,
                        "failure_code": FAILURE_CODE_ENRICHMENT_REQUIRED,
                        "enrichment_outcome": enrichment_outcome,
                    },
                )
                raise _BusinessRejection(
                    (
                        f"enrichment failed for {document_id} and "
                        "require_enrichment_success=True"
                    ),
                    failure_code=FAILURE_CODE_ENRICHMENT_REQUIRED,
                )
            # Optional-failure path: record as a non-required
            # FAILED step so `_warning_count` picks it up and
            # the workflow continues to graph/index/finalize.
            self._record_step(
                step="enrich_stage",
                status=StepStatus.FAILED,
                required=False,
                source=StepSource.CALLER,
                reason="enrichment failed (optional)",
                metadata={
                    "document_id": document_id,
                    "enrichment_outcome": enrichment_outcome,
                },
            )
        elif status == "succeeded_with_warnings":
            # Record as FAILED + required=False to bump
            # `_warning_count` so the final status lifts to
            # `succeeded_with_warnings`. The metadata carries the
            # actual outcome string so the FE renders the right
            # copy.
            self._record_step(
                step="enrich_stage",
                status=StepStatus.FAILED,
                required=False,
                source=StepSource.CALLER,
                reason="enrichment completed with warnings",
                metadata={
                    "document_id": document_id,
                    "enrichment_status": "succeeded_with_warnings",
                    "enrichment_outcome": enrichment_outcome,
                },
            )
        elif status == "skipped":
            self._record_step(
                step="enrich_stage",
                status=StepStatus.SKIPPED,
                required=False,
                source=StepSource.CALLER,
                reason="enrichment skipped by post-compile assessor",
                metadata={
                    "document_id": document_id,
                    "enrichment_outcome": enrichment_outcome,
                },
            )
        else:
            # "succeeded"
            self._record_step(
                step="enrich_stage",
                status=StepStatus.COMPLETED,
                required=False,
                source=StepSource.CALLER,
                reason="enrichment overlay produced",
                metadata={
                    "document_id": document_id,
                    "artifact_id": result.artifact_id or "",
                    "enrichment_outcome": enrichment_outcome,
                },
            )

    async def _run_post_compile_enrich_assessment(
        self,
        request: ProjectProcessingRequest,
        *,
        document_id: str,
        compile_result: "ArtifactActivityResult",
        final_compile_quality: str,
    ) -> "PostCompileEnrichPlan | None":
        """Run the rule-based post-compile enrich assessment, optionally
 consult a fast LLM for ambiguous (OPTIONAL) cases, persist the
 resulting `post_compile_enrich_plan` artifact, and return the
 plan for downstream stage gating.

 The rule-based assessor is pure — runs inline (deterministic,
 Temporal-sandbox-safe). The fast-LLM consult is dispatched via
 an activity which honours `J1_ENRICH_ASSESSMENT_FAST_LLM_*`
 env settings; consult failures NEVER block ingestion (we fall
 back to the rule-based plan). The post-compile artifact write
 is the only other activity dispatched here."""
        try:
            signals = build_signals_from_compile_metrics(
                compile_status=str(compile_result.status),
                final_compile_quality=final_compile_quality,
                content_stats=compile_result.content_stats,
                compile_metrics=compile_result.compile_metrics,
            )
            plan = assess_post_compile_enrich(signals)
        except Exception as exc:  # noqa: BLE001 — defensive; assessor is pure
            workflow.logger.warning(
                "post-compile enrich assessment failed: %s", exc,
            )
            return None

        # Optional fast-LLM consult — only when the rule-based plan is
        # ambiguous (OPTIONAL). The consult activity short-circuits
        # internally when env-disabled / no callable wired; we still
        # dispatch unconditionally so the env gate lives in one place.
        if is_consult_warranted(plan):
            plan = await self._maybe_consult_fast_llm(
                request,
                document_id=document_id,
                rule_based_plan=plan,
                signals=signals,
                compile_result=compile_result,
                final_compile_quality=final_compile_quality,
            )

        if not request.correlation_id:
            return plan
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_post_compile_enrich_plan,
                PersistPostCompileEnrichPlanInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    payload=plan.to_payload(),
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — observability never blocks ingest
            pass
        return plan

    async def _maybe_consult_fast_llm(
        self,
        request: ProjectProcessingRequest,
        *,
        document_id: str,
        rule_based_plan: "PostCompileEnrichPlan",
        signals,
        compile_result: "ArtifactActivityResult",
        final_compile_quality: str,
    ) -> "PostCompileEnrichPlan":
        """Dispatch the fast-LLM consult activity and apply any
 refinement to the rule-based plan. Activity failures
 (timeout, missing config, invalid JSON, exception) all fall
 back to the unmodified rule-based plan — ingestion MUST NEVER
 fail because the optional consult had a bad day.

 SKIP plans never reach this method (they're filtered upstream
 by `is_consult_warranted`); even if they did, the pure
 `apply_fast_llm_refinement` blocks SKIP overrides."""
        compile_warnings = list(
            (compile_result.compile_metrics or {}).get("plan_warnings", [])
            or []
        )
        try:
            consult_result = await workflow.execute_activity_method(
                ProcessingActivities.fast_llm_consult_enrich,
                FastLLMConsultEnrichInput(
                    scope=request.scope,
                    run_id=request.correlation_id or "",
                    document_id=document_id,
                    compile_status=str(compile_result.status),
                    final_compile_quality=final_compile_quality,
                    source_signals=dict(rule_based_plan.source_signals),
                    provisional_recommendation=(
                        rule_based_plan.overall_recommendation.value
                    ),
                    provisional_recommended_tasks=list(
                        rule_based_plan.recommended_tasks
                    ),
                    provisional_skipped_tasks=list(
                        rule_based_plan.skipped_tasks
                    ),
                    compile_warnings=compile_warnings,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
            # Result-shape access is inside the try so a stub /
            # legacy worker that returns the wrong shape can't crash
            # ingestion — we fall back to the rule-based plan.
            if not getattr(consult_result, "consulted", False):
                return rule_based_plan
            rec_value = getattr(consult_result, "recommendation", None)
            rec: EnrichRecommendation | None = None
            if rec_value:
                try:
                    rec = EnrichRecommendation(rec_value)
                except ValueError:
                    rec = None
            refinement = FastLLMRefinement(
                recommendation=rec,
                add_reasons=tuple(
                    getattr(consult_result, "add_reasons", None) or ()
                ),
                add_recommended_tasks=tuple(
                    getattr(consult_result, "add_recommended_tasks", None) or ()
                ),
            )
            return apply_fast_llm_refinement(rule_based_plan, refinement)
        except Exception as exc:  # noqa: BLE001 — consult never blocks ingest
            try:
                workflow.logger.warning(
                    "fast-LLM consult activity failed; using rule-based plan: %s",
                    exc,
                )
            except Exception:  # noqa: BLE001 — log can fail outside workflow runtime in tests
                pass
            return rule_based_plan

    def _evaluate_compile_attempt(
        self,
        compile_result: "ArtifactActivityResult",
        *,
        retry_settings: "CompileRetrySettings",
        assessment_payload: dict | None,
    ) -> "QualityVerdict":
        """Wrap `evaluate_compile_quality` with the workflow's
 per-attempt context (signals from `compile_metrics`, the
 plan's OCR-required hint, the resolved parse_method). Pure
 — never re-invokes any activity."""
        # Build a synthetic ArtifactProcessingResult from the
        # ArtifactActivityResult so the evaluator's primary signature
        # (which reads metadata + status from a result dataclass)
        # works without a duplicate code path.
        metrics = dict(compile_result.compile_metrics or {})
        synthetic = ArtifactProcessingResult(
            status=(
                ResultStatus.SUCCEEDED
                if str(compile_result.status) == "succeeded"
                else ResultStatus.FAILED
            ),
            drafts=[],
            artifacts=[],
            error=compile_result.error,
            message=compile_result.message,
            metadata={
                "chunks_count": int(metrics.get("chunks_count", 0)),
                **(
                    {"total_text_chars": metrics["extracted_text_chars"]}
                    if isinstance(metrics.get("extracted_text_chars"), int)
                    else {}
                ),
            },
        )
        plan_required_ocr = bool(
            assessment_payload
            and "ocr" in (
                assessment_payload.get("required_capabilities") or ()
            )
        )
        mode = (assessment_payload or {}).get("mode") if assessment_payload else None
        parse_method = _parse_method_for_mode(mode) if mode else None
        return evaluate_compile_quality(
            synthetic,
            min_text_chars=retry_settings.min_text_chars,
            min_chunks=retry_settings.min_chunks,
            plan_required_ocr=plan_required_ocr,
            parse_method_used=parse_method,
        )

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

    async def _persist_final_ingestion_report(
        self,
        request: ProjectProcessingRequest,
        *,
        framework_final_status: str,
        warning_count: int = 0,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """persist the aggregated `final_ingestion_report`
 artifact at workflow terminal.

 The activity reads the per-stage artifact payloads on the
 worker side (workflow code can't do I/O), builds the typed
 report, and persists it. Best-effort: any failure is
 logged inside the activity and the workflow proceeds. The
 report is observability, not correctness."""
        if not request.correlation_id:
            return
        document_id: str | None = None
        document_name: str | None = None
        for r in reversed(self._step_results):
            if isinstance(r.metadata, dict):
                doc = r.metadata.get("document_id")
                if doc:
                    document_id = str(doc)
                    name = r.metadata.get("document_name")
                    if name:
                        document_name = str(name)
                    break
        # Workflow start/end timestamps from the run-state cache.
        started_at = (
            self._workflow_started_at_iso
            if hasattr(self, "_workflow_started_at_iso") else None
        )
        completed_at = _safe_now_iso()
        try:
            await workflow.execute_activity_method(
                ProcessingActivities.persist_final_ingestion_report,
                PersistFinalIngestionReportInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    document_id=document_id,
                    document_name=document_name,
                    framework_final_status=framework_final_status,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    warning_count=warning_count,
                    started_at=started_at,
                    completed_at=completed_at,
                    actor=request.actor,
                ),
                start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY.to_temporal(),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks exit
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
 activity (the assessment + post-compile-assessment phases).
 Best-effort like every emit helper; failure never blocks the
 workflow."""
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

    async def _emit_attempt(
        self,
        request: ProjectProcessingRequest,
        *,
        action: str,
        attempt: int,
        document_id: str | None = None,
        artifact_id: str | None = None,
        mode: str | None = None,
        next_mode: str | None = None,
        duration_ms: int | None = None,
        success: bool | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        """Emit a compile / enrich attempt or retry-scheduled event.

        Wraps ``RunsActivities.report_attempt``, which forwards to
        the diagnostic recorder. Best-effort: a telemetry failure
        never blocks the workflow; that's why we don't propagate
        errors here. Used by the compile retry loop and the
        per-artifact enrich loop."""
        if not request.correlation_id:
            return
        try:
            await workflow.execute_activity_method(
                RunsActivities.report_attempt,
                ReportAttemptInput(
                    scope=request.scope,
                    run_id=request.correlation_id,
                    action=action,
                    attempt=attempt,
                    document_id=document_id,
                    artifact_id=artifact_id,
                    mode=mode,
                    next_mode=next_mode,
                    duration_ms=duration_ms,
                    success=success,
                    reason=reason,
                    error=error,
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


    def _stage_enabled(
        self,
        stage: str,
        request_kind: str | None,
        *,
        compile_result: "ArtifactActivityResult | None" = None,
        final_compile_quality: str | None = None,
        enrich_plan: "PostCompileEnrichPlan | None" = None,
    ) -> tuple[bool, str | None, StepSource]:
        """Resolve "should this stage run?" using compile evidence +
 the post-compile enrich plan + the request's stage kind.

 Returns `(enabled, skip_reason, source)`. The reason is None
 when enabled; populated when skipped so the workflow can pass
 it to `_record_step`.

 There is intentionally no IngestPlan parameter — gating is
 compile-first. Pre-compile guesses do not influence
 enrich/graph/index decisions.

 Per-stage rules:
 * `enrich`:
 - SKIP from enrich plan (with blocking issues) →
 skip with `PLANNER` source.
 - RECOMMENDED / REQUIRED → run with `PLANNER` source.
 - Else → defer to caller intent (`CALLER`).
 * `graph`:
 - compile failed / final quality FAILED → skip
 (`PLANNER`).
 - zero chunks produced → skip (`PLANNER`).
 - enrich plan recommends SKIP for blocking compile
 issues → skip (`PLANNER`).
 - low compile quality WITHOUT a caller force →
 skip (`PLANNER`) to avoid extracting from a
 degraded parse.
 - Else → run (`CALLER`).
 * `index`:
 - compile failed → skip (`PLANNER`).
 - zero chunks produced → skip (`PLANNER`).
 - Else → run (`CALLER`).
 """
        if not request_kind:
            return False, f"{stage}_kind not provided in request", StepSource.CALLER

        compile_status = (
            str(getattr(compile_result, "status", "")) if compile_result else ""
        )
        chunks_count = 0
        if compile_result is not None:
            metrics = getattr(compile_result, "compile_metrics", None) or {}
            chunks_count = int(metrics.get("chunks_count", 0) or 0)
            if chunks_count == 0:
                # Fall back to counting `chunk` kinds when the metrics
                # didn't surface a count (legacy compilers / fakes).
                kinds = getattr(compile_result, "kinds", ()) or ()
                chunks_count = sum(1 for k in kinds if k == "chunk")

        if stage == "enrich":
            if enrich_plan is not None:
                if enrich_plan.overall_recommendation == EnrichRecommendation.SKIP:
                    reason = (
                        "; ".join(enrich_plan.blocking_issues)
                        or "post-compile enrich plan recommends SKIP"
                    )
                    return False, reason, StepSource.PLANNER
                if enrich_plan.overall_recommendation in (
                    EnrichRecommendation.RECOMMENDED,
                    EnrichRecommendation.REQUIRED,
                ):
                    return True, None, StepSource.PLANNER
            return True, None, StepSource.CALLER

        if stage == "graph":
            if compile_result is not None and compile_status != "succeeded":
                return (
                    False,
                    "compile did not succeed; cannot build graph",
                    StepSource.PLANNER,
                )
            if final_compile_quality == "failed":
                return (
                    False,
                    "final compile quality is FAILED; refusing to graph",
                    StepSource.PLANNER,
                )
            if compile_result is not None and chunks_count == 0:
                return (
                    False,
                    "compile produced zero chunks; nothing to graph",
                    StepSource.PLANNER,
                )
            if (
                enrich_plan is not None
                and enrich_plan.overall_recommendation == EnrichRecommendation.SKIP
                and enrich_plan.blocking_issues
            ):
                return (
                    False,
                    "; ".join(enrich_plan.blocking_issues),
                    StepSource.PLANNER,
                )
            if final_compile_quality == "low":
                return (
                    False,
                    "compile quality is LOW; skipping graph to avoid "
                    "extracting from a degraded parse",
                    StepSource.PLANNER,
                )
            return True, None, StepSource.CALLER

        if stage == "index":
            if compile_result is not None and compile_status != "succeeded":
                return (
                    False,
                    "compile did not succeed; nothing to index",
                    StepSource.PLANNER,
                )
            if compile_result is not None and chunks_count == 0:
                return (
                    False,
                    "compile produced zero chunks; nothing to index",
                    StepSource.PLANNER,
                )
            return True, None, StepSource.CALLER

        # Unknown stage — caller-driven by default.
        return True, None, StepSource.CALLER

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

        # Compile-first: the only pre-compile work is a cheap
        # deterministic profile (pypdf-based, no LLM, no IngestPlanner)
        # used to derive the AssessmentPlan that drives compile config
        # (parse_method + per-capability toggles). All gating
        # decisions for downstream stages (enrich / graph / index)
        # happen post-compile from compile evidence + the
        # `PostCompileEnrichPlan`. There is no IngestPlan object
        # threaded through this workflow.
        #
        # Failure handling is governed by
        # `request.assessment_failure_policy` (read from
        # `J1_ASSESSMENT_FAILURE_POLICY` at request-build time):
        #  * `fail_open` (default) — assessment failure logs +
        #  leaves payload None; bridge falls back to
        #  `settings.parse_method`. Production-friendly.
        #  * `fail_closed` — assessment failure raises
        #  `_BusinessRejection`; compile step recorded FAILED;
        #  run lands at FAILED_FINAL.
        assessment_payload: dict | None = None
        initial_plan_payload: dict | None = None
        if request.planner_enabled:
            # Wrap the cheap pre-compile work (profile + AssessmentPlan
            # build) in synthetic step.* events so the FE timeline +
            # status panel reflect the assessment stage. Both events
            # are best-effort observability — failure never blocks
            # the workflow's actual assessment work below.
            await self._emit_step_lifecycle(
                request, stage="ASSESS_COMPILE_STRATEGY",
                step="assess_compile_strategy", action="started",
            )
            try:
                profile = await workflow.execute_activity_method(
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
                # Build the InitialExecutionPlan via an
                # activity so domain pack resolution + persistence
                # stay outside the sandbox. The activity returns the
                # plan payload; the workflow holds the compile-stage
                # AssessmentPlan as the legacy `assessment_payload`
                # so existing per-compile-attempt code paths still
                # work unchanged.
                #
                # Backward-compat: if the activity isn't wired (None
                # return) or carries no plan_payload, fall back to
                # the legacy in-workflow DefaultAssessmentPlanner.
                # Lets existing test harnesses + deployments that
                # haven't registered the new activity keep working.
                build_result = await workflow.execute_activity_method(
                    ProcessingActivities.build_initial_execution_plan,
                    BuildInitialExecutionPlanInput(
                        scope=request.scope,
                        run_id=request.correlation_id or "",
                        document_id=document_id,
                        profile=profile,
                        domain_override=request.domain_override,
                        workspace_default_domain=request.workspace_default_domain,
                        allowed_domain_overrides=(),
                        actor=request.actor,
                    ),
                    start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
                    retry_policy=DEFAULT_RETRY.to_temporal(),
                )
                plan_payload_raw = (
                    getattr(build_result, "plan_payload", None)
                    if build_result is not None else None
                )
                if plan_payload_raw:
                    initial_plan_payload = dict(plan_payload_raw)
                    assessment_payload = (
                        initial_plan_payload.get("compile_plan") or None
                    )
                else:
                    # Legacy fallback — build the AssessmentPlan
                    # workflow-side just like wiring.
                    fallback = DefaultAssessmentPlanner().assess(profile)
                    assessment_payload = fallback.to_payload()
                    initial_plan_payload = None
                # Surface the assessment mode as a Temporal search
                # attribute so operators can filter histories without
                # reading the audit log. INGEST_MODE comes from the
                # AssessmentPlan's compile-mode (fast/standard/deep);
                # vision / premium-LLM search attrs are deferred until
                # after compile so they reflect compile evidence +
                # post-compile assessment, not pre-compile guesses.
                mode_value = (
                    assessment_payload.get("mode")
                    if assessment_payload else None
                )
                if mode_value:
                    self._set_search_attribute(
                        SEARCH_ATTR_INGEST_MODE, mode_value,
                    )
                confidence = (
                    assessment_payload.get("confidence")
                    if assessment_payload else None
                )
                domain_id = (
                    initial_plan_payload.get("domain_profile_id")
                    if initial_plan_payload else None
                )
                enrichment_policy_value = (
                    initial_plan_payload.get("enrichment_policy")
                    if initial_plan_payload else None
                )
                self._log_step(
                    request,
                    event="ingestion.assessment.created",
                    stage="assessment",
                    status="completed",
                    document_id=document_id,
                    reason=(
                        f"mode={mode_value} confidence={confidence} "
                        f"domain={domain_id or 'none'} "
                        f"policy={enrichment_policy_value or 'none'}"
                    ),
                )
                # surface domain + policy as search attributes
                # so ops dashboards can filter by domain pack or
                # enrichment policy without crawling audit logs.
                if domain_id:
                    self._set_search_attribute(
                        SEARCH_ATTR_DOMAIN_PROFILE_ID, domain_id,
                    )
                if enrichment_policy_value:
                    self._set_search_attribute(
                        SEARCH_ATTR_ENRICHMENT_POLICY,
                        enrichment_policy_value,
                    )
                await self._emit_step_lifecycle(
                    request, stage="ASSESS_COMPILE_STRATEGY",
                    step="assess_compile_strategy", action="completed",
                )
                # The assessment plan is built and ready for the
                # compile gate to consume. Surface the canonical
                # `assessment_ready` macro stage so ops dashboards
                # see a stable transition between assessing and
                # whatever runs next (compile_pending under
                # two-phase, compiling under one-phase).
                self._set_search_attribute(
                    SEARCH_ATTR_INGEST_STAGE, INGEST_STAGE_ASSESSMENT_READY,
                )
            except Exception as exc:  # noqa: BLE001 — handled per policy
                # Surface the failure on the timeline before the
                # fail_open / fail_closed branches handle it below.
                await self._emit_step_lifecycle(
                    request, stage="ASSESS_COMPILE_STRATEGY",
                    step="assess_compile_strategy", action="failed",
                )
                if request.assessment_failure_policy == \
                        ASSESSMENT_FAILURE_POLICY_FAIL_CLOSED:
                    self._record_step(
                        step="compile",
                        status=StepStatus.FAILED,
                        required=True,
                        source=StepSource.CALLER,
                        reason=(
                            f"AssessmentPlan build failed under "
                            f"fail_closed policy: {exc}"
                        ),
                        error=StepError(
                            type="AssessmentPlanFailed",
                            message=str(exc),
                            retryable=False,
                        ),
                        metadata={"document_id": document_id},
                    )
                    raise _BusinessRejection(
                        f"AssessmentPlan build failed for {document_id}: "
                        f"{exc}"
                    )
                # fail_open: log + proceed with settings.parse_method.
                self._log_step(
                    request,
                    event="ingestion.assessment.failed",
                    stage="assessment",
                    status="warning",
                    document_id=document_id,
                    reason=f"assessment build failed (fail_open): {exc}",
                )
                assessment_payload = None

        compile_op = f"{OPERATION_COMPILE}:{document_id}"
        if await self._gate_before_expensive(request, compile_op):
            return

        # ---- Two-phase compile trigger --------------------------
        # When the request opted into two-phase compile, park the
        # workflow until the user/operator invokes
        # `POST /ingestion-runs/{id}/compile`. The endpoint sends
        # `SIGNAL_TRIGGER_COMPILE`, which flips `_compile_triggered`
        # and releases the gate. Cancel still wins — a cancel
        # received while parked drops us out without dispatching the
        # activity. The gate also runs per-document, so a multi-doc
        # run gates each doc separately.
        if request.two_phase_compile:
            await self._await_compile_trigger(document_id=document_id)
            if self._cancelled:
                return

        # ---- Compile-safety retry loop --------------------------
        # One attempt is the legacy path. When
        # `request.compile_retry_enabled` is True (default) AND the
        # `CompileQualityEvaluator` flags the result for retry, we
        # escalate the AssessmentPlan's mode (fast→standard→deep)
        # and dispatch the activity again. Idempotency: the activity-
        # side cache key includes `mode`, so escalating gets a
        # fresh cache row + no double-write of artifacts; same-mode
        # Temporal-level retries hit the cache and short-circuit.
        self._begin(compile_op)
        compile_attempts_payload: list[dict] = []
        current_assessment_payload = assessment_payload
        initial_mode = (
            (assessment_payload or {}).get("mode") if assessment_payload
            else None
        )
        retry_settings = CompileRetrySettings(
            enabled=request.compile_retry_enabled,
            max_attempts=request.compile_max_attempts,
            min_text_chars=request.compile_retry_min_text_chars,
            min_chunks=request.compile_retry_min_chunks,
        )
        max_attempts = retry_settings.max_attempts if retry_settings.enabled else 1
        compile_result: "ArtifactActivityResult | None" = None
        final_quality = QUALITY_GOOD  # downgraded if retry layer says so
        final_retry_reason: str | None = None
        final_attempt_warnings: list[str] = []

        for attempt_n in range(1, max_attempts + 1):
            attempt_started_at = _safe_now_iso()
            current_mode_for_attempt = (
                (current_assessment_payload or {}).get("mode")
                if current_assessment_payload else None
            )
            attempt_started_clock = _safe_now()
            await self._emit_attempt(
                request,
                action=EVENT_COMPILE_ATTEMPT_STARTED,
                attempt=attempt_n,
                document_id=document_id,
                mode=current_mode_for_attempt,
            )
            compile_result = await workflow.execute_activity_method(
                ProcessingActivities.compile,
                CompileActivityInput(
                    scope=request.scope,
                    document_id=document_id,
                    processor_kind=request.compiler_kind,
                    actor=request.actor,
                    correlation_id=request.correlation_id,
                    assessment_plan_payload=current_assessment_payload,
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
            attempt_completed_at = _safe_now_iso()
            attempt_completed_clock = _safe_now()
            attempt_duration_ms = int(
                (attempt_completed_clock - attempt_started_clock)
                .total_seconds() * 1000
            )
            verdict = self._evaluate_compile_attempt(
                compile_result,
                retry_settings=retry_settings,
                assessment_payload=current_assessment_payload,
            )
            current_mode = (
                (current_assessment_payload or {}).get("mode")
                if current_assessment_payload else None
            )
            await self._emit_attempt(
                request,
                action=EVENT_COMPILE_ATTEMPT_COMPLETED,
                attempt=attempt_n,
                document_id=document_id,
                mode=current_mode,
                duration_ms=attempt_duration_ms,
                success=(str(compile_result.status) == "succeeded"),
                reason=verdict.retry_reason,
                error=compile_result.error,
            )
            current_parse_method = (
                _parse_method_for_mode(current_mode)
                if current_mode else None
            )
            attempt_record = {
                "attempt_number": attempt_n,
                "mode": current_mode,
                "parser": request.compiler_kind,
                "parse_method": current_parse_method,
                "started_at": attempt_started_at,
                "completed_at": attempt_completed_at,
                "status": str(compile_result.status),
                "chunks_count": int(
                    compile_result.compile_metrics.get("chunks_count", 0)
                ) if compile_result.compile_metrics else 0,
                "extracted_text_chars": (
                    compile_result.compile_metrics.get("extracted_text_chars")
                    if compile_result.compile_metrics else None
                ),
                "quality": verdict.quality,
                "retry_reason": verdict.retry_reason,
                "warnings": list(
                    (compile_result.compile_metrics or {}).get(
                        "plan_warnings", []
                    )
                ),
                "mapped_compile_config": {
                    "parse_method": current_parse_method,
                    "assessment_mode": current_mode,
                    "unhandled_capabilities": list(
                        (compile_result.compile_metrics or {}).get(
                            "unhandled_capabilities", []
                        )
                    ),
                },
            }
            compile_attempts_payload.append(attempt_record)
            final_quality = verdict.quality

            # Decide retry. Stop on the last allowed attempt or when
            # the verdict says no retry. `next_compile_mode` returns
            # None for `deep` — that's the "no further escalation"
            # terminus the spec calls out.
            if not verdict.should_retry() or attempt_n >= max_attempts:
                final_retry_reason = (
                    verdict.retry_reason
                    if attempt_n >= max_attempts and verdict.should_retry()
                    else None
                )
                if attempt_n >= max_attempts and verdict.should_retry():
                    final_attempt_warnings.append(
                        f"max_compile_attempts={max_attempts} reached; "
                        f"final quality={verdict.quality}"
                    )
                break
            try:
                current_mode_enum = CompileMode(current_mode or "")
            except ValueError:
                # No initial plan → no mode to escalate from. Treat
                # the failure as terminal (legacy path falls through
                # to the existing failure handling).
                break
            next_mode = next_compile_mode(current_mode_enum)
            if next_mode is None:
                # Already at deep; the verdict requested retry but
                # there's no higher mode. Terminate with low/failed
                # quality + record the reason.
                final_retry_reason = verdict.retry_reason
                final_attempt_warnings.append(
                    "deep mode failed; no higher mode to escalate to"
                )
                break

            attempt_record["status"] = "retried"
            # Build a fresh AssessmentPlan payload with the escalated
            # mode. Required-capability sets get the OCR augmentation
            # when the verdict was "ocr likely needed" so the deeper
            # mode locks OCR on at the mapper.
            new_payload = dict(current_assessment_payload or {})
            new_payload["mode"] = next_mode.value
            if verdict.retry_reason == "ocr_likely_needed":
                required = set(new_payload.get("required_capabilities") or ())
                required.add("ocr")
                new_payload["required_capabilities"] = sorted(required)
            new_payload["reason"] = (
                f"escalated to {next_mode.value} after "
                f"{verdict.retry_reason} on attempt {attempt_n}"
            )
            current_assessment_payload = new_payload
            self._log_step(
                request,
                event="ingestion.compile.retry.scheduled",
                stage="compile",
                status="warning",
                document_id=document_id,
                reason=(
                    f"retrying compile: attempt={attempt_n} reason="
                    f"{verdict.retry_reason} → next_mode={next_mode.value}"
                ),
            )
            # Audit-event sibling of the existing workflow log line;
            # ``_log_step`` writes to the workflow logger only, this
            # call lands the retry on the events.jsonl stream so
            # operators tailing ``/ingestion-runs/{id}/events`` see
            # the escalation in real time.
            await self._emit_attempt(
                request,
                action=EVENT_COMPILE_RETRY_SCHEDULED,
                attempt=attempt_n,
                document_id=document_id,
                mode=current_mode,
                next_mode=next_mode.value,
                reason=verdict.retry_reason,
            )
        # write the compile retry count to the search
        # attribute surface so ops dashboards can aggregate by
        # parse-cost. `retry_count` is attempts beyond the first
        # (0 == no retries, N == N retries after the first attempt).
        # Best-effort upsert: gated on `search_attributes_enabled`.
        self._set_search_attribute_int(
            SEARCH_ATTR_COMPILE_RETRY_COUNT,
            max(0, len(compile_attempts_payload) - 1),
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

        # ── Compile-strategy report ────────────────────────────────
        # Persist the AssessmentPlan + per-attempt audit + final
        # quality verdict as a `compile_strategy_report` artifact so
        # the FE's run-detail page can render the timeline + banners.
        # Best-effort: any persistence error is logged inside the
        # activity, the run still succeeds.
        await self._persist_compile_strategy_report(
            request,
            document_id=document_id,
            initial_assessment=assessment_payload,
            final_assessment=current_assessment_payload,
            initial_mode=initial_mode,
            final_mode=(
                (current_assessment_payload or {}).get("mode")
                if current_assessment_payload else None
            ),
            attempts=compile_attempts_payload,
            final_quality=final_quality,
            final_retry_reason=final_retry_reason,
            final_warnings=final_attempt_warnings,
            unhandled_capabilities=list(
                (compile_result.compile_metrics or {}).get(
                    "unhandled_capabilities", []
                )
            ),
            plan_warnings=list(
                (compile_result.compile_metrics or {}).get(
                    "plan_warnings", []
                )
            ),
            compile_result=compile_result,
        )

        # ── Normalized compile result ─────────────────────
        # Build the typed `NormalizedCompileResult` projection over
        # the activity result + retry history, and persist it as a
        # `compile_result_summary` artifact. Downstream consumers
        # (post-compile assessor, FE Compile Result panel, final
        # report) branch on the typed fields instead of nested
        # dicts. Raw vendor output stays in the workspace and is
        # referenced by id only via `raw_artifact_refs`.
        await self._persist_compile_result_summary(
            request,
            document_id=document_id,
            compile_result=compile_result,
            retry_attempts=compile_attempts_payload,
            final_quality_verdict=final_quality,
        )

        # ── Post-compile enrich assessment ────────────────────────
        # Rule-based assessor decides whether downstream enrichment
        # tasks (table / image / vision / quality) should run. The
        # plan is persisted as a `post_compile_enrich_plan` artifact
        # for FE rendering + future stage-gate consultation. Wrapped
        # in step.* lifecycle events so the FE timeline + status
        # panel surface the assessment stage explicitly.
        await self._emit_step_lifecycle(
            request, stage="ASSESS_ENRICHMENT",
            step="assess_enrichment", action="started",
        )
        enrich_plan = await self._run_post_compile_enrich_assessment(
            request,
            document_id=document_id,
            compile_result=compile_result,
            final_compile_quality=final_quality,
        )
        if enrich_plan is not None:
            self._log_step(
                request,
                event="ingestion.post_compile.enrich_assessment",
                stage="post_compile_assess",
                status="completed",
                document_id=document_id,
                reason=(
                    f"enrich={enrich_plan.overall_recommendation.value} "
                    f"recommended={list(enrich_plan.recommended_tasks)}"
                ),
            )
        await self._emit_step_lifecycle(
            request, stage="ASSESS_ENRICHMENT",
            step="assess_enrichment",
            action="completed" if enrich_plan is not None else "skipped",
        )

        # ── typed enrichment stage ──────────────────────
        # Dispatch the CompositeEnrichmentRunner via an activity
        # (registry + persistence happen activity-side; the
        # workflow stays sandbox-safe). The activity short-circuits
        # to a typed `skipped` result when `should_enrich=False`,
        # so the run still produces an explicit overlay record.
        # `require_enrichment_success` enforcement runs after the
        # activity returns — a `failed` enrichment + required flag
        # raises `_BusinessRejection` with the dedicated failure
        # code so the run terminates cleanly.
        if enrich_plan is not None and initial_plan_payload is not None:
            await self._run_enrichment_stage(
                request,
                document_id=document_id,
                compile_result=compile_result,
                enrich_plan=enrich_plan,
                initial_plan_payload=initial_plan_payload,
            )

        # Compile-result-driven search attributes. Both flags are
        # derived ONLY from compile evidence + the post-compile
        # enrich plan — never from pre-compile guesses.
        #
        #  * REQUIRES_VISION — true iff compile actually saw images
        #  in the document (content_stats.has_images or
        #  image_count > 0). Operators filter Temporal histories
        #  by this to find runs that hit the VLM path.
        #  * REQUIRES_PREMIUM_LLM — true iff the post-compile
        #  enrich plan recommends or requires a vision-aware
        #  enrichment task; signals that downstream stages will
        #  consume an LLM with image input.
        self._set_search_attribute(
            SEARCH_ATTR_REQUIRES_VISION,
            "true" if _compile_saw_images(compile_result) else "false",
        )
        self._set_search_attribute(
            SEARCH_ATTR_REQUIRES_PREMIUM_LLM,
            "true" if _enrich_plan_needs_premium_llm(enrich_plan) else "false",
        )

        await self._maybe_review(request, GATE_AFTER_COMPILE)
        if self._cancelled:
            return

        # Stage gate: the post-compile enrich plan is authoritative
        # for the enrich stage; absent that, defer to the caller's
        # enricher_kind. `_stage_enabled` codifies the precedence
        # rules — no IngestPlan involved.
        enrich_enabled, enrich_reason, enrich_source = self._stage_enabled(
            "enrich", request.enricher_kind,
            compile_result=compile_result,
            final_compile_quality=final_quality,
            enrich_plan=enrich_plan,
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
            # Accumulate enrich-produced artifact IDs across the
            # per-artifact loop so the validation gate (run ONCE at
            # the loop's end) sees the aggregate output. Per-artifact
            # validation would create N reports per run; one report
            # per stage per run is the contract.
            enrich_produced_ids: list[str] = []
            for enrich_attempt_idx, artifact_id in enumerate(
                list(compile_result.artifact_ids), start=1,
            ):
                enrich_op = f"{OPERATION_ENRICH}:{artifact_id}"
                if await self._gate_before_expensive(request, enrich_op):
                    return
                self._begin(enrich_op)
                enrich_started_clock = _safe_now()
                await self._emit_attempt(
                    request,
                    action=EVENT_ENRICHMENT_ATTEMPT_STARTED,
                    attempt=enrich_attempt_idx,
                    document_id=document_id,
                    artifact_id=artifact_id,
                    mode=request.enricher_kind,
                )
                enrich_result: ArtifactActivityResult = (
                    await workflow.execute_activity_method(
                        ProcessingActivities.enrich,
                        EnrichActivityInput(
                            scope=request.scope,
                            artifact_id=artifact_id,
                            processor_kind=request.enricher_kind,
                            actor=request.actor,
                            correlation_id=request.correlation_id,
                            # Thread the owning document through so
                            # the diagnostic recorder stamps it onto
                            # every ``j1.ingestion.stage.*`` event for
                            # the enrich stage. Without this, run-
                            # scoped audit consumers can't attribute
                            # an enrich event to a document.
                            document_id=document_id,
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
                    enrich_failed_duration_ms = int(
                        (_safe_now() - enrich_started_clock)
                        .total_seconds() * 1000
                    )
                    await self._emit_attempt(
                        request,
                        action=EVENT_ENRICHMENT_ATTEMPT_COMPLETED,
                        attempt=enrich_attempt_idx,
                        document_id=document_id,
                        artifact_id=artifact_id,
                        mode=request.enricher_kind,
                        duration_ms=enrich_failed_duration_ms,
                        success=False,
                        error=enrich_result.error,
                    )
                    raise _BusinessRejection(
                        f"enrich failed for {artifact_id}: {enrich_result.error}"
                    )
                self._produced_artifact_ids.extend(enrich_result.artifact_ids)
                self._produced_artifact_kinds.extend(enrich_result.kinds)
                enrich_produced_ids.extend(enrich_result.artifact_ids)
                self._record_step(
                    step="enrich",
                    status=StepStatus.COMPLETED,
                    required=True,
                    source=StepSource.CALLER,
                    artifact_count=len(enrich_result.artifact_ids),
                    # ``artifact_id`` here is the source compile
                    # artifact this enrich pass consumed; the resume
                    # snapshot reads this to populate
                    # ``completed_step_instances`` so the per-artifact
                    # identity is recoverable.
                    metadata={
                        "artifact_id": artifact_id,
                        "document_id": document_id,
                    },
                )
                enrich_done_duration_ms = int(
                    (_safe_now() - enrich_started_clock)
                    .total_seconds() * 1000
                )
                await self._emit_attempt(
                    request,
                    action=EVENT_ENRICHMENT_ATTEMPT_COMPLETED,
                    attempt=enrich_attempt_idx,
                    document_id=document_id,
                    artifact_id=artifact_id,
                    mode=request.enricher_kind,
                    duration_ms=enrich_done_duration_ms,
                    success=True,
                )
                self._complete(enrich_op)
            await self._maybe_review(request, GATE_AFTER_ENRICH)
            if self._cancelled:
                return

        graph_enabled, graph_reason, graph_source = self._stage_enabled(
            "graph", request.graph_builder_kind,
            compile_result=compile_result,
            final_compile_quality=final_quality,
            enrich_plan=enrich_plan,
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
                        # Per-run LightRAG workspace + draft-layer
                        # lineage stamping both need the document
                        # id. Without it the bridge can't compute
                        # the scoped working_dir and the producer
                        # emits drafts that lack ``metadata.run_id``
                        # — which then trips the registry-level
                        # ``RegistryLineageError`` guard.
                        document_id=document_id,
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
                    # Single owning document for single-doc workflows;
                    # batch / multi-document indexers leave it None.
                    document_id=(
                        request.target_document_ids[0]
                        if len(request.target_document_ids) == 1 else None
                    ),
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

    async def _await_compile_trigger(self, *, document_id: str) -> None:
        """Park the workflow until `SIGNAL_TRIGGER_COMPILE` fires.

 Used only when `request.two_phase_compile=True`. The gate
 flips `WorkflowState` to `WAITING_FOR_COMPILE_TRIGGER` and
 sets `_current_operation` to a synthetic
 `compile_pending:{doc_id}` op so `get_status` queries can
 tell what the workflow is parked on. On signal, the flag is
 consumed (reset to False) so a subsequent document in the
 same run gates independently. A cancel received while parked
 drops out without raising — the caller checks
 `self._cancelled` after returning."""
        previous_operation = self._current_operation
        previous_state = self._state
        self._compile_triggered = False
        self._current_operation = f"compile_pending:{document_id}"
        self._state = WorkflowState.WAITING_FOR_COMPILE_TRIGGER
        # Surface the compile-pending stage on the Temporal search
        # attribute. The synthetic `compile_pending` op
        # isn't run through `_begin`, so the macro-stage write
        # happens here explicitly. Maps via `_macro_ingest_stage`
        # so a future op-name change picks up the table change.
        self._set_search_attribute(
            SEARCH_ATTR_INGEST_STAGE,
            _macro_ingest_stage(self._current_operation),
        )
        await workflow.wait_condition(
            lambda: self._compile_triggered or self._cancelled
        )
        self._current_operation = previous_operation
        if self._cancelled:
            return
        self._state = (
            previous_state
            if previous_state not in (
                WorkflowState.WAITING_FOR_COMPILE_TRIGGER,
                WorkflowState.PAUSED,
            )
            else WorkflowState.RUNNING
        )

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
                # `workflow.info` raises outside a workflow event loop
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

    @workflow.signal
    def trigger_compile(self) -> None:
        """Release the workflow from `WAITING_FOR_COMPILE_TRIGGER`.

 Sent by `POST /ingestion-runs/{id}/compile`. Idempotent — a
 second signal while compile is already in flight is a no-op
 (the gate consumes-and-resets the flag, so a future second
 document in the same run will gate again as expected)."""
        self._compile_triggered = True

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
