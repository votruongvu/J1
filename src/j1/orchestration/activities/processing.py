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
        try:
            with _heartbeating({
                "stage": "compile",
                "document_id": input.document_id,
                "processor_kind": input.processor_kind,
            }):
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
    "ENRICH": 45,
    "GRAPH": 65,
    "INDEX": 85,
}
_STAGE_END_PROGRESS: dict[str, int] = {
    "COMPILE": 40,
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
    return ArtifactActivityResult(
        status=result.status.value,
        artifact_ids=[r.artifact_id for r in result.artifacts],
        error=result.error,
        message=result.message,
        content_stats=content_stats,
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
