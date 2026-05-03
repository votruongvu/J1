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
class AnswerDTO:
    answer: str
    mode_used: str
    sources: list[CitationDTO] = field(default_factory=list)
    related_artifacts: list[str] = field(default_factory=list)
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
