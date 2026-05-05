from collections.abc import Mapping

from temporalio import activity

from j1.artifacts.registry import ArtifactRegistry
from j1.intake.registry import SourceRegistry
from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    CompileActivityInput,
    EnrichActivityInput,
    GraphActivityInput,
    IndexActivityInput,
    ProcessingActivityResult,
    QueryActivityInput,
    QueryActivityResult,
)
from j1.processing.contracts import (
    EnrichmentProcessor,
    GraphBuilder,
    KnowledgeCompiler,
    QueryProvider,
    SearchIndexer,
)
from j1.processing.results import (
    ArtifactProcessingResult,
    ProcessingResult,
    QueryResult,
)
from j1.processing.service import ProcessingService

ACTIVITY_COMPILE = "j1.processing.compile"
ACTIVITY_ENRICH = "j1.processing.enrich"
ACTIVITY_BUILD_GRAPH = "j1.processing.build_graph"
ACTIVITY_INDEX = "j1.processing.index"
ACTIVITY_QUERY = "j1.processing.query"


class UnknownProcessorError(LookupError):
    pass


class ProcessingActivities:
    def __init__(
        self,
        processing: ProcessingService,
        sources: SourceRegistry,
        artifacts: ArtifactRegistry,
        compilers: Mapping[str, KnowledgeCompiler] | None = None,
        enrichers: Mapping[str, EnrichmentProcessor] | None = None,
        graph_builders: Mapping[str, GraphBuilder] | None = None,
        indexers: Mapping[str, SearchIndexer] | None = None,
        query_providers: Mapping[str, QueryProvider] | None = None,
    ) -> None:
        self._processing = processing
        self._sources = sources
        self._artifacts = artifacts
        self._compilers = dict(compilers or {})
        self._enrichers = dict(enrichers or {})
        self._graph_builders = dict(graph_builders or {})
        self._indexers = dict(indexers or {})
        self._query_providers = dict(query_providers or {})

    def all_activities(self) -> list:
        return [
            self.compile,
            self.enrich,
            self.build_graph,
            self.index,
            self.query,
        ]

    @activity.defn(name=ACTIVITY_COMPILE)
    def compile(self, input: CompileActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        compiler = self._lookup(self._compilers, input.processor_kind, "compiler")
        document = self._sources.get(ctx, input.document_id)
        # Phase A.4: heartbeat at activity start so a configured
        # `heartbeat_timeout` can fire if the underlying compile call
        # (typically mineru / raganything, can take minutes for PDFs)
        # hangs. The compiler call itself is synchronous, so we can't
        # heartbeat mid-compile here — but the start-and-finish
        # heartbeats let the worker prove liveness on either side.
        # Best-effort: outside a Temporal worker the call is a no-op.
        _safe_heartbeat({"stage": "compile", "document_id": input.document_id})
        result = self._processing.compile(
            ctx,
            compiler,
            document,
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        _safe_heartbeat({
            "stage": "compile",
            "document_id": input.document_id,
            "status": result.status.value,
        })
        return _artifact_result(result)

    @activity.defn(name=ACTIVITY_ENRICH)
    def enrich(self, input: EnrichActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        processor = self._lookup(self._enrichers, input.processor_kind, "enricher")
        artifact = self._artifacts.get(ctx, input.artifact_id)
        result = self._processing.enrich(
            ctx,
            processor,
            artifact,
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        return _artifact_result(result)

    @activity.defn(name=ACTIVITY_BUILD_GRAPH)
    def build_graph(self, input: GraphActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        builder = self._lookup(
            self._graph_builders, input.processor_kind, "graph_builder"
        )
        _safe_heartbeat({
            "stage": "build_graph",
            "artifact_count": len(input.artifact_ids),
        })
        result = self._processing.build_graph(
            ctx,
            builder,
            list(input.artifact_ids),
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        _safe_heartbeat({
            "stage": "build_graph",
            "artifact_count": len(input.artifact_ids),
            "status": result.status.value,
        })
        return _artifact_result(result)

    @activity.defn(name=ACTIVITY_INDEX)
    def index(self, input: IndexActivityInput) -> ProcessingActivityResult:
        ctx = input.scope.to_context()
        indexer = self._lookup(self._indexers, input.processor_kind, "indexer")
        result = self._processing.index(
            ctx,
            indexer,
            list(input.artifact_ids),
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        return _processing_result(result)

    @activity.defn(name=ACTIVITY_QUERY)
    def query(self, input: QueryActivityInput) -> QueryActivityResult:
        ctx = input.scope.to_context()
        provider = self._lookup(
            self._query_providers, input.processor_kind, "query_provider"
        )
        result = self._processing.query(
            ctx,
            provider,
            input.question,
            max_results=input.max_results,
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        return _query_result(result)

    @staticmethod
    def _lookup(registry: dict, kind: str, role: str):
        try:
            return registry[kind]
        except KeyError as exc:
            raise UnknownProcessorError(
                f"no {role} registered for kind {kind!r}"
            ) from exc


def _safe_heartbeat(details: dict[str, object]) -> None:
    """Emit an `activity.heartbeat` if we're inside a Temporal worker.

    Outside a worker (e.g. unit tests calling the activity method
    directly), the SDK raises `RuntimeError`. Heartbeats are
    visibility, never correctness, so silently degrade. Details are
    deliberately small structured fields — never document content."""
    try:
        activity.heartbeat(details)
    except Exception:  # noqa: BLE001 — visibility never blocks ingest
        pass


def _artifact_result(result: ArtifactProcessingResult) -> ArtifactActivityResult:
    return ArtifactActivityResult(
        status=result.status.value,
        artifact_ids=[r.artifact_id for r in result.artifacts],
        error=result.error,
        message=result.message,
    )


def _processing_result(result: ProcessingResult) -> ProcessingActivityResult:
    return ProcessingActivityResult(
        status=result.status.value,
        error=result.error,
        message=result.message,
    )


def _query_result(result: QueryResult) -> QueryActivityResult:
    return QueryActivityResult(
        status=result.status.value,
        answer=result.answer,
        citations=list(result.citations),
        error=result.error,
        message=result.message,
    )
