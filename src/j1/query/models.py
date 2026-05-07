from dataclasses import dataclass, field
from enum import StrEnum

from j1.query.scope import QueryScope, default_scope
from j1.review.governance import (
    ConfidenceLevel,
    WarningCategory,
    confidence_level_from_score,
)


class QueryMode(StrEnum):
    AUTO = "auto"
    KNOWLEDGE_FIRST = "knowledge_first"
    GRAPH_FIRST = "graph_first"
    EVIDENCE_FIRST = "evidence_first"
    CONSISTENCY_CHECK = "consistency_check"
    REPORT_GENERATION = "report_generation"


@dataclass(frozen=True)
class QueryRequest:
    question: str
    mode: QueryMode = QueryMode.AUTO
    max_results: int = 10
    artifact_types: list[str] = field(default_factory=list)
    # Search-time filter applied INSIDE the index layer. Defaults to
    # `WorkspaceScope` so every legacy caller gets the historical
    # project-wide behaviour. Validation passes a `RunScope` to
    # restrict retrieval to a single ingestion run.
    scope: QueryScope = field(default_factory=default_scope)


@dataclass(frozen=True)
class SourceReference:
    artifact_id: str
    artifact_type: str
    title: str
    source_document_id: str | None = None
    source_location: str | None = None
    # Server-derived from the matched artifact's metadata at index
    # time. Surfaced as nullable because not every artifact is
    # chunk-grained (e.g. graph_json hits legitimately have no
    # chunk_id). Citations the FE renders read these fields directly.
    chunk_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class GraphPath:
    nodes: list[str]
    edges: list[str] = field(default_factory=list)
    description: str | None = None


@dataclass(frozen=True)
class QueryResponse:
    answer: str
    mode_used: str
    sources: list[SourceReference] = field(default_factory=list)
    related_artifacts: list[str] = field(default_factory=list)
    graph_paths: list[GraphPath] = field(default_factory=list)
    confidence: float = 0.0
    review_required: bool = False
    warnings: list[str] = field(default_factory=list)
    warning_categories: list[WarningCategory] = field(default_factory=list)

    @property
    def confidence_level(self) -> ConfidenceLevel:
        return confidence_level_from_score(self.confidence)
