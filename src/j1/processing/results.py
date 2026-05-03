from dataclasses import dataclass, field
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.cost.breakdown import CostBreakdown, CostResult
from j1.jobs.status import ReviewStatus
from j1.processing.status import ResultStatus

__all__ = [
    "ArtifactDraft",
    "ArtifactProcessingResult",
    "CostBreakdown",
    "CostResult",
    "ModelResponse",
    "ProcessingResult",
    "QueryResult",
    "ResultStatus",
    "ReviewItemResult",
]


@dataclass(frozen=True)
class ArtifactDraft:
    kind: str
    content: bytes
    suggested_extension: str = ""
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessingResult:
    status: ResultStatus
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactProcessingResult:
    status: ResultStatus
    drafts: list[ArtifactDraft] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    cost_events: list[CostBreakdown] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryResult:
    status: ResultStatus
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    cost_events: list[CostBreakdown] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewItemResult:
    status: ResultStatus
    review_item_id: str
    review_status: ReviewStatus
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    cost: CostBreakdown
    metadata: dict[str, Any] = field(default_factory=dict)
