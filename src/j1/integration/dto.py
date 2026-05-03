from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---- Document / source DTOs -----------------------------------------------


@dataclass(frozen=True)
class DocumentDTO:
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    stored_filename: str
    mime_type: str | None
    file_size: int
    checksum: str
    status: str
    created_at: datetime


# ---- Job / workflow DTOs --------------------------------------------------


@dataclass(frozen=True)
class JobStatusDTO:
    job_id: str
    state: str
    current_operation: str | None = None
    documents_total: int = 0
    documents_completed: int = 0
    review_required: bool = False
    budget_approval_required: bool = False
    error: str | None = None


# ---- Search / retrieval / answer DTOs -------------------------------------


@dataclass(frozen=True)
class SearchHitDTO:
    artifact_id: str
    artifact_type: str
    title: str
    score: float
    source_document_id: str | None = None
    source_location: str | None = None
    confidence: float = 0.0
    review_status: str = "not_required"
    extracted_text: str = ""


@dataclass(frozen=True)
class ArtifactDTO:
    artifact_id: str
    tenant_id: str
    project_id: str
    kind: str
    location: str  # workspace-relative (e.g. "compiled/<id>.txt")
    content_hash: str
    byte_size: int
    status: str
    review_status: str
    version: int
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CitationDTO:
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None


@dataclass(frozen=True)
class AnswerRequestDTO:
    question: str
    mode: str = "auto"
    max_results: int = 10
    artifact_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphPathDTO:
    nodes: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    description: str | None = None


@dataclass(frozen=True)
class AnswerDTO:
    answer: str
    mode_used: str
    sources: list[CitationDTO] = field(default_factory=list)
    related_artifacts: list[str] = field(default_factory=list)
    graph_paths: list[GraphPathDTO] = field(default_factory=list)
    confidence: float = 0.0
    confidence_level: str = "ambiguous"
    review_required: bool = False
    warnings: list[str] = field(default_factory=list)
    warning_categories: list[str] = field(default_factory=list)


# ---- Feedback / event DTOs -----------------------------------------------


@dataclass(frozen=True)
class FeedbackDTO:
    target_kind: str
    target_id: str
    rating: int | None = None
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedbackResultDTO:
    feedback_id: str
    submitted_at: datetime


@dataclass(frozen=True)
class EventDTO:
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True)
class EventResultDTO:
    event_id: str


# ---- Project / job-control DTOs -----------------------------------------


@dataclass(frozen=True)
class ProjectDTO:
    project_id: str
    tenant_id: str
    profile: str | None = None


@dataclass(frozen=True)
class ProjectCreateRequestDTO:
    project_id: str
    profile: str | None = None


@dataclass(frozen=True)
class ProjectIngestionRequestDTO:
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: list[str] = field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


@dataclass(frozen=True)
class JobActionResultDTO:
    job_id: str
    action: str


# ---- Cost / review DTOs --------------------------------------------------


@dataclass(frozen=True)
class CostSummaryDTO:
    project_id: str
    tenant_id: str
    total_amount: str
    currency: str = "USD"
    by_level: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewItemDTO:
    review_item_id: str
    tenant_id: str
    project_id: str
    target_kind: str
    target_id: str
    review_status: str
    requested_at: datetime
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewDecisionRequestDTO:
    decision: str
    actor: str
    notes: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ReviewDecisionResultDTO:
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None
