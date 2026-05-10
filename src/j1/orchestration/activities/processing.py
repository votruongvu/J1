import contextlib
import contextvars
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

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
    GraphActivityInput,
    IndexActivityInput,
    InsertContentActivityInput,
    PersistErrorReportInput,
    PersistFinalSummaryInput,
    PersistValidationReportInput,
    ProcessingActivityResult,
    QueryActivityInput,
    QueryActivityResult,
    StageValidationActivityResult,
    ValidateStageInput,
)
from j1.processing.contracts import (
    EnrichmentProcessor,
    GraphBuilder,
    KnowledgeCompiler,
    QueryProvider,
    SearchIndexer,
)
from j1.processing.results import (
    ARTIFACT_KIND_PARSED_SOURCE,
    ArtifactProcessingResult,
    ProcessingResult,
    QueryResult,
)
from j1.processing.service import ProcessingService

ACTIVITY_COMPILE = "j1.processing.compile"
ACTIVITY_INSERT_CONTENT = "j1.processing.insert_content"
ACTIVITY_ENRICH = "j1.processing.enrich"
ACTIVITY_BUILD_GRAPH = "j1.processing.build_graph"
ACTIVITY_INDEX = "j1.processing.index"
ACTIVITY_QUERY = "j1.processing.query"
ACTIVITY_PERSIST_ERROR_REPORT = "j1.processing.persist_error_report"
ACTIVITY_PERSIST_VALIDATION_REPORT = "j1.processing.persist_validation_report"
ACTIVITY_PERSIST_FINAL_SUMMARY = "j1.processing.persist_final_summary"
ACTIVITY_VALIDATE_STAGE = "j1.processing.validate_stage"


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
        result_cache: ProcessingResultCache | None = None,
        run_store: IngestionRunStore | None = None,
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
            self.insert_content,
            self.enrich,
            self.build_graph,
            self.index,
            self.query,
            self.persist_error_report,
            self.persist_validation_report,
            self.persist_final_summary,
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

    @activity.defn(name=ACTIVITY_INSERT_CONTENT)
    def insert_content(
        self, input: InsertContentActivityInput,
    ) -> ArtifactActivityResult:
        """Drive `RAGAnything.insert_content_list` for the document
        whose `parsed_source` artifact was registered upstream.

        Used by the workflow when
        `pipeline_mode=split_parse_insert`. The compile activity
        ran first (parse-only) and registered a `parsed_source`
        artifact; this activity reads it back, calls the compiler's
        `insert_content` method, materialises chunk + graph drafts.
        """
        import json
        from pathlib import Path
        from j1.workspace.layout import WorkspaceArea

        ctx = input.scope.to_context()
        compiler = self._lookup(
            self._compilers, input.processor_kind, "compiler",
        )
        if not hasattr(compiler, "insert_content"):
            raise UnknownProcessorError(
                f"compiler {input.processor_kind!r} does not expose "
                "`insert_content` — split_parse_insert mode requires "
                "RAGAnythingCompiler ≥ this release. Set "
                "J1_RAGANYTHING_PIPELINE_MODE=complete to use the legacy "
                "single-shot path."
            )
        document = self._sources.get(ctx, input.document_id)

        # Read the parsed_source artifact and parse content_list +
        # doc_id. The artifact was registered by the parse activity
        # upstream; we resolve its on-disk path the same way the
        # ingestion-review service does.
        parsed_record = self._artifacts.get(
            ctx, input.parsed_source_artifact_id,
        )
        location = (parsed_record.location or "").strip()
        if "/" not in location:
            raise UnknownProcessorError(
                f"parsed_source artifact {input.parsed_source_artifact_id!r} "
                f"has malformed location {location!r}"
            )
        area_name, _, rest = location.partition("/")
        # Walk relative to the workspace's compiled area. The compile
        # activity emitted parsed_source under WorkspaceArea.COMPILED
        # via `_handle_artifact_output`.
        # NOTE: ProcessingService doesn't expose its workspace
        # resolver directly; reach in via the registry's ctx-bound
        # path resolution (we use the same `area_name` the registry
        # wrote).
        try:
            area = WorkspaceArea(area_name)
        except ValueError as exc:
            raise UnknownProcessorError(
                f"parsed_source artifact area {area_name!r} not recognised"
            ) from exc
        # The ProcessingService has the workspace resolver bound;
        # delegate the read through it via a small helper.
        artifact_path = (
            self._processing._workspace.area(ctx, area) / rest  # noqa: SLF001
        )
        try:
            payload = json.loads(
                Path(artifact_path).read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise UnknownProcessorError(
                f"failed to read parsed_source artifact {input.parsed_source_artifact_id!r}: {exc}"
            ) from exc
        content_list = payload.get("content_list") or []
        doc_id = str(payload.get("doc_id") or input.document_id)

        self._report_step_start(
            ctx, input, stage="INSERT_CONTENT", step="insert_content",
            engine=input.processor_kind,
        )
        try:
            with _heartbeating({
                "stage": "insert_content",
                "document_id": input.document_id,
                "processor_kind": input.processor_kind,
            }):
                result = self._processing.insert_content(
                    ctx,
                    compiler,
                    document,
                    content_list=content_list,
                    doc_id=doc_id,
                    source_filename=input.source_filename,
                    actor=input.actor,
                    correlation_id=input.correlation_id,
                )
        except Exception as exc:
            self._report_step_failure(
                ctx, input, stage="INSERT_CONTENT", step="insert_content",
                exc=exc,
            )
            raise
        _safe_heartbeat({
            "stage": "insert_content",
            "document_id": input.document_id,
            "status": result.status.value,
        })
        self._report_step_outcome(
            ctx, input, stage="INSERT_CONTENT", step="insert_content",
            result=result,
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
        # failure). Path resolution mirrors `insert_content`'s
        # parsed-source read: split on `/`, validate the area name,
        # join under the workspace's area dir.
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
            RunStatus.ASSESSING,
            RunStatus.PLAN_READY,
            RunStatus.WAITING_FOR_CONFIRMATION,
            RunStatus.RUNNING,
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
    "INSERT_CONTENT": 35,
    "ENRICH": 45,
    "GRAPH": 65,
    "INDEX": 85,
}
_STAGE_END_PROGRESS: dict[str, int] = {
    "COMPILE": 30,
    "INSERT_CONTENT": 42,
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
    # Surface the parsed_source artifact id when present — split-mode
    # handoff for the workflow's `insert_content` activity. In legacy
    # `complete` mode the bridge produces chunk + graph artifacts only;
    # this scan returns None and the workflow keeps the existing path.
    parsed_source_artifact_id: str | None = None
    for record in result.artifacts:
        if getattr(record, "kind", None) == ARTIFACT_KIND_PARSED_SOURCE:
            parsed_source_artifact_id = record.artifact_id
            break
    # Surface the kinds tuple so `_validate_completion` can enforce
    # per-stage required outputs without a separate registry query.
    kinds = tuple(
        str(getattr(r, "kind", "") or "") for r in result.artifacts
    )
    return ArtifactActivityResult(
        status=result.status.value,
        artifact_ids=[r.artifact_id for r in result.artifacts],
        error=result.error,
        message=result.message,
        content_stats=content_stats,
        parsed_source_artifact_id=parsed_source_artifact_id,
        kinds=kinds,
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
