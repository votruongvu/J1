from dataclasses import dataclass, field
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.cost.breakdown import CostBreakdown, CostResult
from j1.jobs.status import ReviewStatus
from j1.processing.status import ResultStatus

# Canonical artifact-kind strings shared across providers, projectors,
# and the validation/review surface. Each kind names the contract a
# producer claims to satisfy; consumers (review projectors, retrieval,
# the FE Assets/Chunks/Graph tabs) match on these. Stable across
# releases — renaming silently breaks every consumer that reads them.
ARTIFACT_KIND_CHUNK = "chunk"
ARTIFACT_KIND_COMPILED_TEXT = "compiled.text"

__all__ = [
    "ARTIFACT_KIND_CHUNK",
    "ARTIFACT_KIND_COMPILED_TEXT",
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
    review_required: bool = False


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
