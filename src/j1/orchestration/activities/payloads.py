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
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id`` so the
    # compiler can load the up-front-allocated candidate via
    # ``require_existing_target_snapshot`` instead of lazily
    # creating one inside the activity.
    target_snapshot_id: str | None = None


@dataclass(frozen=True)
class EnrichActivityInput:
    scope: ProjectScope
    artifact_id: str
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None
    # Workflow-supplied owning document so the diagnostic recorder
    # can stamp ``document_id`` onto the per-stage audit events.
    # Without it the enrich stage events used to land with
    # ``document_id=None`` even though the workflow always knows
    # which document the per-artifact loop is enriching. Optional
    # for legacy callers that don't carry the document context.
    document_id: str | None = None
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id``.
    target_snapshot_id: str | None = None


@dataclass(frozen=True)
class GraphActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None
    # Per-run + per-document scoping for the production graph build
    # path. When supplied, the bridge points LightRAG's working_dir
    # at the run's scoped subdir AND stamps each emitted graph_json
    # draft with ``metadata.run_id`` + ``metadata.document_id``.
    # Optional for backward compatibility — legacy callers fall back
    # to the global workdir, and the registry-level lineage guard
    # at ``JsonArtifactRegistry.add()`` catches any draft that still
    # arrives without ``run_id``.
    document_id: str | None = None
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id``.
    target_snapshot_id: str | None = None


@dataclass(frozen=True)
class IndexActivityInput:
    scope: ProjectScope
    artifact_ids: list[str]
    processor_kind: str
    actor: str = "system"
    correlation_id: str | None = None
    # Workflow-supplied owning document for diagnostic attribution;
    # see ``EnrichActivityInput.document_id`` for rationale. Optional
    # for legacy callers (multi-document indexers may pass None).
    document_id: str | None = None
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id``.
    target_snapshot_id: str | None = None


@dataclass(frozen=True)
class QueryActivityInput:
    scope: ProjectScope
    question: str
    processor_kind: str
    max_results: int | None = None
    actor: str = "system"
    correlation_id: str | None = None


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
 `NormalizedCompileResult.to_payload` dict — same transport
 shape as the other persist payloads."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class PersistEnrichmentResultInput:
    """Workflow → activity payload for the `enrichment_result`
 artifact. Carries the typed `EnrichmentResult.to_payload`
 dict."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"


@dataclass(frozen=True)
class PersistFinalIngestionReportInput:
    """workflow → activity payload for the
 `final_ingestion_report` artifact.

 The activity has TWO responsibilities (kept in one activity to
 minimise round-trips at terminal time):

 1. Resolve the persisted artifact payloads
 (`initial_execution_plan`, `compile_result_summary`,
 `post_compile_enrich_plan`, `enrichment_result`,
 `final_summary`) from the artifact registry.
 2. Run `build_final_ingestion_report` to project them onto
 the typed report.
 3. Persist the result as a `final_ingestion_report` artifact.

 The workflow doesn't read artifact payloads itself (Temporal
 sandbox forbids file I/O), so the activity owns the full
 fetch + build + persist transaction.

 Best-effort: write failures don't propagate to the workflow's
 terminal status — they're reported on the activity result and
 logged. The report is observability, not correctness."""

    scope: ProjectScope
    run_id: str
    document_id: str | None
    # Workflow's terminal state at the time of the persist call.
    framework_final_status: str
    failure_code: str | None = None
    failure_message: str | None = None
    warning_count: int = 0
    # Document name + workspace identifiers carried through so the
    # activity doesn't have to re-resolve them from the run record.
    document_name: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    operator_notes: tuple[str, ...] = ()
    actor: str = "system"


@dataclass(frozen=True)
class RunEnrichmentStageInput:
    """Workflow → activity payload for `run_enrichment_stage`.

 The activity resolves the domain pack from the registry,
 rebuilds the `NormalizedCompileResult` + `PostCompileEnrichPlan`
 from their persisted payloads, builds an `EnrichmentContext`,
 runs `CompositeEnrichmentRunner`, and persists the resulting
 `EnrichmentResult` as an `enrichment_result` artifact. Caller
 consumes the returned payload to gate the workflow-level
 `require_enrichment_success` check.

 Pack resolution lives in the activity (not workflow) because
 the registry is module-state — touching it from workflow code
 would cause replay non-determinism."""

    scope: ProjectScope
    run_id: str
    document_id: str
    # Persisted typed payloads from earlier stages. Carried as
    # plain dicts so the Temporal data converter handles them
    # without dataclass coupling.
    compile_result_payload: dict[str, Any]
    post_compile_enrich_plan_payload: dict[str, Any]
    initial_plan_payload: dict[str, Any] | None = None
    # Pack-resolution inputs (mirrors `BuildInitialExecutionPlanInput`).
    domain_override: str | None = None
    workspace_default_domain: str | None = None
    allowed_domain_overrides: tuple[str, ...] = ()
    actor: str = "system"


@dataclass(frozen=True)
class RunEnrichmentStageResult:
    """Activity → workflow return for `run_enrichment_stage`.

 Carries the persisted enrichment payload + artifact id + the
 decision flags the workflow needs to gate
 `require_enrichment_success` enforcement."""

    status: str  # succeeded / succeeded_with_warnings / failed / skipped
    plan_payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None
    # Mirrors `PostCompileEnrichPlan.require_enrichment_success`
    # surfaced at activity time so the workflow doesn't have to
    # re-parse the plan to enforce the policy.
    require_enrichment_success: bool = False
    persist_error: str | None = None
    # count of per-module retry attempts inside the
    # runner (sum of `attempts - 1` across module outcomes). The
    # workflow upserts this into `J1EnrichmentRetryCount` so ops
    # dashboards can aggregate by enrichment cost. Always >= 0;
    # 0 means "no module needed a retry".
    retry_count: int = 0


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
    # we don't keep a separate `to_payload` form.
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
class AllocateTargetSnapshotInput:
    """Phase 9 follow-up: allocate a candidate ``DocumentSnapshot``
    for the bulk-job per-document loop.

    Single-document REST flows allocate the candidate UP-FRONT at the
    REST boundary and thread the id through
    ``ProjectProcessingRequest.target_snapshot_id``. Bulk-job flows
    (``POST /ingestion-jobs``) process N documents in one workflow,
    so they can't pre-allocate a single snapshot — instead, the
    workflow calls this activity at the start of each document's
    processing and threads the per-document snapshot id into the
    document's Compile/Enrich/Graph/Index activity inputs.

    Returns the new snapshot's id. ``run_id`` is recorded as
    ``created_by_run_id`` on the snapshot record so the lineage
    chain stays intact."""

    scope: ProjectScope
    document_id: str
    run_id: str


@dataclass(frozen=True)
class AllocateTargetSnapshotResult:
    snapshot_id: str


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
    # Phase 9: up-front snapshot allocation. The workflow allocates
    # the candidate ``DocumentSnapshot`` (REST boundary for single-
    # doc flows; ``allocate_target_snapshot`` activity for bulk-job
    # per-document) and threads the id through so this activity
    # validates via ``require_existing_target_snapshot`` and
    # addresses outputs under the snapshot-scoped workspace.
    target_snapshot_id: str | None = None


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
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id`` so the
    # artifact-registration activity can stamp ``snapshot_id`` via
    # ``require_existing_target_snapshot`` instead of
    # lazy-allocating from run_id.
    target_snapshot_id: str | None = None


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
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id``.
    target_snapshot_id: str | None = None


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
    # Per-run + per-document scoping. When present, the graph
    # builder routes LightRAG's working_dir to
    # ``{workdir}/runs/{tenant}/{project}/{document_id}/{correlation_id}/``
    # so two reindex attempts for the same document don't overwrite
    # each other's graphml. Optional for backward compatibility —
    # legacy callers without document context fall back to the
    # global workdir.
    document_id: str | None = None
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id`` so the graph
    # builder can land its outputs under the snapshot-scoped
    # workspace directly.
    target_snapshot_id: str | None = None


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
    # Phase 9: snapshot identity threaded from
    # ``ProjectProcessingRequest.target_snapshot_id``.
    target_snapshot_id: str | None = None


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
