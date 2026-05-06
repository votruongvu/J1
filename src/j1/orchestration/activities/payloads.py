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
