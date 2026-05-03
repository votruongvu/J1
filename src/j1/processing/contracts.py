from typing import Any, Protocol

from j1.processing.results import (
    ArtifactProcessingResult,
    ModelResponse,
    ProcessingResult,
    QueryResult,
)
from j1.projects.context import ProjectContext


class KnowledgeCompiler(Protocol):
    kind: str

    def compile(
        self, ctx: ProjectContext, document_id: str
    ) -> ArtifactProcessingResult: ...


class EnrichmentProcessor(Protocol):
    kind: str

    def enrich(
        self, ctx: ProjectContext, artifact_id: str
    ) -> ArtifactProcessingResult: ...


class GraphBuilder(Protocol):
    kind: str

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> ArtifactProcessingResult: ...


class SearchIndexer(Protocol):
    kind: str

    def index(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> ProcessingResult: ...


class QueryProvider(Protocol):
    kind: str

    def query(
        self,
        ctx: ProjectContext,
        question: str,
        *,
        max_results: int | None = None,
    ) -> QueryResult: ...


class ModelProvider(Protocol):
    kind: str

    def complete(
        self,
        ctx: ProjectContext,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelResponse: ...
