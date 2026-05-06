"""Shared wiring used by both the API and worker dev entrypoints.

Intentionally minimal. This is a *deployment* — not part of the
framework — so it lives under `deploy/dev/` and never inside `src/j1/`.
The framework remains a library; this file demonstrates one concrete
way to wire it.
"""

from collections.abc import Mapping

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
) -> WorkerSpec:
    """Build the `WorkerSpec` registered by the dev worker.

    Processor maps default to empty — adequate for confirming the
    stack stands up. Real processor wiring is deployment-specific
    (vendor SDKs, model providers, etc.) and lives elsewhere.
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
    activities += ProcessingActivities(
        processing=processing,
        sources=sources,
        artifacts=artifacts,
        compilers=dict(compilers or {}),
        enrichers=dict(enrichers or {}),
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
