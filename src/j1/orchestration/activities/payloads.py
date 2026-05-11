from dataclasses import dataclass, field
from typing import Any

from j1.projects.context import ProjectContext


@dataclass(frozen=True)
class ProjectScope:
    tenant_id: str
    project_id: str
    profile: str | None = None

    def to_context(self) -> ProjectContext:
        return ProjectContext(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            profile=self.profile,
        )

    @classmethod
    def from_context(cls, ctx: ProjectContext) -> "ProjectScope":
        return cls(
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            profile=ctx.profile,
        )


@dataclass(frozen=True)
class CompileActivityInput:
    scope: ProjectScope
    document_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None
    # Vendor-neutral compile plan — built by the workflow from the
    # document profile (see `_build_assessment_plan` in
    # `project_processing.py`). Carried as a plain dict so the
    # Temporal data converter handles it without taking a dependency
    # on `j1.processing.assessment` from this payload module.
    # The activity reconstructs an `AssessmentPlan` from the dict
    # before forwarding to the compiler. None on legacy callers /
    # the bulk-job path that doesn't run profiling — bridge falls
    # back to `settings.parse_method`.
    assessment_plan_payload: dict | None = None


@dataclass(frozen=True)
class EnrichActivityInput:
    scope: ProjectScope
    artifact_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class GraphActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class IndexActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class QueryActivityInput:
    scope: ProjectScope
    question: str
    processor_kind: str
    max_results: int | None = None
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class PersistValidationReportInput:
    """Workflow → activity payload for the `validation_report`
    artifact. Persisted before the COMPLETED transition (or by the
    failure handler when validation itself triggered the failure)
    so operators can see WHICH rules ran and which ones tripped."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    passed: bool
    errors: list[str] = field(default_factory=list)
    rules_evaluated: list[str] = field(default_factory=list)
    actor: str = "system"


@dataclass(frozen=True)
class PersistFinalSummaryInput:
    """Workflow → activity payload for the `final_summary` artifact.
    Carries the at-a-glance run outcome at terminal state."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    final_status: str
    executed_steps: list[dict[str, Any]] = field(default_factory=list)
    artifact_kind_counts: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    failure_code: str | None = None
    failure_message: str | None = None
    actor: str = "system"


@dataclass(frozen=True)
class PersistCompileStrategyReportInput:
    """Workflow → activity payload for the
    `compile_strategy_report` artifact. Carries the AssessmentPlan
    + per-attempt audit + final-quality verdict in plain-dict form;
    the activity passes it straight to
    `ProcessingService.persist_compile_strategy_report`."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class PersistPostCompileEnrichPlanInput:
    """Workflow → activity payload for the
    `post_compile_enrich_plan` artifact. Carries the rule-based
    enrich assessment verdict (recommendation + recommended/skipped
    tasks + source signals + decision_source) as a plain dict."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class PersistCompileResultSummaryInput:
    """Workflow → activity payload for the
    `compile_result_summary` artifact. Carries the typed
    `NormalizedCompileResult.to_payload()` dict — same transport
    shape as the other persist payloads."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class PersistInitialExecutionPlanInput:
    """Workflow → activity payload for the
    `initial_execution_plan` artifact. Carries the cheap pre-compile
    plan (selected domain, enrichment_policy, candidate modules,
    cheap_signals, wrapped compile plan) as a plain dict — same
    transport shape as `PersistPostCompileEnrichPlanInput`."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class BuildInitialExecutionPlanInput:
    """Workflow → activity payload for `build_initial_execution_plan`.

    The activity owns the full work unit: resolve the domain pack
    (override → workspace default → fallback to general), build the
    `InitialExecutionPlan` from the cheap profile + resolved pack,
    persist as an `initial_execution_plan` artifact, and return the
    payload to the workflow.

    Pack resolution is the activity's job because the registry is a
    module-state singleton — touching it from workflow code would
    cause replay non-determinism. The plan-build helper itself is
    pure but takes the resolved pack as input."""

    scope: ProjectScope
    run_id: str
    document_id: str
    # The DocumentProfile dataclass — same shape the
    # `profile_document` activity already returns. Temporal's data
    # converter handles the frozen dataclass round-trip directly;
    # we don't keep a separate `to_payload()` form.
    profile: "Any"
    # Operator-supplied override (e.g. `civil_engineering`). None ⇒
    # the activity walks the workspace default → fallback chain.
    domain_override: str | None = None
    workspace_default_domain: str | None = None
    allowed_domain_overrides: tuple[str, ...] = ()
    # Optional caller hints (concurrency, model-tier) the deployment
    # has already resolved. The plan merges with policy defaults.
    resource_hints: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class BuildInitialExecutionPlanResult:
    """Activity → workflow return for `build_initial_execution_plan`.

    Carries the built plan payload so the workflow can route it
    downstream + the artifact_id for the persisted JSON. `status` is
    `"succeeded"` / `"failed"` mirroring the rest of the activity
    payloads."""

    status: str
    plan_payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None
    error: str | None = None
    # Selected domain id (mirrors plan_payload["domain_profile_id"])
    # — surfaced as a top-level field so the workflow can update
    # search attributes / step events without re-parsing the payload.
    domain_profile_id: str | None = None


@dataclass(frozen=True)
class FastLLMConsultEnrichInput:
    """Workflow → activity payload for the optional fast-LLM consult.

    Carries ONLY compact signals + the rule-based provisional plan.
    NEVER document content. The activity is no-op when the consult
    is disabled; it MUST never raise (any internal failure is
    swallowed and reported as `consulted=False`)."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    compile_status: str
    final_compile_quality: str
    source_signals: dict[str, Any] = field(default_factory=dict)
    provisional_recommendation: str = "optional"  # one of skip/optional/recommended/required
    provisional_recommended_tasks: list[str] = field(default_factory=list)
    provisional_skipped_tasks: list[str] = field(default_factory=list)
    compile_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FastLLMConsultEnrichResult:
    """Activity → workflow return. `consulted=False` always means
    'fall back to rule-based plan' (whatever the reason)."""

    consulted: bool
    fallback_reason: str | None = None
    # Refinement fields, only meaningful when `consulted=True`. The
    # workflow constructs a `FastLLMRefinement` from these and feeds
    # it through `apply_fast_llm_refinement`.
    recommendation: str | None = None  # never "skip" — the activity drops it
    add_reasons: list[str] = field(default_factory=list)
    add_recommended_tasks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidateStageInput:
    """Workflow → activity payload for `validate_stage`. Carries the
    stage name + the artifacts the stage produced + the scope keys
    the validator uses for cross-checks. The activity reads the
    artifact files back from disk, runs the per-stage validator,
    persists a `stage_validation_report` artifact, and returns the
    result so the workflow can decide between `_record_step(COMPLETED)`
    and `_record_step(FAILED)`."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    stage_name: str  # one of STAGE_COMPILE / GENERATE_CHUNKS / ENRICH / GRAPH
    output_artifact_ids: list[str] = field(default_factory=list)
    # Optional flags for stages whose validator's behaviour depends
    # on the run's plan. `enrich_required` / `graph_required` come
    # from the workflow's `_stage_enabled` decision.
    enrich_required: bool = False
    graph_required: bool = False
    # For graph: the chunk artifact ids the graph should be grounded
    # in. Empty when the workflow doesn't track them (legacy).
    chunk_artifact_ids: list[str] = field(default_factory=list)
    attempt: int = 1
    actor: str = "system"


@dataclass(frozen=True)
class StageValidationActivityResult:
    """Workflow → activity return for `validate_stage`. Mirrors the
    `StageValidationResult` shape but as a Temporal-data-converter
    friendly dataclass. The workflow inspects `passed` to gate the
    COMPLETED transition; full payload is also persisted as the
    `stage_validation_report` artifact."""

    stage_name: str
    validation_status: str
    passed: bool
    error_count: int = 0
    warning_count: int = 0
    check_count: int = 0
    artifact_id: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerifyCompileInput:
    """Workflow → activity payload for `verify_compile_output`.

    Verification is the post-compile health gate: it counts chunks
    produced by the compile activity, optionally checks the index
    is reachable, and returns a structured pass/fail with a stable
    `reason_code` the workflow lifts into `IngestionRun.failure_code`
    on rejection. See `FAILURE_CODE_*` in `j1.runs.models`."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    # Artifact ids the compile activity produced (chunk records,
    # parser manifests, etc.). The verifier counts chunk artifacts
    # by `kind == "chunk"`.
    output_artifact_ids: list[str] = field(default_factory=list)
    # Per-artifact kinds, parallel to `output_artifact_ids`. When
    # populated, the verifier uses these directly instead of reading
    # each record back from storage to inspect `kind`.
    output_artifact_kinds: tuple[str, ...] = field(default_factory=tuple)
    # Minimum chunks the verifier requires. 0 disables the check
    # (e.g. for an empty-document run that legitimately produced
    # nothing). Defaults to 1 — every non-empty compile must yield
    # at least one chunk.
    min_chunks: int = 1
    # When True, the verifier also confirms the index activity ran
    # successfully (looks for an `index_manifest`-kind artifact in
    # `output_artifact_kinds`). Off by default — the workflow only
    # opts in after `OPERATION_INDEX` completes.
    require_index_manifest: bool = False
    attempt: int = 1
    actor: str = "system"


@dataclass(frozen=True)
class VerifyCompileActivityResult:
    """Workflow → activity return for `verify_compile_output`.

    `passed=False` means the workflow should fail the run with
    `failure_code=reason_code`. `reason_code` is one of the
    `FAILURE_CODE_*` strings from `j1.runs.models` (e.g.
    `CHUNK_FAILED`, `INDEX_FAILED`, `VERIFICATION_FAILED`). When
    `passed=True`, `reason_code` is None and the workflow proceeds."""

    passed: bool
    reason_code: str | None = None
    message: str | None = None
    chunk_count: int = 0
    artifact_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PersistErrorReportInput:
    """Workflow → activity payload for the failure-path
    `error_report` artifact. The workflow calls this from its
    FAILED_FINAL handler so operators can inspect why a run failed
    via the same artifact-listing path that surfaces successful
    artifacts."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    failure_code: str
    failure_message: str
    stage: str | None = None
    step: str | None = None
    # JSON-friendly snapshot of the per-step status table at the
    # moment of failure. Pydantic / Temporal data converter
    # serialisable.
    step_results: list[dict[str, Any]] = field(default_factory=list)
    actor: str = "system"


@dataclass(frozen=True)
class ArtifactActivityResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    message: str | None = None
    # Optional content signals surfaced by the compile processor —
    # e.g. {"has_images": True, "has_tables": False, "page_count": 12}.
    # The post-compile planner merges these into the DocumentProfile so
    # downstream stages (enrich / graph) decide based on observed
    # content rather than extension heuristics. None = processor did
    # not populate; planner falls back to the deterministic profile.
    content_stats: dict[str, Any] | None = None
    # Per-artifact kinds the activity actually produced (e.g.
    # `("chunk", "chunk", "chunk")`, `("graph_json",)` from build_graph).
    # The workflow's `_validate_completion` reads this to enforce
    # per-stage required outputs — a graph step that "completed"
    # without a graph_json artifact is a contract violation, not a
    # SUCCEEDED state.
    # Defaults to empty so older test fixtures that build
    # `ArtifactActivityResult(status="succeeded", artifact_ids=...)`
    # by hand keep working; the validation only fires when the field
    # is populated.
    kinds: tuple[str, ...] = field(default_factory=tuple)
    # Quality signals the compile-safety-retry layer reads to decide
    # whether to escalate to a higher mode. Populated by the activity
    # from the bridge's manifest (`total_text_chars` etc.); empty
    # when the producer didn't surface them. The retry layer treats
    # missing signals as "unknown" and skips the corresponding rule
    # rather than retrying defensively.
    compile_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessingActivityResult:
    status: str
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class QueryActivityResult:
    status: str
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ValidateContextResult:
    valid: bool
    message: str | None = None


@dataclass(frozen=True)
class SetDocumentStatusInput:
    """Workflow → activity payload for `j1.project.set_document_status`.

    Used to flip a document's status off `PENDING` once the workflow
    has finished processing it. `status` must be the wire value of a
    `ProcessingStatus` enum member (e.g. `"succeeded"` / `"failed"` /
    `"cancelled"`). Best-effort: if the document is missing the
    activity logs and returns rather than raising — telemetry never
    blocks workflow progress."""

    scope: ProjectScope
    document_id: str
    status: str


@dataclass(frozen=True)
class SpendSummary:
    total_amount: str
    currency: str
    event_count: int


@dataclass(frozen=True)
class FinalizeInput:
    scope: ProjectScope
    state: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


# ---- Lifecycle activity payloads ----------------------------------------------


@dataclass(frozen=True)
class ValidateProjectInput:
    scope: ProjectScope


@dataclass(frozen=True)
class ValidateProjectResult:
    status: str
    message: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class PrepareWorkspaceInput:
    scope: ProjectScope


@dataclass(frozen=True)
class PrepareWorkspaceResult:
    status: str
    error: str | None = None


@dataclass(frozen=True)
class DocumentSource:
    source_path: str
    original_filename: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class RegisterDocumentsInput:
    scope: ProjectScope
    documents: list[DocumentSource]
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class SkippedDocument:
    source_path: str
    existing_document_id: str


@dataclass(frozen=True)
class DocumentRegistrationError:
    source_path: str
    error: str
    error_type: str


@dataclass(frozen=True)
class RegisterDocumentsResult:
    status: str
    registered_document_ids: list[str] = field(default_factory=list)
    skipped: list[SkippedDocument] = field(default_factory=list)
    errors: list[DocumentRegistrationError] = field(default_factory=list)


@dataclass(frozen=True)
class FinalizeProcessingInput:
    scope: ProjectScope
    state: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class FinalizeProcessingResult:
    status: str
    audit_event_id: str | None = None


# ---- Knowledge activity payloads ----------------------------------------------


@dataclass(frozen=True)
class DraftPayload:
    kind: str
    content: bytes
    suggested_extension: str = ""
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    review_required: bool = False


@dataclass(frozen=True)
class CostBreakdownPayload:
    vendor: str
    model: str
    unit_kind: str
    units: int
    amount: str
    currency: str = "USD"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeCompilationInput:
    scope: ProjectScope
    document_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class KnowledgeCompilationResult:
    status: str
    drafts: list[DraftPayload] = field(default_factory=list)
    cost_events: list[CostBreakdownPayload] = field(default_factory=list)
    message: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RegisterArtifactsInput:
    scope: ProjectScope
    drafts: list[DraftPayload]
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class RegisterArtifactsResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)
    reused_artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ArtifactEnrichmentInput:
    scope: ProjectScope
    artifact_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class ArtifactEnrichmentResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)
    cost_events: list[CostBreakdownPayload] = field(default_factory=list)
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class GraphCorpusInput:
    scope: ProjectScope
    include_kinds: list[str] = field(default_factory=list)
    exclude_kinds: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphCorpusResult:
    status: str
    artifact_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphBuildInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class GraphBuildResult:
    status: str
    drafts: list[DraftPayload] = field(default_factory=list)
    cost_events: list[CostBreakdownPayload] = field(default_factory=list)
    error: str | None = None
    message: str | None = None


# ---- Search activity payloads ------------------------------------------------


@dataclass(frozen=True)
class SearchIndexInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class SearchIndexResult:
    status: str
    indexed_artifact_count: int = 0
    error: str | None = None
    message: str | None = None


# ---- Accounting activity payloads --------------------------------------------


@dataclass(frozen=True)
class CalculateCostInput:
    scope: ProjectScope


@dataclass(frozen=True)
class CalculateCostResult:
    status: str
    total_amount: str = "0"
    currency: str = "USD"
    event_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class WriteAuditInput:
    scope: ProjectScope
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload: dict[str, str] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True)
class WriteAuditResult:
    status: str
    audit_event_id: str | None = None


# ---- Review activity payloads ------------------------------------------------


@dataclass(frozen=True)
class ReviewItemSpec:
    target_kind: str
    target_id: str
    notes: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateReviewItemsInput:
    scope: ProjectScope
    items: list[ReviewItemSpec]
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class CreatedReviewItem:
    review_item_id: str
    target_kind: str
    target_id: str


@dataclass(frozen=True)
class CreateReviewItemsResult:
    status: str
    items: list[CreatedReviewItem] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ApplyReviewDecisionInput:
    scope: ProjectScope
    review_item_id: str
    decision: str
    actor: str
    notes: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ApplyReviewDecisionResult:
    status: str
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None
