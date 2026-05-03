from dataclasses import replace

from j1.errors.exceptions import QueryRoutingError
from j1.projects.context import ProjectContext
from j1.query.classifier import QueryIntentClassifier
from j1.query.models import QueryMode, QueryRequest, QueryResponse
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)

FALLBACK_NOTE = "knowledge_first returned no sources; graph_first fallback applied."


class HybridQueryEngine:
    """Routes a `QueryRequest` to the appropriate provider.

    In `AUTO` mode the classifier picks the mode. If the chosen mode is
    `KNOWLEDGE_FIRST` and that provider returns no sources, the engine
    additionally invokes `GRAPH_FIRST` and merges the responses (per spec).
    """

    def __init__(
        self,
        classifier: QueryIntentClassifier,
        knowledge_provider: KnowledgeQueryProvider,
        graph_provider: GraphQueryProvider,
        evidence_provider: EvidenceProvider,
        consistency_provider: ConsistencyProvider,
        report_generator: ReportGenerator,
    ) -> None:
        self._classifier = classifier
        self._providers = {
            QueryMode.KNOWLEDGE_FIRST: knowledge_provider,
            QueryMode.GRAPH_FIRST: graph_provider,
            QueryMode.EVIDENCE_FIRST: evidence_provider,
            QueryMode.CONSISTENCY_CHECK: consistency_provider,
            QueryMode.REPORT_GENERATION: report_generator,
        }

    def query(
        self, ctx: ProjectContext, request: QueryRequest
    ) -> QueryResponse:
        explicit = request.mode != QueryMode.AUTO
        mode = (
            request.mode if explicit else self._classifier.classify(request.question)
        )

        provider = self._providers.get(mode)
        if provider is None:
            raise QueryRoutingError(
                f"no provider registered for mode {mode.value!r}"
            )

        response = provider.query(ctx, request)

        if (
            not explicit
            and mode == QueryMode.KNOWLEDGE_FIRST
            and not response.sources
        ):
            graph_provider = self._providers.get(QueryMode.GRAPH_FIRST)
            if graph_provider is not None:
                graph_response = graph_provider.query(ctx, request)
                if graph_response.sources or graph_response.graph_paths:
                    response = _merge(response, graph_response)

        return response


def _merge(primary: QueryResponse, fallback: QueryResponse) -> QueryResponse:
    return replace(
        primary,
        answer=f"{primary.answer}\n\n{fallback.answer}",
        sources=list(primary.sources) + list(fallback.sources),
        related_artifacts=list(primary.related_artifacts)
        + list(fallback.related_artifacts),
        graph_paths=list(primary.graph_paths) + list(fallback.graph_paths),
        warnings=list(primary.warnings) + list(fallback.warnings) + [FALLBACK_NOTE],
        warning_categories=(
            list(primary.warning_categories) + list(fallback.warning_categories)
        ),
        confidence=max(primary.confidence, fallback.confidence),
    )
