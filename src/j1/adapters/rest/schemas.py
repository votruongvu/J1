from datetime import datetime
from typing import Any

from pydantic import Field

from j1.adapters.rest.envelope import CamelModel


# ---- Documents -------------------------------------------------------


class DocumentMetadataInput(CamelModel):
    """Optional metadata sent alongside a multipart upload (form fields)."""

    actor: str | None = None
    correlation_id: str | None = None


class DocumentRecord(CamelModel):
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    stored_filename: str
    mime_type: str | None = None
    file_size: int
    checksum: str
    status: str
    created_at: datetime


class DocumentStatusRecord(CamelModel):
    document_id: str
    status: str


# ---- Ingestion jobs --------------------------------------------------


class IngestRequest(CamelModel):
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    actor: str = "system"
    correlation_id: str | None = None


class JobStartRecord(CamelModel):
    job_id: str
    document_id: str
    status: str = "running"


class JobStatusRecord(CamelModel):
    job_id: str
    state: str
    current_operation: str | None = None
    documents_total: int = 0
    documents_completed: int = 0
    review_required: bool = False
    budget_approval_required: bool = False
    error: str | None = None


class JobEventRecord(CamelModel):
    event_id: str
    occurred_at: datetime
    actor: str
    action: str
    target_kind: str
    target_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class JobEventsRecord(CamelModel):
    job_id: str
    events: list[JobEventRecord]


# ---- Search / retrieve / answer --------------------------------------


class SearchRequest(CamelModel):
    query: str = Field(min_length=1)
    artifact_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=20, ge=1, le=200)


class SearchHitRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    title: str
    score: float
    source_document_id: str | None = None
    source_location: str | None = None
    confidence: float = 0.0
    review_status: str = "not_required"


class SearchResultRecord(CamelModel):
    query: str
    hits: list[SearchHitRecord]


class RetrieveRequest(CamelModel):
    query: str = Field(min_length=1)
    artifact_types: list[str] = Field(default_factory=list)
    max_blocks: int = Field(default=10, ge=1, le=50)


class CitationRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None


class ContextBlockRecord(CamelModel):
    artifact_id: str
    artifact_type: str
    text: str
    citation: CitationRecord


class RetrieveResultRecord(CamelModel):
    query: str
    blocks: list[ContextBlockRecord]


class AnswerRequest(CamelModel):
    question: str = Field(min_length=1)
    mode: str = "auto"
    artifact_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=10, ge=1, le=50)


class GraphPathRecord(CamelModel):
    nodes: list[str] = Field(default_factory=list)
    edges: list[str] = Field(default_factory=list)
    description: str | None = None


class AnswerRecord(CamelModel):
    question: str
    answer: str
    mode_used: str
    citations: list[CitationRecord]
    related_artifacts: list[str] = Field(default_factory=list)
    graph_paths: list[GraphPathRecord] = Field(default_factory=list)
    confidence: float = 0.0
    confidence_level: str = "ambiguous"
    review_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    warning_categories: list[str] = Field(default_factory=list)


# ---- Citations / sources ---------------------------------------------


class CitationDetailRecord(CamelModel):
    citation_id: str
    artifact_id: str
    artifact_type: str
    source_document_id: str | None = None
    source_location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceDetailRecord(CamelModel):
    source_id: str
    document_id: str
    tenant_id: str
    project_id: str
    original_filename: str
    mime_type: str | None = None
    file_size: int
    checksum: str
    status: str
    created_at: datetime


# ---- Feedback --------------------------------------------------------


class FeedbackRequest(CamelModel):
    target_kind: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    rating: int | None = Field(default=None, ge=-1, le=1)
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackReceiptRecord(CamelModel):
    feedback_id: str
    submitted_at: datetime


# ---- Health / version / capabilities ---------------------------------


class HealthRecord(CamelModel):
    status: str = "ok"


class VersionRecord(CamelModel):
    version: str


class CapabilityRecord(CamelModel):
    name: str
    available: bool
    description: str | None = None


class CapabilitiesRecord(CamelModel):
    api_version: str
    capabilities: list[CapabilityRecord]


# ---- Projects --------------------------------------------------------


class ProjectCreateRequest(CamelModel):
    project_id: str = Field(min_length=1)
    profile: str | None = None


class ProjectRecord(CamelModel):
    project_id: str
    tenant_id: str
    profile: str | None = None


# ---- Project ingestion jobs (workflow control) -----------------------


class ProjectIngestionRequest(CamelModel):
    compiler_kind: str = Field(min_length=1)
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: list[str] = Field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


class JobActionRecord(CamelModel):
    job_id: str
    action: str


# ---- Artifacts -------------------------------------------------------


class ArtifactRecord(CamelModel):
    artifact_id: str
    tenant_id: str
    project_id: str
    kind: str
    location: str
    content_hash: str
    byte_size: int
    status: str
    review_status: str
    version: int
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactListRecord(CamelModel):
    artifacts: list[ArtifactRecord]


# ---- Cost ------------------------------------------------------------


class CostSummaryRecord(CamelModel):
    project_id: str
    tenant_id: str
    total_amount: str
    currency: str = "USD"
    by_level: dict[str, str] = Field(default_factory=dict)


# ---- Reviews ---------------------------------------------------------


class ReviewItemRecord(CamelModel):
    review_item_id: str
    tenant_id: str
    project_id: str
    target_kind: str
    target_id: str
    review_status: str
    requested_at: datetime
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewListRecord(CamelModel):
    items: list[ReviewItemRecord]


class ReviewDecisionRequest(CamelModel):
    decision: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    notes: str | None = None
    correlation_id: str | None = None


class ReviewDecisionRecord(CamelModel):
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None
