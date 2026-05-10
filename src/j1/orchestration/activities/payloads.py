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


@dataclass(frozen=True)
class InsertContentActivityInput:
    """Workflow → activity payload for the split-mode insert step.

    Drives `RAGAnything.insert_content_list` from a previously-
    persisted `parsed_source` artifact. The `parsed_source_artifact_id`
    is the artifact id the parse activity registered upstream — the
    insert activity reads its bytes back from disk to recover the
    content_list + doc_id without re-parsing the source file."""

    scope: ProjectScope
    document_id: str
    processor_kind: str
    parsed_source_artifact_id: str
    source_filename: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


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
    # Split-mode handoff: id of the `parsed_source` artifact when the
    # compile activity ran in `split_parse_insert` mode. The workflow
    # passes this to the `insert_content` activity so it can read the
    # pre-parsed content_list back without re-parsing the source. None
    # in legacy `complete` mode (compile produces chunks directly).
    parsed_source_artifact_id: str | None = None
    # Per-artifact kinds the activity actually produced (e.g.
    # `("chunk", "chunk", "chunk")` from insert_content,
    # `("graph_json",)` from build_graph). The workflow's
    # `_validate_completion` reads this to enforce per-stage required
    # outputs — a graph step that "completed" without a graph_json
    # artifact is a contract violation, not a SUCCEEDED state.
    # Defaults to empty so older test fixtures that build
    # `ArtifactActivityResult(status="succeeded", artifact_ids=...)`
    # by hand keep working; the validation only fires when the field
    # is populated.
    kinds: tuple[str, ...] = field(default_factory=tuple)


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
