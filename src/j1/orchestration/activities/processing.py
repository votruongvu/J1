import contextlib
import contextvars
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from j1.processing.enrich_assessment import (
        FastLLMConsultPrompt,
        FastLLMRefinement,
    )
    from j1.processing.enrich_assessment_settings import FastLLMConsultSettings

# Optional fast-LLM consult callable signature. The worker bootstrap
# resolves this from env settings + the LLM registry; when None, the
# consult activity returns `consulted=False` and ingestion runs on
# the rule-based plan only.
FastLLMConsultCallable = Callable[
    ["FastLLMConsultPrompt", "FastLLMConsultSettings"],
    "FastLLMRefinement | None",
]

from temporalio import activity

from j1.artifacts.registry import ArtifactRegistry
from j1.intake.registry import SourceRegistry
from j1.processing.cache import (
    CACHE_STATUS_COMPLETED,
    CACHE_STATUS_FAILED,
    CACHE_STATUS_PROCESSING,
    ProcessingCacheEntry,
    ProcessingResultCache,
)
from j1.runs.models import RunStatus
from j1.runs.reporter import ProgressReporter
from j1.runs.store import IngestionRunStore
from j1.orchestration.activities.payloads import (
    ArtifactActivityResult,
    CompileActivityInput,
    EnrichActivityInput,
    FastLLMConsultEnrichInput,
    FastLLMConsultEnrichResult,
    GraphActivityInput,
    IndexActivityInput,
    PersistCompileStrategyReportInput,
    PersistErrorReportInput,
    PersistFinalSummaryInput,
    BuildInitialExecutionPlanInput,
    BuildInitialExecutionPlanResult,
    PersistCompileResultSummaryInput,
    PersistEnrichmentResultInput,
    PersistInitialExecutionPlanInput,
    PersistPostCompileEnrichPlanInput,
    PersistValidationReportInput,
    RunEnrichmentStageInput,
    RunEnrichmentStageResult,
    ProcessingActivityResult,
    QueryActivityInput,
    QueryActivityResult,
    StageValidationActivityResult,
    ValidateStageInput,
    VerifyCompileActivityResult,
    VerifyCompileInput,
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
ACTIVITY_PERSIST_ERROR_REPORT = "j1.processing.persist_error_report"
ACTIVITY_PERSIST_VALIDATION_REPORT = "j1.processing.persist_validation_report"
ACTIVITY_PERSIST_FINAL_SUMMARY = "j1.processing.persist_final_summary"
ACTIVITY_PERSIST_COMPILE_STRATEGY_REPORT = "j1.processing.persist_compile_strategy_report"
ACTIVITY_PERSIST_POST_COMPILE_ENRICH_PLAN = "j1.processing.persist_post_compile_enrich_plan"
ACTIVITY_PERSIST_INITIAL_EXECUTION_PLAN = "j1.processing.persist_initial_execution_plan"
ACTIVITY_BUILD_INITIAL_EXECUTION_PLAN = "j1.processing.build_initial_execution_plan"
ACTIVITY_PERSIST_COMPILE_RESULT_SUMMARY = "j1.processing.persist_compile_result_summary"
ACTIVITY_PERSIST_ENRICHMENT_RESULT = "j1.processing.persist_enrichment_result"
ACTIVITY_RUN_ENRICHMENT_STAGE = "j1.processing.run_enrichment_stage"
ACTIVITY_FAST_LLM_CONSULT_ENRICH = "j1.processing.fast_llm_consult_enrich"
ACTIVITY_VALIDATE_STAGE = "j1.processing.validate_stage"
ACTIVITY_VERIFY_COMPILE_OUTPUT = "j1.processing.verify_compile_output"


class UnknownProcessorError(LookupError):
    pass


@dataclass(frozen=True)
class _PersistOutcome:
    """Tiny helper-return for `_persist_enrichment_payload`. Carries
    the artifact_id (None on failure) + the error message (None on
    success). Defined as a private dataclass so the call sites don't
    need a tuple-unpack convention."""

    artifact_id: str | None
    error: str | None


def _persist_enrichment_payload(
    service,
    ctx,
    *,
    run_id: str,
    document_id: str | None,
    payload: dict,
    actor: str,
) -> _PersistOutcome:
    """Persist the typed `EnrichmentResult.to_payload()` dict via
    `ProcessingService.persist_enrichment_result`. Best-effort: a
    write failure surfaces as a populated `error` on the return;
    the inline payload still flows to the workflow."""
    try:
        record = service.persist_enrichment_result(
            ctx,
            run_id=run_id,
            document_id=document_id,
            payload=dict(payload),
            actor=actor,
        )
        return _PersistOutcome(artifact_id=record.artifact_id, error=None)
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        return _PersistOutcome(
            artifact_id=None,
            error=f"{type(exc).__name__}: {exc}",
        )


class _EmptyDetectionContext:
    """Sentinel detection context for pre-compile pack resolution.

    `select_domain(..., detection_enabled=False)` skips per-pack
    detection entirely, but the registry's signature still expects a
    detection_context object. This sentinel provides the attribute
    surface a pack detector would read (all empty) so the selector
    can run override → workspace → fallback without touching real
    document content."""

    title: str = ""
    title_quality: str = "unknown"
    filename: str | None = None
    early_page_text: str = ""
    heading_outline: tuple = ()
    table_captions: tuple = ()
    image_captions: tuple = ()
    document_type_hint: str | None = None


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
        result_cache: ProcessingResultCache | None = None,
        run_store: IngestionRunStore | None = None,
        fast_llm_consult: "FastLLMConsultCallable | None" = None,
    ) -> None:
        self._processing = processing
        self._sources = sources
        self._artifacts = artifacts
        self._compilers = dict(compilers or {})
        self._enrichers = dict(enrichers or {})
        self._graph_builders = dict(graph_builders or {})
        self._indexers = dict(indexers or {})
        self._query_providers = dict(query_providers or {})
        # Optional fast-LLM consult callable, bound at worker
        # bootstrap from `J1_ENRICH_ASSESSMENT_FAST_LLM_*` env vars.
        # When None, the consult activity returns `consulted=False`
        # and the workflow falls back to the rule-based enrich plan.
        # Signature: `(prompt, settings) -> FastLLMRefinement | None`.
        self._fast_llm_consult = fast_llm_consult
        # User-facing progress events. Optional — when None, the
        # framework runs exactly as before (no progress events
        # emitted). Bootstrap wires a CompositeProgressReporter
        # that fans out to audit + Temporal heartbeat.
        self._reporter = progress_reporter
        # IngestionRun record store. Wired in production so step events
        # also flip `IngestionRun.status` from ASSESSING → RUNNING and
        # advance `current_stage` / `current_step` / `progress_percent`
        # mid-flight. Without this the FE's `GET /ingestion-runs/{id}`
        # response stays at ASSESSING until terminal, which keeps the
        # run-detail page on "Building execution plan…" until the run
        # finishes. None preserves legacy behaviour.
        self._run_store = run_store
        # Idempotency cache for expensive deterministic processing
        # (today: compile / parse). When wired, an activity that
        # finds a `completed` cache entry for the same input bypasses
        # the underlying processor call entirely and returns the
        # previously-produced artifact ids. Optional — None means the
        # activity always re-runs the processor (legacy behaviour,
        # safe for deployments that haven't migrated their workspace
        # area to include the cache file).
        self._cache = result_cache

    def all_activities(self) -> list:
        return [
            self.compile,
            self.enrich,
            self.build_graph,
            self.index,
            self.query,
            self.persist_error_report,
            self.persist_validation_report,
            self.persist_final_summary,
            self.persist_compile_strategy_report,
            self.persist_post_compile_enrich_plan,
            self.fast_llm_consult_enrich,
            self.validate_stage,
        ]

    @activity.defn(name=ACTIVITY_COMPILE)
    def compile(self, input: CompileActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        compiler = self._lookup(self._compilers, input.processor_kind, "compiler")
        document = self._sources.get(ctx, input.document_id)

        # ---- Idempotency check ------------------------------------
        # Skip the expensive processor call entirely if a `completed`
        # result for the same (document_hash, processor_kind, ...)
        # already exists. This catches:
        #   - Temporal activity retries after a worker crash, where
        #     the previous attempt completed successfully but
        #     Temporal didn't see the heartbeat.
        #   - Re-runs of a document that was already processed in a
        #     prior workflow (cache survives across workflows).
        # `processor_version` and `mode` come from the compiler
        # interface when implementations expose them; the empty
        # default keeps existing compilers working without changes.
        cache_key_parts = _compile_cache_key_parts(input, compiler, document)
        cached = (
            self._cache.lookup(ctx, **cache_key_parts)
            if self._cache is not None
            else None
        )
        if cached is not None and cached.status == CACHE_STATUS_COMPLETED:
            _safe_heartbeat({
                "stage": "compile",
                "document_id": input.document_id,
                "status": "succeeded",
                "cache": "hit",
            })
            return ArtifactActivityResult(
                status="succeeded",
                artifact_ids=list(cached.artifact_ids),
                message="reused from processing-result cache",
            )

        # Write a `processing` marker BEFORE the processor call. Two
        # reasons: (1) operators inspecting the cache file see the
        # row immediately ("this document is being parsed RIGHT
        # NOW") instead of having to infer from the absence of a
        # `completed` row; (2) defense in depth — if a future
        # extension wants to gate concurrent attempts on this marker
        # the structure is already in place. We don't gate today
        # because Temporal's deterministic workflow_id already
        # prevents two parallel workflows for the same document, and
        # within one workflow only one attempt of an activity is
        # active at a time.
        self._record_cache_processing(ctx, input, document, cache_key_parts)
        self._report_step_start(
            ctx, input, stage="COMPILE", step="compile",
            engine=input.processor_kind,
        )
        # Background ticker keeps `activity.heartbeat` alive every 30s
        # while the synchronous compile (raganything → MinerU) runs.
        # Without this, real documents (PDFs that take >2 min to
        # parse) trip the activity's `heartbeat_timeout`, Temporal
        # marks the attempt failed, and the retry policy spawns a
        # FRESH MinerU subprocess — the "MinerU runs many times for
        # one upload" symptom. The 30 s interval pairs with a
        # `heartbeat_timeout` of ~5 min: short enough to recover
        # quickly from a worker crash, long enough that intermittent
        # GIL contention or network glitches don't fire false
        # liveness failures.
        # Reconstruct the AssessmentPlan from its dict payload (the
        # workflow serialises it that way to keep this payload module
        # free of `j1.processing.assessment` imports). None on legacy
        # callers + bulk-job mode → bridge falls back to
        # `settings.parse_method`.
        assessment_plan = None
        if input.assessment_plan_payload is not None:
            try:
                from j1.processing.assessment import AssessmentPlan
                assessment_plan = AssessmentPlan.from_payload(
                    input.assessment_plan_payload,
                )
            except Exception:  # noqa: BLE001 — defensive; never block compile
                assessment_plan = None
        # Pass `assessment_plan` only when the underlying service
        # accepts it. Stub `ProcessingService` implementations in
        # tests don't always carry the new kwarg; introspecting
        # avoids a TypeError on legacy stubs while honouring the
        # plan in real deployments.
        compile_kwargs: dict = {
            "actor": input.actor,
            "correlation_id": input.correlation_id,
        }
        if assessment_plan is not None:
            try:
                import inspect
                sig = inspect.signature(self._processing.compile)
                if "assessment_plan" in sig.parameters:
                    compile_kwargs["assessment_plan"] = assessment_plan
            except (TypeError, ValueError):
                pass
        try:
            with _heartbeating({
                "stage": "compile",
                "document_id": input.document_id,
                "processor_kind": input.processor_kind,
            }):
                result = self._processing.compile(
                    ctx, compiler, document, **compile_kwargs,
                )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="COMPILE", step="compile", exc=exc,
            )
            self._record_cache_failure(ctx, input, document, exc, cache_key_parts)
            raise
        _safe_heartbeat({
            "stage": "compile",
            "document_id": input.document_id,
            "status": result.status.value,
        })
        self._report_step_outcome(
            ctx, input, stage="COMPILE", step="compile", result=result,
        )
        # Persist the outcome in the cache. Successes short-circuit
        # subsequent retries; failures are recorded for operator
        # visibility but DO NOT block retry (Temporal's retry policy
        # is the source of truth for that — failures may be transient
        # in ways the cache can't know).
        if self._cache is not None:
            now = datetime.now(timezone.utc)
            status_value = result.status.value
            if status_value == "succeeded" and result.artifacts:
                self._cache.upsert(
                    ctx,
                    ProcessingCacheEntry(
                        cache_key=_make_key(cache_key_parts),
                        document_id=input.document_id,
                        document_hash=document.checksum,
                        processor_kind=input.processor_kind,
                        processor_version=cache_key_parts["processor_version"],
                        mode=cache_key_parts["mode"],
                        status=CACHE_STATUS_COMPLETED,
                        artifact_ids=tuple(a.artifact_id for a in result.artifacts),
                        created_at=now,
                        updated_at=now,
                    ),
                )
            elif status_value == "failed":
                self._cache.upsert(
                    ctx,
                    ProcessingCacheEntry(
                        cache_key=_make_key(cache_key_parts),
                        document_id=input.document_id,
                        document_hash=document.checksum,
                        processor_kind=input.processor_kind,
                        processor_version=cache_key_parts["processor_version"],
                        mode=cache_key_parts["mode"],
                        status=CACHE_STATUS_FAILED,
                        artifact_ids=(),
                        created_at=now,
                        updated_at=now,
                        error_type="ProcessorFailure",
                        error_message=(result.error or result.message or "")[:512] or None,
                    ),
                )
        return _artifact_result(result)

    def _record_cache_processing(
        self,
        ctx,
        input: CompileActivityInput,
        document,
        cache_key_parts: dict,
    ) -> None:
        """Mark the cache row as `processing` before invoking the
        processor. Best-effort, non-blocking — lookup-time gating
        isn't done today (Temporal's deterministic workflow_id +
        single-active-attempt-per-activity already prevent the
        races this would catch). The row exists for operator
        visibility: the cache file should always answer 'what's
        happening with this document right now?'."""
        if self._cache is None:
            return
        try:
            now = datetime.now(timezone.utc)
            attempt = _current_activity_attempt()
            self._cache.upsert(
                ctx,
                ProcessingCacheEntry(
                    cache_key=_make_key(cache_key_parts),
                    document_id=input.document_id,
                    document_hash=document.checksum,
                    processor_kind=input.processor_kind,
                    processor_version=cache_key_parts["processor_version"],
                    mode=cache_key_parts["mode"],
                    status=CACHE_STATUS_PROCESSING,
                    artifact_ids=(),
                    created_at=now,
                    updated_at=now,
                    attempt=attempt,
                ),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks ingest
            pass

    def _record_cache_failure(
        self,
        ctx,
        input: CompileActivityInput,
        document,
        exc: Exception,
        cache_key_parts: dict,
    ) -> None:
        """Audit-trail the failure for operators inspecting the cache.

        Doesn't gate retries — Temporal's retry policy is the source
        of truth for that. We just record what happened so the
        cache file can answer 'has this document failed before?'
        without a separate join."""
        if self._cache is None:
            return
        try:
            now = datetime.now(timezone.utc)
            self._cache.upsert(
                ctx,
                ProcessingCacheEntry(
                    cache_key=_make_key(cache_key_parts),
                    document_id=input.document_id,
                    document_hash=document.checksum,
                    processor_kind=input.processor_kind,
                    processor_version=cache_key_parts["processor_version"],
                    mode=cache_key_parts["mode"],
                    status=CACHE_STATUS_FAILED,
                    artifact_ids=(),
                    created_at=now,
                    updated_at=now,
                    attempt=_current_activity_attempt(),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:512],
                ),
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks retry
            pass

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
            with _heartbeating({
                "stage": "enrich",
                "artifact_id": input.artifact_id,
                "processor_kind": input.processor_kind,
            }):
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
        self._report_step_start(
            ctx, input, stage="GRAPH", step="build_graph",
            engine=input.processor_kind,
        )
        try:
            with _heartbeating({
                "stage": "build_graph",
                "artifact_count": len(input.artifact_ids),
                "processor_kind": input.processor_kind,
            }):
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
            with _heartbeating({
                "stage": "index",
                "artifact_count": len(input.artifact_ids),
                "processor_kind": input.processor_kind,
            }):
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

    @activity.defn(name=ACTIVITY_PERSIST_ERROR_REPORT)
    def persist_error_report(
        self, input: PersistErrorReportInput,
    ) -> ArtifactActivityResult:
        """Persist the failure-path `error_report` artifact so the FE
        artifact-listing surface picks it up under the failed run.

        Called from the workflow's FAILED_FINAL handler before
        `_safe_finalize` so the artifact lands in time for the run's
        terminal event. Best-effort from the workflow's perspective:
        any persistence failure is logged but does NOT mask the
        original `_BusinessRejection` — the workflow re-raises
        regardless of whether this activity succeeded."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_error_report(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                failure_code=input.failure_code,
                failure_message=input.failure_message,
                stage=input.stage,
                step=input.step,
                step_results=list(input.step_results) if input.step_results else None,
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_PERSIST_VALIDATION_REPORT)
    def persist_validation_report(
        self, input: PersistValidationReportInput,
    ) -> ArtifactActivityResult:
        """Persist `validation_report.json` summarising
        `_validate_completion`'s outcome. Called from the workflow at
        EVERY terminal transition (success or failure) so operators
        can see WHICH rules ran and which ones tripped without
        re-running validation. Best-effort — failure here doesn't
        change the workflow's terminal status."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_validation_report(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                passed=input.passed,
                errors=list(input.errors),
                rules_evaluated=list(input.rules_evaluated),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_PERSIST_FINAL_SUMMARY)
    def persist_final_summary(
        self, input: PersistFinalSummaryInput,
    ) -> ArtifactActivityResult:
        """Persist `final_summary.json` at terminal state. Carries the
        at-a-glance run outcome (status + executed-stage tally +
        artifact-kind counts + warning count + failure detail).
        Best-effort like the other terminal-state artifact writes."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_final_summary(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                final_status=input.final_status,
                executed_steps=list(input.executed_steps),
                artifact_kind_counts=dict(input.artifact_kind_counts),
                warning_count=input.warning_count,
                failure_code=input.failure_code,
                failure_message=input.failure_message,
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_PERSIST_COMPILE_STRATEGY_REPORT)
    def persist_compile_strategy_report(
        self, input: PersistCompileStrategyReportInput,
    ) -> ArtifactActivityResult:
        """Persist the AssessmentPlan + retry-attempts +
        final-quality verdict as a `compile_strategy_report`
        artifact. Best-effort — any persistence error is logged
        inside the activity and the workflow proceeds; the run's
        compile result is the durable signal, this artifact is
        purely observability."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_compile_strategy_report(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=dict(input.payload),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_PERSIST_ENRICHMENT_RESULT)
    def persist_enrichment_result(
        self, input: PersistEnrichmentResultInput,
    ) -> ArtifactActivityResult:
        """Persist the Wave-6 typed enrichment overlay as an
        `enrichment_result` artifact. Best-effort — write failure
        returned in the response; the inline payload is what the
        workflow + downstream consumers rely on."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_enrichment_result(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=dict(input.payload),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_RUN_ENRICHMENT_STAGE)
    def run_enrichment_stage(
        self, input: RunEnrichmentStageInput,
    ) -> RunEnrichmentStageResult:
        """Run the Wave-6 typed enrichment overlay stage (Wave 6.5).

        Resolves the domain pack via the registry (override →
        workspace default → fallback to general), rebuilds the
        `NormalizedCompileResult` + `PostCompileEnrichPlan` from
        their persisted payloads, builds an `EnrichmentContext`,
        runs `CompositeEnrichmentRunner` over the default skeleton
        module set, and persists the resulting `EnrichmentResult`
        as an `enrichment_result` artifact.

        Skipped-path handling: when `enrich_plan.should_enrich` is
        False, the activity short-circuits to
        `build_skipped_enrichment_result()` (typed sentinel with
        `status="skipped"` + reason). The artifact is still
        persisted so downstream consumers see an explicit skipped
        record rather than the absence of an artifact.

        Best-effort persistence: a write failure is recorded on
        `persist_error` but the inline result is still returned to
        the workflow, so `require_enrichment_success` enforcement
        + final-summary copy stay accurate even when the artifact
        write fails."""
        from j1.domains.registry import default_registry, select_domain
        from j1.processing.compile_result import NormalizedCompileResult
        from j1.processing.enrich_assessment import PostCompileEnrichPlan
        from j1.processing.enrichment_modules import (
            CompositeEnrichmentRunner,
            EnrichmentContext,
            MetadataEnrichmentModule,
            TerminologyEnrichmentModule,
            ValidationEnrichmentModule,
            build_skipped_enrichment_result,
        )
        from j1.processing.initial_execution_plan import InitialExecutionPlan

        ctx = input.scope.to_context()

        # Reconstruct typed inputs from their persisted dict payloads.
        try:
            compile_result = NormalizedCompileResult.from_payload(
                dict(input.compile_result_payload),
            )
            enrich_plan = PostCompileEnrichPlan.from_payload(
                dict(input.post_compile_enrich_plan_payload),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return RunEnrichmentStageResult(
                status="failed",
                persist_error=(
                    f"input payload reconstruction failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        initial_plan = (
            InitialExecutionPlan.from_payload(dict(input.initial_plan_payload))
            if input.initial_plan_payload else None
        )

        # Resolve the active domain pack.
        registry = default_registry()
        allowed = (
            frozenset(input.allowed_domain_overrides)
            if input.allowed_domain_overrides else None
        )
        domain_context = select_domain(
            registry=registry,
            detection_context=_EmptyDetectionContext(),
            user_override=input.domain_override,
            workspace_default=input.workspace_default_domain,
            detection_enabled=False,
            allowed_overrides=allowed,
        )
        domain_pack = registry.get(domain_context.selected_domain)

        # Skip-path: build a sentinel typed overlay so the FE sees
        # an explicit "enrichment skipped" record.
        if not enrich_plan.should_enrich:
            skip_reason = (
                "; ".join(enrich_plan.blocking_issues)
                or "; ".join(enrich_plan.reasons)
                or "enrichment skipped by post-compile assessor"
            )
            skipped = build_skipped_enrichment_result(
                document_id=input.document_id,
                reason=skip_reason,
                domain_id=(domain_pack.id if domain_pack else None),
            )
            payload = skipped.to_payload()
            persist_error = _persist_enrichment_payload(
                self._processing, ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=payload,
                actor=input.actor,
            )
            return RunEnrichmentStageResult(
                status="skipped",
                plan_payload=payload,
                artifact_id=persist_error.artifact_id,
                require_enrichment_success=enrich_plan.require_enrichment_success,
                persist_error=persist_error.error,
            )

        # Run-path: assemble context, dispatch the runner.
        context = EnrichmentContext(
            document_id=input.document_id,
            compile_result=compile_result,
            enrich_plan=enrich_plan,
            domain_pack=domain_pack,
            initial_plan=initial_plan,
        )
        runner = CompositeEnrichmentRunner(modules=[
            MetadataEnrichmentModule(),
            TerminologyEnrichmentModule(),
            ValidationEnrichmentModule(),
        ])
        result = runner.run(context)
        payload = result.to_payload()
        persist_outcome = _persist_enrichment_payload(
            self._processing, ctx,
            run_id=input.run_id,
            document_id=input.document_id,
            payload=payload,
            actor=input.actor,
        )
        return RunEnrichmentStageResult(
            status=result.status,
            plan_payload=payload,
            artifact_id=persist_outcome.artifact_id,
            require_enrichment_success=enrich_plan.require_enrichment_success,
            persist_error=persist_outcome.error,
        )

    @activity.defn(name=ACTIVITY_PERSIST_COMPILE_RESULT_SUMMARY)
    def persist_compile_result_summary(
        self, input: PersistCompileResultSummaryInput,
    ) -> ArtifactActivityResult:
        """Persist the typed `NormalizedCompileResult` as a
        `compile_result_summary` artifact. Best-effort — a write
        failure is returned in the response; the workflow logs it
        and continues because the durable signal for downstream
        stages is the inline payload the workflow already holds."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_compile_result_summary(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=dict(input.payload),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_BUILD_INITIAL_EXECUTION_PLAN)
    def build_initial_execution_plan(
        self, input: BuildInitialExecutionPlanInput,
    ) -> BuildInitialExecutionPlanResult:
        """Resolve the domain pack, build the
        `InitialExecutionPlan`, persist it as an
        `initial_execution_plan` artifact, and return the payload.

        Pack-resolution precedence: override → workspace default →
        general fallback. NO auto-detection at pre-compile time —
        the detection context (title / headings / early-page text)
        isn't available until compile output. The activity therefore
        uses `select_domain(..., detection_enabled=False)` so the
        resolution stays cheap and deterministic.

        Best-effort persistence: a write error is reported on the
        result but the inline payload still flows to the workflow,
        so downstream stages have the plan even when the artifact
        write failed."""
        from j1.domains.registry import default_registry, select_domain
        from j1.processing.initial_execution_plan import (
            build_initial_execution_plan as _build_plan,
        )

        ctx = input.scope.to_context()

        registry = default_registry()
        allowed = (
            frozenset(input.allowed_domain_overrides)
            if input.allowed_domain_overrides else None
        )
        # Sentinel context: no title / no headings / no captions.
        # `detection_enabled=False` makes the absence inert — the
        # selector falls through override → workspace → fallback
        # without scoring a single rule.
        sentinel_ctx = _EmptyDetectionContext()
        domain_context = select_domain(
            registry=registry,
            detection_context=sentinel_ctx,
            user_override=input.domain_override,
            workspace_default=input.workspace_default_domain,
            detection_enabled=False,
            allowed_overrides=allowed,
        )
        pack = registry.get(domain_context.selected_domain)
        plan = _build_plan(
            input.profile,
            domain_pack=pack,
            resource_hints=dict(input.resource_hints) or None,
        )
        plan_payload = plan.to_payload()
        # Surface the selection trail on the plan as well so the FE
        # can render "domain picked via: user override" without
        # parsing the audit log.
        plan_payload.setdefault("domain_selection_source", domain_context.selection_source)
        plan_payload.setdefault("domain_selection_confidence", domain_context.confidence)

        artifact_id: str | None = None
        error: str | None = None
        try:
            record = self._processing.persist_initial_execution_plan(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=plan_payload,
                actor=input.actor,
            )
            artifact_id = record.artifact_id
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            error = f"{type(exc).__name__}: {exc}"

        return BuildInitialExecutionPlanResult(
            status="succeeded",
            plan_payload=plan_payload,
            artifact_id=artifact_id,
            error=error,
            domain_profile_id=plan.domain_profile_id,
        )

    @activity.defn(name=ACTIVITY_PERSIST_INITIAL_EXECUTION_PLAN)
    def persist_initial_execution_plan(
        self, input: PersistInitialExecutionPlanInput,
    ) -> ArtifactActivityResult:
        """Persist the pre-compile initial execution plan as an
        `initial_execution_plan` artifact. Best-effort — any
        persistence error is returned in the response; the workflow
        logs it and proceeds because the durable signal for
        downstream stages is the inline plan the workflow already
        holds, not the artifact."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_initial_execution_plan(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=dict(input.payload),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_PERSIST_POST_COMPILE_ENRICH_PLAN)
    def persist_post_compile_enrich_plan(
        self, input: PersistPostCompileEnrichPlanInput,
    ) -> ArtifactActivityResult:
        """Persist the post-compile rule-based enrich-assessment
        verdict as a `post_compile_enrich_plan` artifact. Best-effort
        — any persistence error is returned in the response, the
        workflow logs it and proceeds; the durable signal for
        downstream stage gating is the inline assessment result the
        workflow already holds."""
        ctx = input.scope.to_context()
        try:
            record = self._processing.persist_post_compile_enrich_plan(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=dict(input.payload),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=[record.artifact_id],
            kinds=(record.kind,),
        )

    @activity.defn(name=ACTIVITY_FAST_LLM_CONSULT_ENRICH)
    def fast_llm_consult_enrich(
        self, input: FastLLMConsultEnrichInput,
    ) -> FastLLMConsultEnrichResult:
        """Optional fast-LLM consult on the rule-based enrich plan.

        Activity-side logic:
          1. Resolve `FastLLMConsultSettings` from env. Disabled by
             default → return `consulted=False`.
          2. If no `fast_llm_consult` callable was wired at bootstrap
             (worker has no LLM client for the configured provider/
             model) → return `consulted=False`.
          3. Settings disabled, missing provider, or missing model →
             return `consulted=False`.
          4. Call the callable; any exception → log + return
             `consulted=False` (NEVER raise).
          5. Callable returns None or unparseable refinement →
             `consulted=False`.

        The callable is responsible for honouring the configured
        timeout. The activity wraps everything in a broad except so
        a misbehaving LLM cannot fail ingestion."""
        from j1.processing.enrich_assessment import (
            EnrichRecommendation,
            FastLLMConsultPrompt,
        )
        from j1.processing.enrich_assessment_settings import (
            load_fast_llm_consult_settings,
        )

        settings = load_fast_llm_consult_settings()
        if not settings.is_actionable():
            return FastLLMConsultEnrichResult(
                consulted=False,
                fallback_reason=(
                    "fast-LLM consult disabled or missing provider/model"
                ),
            )
        if self._fast_llm_consult is None:
            return FastLLMConsultEnrichResult(
                consulted=False,
                fallback_reason=(
                    "fast-LLM consult enabled in env but no callable "
                    "wired at worker bootstrap"
                ),
            )
        try:
            provisional = EnrichRecommendation(
                input.provisional_recommendation
            )
        except ValueError:
            return FastLLMConsultEnrichResult(
                consulted=False,
                fallback_reason=(
                    "provisional_recommendation has unrecognised value"
                ),
            )
        prompt = FastLLMConsultPrompt(
            compile_status=input.compile_status,
            final_compile_quality=input.final_compile_quality,
            source_signals=dict(input.source_signals or {}),
            provisional_recommendation=provisional,
            provisional_recommended_tasks=tuple(
                input.provisional_recommended_tasks or ()
            ),
            provisional_skipped_tasks=tuple(
                input.provisional_skipped_tasks or ()
            ),
            compile_warnings=tuple(input.compile_warnings or ()),
        )
        try:
            refinement = self._fast_llm_consult(prompt, settings)
        except Exception as exc:  # noqa: BLE001 — consult must never fail ingest
            return FastLLMConsultEnrichResult(
                consulted=False,
                fallback_reason=f"{type(exc).__name__}: {exc}",
            )
        if refinement is None:
            return FastLLMConsultEnrichResult(
                consulted=False,
                fallback_reason="callable returned no refinement",
            )
        rec_value = (
            refinement.recommendation.value
            if refinement.recommendation is not None else None
        )
        return FastLLMConsultEnrichResult(
            consulted=True,
            recommendation=rec_value,
            add_reasons=list(refinement.add_reasons),
            add_recommended_tasks=list(refinement.add_recommended_tasks),
        )

    @activity.defn(name=ACTIVITY_VALIDATE_STAGE)
    def validate_stage(
        self, input: ValidateStageInput,
    ) -> StageValidationActivityResult:
        """Run the per-stage validation contract for one stage of one
        run. Reads back each artifact the stage produced, dispatches
        to the right validator (`validate_compile` / `validate_chunks`
        / etc.), persists a `stage_validation_report` artifact with
        the full result, and returns a compact summary the workflow
        uses to decide between COMPLETED and FAILED.

        Failure modes:
          * Unknown `stage_name` → returns `passed=True` with a
            warning check. Defensive: an unrecognised stage isn't
            a fatal workflow event; the validation just doesn't
            assert anything.
          * Unreadable artifact → check fails, persisted in the
            report, surfaced as `passed=False`.
          * Persist failure → return `passed=False` with the error
            in the response. The workflow treats this as a stage
            failure (we can't audit a stage we couldn't validate
            durably)."""
        from pathlib import Path
        from j1.processing.stage_validation import (
            STAGE_COMPILE,
            STAGE_ENRICH,
            STAGE_GENERATE_CHUNKS,
            STAGE_GRAPH,
            StageValidationCheck,
            StageValidationResult,
            VALIDATION_STATUS_FAILED,
            VALIDATION_STATUS_WARNING,
            VALIDATOR_VERSION,
            aggregate_status,
        )
        from j1.processing.stage_validators import (
            validate_chunks,
            validate_compile,
            validate_enrich,
            validate_graph,
        )
        from j1.workspace.layout import WorkspaceArea

        ctx = input.scope.to_context()

        # Resolve every artifact id to a record. Skip-on-missing —
        # the validator surfaces it as a check failure rather than
        # raising here, so the report is still persisted.
        artifacts: list = []
        missing_ids: list[str] = []
        for aid in input.output_artifact_ids:
            try:
                artifacts.append(self._artifacts.get(ctx, aid))
            except Exception:  # noqa: BLE001
                missing_ids.append(aid)

        # Read-back closure — gives validators raw bytes (or None on
        # failure). Path resolution: split the artifact location on
        # `/`, validate the area name, join under the workspace's
        # area dir.
        def _read_back(record) -> bytes | None:
            location = (record.location or "").strip()
            if not location:
                return None
            area_name, _, rest = location.partition("/")
            if not area_name or not rest:
                return None
            try:
                area = WorkspaceArea(area_name)
            except ValueError:
                return None
            path = self._processing._workspace.area(ctx, area) / rest  # noqa: SLF001
            try:
                return Path(path).read_bytes()
            except (OSError, ValueError):
                return None

        # Stage dispatch.
        stage = input.stage_name
        checks: list[StageValidationCheck] = []
        for missing_aid in missing_ids:
            checks.append(StageValidationCheck(
                name="artifact_registered",
                status="failed",
                message=(
                    f"artifact_id {missing_aid!r} not found in registry "
                    "— stage reported it as output but the record is gone"
                ),
            ))
        if stage == STAGE_COMPILE:
            checks.extend(validate_compile(
                artifacts=artifacts,
                expected_tenant=ctx.tenant_id,
                expected_project=ctx.project_id,
                expected_run_id=input.run_id,
                expected_document_id=input.document_id or "",
                read_back=_read_back,
            ))
        elif stage == STAGE_GENERATE_CHUNKS:
            checks.extend(validate_chunks(
                artifacts=artifacts,
                expected_tenant=ctx.tenant_id,
                expected_project=ctx.project_id,
                expected_run_id=input.run_id,
                expected_document_id=input.document_id or "",
                read_back=_read_back,
            ))
        elif stage == STAGE_ENRICH:
            checks.extend(validate_enrich(
                artifacts=artifacts,
                expected_tenant=ctx.tenant_id,
                expected_project=ctx.project_id,
                expected_run_id=input.run_id,
                expected_document_id=input.document_id,
                enrich_required=input.enrich_required,
                read_back=_read_back,
            ))
        elif stage == STAGE_GRAPH:
            checks.extend(validate_graph(
                artifacts=artifacts,
                expected_tenant=ctx.tenant_id,
                expected_project=ctx.project_id,
                expected_run_id=input.run_id,
                expected_document_id=input.document_id,
                graph_required=input.graph_required,
                chunk_artifact_ids=set(input.chunk_artifact_ids),
                read_back=_read_back,
            ))
        else:
            checks.append(StageValidationCheck(
                name="unknown_stage",
                status="warning",
                message=(
                    f"stage_name {stage!r} has no registered validator; "
                    "skipping content checks"
                ),
            ))

        validation_status = aggregate_status(checks)
        errors = [
            c.message or c.name
            for c in checks if c.status == "failed"
        ]
        warnings = [
            c.message or c.name
            for c in checks if c.status == "warning"
        ]

        # Build the durable result.
        result = StageValidationResult(
            stage_name=stage,
            run_id=input.run_id,
            document_id=input.document_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            workspace_id=None,  # workspace_id isn't on ProjectScope today
            attempt=input.attempt,
            validation_status=validation_status,
            checks=list(checks),
            errors=errors,
            warnings=warnings,
            output_refs=list(input.output_artifact_ids),
            artifact_refs=[a.artifact_id for a in artifacts],
            validator_version=VALIDATOR_VERSION,
        )

        # Persist the report artifact. Failure to persist is itself
        # a validation failure — we can't audit what we couldn't save.
        artifact_id: str | None = None
        try:
            record = self._processing.persist_stage_validation_report(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                stage_name=stage,
                attempt=input.attempt,
                payload=result.to_payload(),
                actor=input.actor,
            )
            artifact_id = record.artifact_id
        except Exception as exc:  # noqa: BLE001
            # Demote validation_status to failed so the workflow
            # records FAILED rather than COMPLETED.
            errors.append(
                f"persist_stage_validation_report failed: "
                f"{type(exc).__name__}: {exc}"
            )
            validation_status = VALIDATION_STATUS_FAILED

        passed = validation_status in (
            "passed", VALIDATION_STATUS_WARNING,
        )
        return StageValidationActivityResult(
            stage_name=stage,
            validation_status=validation_status,
            passed=passed,
            error_count=len(errors),
            warning_count=len(warnings),
            check_count=len(checks),
            artifact_id=artifact_id,
            errors=errors,
        )

    @activity.defn(name=ACTIVITY_VERIFY_COMPILE_OUTPUT)
    def verify_compile_output(
        self, input: VerifyCompileInput,
    ) -> VerifyCompileActivityResult:
        """Post-compile health gate.

        Verifies the compile activity produced enough chunk artifacts
        (`min_chunks`, default 1) and — when `require_index_manifest`
        is set — that the index activity wrote a manifest. Returns
        `passed=False` with one of the `FAILURE_CODE_*` strings from
        `j1.runs.models` when the gate rejects; the workflow lifts
        that into `IngestionRun.failure_code` on rejection.

        Kind-only check: we look at the artifact kinds passed in by
        the workflow OR (fallback) resolve each artifact id against
        the registry. Schema integrity is the per-stage validators'
        job (`validate_compile` / `validate_chunks`) — this gate
        catches the cheap "compile succeeded but produced nothing"
        case before declaring SUCCEEDED.

        Failure modes:
          * `chunk_count < min_chunks` → CHUNK_FAILED.
          * `require_index_manifest=True` and no `index_manifest`
            artifact → INDEX_FAILED.
          * Unresolvable artifact ids when kinds aren't provided →
            VERIFICATION_FAILED (we can't verify what we can't read).
        """
        from j1.processing.stage_validators import (
            verify_compile_output_health,
        )
        from j1.runs.models import FAILURE_CODE_VERIFICATION_FAILED

        ctx = input.scope.to_context()
        artifact_kinds: tuple[str, ...] = tuple(input.output_artifact_kinds)
        errors: list[str] = []
        if not artifact_kinds and input.output_artifact_ids:
            resolved: list[str] = []
            for aid in input.output_artifact_ids:
                try:
                    record = self._artifacts.get(ctx, aid)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"artifact_id {aid!r} not resolvable: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                resolved.append(record.kind)
            artifact_kinds = tuple(resolved)
        if errors:
            return VerifyCompileActivityResult(
                passed=False,
                reason_code=FAILURE_CODE_VERIFICATION_FAILED,
                message=(
                    "verification could not resolve all compile artifacts; "
                    "cannot confirm output health"
                ),
                chunk_count=0,
                artifact_count=len(input.output_artifact_ids),
                errors=errors,
            )
        passed, reason_code, message, chunk_count = verify_compile_output_health(
            artifact_kinds=artifact_kinds,
            min_chunks=input.min_chunks,
            require_index_manifest=input.require_index_manifest,
        )
        return VerifyCompileActivityResult(
            passed=passed,
            reason_code=reason_code,
            message=message,
            chunk_count=chunk_count,
            artifact_count=len(artifact_kinds),
            errors=[],
        )

    # ---- Progress-reporter integration -------------------------

    def _report_step_start(
        self, ctx, input, *, stage: str, step: str, engine: str | None,
    ) -> None:
        """Emit `step.started` if a reporter is configured AND the
        caller supplied a `correlation_id` (which by convention
        equals `run_id`). No-op otherwise — keeps existing behaviour
        for deployments that don't opt into the progress surface.

        Also flips the `IngestionRun` record to `RUNNING` and updates
        `current_stage` / `current_step` / `progress_percent` so the
        FE's polling endpoint reflects mid-pipeline state. Without
        this update the run sits at ASSESSING until terminal and the
        UI's PrimaryStatusPanel stays on 'Building execution plan…'."""
        if input.correlation_id:
            self._update_run_progress(
                ctx, run_id=input.correlation_id,
                status=RunStatus.RUNNING,
                stage=stage, step=step,
                progress_percent=_STAGE_START_PROGRESS.get(stage),
            )
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
        status_value = (
            result.status.value if hasattr(result.status, "value")
            else str(result.status)
        )
        if input.correlation_id and status_value == "succeeded":
            # Advance the run record to the end-of-stage progress
            # tick so the FE's progress bar moves between stages
            # rather than sitting at the start-of-stage tick until
            # terminal. Stage stays the same — the next stage's
            # `_report_step_start` call updates it.
            self._update_run_progress(
                ctx, run_id=input.correlation_id,
                status=RunStatus.RUNNING,
                stage=stage, step=step,
                progress_percent=_STAGE_END_PROGRESS.get(stage),
            )
        if self._reporter is None or not input.correlation_id:
            return
        try:
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

    def _update_run_progress(
        self,
        ctx,
        *,
        run_id: str,
        status: RunStatus,
        stage: str,
        step: str,
        progress_percent: int | None,
    ) -> None:
        """Best-effort update of the IngestionRun record so the FE's
        polling endpoint sees mid-pipeline state.

        Mirrors `_persist_run_terminal` in `RunsActivities` but for
        non-terminal transitions: status flips to RUNNING the first
        time a stage starts, and `current_stage` / `current_step` /
        `progress_percent` track the most recent stage event. Failures
        are swallowed — telemetry never blocks ingest. No-op when
        `run_store` is unwired (legacy deployments)."""
        if self._run_store is None:
            return
        try:
            run = self._run_store.get(ctx, run_id)
        except Exception:  # noqa: BLE001
            return
        if run is None or run.is_terminal():
            return
        # Forward-only status promotion. Once the workflow advances
        # past the confirm gate, the next activity legitimately moves
        # PLAN_READY / WAITING_FOR_CONFIRMATION → RUNNING; without the
        # transitional states in this set the run stays visually
        # stuck at PLAN_READY until terminal. CANCELLING and PAUSED
        # are deliberately omitted: an in-flight activity must not
        # un-cancel or un-pause a run that operations explicitly
        # halted.
        promote_from = (
            RunStatus.CREATED,
            RunStatus.RECEIVED,
            RunStatus.ASSESSING,
            RunStatus.PLAN_READY,
            RunStatus.ASSESSMENT_READY,
            RunStatus.WAITING_FOR_CONFIRMATION,
            RunStatus.COMPILE_PENDING,
            RunStatus.RUNNING,
            RunStatus.COMPILING,
            RunStatus.VERIFYING,
        )
        if run.status in promote_from:
            run.status = status
        run.current_stage = stage
        run.current_step = step
        if progress_percent is not None:
            # Never regress the bar — concurrent activities can race
            # the writes and end-of-stage shouldn't be undone by a
            # later start-of-stage at the same percent.
            run.progress_percent = max(run.progress_percent, progress_percent)
        run.updated_at = datetime.now(timezone.utc)
        try:
            # Re-read immediately before upsert. The JSONL store is
            # append-only with last-snapshot semantics, so a workflow
            # finalize that writes a terminal status between our
            # initial read (line 575) and this point would otherwise
            # be clobbered by our non-terminal write. Cheap re-read
            # tightens (but doesn't eliminate) the race window.
            latest = self._run_store.get(ctx, run_id)
            if latest is not None and latest.is_terminal():
                return
            self._run_store.upsert(ctx, run)
        except Exception:  # noqa: BLE001 — telemetry never blocks ingest
            pass

    @staticmethod
    def _lookup(registry: dict, kind: str, role: str):
        try:
            return registry[kind]
        except KeyError as exc:
            raise UnknownProcessorError(
                f"no {role} registered for kind {kind!r}"
            ) from exc


# Per-stage progress ticks (0..100). Coarse but visible: the FE's
# progress bar advances on each stage boundary so users see motion
# rather than a single jump from 0% to 100% at run terminal. The
# numbers are deliberately conservative — index completion only
# reaches 95% so `_persist_run_terminal` can land the final 100%.
_STAGE_START_PROGRESS: dict[str, int] = {
    "COMPILE": 10,
    "POST_COMPILE_ASSESS": 35,
    "ENRICH": 45,
    "GRAPH": 65,
    "INDEX": 85,
}
_STAGE_END_PROGRESS: dict[str, int] = {
    "COMPILE": 30,
    "POST_COMPILE_ASSESS": 42,
    "ENRICH": 60,
    "GRAPH": 80,
    "INDEX": 95,
}


def _compile_cache_key_parts(input, compiler, document) -> dict:
    """Build the cache-key parts for a compile activity input.

    `processor_version` and `mode` are pulled from the compiler when
    it surfaces them (an attribute named `version` / `mode`). Most
    compiler implementations don't yet, so the cache key collapses to
    `(document_hash, processor_kind)` — sufficient for the immediate
    "don't re-parse the same document" guarantee. Implementations
    that bump output shape should expose `version` so cache rows
    invalidate cleanly across upgrades.

    `document_hash` comes from the registry's checksum field —
    content-derived, prefix-tagged (`sha256:…`), and stable across
    re-uploads of identical content."""
    return {
        "document_hash": getattr(document, "checksum", "") or "",
        "processor_kind": input.processor_kind or "",
        "processor_version": str(getattr(compiler, "version", "") or ""),
        "mode": str(getattr(compiler, "mode", "") or ""),
    }


def _make_key(parts: dict) -> str:
    from j1.processing.cache import make_cache_key
    return make_cache_key(**parts)


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


def _current_activity_attempt() -> int:
    """Return the current attempt number (1-based) when running
    inside a Temporal worker, else 1.

    Lets cache rows record which attempt produced them — useful for
    operators triaging "did the second attempt also fail?"."""
    try:
        info = activity.info()
        return int(getattr(info, "attempt", 1))
    except Exception:  # noqa: BLE001 — outside Temporal context
        return 1


@contextlib.contextmanager
def _heartbeating(details: dict[str, object], *, interval_seconds: float = 30.0):
    """Background heartbeat ticker for long-running synchronous calls.

    Use as a context manager around any blocking call that may exceed
    the activity's `heartbeat_timeout`. A daemon thread emits
    `activity.heartbeat(details)` every `interval_seconds` until the
    block exits. The first heartbeat fires immediately on entry so
    Temporal sees the activity is alive even if the call returns
    quickly.

    Without this, the compile activity hits `heartbeat_timeout` mid-
    parse on real documents (MinerU + raganything routinely run for
    minutes), Temporal marks the activity timed-out, and retries —
    spawning fresh subprocesses on every retry. The "many MinerU
    starts for one document" symptom.

    Threading + contextvars: `temporalio.activity.heartbeat()` reads
    the current activity from a `ContextVar`. `threading.Thread`
    does NOT propagate contextvars, so a naive daemon-thread call to
    `activity.heartbeat()` raises `RuntimeError: Not in activity
    context`. We capture the current context (which includes the
    activity contextvar set by the worker before invoking us) and
    run each heartbeat invocation under that context via
    `ctx.run(...)`. This is the standard Python pattern for
    propagating contextvars to threads.

    Heartbeat semantics: this proves the WORKER is alive, not that
    progress is being made. Callers that have real per-step progress
    (page counters, etc.) should heartbeat with those richer details
    via `_safe_heartbeat` directly; the ticker is the safety net for
    everyone else."""
    stop = threading.Event()
    captured_ctx = contextvars.copy_context()

    def _tick() -> None:
        # First beat fires immediately — Temporal needs at least one
        # heartbeat per `heartbeat_timeout` window, and we don't want
        # to wait `interval_seconds` for the first one.
        captured_ctx.run(_safe_heartbeat, details)
        while not stop.wait(interval_seconds):
            captured_ctx.run(_safe_heartbeat, details)

    thread = threading.Thread(target=_tick, daemon=True, name="j1-activity-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval_seconds + 1)


def _artifact_result(result: ArtifactProcessingResult) -> ArtifactActivityResult:
    # Surface only the keys the planner actually consumes today
    # (`has_images` / `has_tables` / `has_scanned_pages` / `page_count`
    # / `text_extractable_ratio`) so the activity payload doesn't
    # accidentally carry processor-internal blobs that aren't safe for
    # the audit log. Compile processors that don't populate any of
    # these leave `content_stats=None` — the planner falls back to the
    # deterministic profile.
    content_stats: dict[str, Any] | None = None
    if result.metadata:
        signal_keys = (
            "has_images",
            "has_tables",
            "has_scanned_pages",
            "page_count",
            "text_extractable_ratio",
            # Manifest signals (post-parse counts + quality scores).
            # These flow into `DocumentProfile` via the workflow's
            # `_merge_compile_signals` helper and feed the planner /
            # completion-validation gate.
            "image_count",
            "table_count",
            "equation_count",
            "text_block_count",
            "total_text_chars",
            "empty_page_ratio",
            "parse_quality_score",
            "text_sufficiency_score",
            "layout_complexity_score",
            # Per-image triage decisions surfaced by the parser. List
            # of dicts with `image_id` / `decision` / `role` / etc.
            # Empty list = parser surfaced no images; absent key =
            # parser doesn't surface per-image data at all.
            "images",
        )
        picked = {
            k: result.metadata[k]
            for k in signal_keys
            if k in result.metadata
        }
        if picked:
            content_stats = picked
    # Surface the kinds tuple so `_validate_completion` can enforce
    # per-stage required outputs without a separate registry query.
    kinds = tuple(
        str(getattr(r, "kind", "") or "") for r in result.artifacts
    )
    # Compile-safety-retry signals — read from the bridge's manifest
    # metadata. `chunks_count` falls back to counting `kinds` when
    # the manifest didn't surface it. The retry layer treats missing
    # `extracted_text_chars` as "unknown" + skips the chars-below-
    # threshold rule rather than retrying defensively.
    compile_metrics: dict[str, Any] = {}
    if result.metadata:
        chunks_count = result.metadata.get(
            "chunks_count",
            result.metadata.get("text_block_count"),
        )
        if not isinstance(chunks_count, int):
            chunks_count = sum(1 for k in kinds if k == "chunk")
        text_chars = result.metadata.get("total_text_chars")
        compile_metrics["chunks_count"] = int(chunks_count)
        if isinstance(text_chars, int):
            compile_metrics["extracted_text_chars"] = text_chars
        # Surface plan-derived warnings + unhandled capabilities
        # (already on metadata via the bridge) so the workflow
        # doesn't have to re-fetch the artifact.
        for key in (
            "plan_warnings",
            "unhandled_capabilities",
            "assessment_mode",
        ):
            if key in result.metadata:
                compile_metrics[key] = result.metadata[key]
    return ArtifactActivityResult(
        status=result.status.value,
        artifact_ids=[r.artifact_id for r in result.artifacts],
        error=result.error,
        message=result.message,
        content_stats=content_stats,
        kinds=kinds,
        compile_metrics=compile_metrics,
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
