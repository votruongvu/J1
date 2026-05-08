"""Shared wiring used by both the API and worker dev entrypoints.

Intentionally minimal. This is a *deployment* — not part of the
framework — so it lives under `deploy/dev/` and never inside `src/j1/`.
The framework remains a library; this file demonstrates one concrete
way to wire it.
"""

import os
from collections.abc import Mapping

from j1.intake.service import (
    DEFAULT_ALLOWED_UPLOAD_EXTENSIONS,
    DEFAULT_MAX_UPLOAD_BYTES,
)
from j1 import (
    AccountingActivities,
    AnswerService,
    ApiKeyAuthenticator,
    ApplicationFacade,
    BulkExportService,
    BulkImportService,
    CitationLookupService,
    ConsistencyProvider,
    CostAggregator,
    CostSummaryService,
    DefaultAuditRecorder,
    DefaultCostRecorder,
    DocumentIngestionService,
    DocumentIntakeService,
    EvidenceProvider,
    EventPublisherService,
    FeedbackService,
    GraphQueryProvider,
    HybridQueryEngine,
    JsonArtifactRegistry,
    JsonReviewQueue,
    JsonSourceRegistry,
    JsonlAuditSink,
    JsonlCostSink,
    JsonlFeedbackStore,
    KnowledgeProcessingActivities,
    KnowledgeQueryProvider,
    ProcessingActivities,
    ProcessingService,
    ProfileLoader,
    ProjectActivities,
    ProjectAdminService,
    ProjectLifecycleActivities,
    ProjectProcessingWorkflow,
    DocumentProcessingWorkflow,
    QueryIntentClassifier,
    ReportGenerator,
    RetrievalService,
    ReviewActivities,
    ReviewService,
    SearchActivities,
    SearchService,
    Settings,
    SourceLookupService,
    SqliteSearchIndexer,
    TemporalJobControlService,
    WorkerSpec,
    WorkspaceResolver,
    load_security_settings,
)
from j1.orchestration.activities.profiling import ProfilingActivities
from j1.orchestration.activities.runs import RunsActivities
from j1.processing.cache import JsonlProcessingResultCache
from j1.processing.profiling import DeterministicDocumentProfiler
from j1.runs import (
    AuditProgressReporter,
    IngestionRunStore,
    JsonlIngestionRunStore,
    ProgressReporter,
)

DEFAULT_PROFILE_ID = "default"


def _resolve_max_upload_bytes() -> int:
    """Resolve the per-upload size cap, honouring `J1_MAX_UPLOAD_BYTES`.

    A non-positive override falls back to the framework default so a
    misconfigured value doesn't accidentally disable the boundary.
    """
    raw = os.environ.get("J1_MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


def _resolve_allowed_upload_extensions() -> tuple[str, ...]:
    """Resolve the upload allow-list, honouring
    `J1_ALLOWED_UPLOAD_EXTENSIONS` (comma-separated, with or without
    leading dots). The literal value `*` disables the boundary
    (allow-anything mode); empty / unset uses the framework default.
    """
    raw = os.environ.get("J1_ALLOWED_UPLOAD_EXTENSIONS", "").strip()
    if not raw:
        return DEFAULT_ALLOWED_UPLOAD_EXTENSIONS
    if raw == "*":
        return ()
    parts = [p.strip() for p in raw.split(",")]
    return tuple(p for p in parts if p)


def build_settings() -> Settings:
    """Reads `J1_DATA_ROOT` and friends from the process environment."""
    from j1.config.settings import load_settings
    return load_settings()


def build_workspace(settings: Settings) -> WorkspaceResolver:
    return WorkspaceResolver(settings)


def build_application_facade(workspace: WorkspaceResolver) -> ApplicationFacade:
    """Construct a fully wired `ApplicationFacade`.

    Backed entirely by the framework's filesystem-based registries +
    SQLite FTS5 — the same setup that production deployments use as a
    single-writer baseline. No external services beyond Temporal.
    """
    audit_sink = JsonlAuditSink(workspace)
    audit_recorder = DefaultAuditRecorder(audit_sink)
    cost_sink = JsonlCostSink(workspace)
    cost_recorder = DefaultCostRecorder(cost_sink)

    sources = JsonSourceRegistry(workspace)
    artifacts = JsonArtifactRegistry(workspace)
    reviews = JsonReviewQueue(workspace)
    feedback_store = JsonlFeedbackStore(workspace)

    intake = DocumentIntakeService(
        workspace=workspace, registry=sources, audit_sink=audit_sink,
        max_upload_bytes=_resolve_max_upload_bytes(),
        allowed_extensions=_resolve_allowed_upload_extensions(),
    )
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifacts,
        audit=audit_recorder, cost=cost_recorder,
    )
    indexer = SqliteSearchIndexer(workspace, artifacts, sources)

    profile = None
    try:
        profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    except Exception:
        profile = None

    query_engine = None
    if profile is not None:
        query_engine = HybridQueryEngine(
            classifier=QueryIntentClassifier(),
            knowledge_provider=KnowledgeQueryProvider(indexer),
            graph_provider=GraphQueryProvider(artifacts, workspace),
            evidence_provider=EvidenceProvider(indexer, sources),
            consistency_provider=ConsistencyProvider(artifacts, workspace),
            report_generator=ReportGenerator(indexer, profile),
        )

    review_activities = ReviewActivities(review_queue=reviews, audit=audit_recorder)

    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake),
        retrieval=RetrievalService(artifacts),
        citation_lookup=CitationLookupService(artifacts),
        source_lookup=SourceLookupService(sources),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(indexer),
        answer=AnswerService(query_engine) if query_engine else None,
        project_admin=ProjectAdminService(workspace),
        cost_summary=CostSummaryService(CostAggregator(workspace)),
        review=ReviewService(reviews, review_activities),
    )


def build_review_service(workspace: WorkspaceResolver):
    """Build the `IngestionResultReviewService` for the REST adapter.

    Read-only surface over completed runs (Results > Overview etc.).
    Constructs lightweight JSONL-backed dependencies — the run store
    sits alongside `build_run_progress_surface`'s instance, and the
    artifact registry is the same JSONL the worker writes to. No
    external services. Without this wired, the REST adapter degrades
    `/ingestion-runs/{id}/summary` (and the rest of the review surface
    as it ships) to 503."""
    from j1.ingestion_review import IngestionResultReviewService
    return IngestionResultReviewService(
        run_store=JsonlIngestionRunStore(workspace),
        artifact_registry=JsonArtifactRegistry(workspace),
        workspace=workspace,
    )


def build_validation_service(workspace: WorkspaceResolver):
    """Build the `IngestionValidationService` for the REST adapter.

    Phase 1 surface: synchronous manual test queries scoped to one
    ingestion run. Phase 2 adds generated set / set-execution.
    Reuses the same `HybridQueryEngine` providers the facade builds
    for the public `/answer` endpoint, but invokes them with
    `RunScope` so retrieval is restricted to artifacts produced by
    the run under test.

    Returns `None` when the deployment doesn't have a profile loaded
    — the engine needs a profile for `ReportGenerator`'s template
    lookup, and without it we can't construct the engine. The REST
    adapter degrades `/ingestion-runs/{id}/test-query` to 503 in
    that case, mirroring the `/answer` degradation pattern.

    Phase 2 dependencies (set/run stores + generator) are always
    wired when the validation service is constructible — there's no
    failure mode where the manual-query path works but the set/run
    path doesn't, given a profile is present. The FAST/text LLM is
    optional (the generator falls back to the heuristic question
    producer when no client is supplied).
    """
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.compose import bootstrap_from_env
    from j1.profiles.loader import ProfileLoader
    from j1.query.classifier import QueryIntentClassifier
    from j1.query.engine import HybridQueryEngine
    from j1.query.providers import (
        ConsistencyProvider,
        EvidenceProvider,
        GraphQueryProvider,
        KnowledgeQueryProvider,
        ReportGenerator,
    )
    from j1.validation import (
        DefaultLLMJudge,
        DefaultTestCaseGenerator,
        IngestionValidationService,
        JsonlValidationRunStore,
        JsonlValidationSetStore,
    )

    try:
        profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
    except Exception:  # noqa: BLE001 — profile is optional for validation
        return None

    sources = JsonSourceRegistry(workspace)
    artifacts = JsonArtifactRegistry(workspace)
    indexer = SqliteSearchIndexer(workspace, artifacts, sources)

    query_engine = HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(indexer),
        graph_provider=GraphQueryProvider(artifacts, workspace),
        evidence_provider=EvidenceProvider(indexer, sources),
        consistency_provider=ConsistencyProvider(artifacts, workspace),
        report_generator=ReportGenerator(indexer, profile),
    )

    # Best-effort LLM client for the test-case generator. Prefer
    # FAST role (cheap structured output) and fall back to text;
    # the generator gracefully degrades to its heuristic question
    # producer if no client is wired.
    llm_client = None
    try:
        boot = bootstrap_from_env()
        if hasattr(boot, "llm_registry"):
            try_fast = getattr(boot.llm_registry, "try_fast", None)
            try_text = getattr(boot.llm_registry, "try_text", None)
            if callable(try_fast):
                llm_client = try_fast()
            if llm_client is None and callable(try_text):
                llm_client = try_text()
    except Exception:  # noqa: BLE001 — bootstrap may not be available in tests
        llm_client = None

    # Phase 3 LLM judge — uses the same FAST/text client as the
    # generator. Optional: if no LLM is configured, the runner
    # simply omits the optional semantic checks (`answer_covers_*`,
    # `answer_grounded_*`, `negative_no_fabrication`) and the
    # validation status reflects the deterministic checks only.
    judge = (
        DefaultLLMJudge(text_client=llm_client) if llm_client else None
    )

    return IngestionValidationService(
        run_store=JsonlIngestionRunStore(workspace),
        artifact_registry=artifacts,
        query_engine=query_engine,
        # Audit recorder writes one `j1.validation.manual_query.completed`
        # event per call — same JSONL stream the `/events` endpoint
        # tails, so manual queries show up in the live timeline.
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        # Phase 2 dependencies — always wire them when we get this
        # far so generate / run aren't 503'd separately from
        # manual query.
        workspace=workspace,
        validation_set_store=JsonlValidationSetStore(workspace),
        validation_run_store=JsonlValidationRunStore(workspace),
        test_case_generator=DefaultTestCaseGenerator(text_client=llm_client),
        # Phase 3 — optional. Off when no LLM client is wired.
        judge=judge,
    )


def build_run_progress_surface(
    workspace: WorkspaceResolver,
) -> tuple[IngestionRunStore, ProgressReporter]:
    """Build the dependencies the user-facing `/ingestion-runs/*` surface
    needs. Returns the run store + an audit-backed progress reporter.

    Both are lightweight (JSONL files under the workspace's audit
    area), reused by `api.py` to power:

      * `POST /ingestion-runs`                      — run record + progress events
      * `GET  /ingestion-runs`                      — list view
      * `GET  /ingestion-runs/{id}`                 — status snapshot
      * `GET  /ingestion-runs/{id}/plan`            — execution plan
      * `POST /ingestion-runs/{id}/confirm`         — plan.confirmed event
      * `GET  /ingestion-runs/{id}/events[/stream]` — historical + live events

    Without these wired, the REST adapter degrades each `/ingestion-runs/*`
    handler to 503 with `ingestion-run store not configured`. The dev
    stack always wires them — production deployments should too unless
    they intentionally don't expose the surface."""
    audit_recorder = DefaultAuditRecorder(JsonlAuditSink(workspace))
    return (
        JsonlIngestionRunStore(workspace),
        AuditProgressReporter(audit_recorder),
    )


def maybe_build_authenticator() -> ApiKeyAuthenticator | None:
    """Construct an authenticator only when API keys are configured.

    Honours the framework's "misconfiguration disables a surface" rule:
    no env var → anonymous mode (suitable for local development).
    """
    settings = load_security_settings()
    if not settings.api_keys:
        return None
    return ApiKeyAuthenticator(settings.api_keys)


def build_worker_spec(
    workspace: WorkspaceResolver,
    *,
    compilers: Mapping[str, object] | None = None,
    enrichers: Mapping[str, object] | None = None,
    graph_builders: Mapping[str, object] | None = None,
    indexers: Mapping[str, object] | None = None,
    query_providers: Mapping[str, object] | None = None,
    llm_registry: object | None = None,
    enrichment_settings: object | None = None,
) -> WorkerSpec:
    """Build the `WorkerSpec` registered by the dev worker.

    Processor maps default to empty for everything except enrichers
    — when `enrichers` is None, we auto-register the
    `CompositeEnricher` so the FE's Results > Assets tab can light up
    out of the box. Real deployments override `enrichers=` with a
    deliberately-curated map.

    `llm_registry` (the `LLMProviderRegistry` from `bootstrap_from_env`)
    is consulted for the auto-registered composite — without it,
    `VisualContentDescriber` constructs with `vision_client=None` and
    emits the 'No vision LLM configured' markdown stub on every run.
    Pass through `boot.llm_registry` from worker.py and visual
    enrichment will route through the configured vision LLM.

    Real processor wiring is deployment-specific (vendor SDKs, model
    providers, etc.) and lives elsewhere.
    """
    audit_sink = JsonlAuditSink(workspace)
    audit_recorder = DefaultAuditRecorder(audit_sink)
    cost_sink = JsonlCostSink(workspace)
    cost_recorder = DefaultCostRecorder(cost_sink)

    sources = JsonSourceRegistry(workspace)
    artifacts = JsonArtifactRegistry(workspace)
    reviews = JsonReviewQueue(workspace)

    intake = DocumentIntakeService(
        workspace=workspace, registry=sources, audit_sink=audit_sink,
        max_upload_bytes=_resolve_max_upload_bytes(),
        allowed_extensions=_resolve_allowed_upload_extensions(),
    )
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifacts,
        audit=audit_recorder, cost=cost_recorder,
    )
    indexer = SqliteSearchIndexer(workspace, artifacts, sources)

    # Progress reporter shared by every workflow exit-point
    # activity (`run.completed`, `run.failed`, `run.cancelled`,
    # `step.skipped`). Same audit recorder the API uses, so the
    # frontend's `GET /ingestion-runs/{id}/events[/stream]` sees one
    # combined timeline regardless of whether the event was emitted
    # by the REST handler or by the worker.
    progress_reporter = AuditProgressReporter(audit_recorder)

    activities: list = []
    activities += RunsActivities(
        progress_reporter=progress_reporter,
        # Without `run_store` wired here, the workflow would emit a
        # `run.failed` audit event but the IngestionRun record's
        # `status` field would stay at RUNNING — and the FE's run-
        # detail page reads that field for the primary status panel
        # (the audit log only feeds the timeline). Result: timeline
        # shows the failure, status badge / panel shows "Running"
        # forever. Pass the same store the API uses so terminal
        # events update both surfaces.
        run_store=JsonlIngestionRunStore(workspace),
    ).all_activities()
    # Profiling activity (`j1.ingestion.profile_document`). Required
    # whenever `planner_enabled=True` flows through the workflow —
    # which is the dev default since the user-facing flow needs the
    # planner output for the FE's run-detail page. Without it the
    # workflow fails with `NotFoundError: Activity function
    # j1.ingestion.profile_document … is not registered on this
    # worker`.
    activities += ProfilingActivities(
        sources=sources,
        workspace=workspace,
        profiler=DeterministicDocumentProfiler(),
    ).all_activities()
    activities += ProjectLifecycleActivities(
        workspace=workspace, intake=intake, audit=audit_recorder,
    ).all_activities()
    activities += ProjectActivities(
        workspace=workspace,
        sources=sources,
        audit=audit_recorder,
    ).all_activities()
    activities += AccountingActivities(
        workspace=workspace, audit=audit_recorder,
    ).all_activities()
    activities += SearchActivities(
        audit=audit_recorder,
        indexers={SqliteSearchIndexer.kind: indexer, **(indexers or {})},
    ).all_activities()
    activities += ReviewActivities(
        review_queue=reviews, audit=audit_recorder,
    ).all_activities()
    # Auto-register the composite enricher when the caller didn't
    # pass an enrichers map. Without this the dev stack's Results >
    # Assets tab stays disabled because no enricher is registered to
    # produce `enriched.tables` / `enriched.visuals` / etc. Loading
    # the profile is best-effort — the composite enrichers degrade
    # to stub outputs when their prompts are absent.
    resolved_enrichers: Mapping[str, object]
    if enrichers is None:
        from j1.enrichers import CompositeEnricher
        from j1.workspace.layout import WorkspaceArea
        try:
            profile = ProfileLoader().load(DEFAULT_PROFILE_ID)
        except Exception:
            from j1.profiles.model import Profile
            profile = Profile(profile_id="default", metadata={})
        # Pull every available client from the bootstrap registry so
        # the composite's children have what they need:
        #   * vision  → `VisualContentDescriber` (already used)
        #   * text    → reserved for future LLM-backed enrichers
        #               (TableExtractor / RequirementExtractor / etc).
        #               Today's stub `_produce` methods ignore the
        #               client; wiring it now means real
        #               implementations don't have to re-plumb later.
        #   * embedding → same — reserved for any enricher that
        #                 wants to compute embeddings (e.g.
        #                 ConsistencyChecker comparing chunks).
        # try_*() returns None when the env config is absent; the
        # composite handles that gracefully.
        vision_client = None
        text_client = None
        embedding_client = None
        if llm_registry is not None:
            if hasattr(llm_registry, "try_vision"):
                vision_client = llm_registry.try_vision()
            if hasattr(llm_registry, "try_text"):
                text_client = llm_registry.try_text()
            if hasattr(llm_registry, "try_embedding"):
                embedding_client = llm_registry.try_embedding()

        # `content_source` reads artifact bytes from disk so VCD has
        # actual image bytes to send the vision LLM. Without this,
        # `_StructuredEnricher._read_content` returns b"" and VCD
        # falls through to the "Image bytes not available" stub on
        # every run.
        def _artifact_content_source(
            artifact_ctx, artifact_id: str,
        ) -> bytes:
            try:
                record = artifacts.get(artifact_ctx, artifact_id)
            except Exception:  # noqa: BLE001 — registry miss → empty bytes
                return b""
            location = (record.location or "").strip()
            if "/" not in location:
                return b""
            area_name, _, sub = location.partition("/")
            try:
                area = WorkspaceArea(area_name)
            except ValueError:
                return b""
            path = workspace.area(artifact_ctx, area) / sub
            try:
                return path.read_bytes()
            except OSError:
                return b""

        # `artifact_lookup` returns the kind (e.g. `compile.image`,
        # `chunk`, `enriched.tables`) so VCD can skip non-image
        # artifacts. Without this, the composite invokes VCD on
        # EVERY compile artifact (chunks + metadata too) and pollutes
        # the Visuals card with "Image bytes not available" stubs.
        def _artifact_lookup(
            artifact_ctx, artifact_id: str,
        ) -> str | None:
            try:
                record = artifacts.get(artifact_ctx, artifact_id)
            except Exception:  # noqa: BLE001
                return None
            return record.kind

        # Per-modality kill switches from `EnrichmentSettings`. When
        # the operator sets `J1_ENRICH_IMAGES=false` /
        # `J1_ENRICH_TABLES=false` / `J1_ENRICH_DIAGRAMS=false` /
        # `J1_ENRICH_SCANNED_PAGES=false`, the composite drops the
        # corresponding sub-enricher at construction time. The three
        # visual flags collectively gate `VisualContentDescriber`
        # (see `_filter_generic_enrichers`). None when no settings
        # were passed → legacy "run everything" behaviour.
        images_enabled: bool | None = None
        tables_enabled: bool | None = None
        diagrams_enabled: bool | None = None
        scanned_pages_enabled: bool | None = None
        if enrichment_settings is not None:
            images_enabled = bool(getattr(enrichment_settings, "images", True))
            tables_enabled = bool(getattr(enrichment_settings, "tables", True))
            diagrams_enabled = bool(
                getattr(enrichment_settings, "diagrams", True),
            )
            scanned_pages_enabled = bool(
                getattr(enrichment_settings, "scanned_pages", True),
            )
        composite = CompositeEnricher.from_default(
            profile,
            content_source=_artifact_content_source,
            artifact_lookup=_artifact_lookup,
            vision_client=vision_client,
            text_client=text_client,
            embedding_client=embedding_client,
            images_enabled=images_enabled,
            tables_enabled=tables_enabled,
            diagrams_enabled=diagrams_enabled,
            scanned_pages_enabled=scanned_pages_enabled,
        )
        resolved_enrichers = {composite.kind: composite}
    else:
        resolved_enrichers = enrichers

    activities += ProcessingActivities(
        processing=processing,
        sources=sources,
        artifacts=artifacts,
        compilers=dict(compilers or {}),
        enrichers=dict(resolved_enrichers),
        graph_builders=dict(graph_builders or {}),
        indexers={SqliteSearchIndexer.kind: indexer, **(indexers or {})},
        query_providers=dict(query_providers or {}),
        # Without a reporter wired here, none of the per-step events
        # (`step.started`, `step.progress`, `step.completed`,
        # `step.failed`, `step.skipped`) make it into the audit log,
        # which means the FE's SSE timeline only shows the two
        # `run.created` / `document.received` events emitted by the
        # REST upload handler — nothing fires from the worker side.
        progress_reporter=progress_reporter,
        # Idempotency cache so retries / re-runs of the same logical
        # document skip the (expensive) compile call when the
        # artifact already exists. Same workspace-area JSONL backing
        # as the audit log, so a single backup covers both.
        result_cache=JsonlProcessingResultCache(workspace),
    ).all_activities()
    activities += KnowledgeProcessingActivities(
        workspace=workspace,
        sources=sources,
        artifacts=artifacts,
        audit=audit_recorder,
        cost=cost_recorder,
        compilers=dict(compilers or {}),
        enrichers=dict(enrichers or {}),
        graph_builders=dict(graph_builders or {}),
    ).all_activities()

    return WorkerSpec(
        workflows=[ProjectProcessingWorkflow, DocumentProcessingWorkflow],
        activities=activities,
    )
