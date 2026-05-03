from dataclasses import dataclass, field
from enum import StrEnum


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


@dataclass(frozen=True)
class SourceReference:
    artifact_id: str
    artifact_type: str
    title: str
    source_document_id: str | None = None
    source_location: str | None = None


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
