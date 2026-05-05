from collections.abc import Mapping

from temporalio import activity

from j1.artifacts.registry import ArtifactRegistry
from j1.intake.registry import SourceRegistry
from j1.runs.reporter import ProgressReporter
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
        progress_reporter: ProgressReporter | None = None,
    ) -> None:
        self._processing = processing
        self._sources = sources
        self._artifacts = artifacts
        self._compilers = dict(compilers or {})
        self._enrichers = dict(enrichers or {})
        self._graph_builders = dict(graph_builders or {})
        self._indexers = dict(indexers or {})
        self._query_providers = dict(query_providers or {})
        # User-facing progress events. Optional — when None, the
        # framework runs exactly as before (no progress events
        # emitted). Bootstrap wires a CompositeProgressReporter
        # that fans out to audit + Temporal heartbeat.
        self._reporter = progress_reporter

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
        # Heartbeat at activity start so a configured
        # `heartbeat_timeout` can fire if the underlying compile call
        # (typically mineru / raganything, can take minutes for PDFs)
        # hangs. The compiler call itself is synchronous, so we can't
        # heartbeat mid-compile here — but the start-and-finish
        # heartbeats let the worker prove liveness on either side.
        # Best-effort: outside a Temporal worker the call is a no-op.
        _safe_heartbeat({"stage": "compile", "document_id": input.document_id})
        self._report_step_start(
            ctx, input, stage="COMPILE", step="compile",
            engine=input.processor_kind,
        )
        try:
            result = self._processing.compile(
                ctx,
                compiler,
                document,
                actor=input.actor,
                correlation_id=input.correlation_id,
            )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="COMPILE", step="compile", exc=exc,
            )
            raise
        _safe_heartbeat({
            "stage": "compile",
            "document_id": input.document_id,
            "status": result.status.value,
        })
        self._report_step_outcome(
            ctx, input, stage="COMPILE", step="compile", result=result,
        )
        return _artifact_result(result)

    @activity.defn(name=ACTIVITY_ENRICH)
    def enrich(self, input: EnrichActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        processor = self._lookup(self._enrichers, input.processor_kind, "enricher")
        artifact = self._artifacts.get(ctx, input.artifact_id)
        self._report_step_start(
            ctx, input, stage="ENRICH", step="enrich",
            engine=input.processor_kind,
        )
        try:
            result = self._processing.enrich(
                ctx,
                processor,
                artifact,
                actor=input.actor,
                correlation_id=input.correlation_id,
            )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="ENRICH", step="enrich", exc=exc,
            )
            raise
        self._report_step_outcome(
            ctx, input, stage="ENRICH", step="enrich", result=result,
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
        self._report_step_start(
            ctx, input, stage="GRAPH", step="build_graph",
            engine=input.processor_kind,
        )
        try:
            result = self._processing.build_graph(
                ctx,
                builder,
                list(input.artifact_ids),
                actor=input.actor,
                correlation_id=input.correlation_id,
            )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="GRAPH", step="build_graph", exc=exc,
            )
            raise
        _safe_heartbeat({
            "stage": "build_graph",
            "artifact_count": len(input.artifact_ids),
            "status": result.status.value,
        })
        self._report_step_outcome(
            ctx, input, stage="GRAPH", step="build_graph", result=result,
        )
        return _artifact_result(result)

    @activity.defn(name=ACTIVITY_INDEX)
    def index(self, input: IndexActivityInput) -> ProcessingActivityResult:
        ctx = input.scope.to_context()
        indexer = self._lookup(self._indexers, input.processor_kind, "indexer")
        self._report_step_start(
            ctx, input, stage="INDEX", step="index",
            engine=input.processor_kind,
        )
        try:
            result = self._processing.index(
                ctx,
                indexer,
                list(input.artifact_ids),
                actor=input.actor,
                correlation_id=input.correlation_id,
            )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="INDEX", step="index", exc=exc,
            )
            raise
        self._report_step_outcome(
            ctx, input, stage="INDEX", step="index", result=result,
        )
        return _processing_result(result)

    @activity.defn(name=ACTIVITY_QUERY)
    def query(self, input: QueryActivityInput) -> QueryActivityResult:
        ctx = input.scope.to_context()
        provider = self._lookup(
            self._query_providers, input.processor_kind, "query_provider"
        )
        # Query is intentionally NOT wrapped in progress events —
        # it's a read path, not part of the ingestion timeline.
        result = self._processing.query(
            ctx,
            provider,
            input.question,
            max_results=input.max_results,
            actor=input.actor,
            correlation_id=input.correlation_id,
        )
        return _query_result(result)

    # ---- Progress-reporter integration -------------------------

    def _report_step_start(
        self, ctx, input, *, stage: str, step: str, engine: str | None,
    ) -> None:
        """Emit `step.started` if a reporter is configured AND the
        caller supplied a `correlation_id` (which by convention
        equals `run_id`). No-op otherwise — keeps existing behaviour
        for deployments that don't opt into the progress surface."""
        if self._reporter is None or not input.correlation_id:
            return
        try:
            self._reporter.report_step_started(
                ctx, run_id=input.correlation_id,
                stage=stage, step=step,
                engine=engine, actor=input.actor or "system",
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks ingest
            pass

    def _report_step_outcome(
        self, ctx, input, *, stage: str, step: str, result,
    ) -> None:
        """Emit `step.completed`, `step.skipped`, or `step.failed`
        based on the activity result's `status`. `result.status` is
        a `ResultStatus` (SUCCEEDED / FAILED / SKIPPED)."""
        if self._reporter is None or not input.correlation_id:
            return
        try:
            status_value = (
                result.status.value if hasattr(result.status, "value")
                else str(result.status)
            )
            artifact_count = len(getattr(result, "artifacts", []) or [])
            if status_value == "succeeded":
                self._reporter.report_step_completed(
                    ctx, run_id=input.correlation_id,
                    stage=stage, step=step,
                    artifact_count=artifact_count,
                    actor=input.actor or "system",
                )
            elif status_value == "skipped":
                self._reporter.report_step_skipped(
                    ctx, run_id=input.correlation_id,
                    stage=stage, step=step,
                    reason=result.message or result.error or "skipped by service",
                    actor=input.actor or "system",
                )
            else:
                # status_value == "failed" — service-level failure
                # (vendor returned non-success). Surface as
                # `step.failed`. The workflow-level fail-fast then
                # converts this into a workflow ApplicationError.
                self._reporter.report_step_failed(
                    ctx, run_id=input.correlation_id,
                    stage=stage, step=step,
                    error_type="ActivityFailure",
                    error_message=result.error or "activity returned non-succeeded status",
                    retryable=False,
                    actor=input.actor or "system",
                )
        except Exception:  # noqa: BLE001
            pass

    def _report_step_failure(
        self, ctx, input, *, stage: str, step: str, exc: Exception,
    ) -> None:
        """Emit `step.failed` for an unhandled exception path before
        re-raising. Critical: the reporter MUST NOT swallow the
        exception — the failure-propagation contract requires the
        workflow to see it."""
        if self._reporter is None or not input.correlation_id:
            return
        try:
            self._reporter.report_step_failed(
                ctx, run_id=input.correlation_id,
                stage=stage, step=step,
                error_type=type(exc).__name__,
                error_message=str(exc),
                retryable=False,
                actor=input.actor or "system",
            )
        except Exception:  # noqa: BLE001
            pass

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
