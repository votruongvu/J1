import contextlib
import contextvars
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from j1.processing.diagnostics import DiagnosticRecorder
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
    PersistFinalIngestionReportInput,
    PersistInitialExecutionPlanInput,
    PersistPostCompileEnrichPlanInput,
    RunEnrichmentStageInput,
    RunEnrichmentStageResult,
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
ACTIVITY_PERSIST_ERROR_REPORT = "j1.processing.persist_error_report"
ACTIVITY_PERSIST_FINAL_SUMMARY = "j1.processing.persist_final_summary"
ACTIVITY_PERSIST_COMPILE_STRATEGY_REPORT = "j1.processing.persist_compile_strategy_report"
ACTIVITY_PERSIST_POST_COMPILE_ENRICH_PLAN = "j1.processing.persist_post_compile_enrich_plan"
ACTIVITY_PERSIST_INITIAL_EXECUTION_PLAN = "j1.processing.persist_initial_execution_plan"
ACTIVITY_BUILD_INITIAL_EXECUTION_PLAN = "j1.processing.build_initial_execution_plan"
ACTIVITY_PERSIST_COMPILE_RESULT_SUMMARY = "j1.processing.persist_compile_result_summary"
ACTIVITY_PERSIST_ENRICHMENT_RESULT = "j1.processing.persist_enrichment_result"
ACTIVITY_PERSIST_FINAL_INGESTION_REPORT = "j1.processing.persist_final_ingestion_report"
ACTIVITY_RUN_ENRICHMENT_STAGE = "j1.processing.run_enrichment_stage"
ACTIVITY_FAST_LLM_CONSULT_ENRICH = "j1.processing.fast_llm_consult_enrich"


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


def _find_existing_enrichment_result(
    artifact_registry,
    ctx,
    *,
    run_id: str,
    document_id: str | None,
) -> dict | None:
    """Look for a previously-persisted `enrichment_result` artifact
 matching this (run, document) pair.

 Returns the artifact's JSON payload (with `_artifact_id` set to
 the registry id) on hit, None on miss. Defensive — registry /
 file errors fall through to None so the activity re-runs the
 stage rather than crashing.

 Lookup is "most recent `enrichment_result` for the run id",
 matched on `metadata["run_id"]`. Used as an idempotency guard
 against Temporal activity retries / workflow replays."""
    import json as _json

    from j1.processing.results import ARTIFACT_KIND_ENRICHMENT_RESULT
    from j1.workspace.layout import WorkspaceArea

    if not run_id:
        return None
    try:
        records = artifact_registry.list_artifacts(
            ctx, kind=ARTIFACT_KIND_ENRICHMENT_RESULT,
        )
    except Exception:  # noqa: BLE001
        return None
    matches = [
        r for r in records
        if (r.metadata or {}).get("run_id") == run_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: r.updated_at, reverse=True)
    # Successfully found an existing record. Returning a sentinel
    # payload signals the activity to short-circuit re-execution.
    # We don't read the full JSON off disk here — the workflow's
    # downstream consumers already hold the inline plan payload
    # from the original execution; the activity just needs to
    # avoid running the LLM-cost runner a second time.
    artifact = matches[0]
    return {
        "_artifact_id": artifact.artifact_id,
        "_cache_hit": True,
        "document_id": document_id or "",
        "status": artifact.metadata.get("status") or "succeeded",
        "domain_id": artifact.metadata.get("domain_id"),
    }


def _read_latest_artifact_payload(
    artifact_registry,
    processing_service,
    ctx,
    *,
    kind: str,
    run_id: str,
) -> tuple[dict | None, str | None]:
    """Resolve the latest artifact of `kind` for `run_id` and return
 (payload_dict, artifact_id).

 Returns (None, None) on miss or on any read / decode error.
 Best-effort by design — the final-ingestion-report builder
 tolerates missing payloads + stages remain PENDING in that case."""
    import json as _json
    from pathlib import PurePosixPath
    from j1.workspace.layout import WorkspaceArea

    if not run_id:
        return (None, None)
    try:
        records = artifact_registry.list_artifacts(ctx, kind=kind)
    except Exception:  # noqa: BLE001
        return (None, None)
    matches = [
        r for r in records
        if (r.metadata or {}).get("run_id") == run_id
    ]
    if not matches:
        # Fall back to "all of kind" — some artifact writers don't
        # populate metadata.run_id; correlation_id on the record is
        # the next best filter.
        matches = [r for r in records if getattr(r, "correlation_id", None) == run_id]
    if not matches:
        return (None, None)
    matches.sort(key=lambda r: r.updated_at, reverse=True)
    record = matches[0]
    # Resolve the path via the workspace + simple path-traversal
    # guard (mirrors `IngestionResultReviewService._resolve_artifact_path`
    # but inline to keep the activity self-contained).
    location = (record.location or "").strip()
    if not location:
        return (None, record.artifact_id)
    parts = PurePosixPath(location).parts
    if len(parts) < 2:
        return (None, record.artifact_id)
    area_name, *rest = parts
    try:
        area = WorkspaceArea(area_name)
    except ValueError:
        return (None, record.artifact_id)
    try:
        area_root = processing_service._workspace.area(ctx, area).resolve()
        candidate = area_root.joinpath(*rest).resolve()
        candidate.relative_to(area_root)  # guard
        text = candidate.read_text(encoding="utf-8")
        payload = _json.loads(text)
    except Exception:  # noqa: BLE001
        return (None, record.artifact_id)
    if not isinstance(payload, dict):
        return (None, record.artifact_id)
    return (payload, record.artifact_id)


def _resolve_report_source_payloads(
    artifact_registry,
    ctx,
    *,
    run_id: str,
    document_id: str | None,  # noqa: ARG001 — reserved for per-doc filtering
) -> dict:
    """Aggregate per-kind payload reads for the final-
 ingestion-report builder. Returns a dict with the five payloads
 (each None on miss) plus the artifact-id ref map.

 All reads are best-effort — any I/O failure produces a missing
 entry and the report builder stays robust to it.

 The function reaches into `processing_service._workspace` to
 resolve paths; that's an intentional cross-module touch — the
 ingestion-review service's path resolver lives on a different
 component, and duplicating the small workspace-resolution + path-
 traversal guard here keeps the activity self-contained without
 pulling the FE-facing read service into the worker."""
    # Lazy import to keep test-time imports thin.
    from j1.processing.results import (
        ARTIFACT_KIND_COMPILE_RESULT_SUMMARY,
        ARTIFACT_KIND_ENRICHMENT_RESULT,
        ARTIFACT_KIND_FINAL_SUMMARY,
        ARTIFACT_KIND_INITIAL_EXECUTION_PLAN,
        ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
    )

    # Need a processing service for workspace path resolution.
    # Caller passes the registry as the first positional; we resolve
    # the processing service from the activity instance via closure
    # in the caller (`persist_final_ingestion_report` above).
    # This helper expects to be called WITH the activity instance's
    # `self._processing`, threaded via the closure below. Activities
    # call `_resolve_report_source_payloads(self._artifacts, ctx,
    # run_id=...)`; for the workspace we use a separate accessor
    # threaded explicitly. Simpler: capture both registries from the
    # caller as kwargs. (Signature kept simple — callers pass the
    # activity registries directly.)
    # NOTE: this function is called only from the
    # `persist_final_ingestion_report` activity which has
    # `self._processing` in scope; we expect the caller to wire
    # `processing_service` through. Since callers always have the
    # activity instance available, we pass the registry directly
    # and use the workspace via the artifact_registry's bound
    # workspace if exposed. As a fallback, we accept a
    # `processing_service` kwarg.
    processing_service = _processing_service_for_registry(artifact_registry)

    initial_plan, init_id = _read_latest_artifact_payload(
        artifact_registry, processing_service, ctx,
        kind=ARTIFACT_KIND_INITIAL_EXECUTION_PLAN, run_id=run_id,
    )
    compile_result, cmp_id = _read_latest_artifact_payload(
        artifact_registry, processing_service, ctx,
        kind=ARTIFACT_KIND_COMPILE_RESULT_SUMMARY, run_id=run_id,
    )
    enrich_plan, pcp_id = _read_latest_artifact_payload(
        artifact_registry, processing_service, ctx,
        kind=ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN, run_id=run_id,
    )
    enrichment_result, enr_id = _read_latest_artifact_payload(
        artifact_registry, processing_service, ctx,
        kind=ARTIFACT_KIND_ENRICHMENT_RESULT, run_id=run_id,
    )
    final_summary, fs_id = _read_latest_artifact_payload(
        artifact_registry, processing_service, ctx,
        kind=ARTIFACT_KIND_FINAL_SUMMARY, run_id=run_id,
    )

    artifact_refs: dict[str, str] = {}
    for key, val in (
        ("initial_execution_plan", init_id),
        ("compile_result_summary", cmp_id),
        ("post_compile_enrich_plan", pcp_id),
        ("enrichment_result", enr_id),
        ("final_summary", fs_id),
    ):
        if val:
            artifact_refs[key] = val

    raw_refs: tuple[str, ...] = tuple(
        str(r) for r in (
            (compile_result or {}).get("raw_artifact_refs") or []
        )
    )

    return {
        "initial_execution_plan": initial_plan,
        "compile_result_summary": compile_result,
        "post_compile_enrich_plan": enrich_plan,
        "enrichment_result": enrichment_result,
        "final_summary": final_summary,
        "artifact_refs": artifact_refs,
        "raw_compile_artifact_refs": raw_refs,
    }


# Module-level cache for the (artifact_registry → processing_service)
# binding. Established at activity construction so the helper above
# can look it up without changing the function signature. Empty
# entries are tolerated — the helper falls back to a stub workspace
# that resolves to None, which the readers tolerate.
_PROCESSING_SERVICE_FOR_REGISTRY: "dict[int, object]" = {}


def _processing_service_for_registry(artifact_registry):
    """Return the `ProcessingService` bound to the same workspace as
 `artifact_registry`, registered at activity construction. Falls
 back to a sentinel whose `_workspace` returns paths that fail
 the read — making the report builder skip missing artifacts."""

    class _NullWorkspace:
        def area(self, _ctx, _area):
            from pathlib import Path
            return Path("/__nonexistent__")

    class _Sentinel:
        _workspace = _NullWorkspace()

    return _PROCESSING_SERVICE_FOR_REGISTRY.get(
        id(artifact_registry), _Sentinel(),
    )


def _persist_enrichment_payload(
    service,
    ctx,
    *,
    run_id: str,
    document_id: str | None,
    payload: dict,
    actor: str,
) -> _PersistOutcome:
    """Persist the typed `EnrichmentResult.to_payload` dict via
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


def _augment_with_image_provider_warnings(
    enrichment_result: Any,
    image_provider: Any,
) -> Any:
    """splice the image-provider's structured warnings
 onto the image module's `EnrichmentModuleOutcome.warnings`.

 The image module runs against the adapter without seeing the
 provider's per-image misses directly; this helper reads
 `image_provider.last_result` after the runner finished and
 rebuilds the typed `EnrichmentResult` with the warnings spliced
 into the image outcome.

 Pure transformation — no I/O. Returns a new `EnrichmentResult`
 when warnings exist; passes the input through unchanged when
 none do."""
    from dataclasses import replace
    from j1.processing.enrichment_overlay import EnrichmentModuleStatus

    last = (
        image_provider.last_result() if hasattr(image_provider, "last_result")
        else None
    )
    if last is None or not last.warnings:
        return enrichment_result
    new_outcomes: list[Any] = []
    spliced = False
    for outcome in enrichment_result.module_outcomes:
        if (
            outcome.module_id == "image_enrichment"
            and outcome.status in (
                EnrichmentModuleStatus.RUN,
                EnrichmentModuleStatus.PARTIAL,
                EnrichmentModuleStatus.SKIPPED,
                EnrichmentModuleStatus.FAILED,
            )
        ):
            new_outcomes.append(replace(
                outcome,
                warnings=tuple(outcome.warnings) + tuple(last.warnings),
            ))
            spliced = True
        else:
            new_outcomes.append(outcome)
    if not spliced:
        return enrichment_result
    return replace(
        enrichment_result,
        module_outcomes=tuple(new_outcomes),
        warnings=tuple(enrichment_result.warnings) + tuple(last.warnings),
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
        # optional LLM clients + shared limiter that the
        # legacy-wrapper enrichment modules consume. When None (tests
        # / dev / deployments without LLM credentials) the wrappers
        # construct successfully but `can_run` returns False with
        # "no LLM client configured" — the runner records each as
        # SKIPPED. Bootstrap wires these from the same role registry
        # the `CompositeEnricher` uses.
        enrichment_text_client: object | None = None,
        enrichment_vision_client: object | None = None,
        enrichment_llm_call_limiter: object | None = None,
        diagnostic_recorder: "DiagnosticRecorder | None" = None,
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
        # register this (artifact_registry → processing_service)
        # binding so `_resolve_report_source_payloads` can look it up
        # without changing the helper signature. Activity instances
        # share the workspace via the processing service.
        _PROCESSING_SERVICE_FOR_REGISTRY[id(self._artifacts)] = self._processing
        # LLM clients + shared limiter for the legacy-
        # wrapper enrichment modules. When None, the wrappers
        # construct cleanly but `can_run` returns False with
        # "no LLM client configured" so they skip per-run.
        self._enrichment_text_client = enrichment_text_client
        self._enrichment_vision_client = enrichment_vision_client
        self._enrichment_llm_call_limiter = enrichment_llm_call_limiter
        # Phase-1 ingestion diagnostics. Optional collaborator —
        # when None the activities run unchanged. When wired, each
        # stage entry/exit emits a structured ``j1.ingestion.stage.*``
        # audit event and accumulates into the per-run report that
        # lands at terminal time.
        self._diagnostics = diagnostic_recorder

    def all_activities(self) -> list:
        # This MUST list every `@activity.defn`-decorated method on the
        # class. The Temporal worker registers only what's returned
        # here; any decorated method missing from this list silently
        # becomes a NotFoundError at activity-dispatch time, with two
        # well-known consequences:
        #   * `build_initial_execution_plan` missing → assessment block
        #     raises → fail_open swallows it → bridge falls back to env
        #     defaults → "No AssessmentPlan was attached" banner.
        #   * `persist_compile_result_summary` missing → workflow fails
        #     mid-compile when it tries to persist the artifact.
        # Add new activities to BOTH the `@activity.defn` decorator AND
        # this list when you add them — there's no automatic discovery.
        return [
            self.compile,
            self.enrich,
            self.build_graph,
            self.index,
            self.query,
            self.persist_error_report,
            self.persist_final_summary,
            self.persist_compile_strategy_report,
            self.persist_enrichment_result,
            self.run_enrichment_stage,
            self.persist_compile_result_summary,
            self.build_initial_execution_plan,
            self.persist_initial_execution_plan,
            self.persist_post_compile_enrich_plan,
            self.persist_final_ingestion_report,
            self.fast_llm_consult_enrich,
        ]

    @activity.defn(name=ACTIVITY_COMPILE)
    def compile(self, input: CompileActivityInput) -> ArtifactActivityResult:
        ctx = input.scope.to_context()
        # Phase-1 diagnostic stage wrap. No-op when recorder is
        # None. The wrap binds the active RunContext so LLM calls
        # invoked deep in the bridge stack get attributed to this
        # run (via the limiter's contextvar lookup), and records
        # stage start/end timing + counters built up by
        # ``_compile_impl`` as it discovers them.
        counters: dict[str, int] = {}
        with self._diag_stage_wrap(
            ctx, input.correlation_id, input.document_id,
            stage_name="compile",
            counters=counters,
        ):
            return self._compile_impl(input, ctx, counters)

    def _compile_impl(
        self,
        input: CompileActivityInput,
        ctx,
        diag_counters: dict[str, int],
    ) -> ArtifactActivityResult:
        compiler = self._lookup(self._compilers, input.processor_kind, "compiler")
        document = self._sources.get(ctx, input.document_id)

        # ---- Idempotency check ------------------------------------
        # Skip the expensive processor call entirely if a `completed`
        # result for the same (document_hash, processor_kind,...)
        # already exists. This catches:
        #  - Temporal activity retries after a worker crash, where
        #  the previous attempt completed successfully but
        #  Temporal didn't see the heartbeat.
        #  - Re-runs of a document that was already processed in a
        #  prior workflow (cache survives across workflows).
        # `processor_version` and `mode` come from the compiler
        # interface when implementations expose them; the empty
        # default keeps existing compilers working without changes.
        cache_key_parts = _compile_cache_key_parts(input, compiler, document)
        # An explicit reindex bypasses the cache: the REST contract
        # documents that "reindex ALWAYS re-parses the original
        # uploaded file" (adapters/rest/app.py docstring on
        # ``POST /documents/{id}/reindex``). The cache key omits
        # ``run_id`` / ``target_snapshot_id``, so without this guard a
        # prior successful entry would short-circuit the parse and
        # propagate stale artifact ids that may no longer exist in the
        # registry (the failure surfaces in enrich/graph as
        # ``ArtifactNotFoundError``).
        reindex_skip_cache = bool(getattr(input, "reindex_of", None))
        cached = (
            self._cache.lookup(ctx, **cache_key_parts)
            if self._cache is not None and not reindex_skip_cache
            else None
        )
        if cached is not None and cached.status == CACHE_STATUS_COMPLETED:
            # Defense in depth: a cache hit is only honoured when EVERY
            # cached artifact still resolves in the registry. A prior
            # successful run can have its artifacts pruned, invalidated,
            # or moved across snapshots; returning their ids here makes
            # the compile look like a no-op while the downstream stages
            # blow up with ArtifactNotFoundError. Verifying up-front
            # turns this into a clean cache miss → re-compile.
            missing_cached_artifacts = [
                aid for aid in cached.artifact_ids
                if not self._cached_artifact_exists(ctx, aid)
            ]
            if missing_cached_artifacts:
                _safe_heartbeat({
                    "stage": "compile",
                    "document_id": input.document_id,
                    "status": "cache_stale",
                    "missing_artifacts": len(missing_cached_artifacts),
                })
                cached = None
            else:
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

        # ---- Refresh-enrich compile reuse -----------------------------
        # Refresh-enrich runs (``run_type="refresh_enrich"``) carry
        # ``metadata.reused_compile_from_run_id`` pointing at the
        # previous active run whose compile output should be reused.
        # When present, we clone the source run's compile artifacts
        # under the new run_id rather than re-running MinerU — that's
        # the load-bearing user-visible value of refresh-enrich
        # (skip the expensive parse, re-run cheap enrichment).
        #
        # Behaviour falls through to the normal compile path when:
        #   * run_store isn't wired (legacy / tests);
        #   * the run record isn't found by correlation_id;
        #   * the source run has no compile artifacts;
        #   * the metadata key is missing or empty.
        reused = self._maybe_reuse_compile_artifacts(ctx, input)
        if reused is not None:
            _safe_heartbeat({
                "stage": "compile",
                "document_id": input.document_id,
                "status": "succeeded",
                "cache": "refresh_enrich_reuse",
            })
            return reused

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
        try:
            import inspect
            sig = inspect.signature(self._processing.compile)
            if (
                assessment_plan is not None
                and "assessment_plan" in sig.parameters
            ):
                compile_kwargs["assessment_plan"] = assessment_plan
            # Phase 9: forward the snapshot identity so the
            # ProcessingService can route it into the compiler's
            # snapshot-scoped workspace.
            if (
                getattr(input, "target_snapshot_id", None)
                and "target_snapshot_id" in sig.parameters
            ):
                compile_kwargs["target_snapshot_id"] = (
                    input.target_snapshot_id
                )
            # `minimum_queryable` execution profile: forward the
            # adapter-layer no-op-LLM hook so the compiler can ask
            # the bridge to swap LightRAG's stage-2 entity/relationship
            # extraction for a no-op. Introspection guard mirrors the
            # other forwards so older `ProcessingService` stubs in
            # tests don't trip a TypeError.
            if (
                getattr(input, "disable_entity_extraction", False)
                and "disable_entity_extraction" in sig.parameters
            ):
                compile_kwargs["disable_entity_extraction"] = True
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
        # Stamp counters the diagnostic stage wrapper will surface.
        # ``compile_metrics`` keys mirror what existing tests assert
        # against (chunk_count, extracted_text_chars, etc.) — we
        # forward them as-is so the diagnostic report carries the
        # same numbers the runtime already computed.
        out = _artifact_result(result)
        try:
            # Pull diagnostic counters straight from the activity
            # result. ``out.compile_metrics`` is the typed shape
            # ``_artifact_result`` produces; ``result.metadata`` is
            # the raw bridge payload — we check both because
            # different code paths (mock compilers, the bridge,
            # tests) populate one or the other.
            metric_sources: list[dict[str, Any]] = []
            cm = getattr(out, "compile_metrics", None)
            if isinstance(cm, dict):
                metric_sources.append(cm)
            if isinstance(result.metadata, dict):
                metric_sources.append(result.metadata)
            for src in metric_sources:
                for key, target in (
                    ("chunks_count", "chunk_count"),
                    ("chunk_count", "chunk_count"),
                    ("extracted_text_chars", "extracted_text_chars"),
                    ("page_count", "page_count"),
                    ("image_count", "image_count"),
                    ("table_count", "table_count"),
                ):
                    val = src.get(key)
                    if isinstance(val, (int, float)) and target not in diag_counters:
                        diag_counters[target] = int(val)
            diag_counters["artifact_count"] = len(out.artifact_ids or [])
            # Record the MinerU parse phase as its own stage event
            # when the bridge surfaced its wall-clock — otherwise
            # the operator can't tell whether time was spent in
            # MinerU itself vs. before/after.
            parse_elapsed_ms = None
            for src in metric_sources:
                v = src.get("parse_elapsed_ms")
                if isinstance(v, (int, float)):
                    parse_elapsed_ms = int(v)
                    break
            if (
                parse_elapsed_ms is not None
                and self._diagnostics is not None
                and input.correlation_id is not None
            ):
                parse_counters: dict[str, int] = {}
                for k in (
                    "chunk_count", "extracted_text_chars",
                    "page_count", "image_count", "table_count",
                ):
                    if k in diag_counters:
                        parse_counters[k] = diag_counters[k]
                self._diagnostics.record_stage_event(
                    ctx=ctx,
                    run_id=input.correlation_id,
                    stage_name="parse",
                    document_id=input.document_id,
                    duration_ms=parse_elapsed_ms,
                    success=True,
                    counters=parse_counters,
                )
        except Exception:  # noqa: BLE001 — diagnostics must not break compile
            pass
        return out

    def _maybe_reuse_compile_artifacts(
        self,
        ctx,
        input: CompileActivityInput,
    ) -> "ArtifactActivityResult | None":
        """Short-circuit compile when the run is a refresh-enrich.

        Reads ``metadata.reused_compile_from_run_id`` off the run
        record (looked up by ``correlation_id``). When present,
        clones every compile-stage artifact from the source run
        under the new run_id and returns SUCCESS — skipping the
        expensive MinerU / raganything parse entirely.

        Returns ``None`` when:
          * the run-store collaborator isn't wired;
          * the run record isn't found by correlation_id;
          * the metadata key is missing/empty;
          * the source run has no compile artifacts to clone.

        In every ``None`` case the caller falls through to the
        regular compile path — so a misconfigured refresh-enrich
        degrades safely to "full reindex" rather than to a failure.
        """
        if self._run_store is None:
            return None
        run_id = input.correlation_id
        if not run_id:
            return None
        try:
            run = self._run_store.get(ctx, run_id)
        except Exception:  # noqa: BLE001
            return None
        if run is None:
            return None
        meta = dict(run.metadata or {})
        source_run_id = str(meta.get("reused_compile_from_run_id") or "")
        if not source_run_id:
            return None
        # Collect compile-stage artifacts from the source run. The
        # compile stage's primary outputs are ``compiled.text`` and
        # any ``chunk`` artifacts produced from the same parse —
        # both carry ``metadata.run_id == source_run_id``. We clone
        # records whose ``kind`` starts with ``compiled.`` OR is
        # exactly ``chunk`` (the broad set the downstream stages
        # consume).
        try:
            all_records = self._artifacts.list_artifacts(ctx)
        except Exception:  # noqa: BLE001
            return None
        source_records = [
            r for r in all_records
            if str(r.metadata.get("run_id", "")) == source_run_id
            and r.source_document_ids
            and input.document_id in r.source_document_ids
            and (r.kind.startswith("compiled.") or r.kind == "chunk")
        ]
        if not source_records:
            return None
        # Clone each source artifact under the new run_id. The file
        # bytes are shared (same ``location``) — the registry entry
        # is duplicated with a fresh ``artifact_id`` and ``run_id``
        # stamped in metadata. Downstream stages filter by run_id
        # so the new run's enrichment / graph / index see ONLY
        # these clones, not the original source rows.
        from dataclasses import replace as _replace
        from datetime import datetime, timezone
        new_artifact_ids: list[str] = []
        now = datetime.now(timezone.utc)
        for r in source_records:
            new_id = f"{run_id}-{r.artifact_id}"
            new_metadata = dict(r.metadata or {})
            new_metadata["run_id"] = run_id
            new_metadata["reused_from_artifact_id"] = r.artifact_id
            new_metadata["reused_from_run_id"] = source_run_id
            clone = _replace(
                r,
                artifact_id=new_id,
                metadata=new_metadata,
                created_at=now,
                updated_at=now,
            )
            try:
                # ``_raw_add`` bypasses the lineage guard (which
                # rejects ``chunk`` artifacts without ``run_id``);
                # ours have the new run_id stamped above so the
                # bypass is purely to skip an already-passed check.
                self._artifacts._raw_add(clone)
                new_artifact_ids.append(new_id)
            except Exception:  # noqa: BLE001
                # One failed clone makes the whole reuse path
                # unreliable — fall back to a full parse rather
                # than ship a partial set.
                return None
        return ArtifactActivityResult(
            status="succeeded",
            artifact_ids=new_artifact_ids,
            message=(
                f"reused {len(new_artifact_ids)} compile artifact(s) "
                f"from run {source_run_id} (refresh-enrich)"
            ),
        )

    def _cached_artifact_exists(self, ctx, artifact_id: str) -> bool:
        """True iff ``artifact_id`` still resolves in the registry for
        this project. Used by the compile cache-hit path to detect a
        stale entry (artifacts pruned / invalidated since the original
        run) before returning ids whose downstream lookup would raise
        ``ArtifactNotFoundError`` mid-pipeline."""
        from j1.artifacts.registry import ArtifactNotFoundError
        try:
            self._artifacts.get(ctx, artifact_id)
        except ArtifactNotFoundError:
            return False
        except Exception:  # noqa: BLE001 — registry transient -> treat as stale
            return False
        return True

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
        with self._diag_stage_wrap(
            ctx, input.correlation_id, input.document_id,
            stage_name="enrich",
            counters={},
        ):
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
        with self._diag_stage_wrap(
            ctx, input.correlation_id, input.document_id,
            stage_name="build_graph",
            counters={"input_artifact_count": len(input.artifact_ids or [])},
        ):
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
                        document_id=input.document_id,
                        target_snapshot_id=getattr(
                            input, "target_snapshot_id", None,
                        ),
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
        with self._diag_stage_wrap(
            ctx, input.correlation_id, input.document_id,
            stage_name="index",
            counters={"input_artifact_count": len(input.artifact_ids or [])},
        ):
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
        """Persist the typed enrichment overlay as an
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
        """Run the typed enrichment overlay stage.

 Resolves the domain pack via the registry (override →
 workspace default → fallback to general), rebuilds the
 `NormalizedCompileResult` + `PostCompileEnrichPlan` from
 their persisted payloads, builds an `EnrichmentContext`,
 runs `CompositeEnrichmentRunner` over the default skeleton
 module set, and persists the resulting `EnrichmentResult`
 as an `enrichment_result` artifact.

 Skipped-path handling: when `enrich_plan.should_enrich` is
 False, the activity short-circuits to
 `build_skipped_enrichment_result` (typed sentinel with
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

        # resolve `require_enrichment_success` using the
        # full precedence chain: request override → domain pack
        # opinion → env fallback → system default. Today the
        # request shape doesn't carry a per-run override; the
        # resolver still walks the chain so domain-absent runs pick
        # up the env-level fallback from
        # `EnrichmentConcurrencySettings.require_enrichment_success`.
        from j1.processing.enrichment_policy import (
            resolve_require_enrichment_success,
        )
        from j1.processing.enrichment_settings import (
            load_enrichment_settings as load_concurrency_settings,
        )
        concurrency_settings = load_concurrency_settings()
        resolved_require_success = resolve_require_enrichment_success(
            request_override=None,
            project_default=None,
            domain_policy=(
                domain_pack.enrichment_policy if domain_pack else None
            ),
            env_default=(
                concurrency_settings.require_enrichment_success
                if concurrency_settings.enabled else None
            ),
        )
        require_success = resolved_require_success.require_enrichment_success

        # idempotency check. When an `enrichment_result`
        # artifact already exists for this (run, doc) pair, return
        # the cached payload instead of re-running the runner.
        # Guards against Temporal activity retries / workflow
        # replays that would otherwise pay the LLM cost twice.
        # Best-effort: a registry-lookup failure falls through to
        # the run path so a transient registry hiccup doesn't
        # bypass enrichment entirely.
        cached_payload = _find_existing_enrichment_result(
            self._artifacts, ctx, run_id=input.run_id,
            document_id=input.document_id,
        )
        if cached_payload is not None:
            return RunEnrichmentStageResult(
                status=str(cached_payload.get("status") or "succeeded"),
                plan_payload=cached_payload,
                artifact_id=cached_payload.get("_artifact_id"),
                require_enrichment_success=require_success,
                persist_error=None,
            )

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
                require_enrichment_success=require_success,
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
        # register the legacy-compatible adapter modules
        # alongside the skeletons. The adapters skip cleanly
        # when the activity wasn't constructed with LLM clients
        # (tests / dev without credentials) so adding them here is
        # safe in every deployment.
        #
        # construct the `PerImageVisionAdapter` PER RUN
        # (not at worker startup) so it can resolve actual image
        # bytes from the current run's compile-image artifacts.
        # When no raw vision client is wired (`vision_client=None`),
        # the image module skips with "no vision LLM client
        # configured"; when the client exists but no compile images
        # were detected, the module skips with "compile detected no
        # images"; when images WERE detected but their bytes can't
        # be loaded, the provider's warnings flow through to the
        # outcome.
        from j1.processing.enrichment_clients import (
            PerImageVisionAdapter,
            WorkspaceImageBytesProvider,
        )
        from j1.processing.legacy_enricher_modules import (
            build_legacy_enricher_modules,
        )
        image_provider: WorkspaceImageBytesProvider | None = None
        vision_adapter: object | None = None
        if self._enrichment_vision_client is not None:
            image_provider = WorkspaceImageBytesProvider(
                artifact_registry=self._artifacts,
                workspace=self._processing._workspace,
                ctx=ctx,
                document_id=input.document_id,
                run_id=input.run_id,
            )
            # Detect whether the supplied vision client is already an
            # adapter (any object implementing the `VisionAnalysisClient`
            # Protocol's `analyze` method) or the raw production
            # `VisionLLMClient` (per-image bytes; `analyze_image`).
            # In the former case we use it as-is — preserves the
            #  backward-compatible path where tests pass a
            # pre-constructed adapter directly. In the latter case
            # (production path), construct a per-run adapter that
            # wraps the raw client with the workspace-aware
            # `WorkspaceImageBytesProvider` AND the shared LLM-call
            # limiter (per-image acquisition).
            if hasattr(self._enrichment_vision_client, "analyze"):
                vision_adapter = self._enrichment_vision_client
            else:
                vision_adapter = PerImageVisionAdapter(
                    self._enrichment_vision_client,
                    image_provider=image_provider,
                    llm_call_limiter=self._enrichment_llm_call_limiter,
                )
        legacy_modules = build_legacy_enricher_modules(
            text_client=self._enrichment_text_client,
            vision_client=vision_adapter,
            llm_call_limiter=self._enrichment_llm_call_limiter,
        )
        runner = CompositeEnrichmentRunner(modules=[
            MetadataEnrichmentModule(),
            TerminologyEnrichmentModule(),
            ValidationEnrichmentModule(),
            *legacy_modules,
        ])
        result = runner.run(context)
        # surface the image-provider warnings (if any)
        # on the image module's outcome so missing-byte misses
        # reach the final report. We splice the warnings into the
        # already-built outcome rather than re-running the module.
        if image_provider is not None:
            result = _augment_with_image_provider_warnings(
                result, image_provider,
            )
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
        from j1.processing.profiling import DocumentProfile

        ctx = input.scope.to_context()

        # Coerce the profile to a `DocumentProfile`. Temporal's data
        # converter reconstructs the top-level `BuildInitialExecutionPlanInput`
        # dataclass but leaves nested fields typed `Any` as dicts —
        # `input.profile` therefore arrives as `dict`, not the typed
        # class, and downstream attribute access (`profile.extension`)
        # would raise `AttributeError`. `from_payload` accepts either
        # shape and round-trips cleanly.
        profile = DocumentProfile.from_payload(input.profile)

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
            profile,
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

    @activity.defn(name=ACTIVITY_PERSIST_FINAL_INGESTION_REPORT)
    def persist_final_ingestion_report(
        self, input: PersistFinalIngestionReportInput,
    ) -> ArtifactActivityResult:
        """build the typed `FinalIngestionReport` from the
 per-stage artifacts already on disk, then persist it as a
 `final_ingestion_report` artifact.

 Activity flow:
 1. Resolve the persisted artifact payloads for this
 (run, doc) pair by reading the latest matching
 `initial_execution_plan` / `compile_result_summary` /
 `post_compile_enrich_plan` / `enrichment_result` /
 `final_summary` artifacts off the registry.
 2. Build the typed `FinalIngestionReport`.
 3. Persist as a `final_ingestion_report` artifact.

 Best-effort throughout: any read or persistence error
 produces a `status="failed"` result; the workflow logs it
 and proceeds. The report is observability, not correctness."""
        from j1.processing.final_ingestion_report import (
            ReportSourceInputs,
            build_final_ingestion_report,
        )

        ctx = input.scope.to_context()
        try:
            sources = _resolve_report_source_payloads(
                self._artifacts, ctx,
                run_id=input.run_id,
                document_id=input.document_id,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=(
                    f"resolve_report_source_payloads failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        try:
            inputs = ReportSourceInputs(
                run_id=input.run_id,
                document_id=input.document_id,
                document_name=input.document_name,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                started_at=input.started_at,
                completed_at=input.completed_at,
                framework_final_status=input.framework_final_status,
                failure_code=input.failure_code,
                failure_message=input.failure_message,
                warning_count=input.warning_count,
                initial_execution_plan=sources["initial_execution_plan"],
                compile_result_summary=sources["compile_result_summary"],
                post_compile_enrich_plan=sources["post_compile_enrich_plan"],
                enrichment_result=sources["enrichment_result"],
                final_summary=sources["final_summary"],
                artifact_refs=sources["artifact_refs"],
                raw_compile_artifact_refs=sources["raw_compile_artifact_refs"],
                operator_notes=input.operator_notes,
            )
            report = build_final_ingestion_report(inputs)
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=(
                    f"build_final_ingestion_report failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        try:
            record = self._processing.persist_final_ingestion_report(
                ctx,
                run_id=input.run_id,
                document_id=input.document_id,
                payload=report.to_dict(),
                actor=input.actor,
            )
        except Exception as exc:  # noqa: BLE001
            return ArtifactActivityResult(
                status="failed",
                error=f"persist_final_ingestion_report failed: {exc}",
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

    # ---- Diagnostic recorder integration ----------------------

    @contextlib.contextmanager
    def _diag_stage_wrap(
        self,
        ctx,
        run_id: str | None,
        document_id: str | None,
        *,
        stage_name: str,
        counters: dict[str, int],
    ):
        """Combined stage wrap: binds the ambient :class:`RunContext`
        for LLM-call attribution AND records stage timing on exit.

        ``counters`` is a live dict the activity body can mutate as
        new numbers become known (e.g. ``compile`` learns
        ``chunk_count`` only after MinerU returns). The values at
        ``finally`` time are what land on the stage event.

        No-op (just runs the body) when the diagnostic recorder
        isn't wired or ``run_id`` is missing — the wrap stays
        backward-compatible with deployments that don't opt in.
        """
        if self._diagnostics is None or not run_id:
            yield
            return
        # Lazy import to keep the diagnostics module out of every
        # processing.py import path; the activity module is
        # imported during worker bootstrap which we want fast.
        from j1.processing.diagnostics import (
            RunContext, set_current_run_context,
        )
        rc = RunContext(
            run_id=run_id,
            document_id=document_id,
            stage=stage_name,
            recorder=self._diagnostics,
            ctx=ctx,
        )
        # ``stage()`` (context manager) emits ``stage.started`` at
        # entry and ``stage.completed`` at exit — proper start/end
        # timing on the wall clock. The previous implementation
        # called ``record_stage_event`` in the ``finally`` block,
        # which emitted BOTH events with the same end-of-stage
        # timestamp; downstream consumers couldn't tell when the
        # stage actually began. The wrapped ``stage()`` also lets
        # the live counters dict mutate during the activity body
        # — its final contents land on the completed event when
        # the with-block exits.
        with set_current_run_context(rc), self._diagnostics.stage(
            ctx=ctx,
            run_id=run_id,
            stage_name=stage_name,
            counters=dict(counters),
            document_id=document_id,
        ) as stage_handle:
            try:
                yield
            finally:
                # Push the activity's accumulated counter snapshot
                # onto the stage record so ``stage.completed``
                # carries the final numbers, not the empty initial
                # set. ``update`` is a no-op when the recorder is
                # unwired (``_no_op=True``).
                try:
                    stage_handle.update(**counters)
                except Exception:  # noqa: BLE001
                    pass

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

 Threading + contextvars: `temporalio.activity.heartbeat` reads
 the current activity from a `ContextVar`. `threading.Thread`
 does NOT propagate contextvars, so a naive daemon-thread call to
 `activity.heartbeat` raises `RuntimeError: Not in activity
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
            # Phase-1 ingestion-diagnostics signals from the
            # RAGAnything/MinerU bridge. Optional — the recorder
            # handles missing keys via dict.get(), and legacy
            # bridges that don't surface them still work.
            "parse_elapsed_ms",
            "parse_method",
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
