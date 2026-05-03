from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.review.models import ReviewItem


# ---- Project ---------------------------------------------------------


class ProjectCreateRequest(BaseModel):
    project_id: str
    profile: str | None = None


class ProjectResponse(BaseModel):
    project_id: str
    tenant_id: str
    profile: str | None = None


# ---- Documents -------------------------------------------------------


class DocumentResponse(BaseModel):
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
    duplicate: bool = False

    @classmethod
    def from_record(cls, record: DocumentRecord, *, duplicate: bool = False) -> "DocumentResponse":
        return cls(
            document_id=record.document_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            original_filename=record.original_filename,
            stored_filename=record.stored_filename,
            mime_type=record.mime_type,
            file_size=record.file_size,
            checksum=record.checksum,
            status=record.status.value,
            created_at=record.created_at,
            duplicate=duplicate,
        )


# ---- Processing ------------------------------------------------------


class StartProcessingRequest(BaseModel):
    compiler_kind: str
    enricher_kind: str | None = None
    graph_builder_kind: str | None = None
    indexer_kind: str | None = None
    budget_limit_amount: str | None = None
    budget_currency: str = "USD"
    review_after: list[str] = Field(default_factory=list)
    actor: str = "system"
    correlation_id: str | None = None


class StartProcessingResponse(BaseModel):
    workflow_id: str
    project_id: str
    status: str = "running"


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    project_id: str
    state: str
    current_operation: str | None = None
    pending_operation: str | None = None
    completed_operations: list[str] = Field(default_factory=list)
    documents_total: int = 0
    documents_completed: int = 0
    produced_artifact_ids: list[str] = Field(default_factory=list)
    review_required: bool = False
    review_gate: str | None = None
    budget_approval_required: bool = False
    error: str | None = None


class WorkflowActionResponse(BaseModel):
    workflow_id: str
    action: str


# ---- Artifacts -------------------------------------------------------


class ArtifactSummary(BaseModel):
    """Public artifact view — relative `location` only, no absolute paths."""

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

    @classmethod
    def from_record(cls, record: ArtifactRecord) -> "ArtifactSummary":
        return cls(
            artifact_id=record.artifact_id,
            tenant_id=record.project.tenant_id,
            project_id=record.project.project_id,
            kind=record.kind,
            location=record.location,
            content_hash=record.content_hash,
            byte_size=record.byte_size,
            status=record.status.value,
            review_status=record.review_status.value,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            source_document_ids=list(record.source_document_ids),
            source_artifact_ids=list(record.source_artifact_ids),
            metadata=dict(record.metadata),
        )


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactSummary]


# ---- Query -----------------------------------------------------------


class QueryEndpointRequest(BaseModel):
    question: str
    mode: str = "auto"
    max_results: int = 10
    artifact_types: list[str] = Field(default_factory=list)


class SourceReferenceResponse(BaseModel):
    artifact_id: str
    artifact_type: str
    title: str
    source_document_id: str | None = None
    source_location: str | None = None


class GraphPathResponse(BaseModel):
    nodes: list[str]
    edges: list[str] = Field(default_factory=list)
    description: str | None = None


class QueryEndpointResponse(BaseModel):
    answer: str
    mode_used: str
    sources: list[SourceReferenceResponse] = Field(default_factory=list)
    related_artifacts: list[str] = Field(default_factory=list)
    graph_paths: list[GraphPathResponse] = Field(default_factory=list)
    confidence: float = 0.0
    confidence_level: str = "ambiguous"
    review_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    warning_categories: list[str] = Field(default_factory=list)


# ---- Cost ------------------------------------------------------------


class CostSummaryResponse(BaseModel):
    project_id: str
    tenant_id: str
    total_amount: str
    currency: str = "USD"
    by_level: dict[str, str] = Field(default_factory=dict)


# ---- Reviews ---------------------------------------------------------


class ReviewItemResponse(BaseModel):
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

    @classmethod
    def from_item(cls, item: ReviewItem) -> "ReviewItemResponse":
        return cls(
            review_item_id=item.review_item_id,
            tenant_id=item.project.tenant_id,
            project_id=item.project.project_id,
            target_kind=item.target_kind,
            target_id=item.target_id,
            review_status=item.review_status.value,
            requested_at=item.requested_at,
            actor=item.actor,
            notes=item.notes,
            metadata=dict(item.metadata),
        )


class ReviewListResponse(BaseModel):
    items: list[ReviewItemResponse]


class ReviewDecisionRequest(BaseModel):
    decision: str
    actor: str
    notes: str | None = None
    correlation_id: str | None = None


class ReviewDecisionResponse(BaseModel):
    review_item_id: str
    review_status: str
    audit_event_id: str | None = None


# ---- Errors ----------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: str

    model_config = ConfigDict(extra="allow")
