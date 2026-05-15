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
    ApiKeyAuthenticator,
    ApplicationFacade,
    BulkExportService,
    BulkImportService,
    CitationLookupService,
    CostAggregator,
    CostSummaryService,
    DefaultAuditRecorder,
    DefaultCostRecorder,
    DocumentIngestionService,
    DocumentIntakeService,
    EventPublisherService,
    FeedbackService,
    JsonArtifactRegistry,
    JsonReviewQueue,
    JsonSourceRegistry,
    JsonlAuditSink,
    JsonlCostSink,
    JsonlFeedbackStore,
    KnowledgeProcessingActivities,
    ProcessingActivities,
    ProcessingService,
    ProfileLoader,
    ProjectActivities,
    ProjectAdminService,
    ProjectLifecycleActivities,
    ProjectProcessingWorkflow,
    DocumentProcessingWorkflow,
    RetrievalService,
    ReviewActivities,
    ReviewService,
    SearchActivities,
    SearchService,
    Settings,
    SourceLookupService,
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


def build_runtime_config():
    """Phase 1: read the unified RuntimeConfig and run startup
    validation. In PROD profile, missing provider config raises
    ``ConfigError`` before the API/worker reach a ready state. In
    DEV the loader is lenient — missing optional providers fall
    back to the local seam."""
    from j1.config.runtime import load_runtime_config
    cfg = load_runtime_config()
    cfg.validate()
    return cfg


def build_workspace(settings: Settings) -> WorkspaceResolver:
    return WorkspaceResolver(settings)


def build_snapshot_services(workspace: WorkspaceResolver):
    """Phase 3: snapshot-centered services.

    Returns ``(snapshot_service, layout, index_refs, coordinator)``.
    The coordinator is the new ingestion entrypoint — Phase 3
    wires it alongside the legacy run-keyed workflow so the
    cutover is incremental. Returns the snapshot_service alone is
    enough for the activity layer to stamp ``snapshot_id`` onto
    artifacts and for the promote-on-success hook to promote
    ``document.active_snapshot_id``.
    """
    from j1.documents.index_refs import JsonIndexRefStore
    from j1.documents.snapshot_layout import SnapshotLayout
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore

    snapshot_store = JsonlDocumentSnapshotStore(workspace)
    snapshot_service = DocumentSnapshotService(store=snapshot_store)
    layout = SnapshotLayout(data_root=workspace.data_root)
    index_refs = JsonIndexRefStore(workspace)
    return snapshot_service, layout, index_refs


def build_evidence_adapter(workspace: WorkspaceResolver, artifacts, sources):
    """Phase 8: construct the canonical Postgres FTS evidence
    adapter. PostgreSQL FTS is the only supported lexical/evidence
    backend; the legacy SQLite path was deleted.
    """
    from j1.config.runtime import load_runtime_config
    from j1.search.evidence_adapter import select_evidence_adapter
    from j1.search.postgres_fts import (
        PostgresFtsAdapter,
        make_psycopg_connection_factory,
    )

    cfg = load_runtime_config()
    dsn = cfg.evidence.effective_dsn(cfg.metadata)
    if not dsn:
        raise RuntimeError(
            "Phase 8: PostgreSQL FTS requires a DSN. Set "
            "J1_EVIDENCE_DSN or J1_METADATA_DSN. The SQLite FTS5 "
            "fallback was deleted — there is no compatibility path."
        )
    factory = make_psycopg_connection_factory(dsn)
    if factory is None:
        raise RuntimeError(
            "psycopg is not installed. Install j1[postgres] or pip "
            "install 'psycopg[binary]>=3.1'."
        )
    backend = PostgresFtsAdapter(factory, schema=cfg.metadata.schema)
    resolver = _build_chunk_resolver(workspace, artifacts)
    return select_evidence_adapter(
        "postgres_fts",
        postgres_backend=backend,
        postgres_chunk_resolver=resolver,
        postgres_schema=cfg.metadata.schema,
    )


def _build_chunk_resolver(workspace, artifacts):
    """Walk the artifact registry for chunk-kind records and yield
    one ``{chunk_id, content, metadata}`` dict per chunk row. The
    Postgres FTS adapter consumes this when materialising evidence
    rows for an artifact_id.

    Phase 5: bounded LRU cache. The cap is operator-configurable via
    ``J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS`` (default 1024 —
    a few MB at typical chunk sizes). The cache is per-worker; the
    activity layer doesn't clear it between invocations because
    the LRU keeps memory bounded already.

    The resolver reads only from the workspace-relative
    ``record.location`` produced by the snapshot-aware artifact
    registration helper — it never touches the legacy
    ``{workdir}/runs/{run_id}/...`` path. If an artifact's location
    doesn't resolve cleanly, the chunk is skipped without raising.
    """
    import os
    from collections import OrderedDict

    try:
        cap = int(
            os.environ.get(
                "J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS", "1024",
            )
        )
    except ValueError:
        cap = 1024
    cap = max(0, cap)  # 0 disables caching cleanly.

    cache: "OrderedDict[str, dict | None]" = OrderedDict()

    def _cache_get(key: str):
        if key not in cache:
            return None, False
        value = cache.pop(key)
        cache[key] = value  # mark as most recently used
        return value, True

    def _cache_put(key: str, value):
        if cap == 0:
            return
        cache[key] = value
        while len(cache) > cap:
            cache.popitem(last=False)  # evict oldest

    def _resolver(ctx, artifact_id):
        cached, hit = _cache_get(artifact_id)
        if hit:
            if cached is not None:
                yield cached
            return
        try:
            record = artifacts.get(ctx, artifact_id)
        except Exception:
            _cache_put(artifact_id, None)
            return
        if record.kind != "chunk":
            _cache_put(artifact_id, None)
            return
        from pathlib import PurePosixPath
        from j1.workspace.layout import WorkspaceArea
        parts = PurePosixPath(record.location).parts
        if len(parts) < 2:
            _cache_put(artifact_id, None)
            return
        area_name, *rest = parts
        try:
            area = WorkspaceArea(area_name)
        except ValueError:
            _cache_put(artifact_id, None)
            return
        path = workspace.area(ctx, area).joinpath(*rest)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            _cache_put(artifact_id, None)
            return
        entry = {
            "chunk_id": record.metadata.get("chunk_id") or record.artifact_id,
            "content": content,
            "metadata": dict(record.metadata or {}),
        }
        _cache_put(artifact_id, entry)
        yield entry

    return _resolver


def build_application_facade(workspace: WorkspaceResolver) -> ApplicationFacade:
    """Construct a fully wired `ApplicationFacade`.

 Phase 3 retry: the canonical evidence adapter is built via
 ``build_evidence_adapter`` (Postgres FTS by default; SQLite only
 when explicitly selected). The legacy ``SqliteSearchIndexer`` is
 still constructed for the REST ``/search`` endpoint's ``SearchService``
 because that endpoint is a debug/operator surface — the canonical
 query path goes through ``SmartQueryOrchestrator``. The legacy
 indexer DOES NOT receive new writes by default: ingestion routes
 evidence writes through the canonical adapter inside
 ``SearchActivities``.
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
    # SmartQueryOrchestrator — required by ProcessingService.query
    # once the orchestrator path is wired. Built once here and
    # threaded into every query consumer. Falls back to ``None``
    # when the LLM registry isn't available, in which case
    # ProcessingService.query raises with a clear message.
    smart_query_orchestrator = _build_orchestrator_or_none(workspace)
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifacts,
        audit=audit_recorder, cost=cost_recorder,
        smart_query_orchestrator=smart_query_orchestrator,
    )
    # Phase 4: the canonical evidence adapter IS the search surface.
    # ``SearchService`` resolves the active-snapshot allowlist via the
    # source registry and dispatches to the adapter. REST /search
    # endpoints inherit this — they no longer reach SQLite by
    # default. Operators opting into SQLite (debug / comparison)
    # set ``J1_EVIDENCE_BACKEND=sqlite_fts5``; the adapter still
    # speaks the new interface so SearchService doesn't care which
    # backend is behind it.
    evidence_adapter = build_evidence_adapter(workspace, artifacts, sources)

    review_activities = ReviewActivities(review_queue=reviews, audit=audit_recorder)

    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake),
        retrieval=RetrievalService(artifacts),
        citation_lookup=CitationLookupService(artifacts),
        source_lookup=SourceLookupService(sources),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
        search=SearchService(evidence_adapter, sources),
        project_admin=ProjectAdminService(workspace),
        cost_summary=CostSummaryService(CostAggregator(workspace)),
        review=ReviewService(reviews, review_activities),
    )


def build_document_lifecycle_service(workspace: WorkspaceResolver):
    """Build the ``DocumentLifecycleService`` for the REST adapter.

    Owns the document-centric attach / detach / remove flow.
    Without this wired, the three ``POST /documents/{id}/<action>``
    endpoints return 503 — which is what made operators see
    "Remove failed" toasts immediately after clicking the
    button in the Document UI.

    All dependencies are the same JSONL-backed singletons the
    rest of the dev stack uses: the source registry for the
    ``knowledge_state`` field on the document record, the
    artifact registry to flip ``metadata.knowledge_state`` on
    every artifact tied to the document, and the audit recorder
    so each transition lands in the audit log.
    """
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.documents.service import DocumentLifecycleService
    return DocumentLifecycleService(
        registry=JsonSourceRegistry(workspace),
        artifact_registry=JsonArtifactRegistry(workspace),
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        cleanup=build_document_cleanup_service(workspace),
    )


def build_diagnostic_recorder(
    *,
    workspace: WorkspaceResolver,
    audit=None,
):
    """Build the per-worker ``DiagnosticRecorder`` for Phase-1
    ingestion diagnostics.

    Same JSONL-backed audit recorder + artifact registry the other
    services already use — the diagnostic report lands as a
    ``compiled.ingestion_diagnostic_report`` artifact in the
    project's compiled area, next to the strategy report.

    Audit recorder is optional: when omitted, the recorder still
    builds an in-memory aggregate and writes the final artifact,
    but the per-step ``j1.ingestion.*`` audit events are skipped
    (operator can't tail them in real time)."""
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.processing.diagnostics import DiagnosticRecorder
    if audit is None:
        audit = DefaultAuditRecorder(JsonlAuditSink(workspace))
    return DiagnosticRecorder(
        audit=audit,
        artifact_registry=JsonArtifactRegistry(workspace),
        workspace=workspace,
    )


def build_smart_query_orchestrator(
    *,
    workspace: WorkspaceResolver,
    llm_registry=None,
    raganything_provider=None,
):
    """Build the ``SmartQueryOrchestrator`` with production routes.

    Returns ``None`` when the LLM registry isn't wired — the
    orchestrator needs a synthesis LLM, and without one the
    legacy fallback path is the right behaviour (no silent
    half-built orchestrator that synthesises nothing).

    Wires the three production routes:

      * **RAGAnything** (primary) — semantic + graph retrieval
        through the existing ``RAGAnythingQueryProvider``. Falls
        through to a no-op stub when the provider isn't wired
        (deployments without RAGAnything still get BM25-only
        retrieval).
      * **BM25** (auxiliary lexical recall) — ``SqliteSearchIndexer``
        with the eligibility-gate resolver from ``j1.query.eligibility``
        so cross-run leaks are blocked at the SQL layer.
      * **ArtifactLookup** (direct enriched-artifact reads) —
        ``JsonArtifactRegistry`` + a workspace-path body loader.

    One-line wire-in at the deployment boundary:

    .. code-block:: python

        orch = build_smart_query_orchestrator(
            workspace=workspace, llm_registry=boot.llm_registry,
            raganything_provider=rag_provider,
        )
        validation_service = IngestionValidationService(
            ..., smart_query_orchestrator=orch,
        )
        processing_service = ProcessingService(
            ..., smart_query_orchestrator=orch,
        )
        create_rest_api(facade, ..., smart_query_orchestrator=orch)
    """
    if llm_registry is None:
        # The orchestrator without an LLM is misleading — it would
        # always return ``answer_nonempty=False``. Better to return
        # ``None`` and let callers fall back to legacy.
        import logging
        logging.getLogger("j1.query.wiring").warning(
            "SmartQueryOrchestrator NOT built: llm_registry not "
            "wired. Manual-query and validation paths will fall "
            "back to the legacy HybridQueryEngine.",
        )
        return None

    from j1.artifacts.registry import JsonArtifactRegistry
    from j1.query.eligibility import resolve_eligible_active_run_ids
    from j1.query.orchestrator import SmartQueryOrchestrator
    from j1.query.query_plan import RetrievalRouteKind
    from j1.query.retrieval_routes import (
        ArtifactLookupAdapter,
        LexicalEvidenceAdapter,
        RAGAnythingAdapter,
    )

    artifacts = JsonArtifactRegistry(workspace)
    sources = JsonSourceRegistry(workspace)
    # ``run_store`` + ``snapshot_store`` give the eligibility
    # resolver a way to handle ``RunScope`` directly — required for
    # the Run Detail "validate the produced snapshot" flow, which
    # must work even when the candidate snapshot isn't promoted to
    # active yet (so the active-snapshot resolver would otherwise
    # return an empty set).
    from j1.runs.store import JsonlIngestionRunStore as _JsonRunStore
    from j1.documents.snapshot_store import (
        JsonlDocumentSnapshotStore as _JsonSnapStore,
    )
    runs_store = _JsonRunStore(workspace)
    snap_store = _JsonSnapStore(workspace)

    def _resolve_eligible_snapshots(ctx, scope):
        """The lexical adapter wants ``frozenset[str] | None``;
        ``resolve_eligible_active_run_ids`` returns ``EligibilityResult``.
        Phase 6: snapshot-id allowlist is the canonical visibility key."""
        try:
            result = resolve_eligible_active_run_ids(
                ctx=ctx, scope=scope, registry=sources,
                run_store=runs_store, snapshot_store=snap_store,
            )
            return result.snapshot_ids
        except Exception:  # noqa: BLE001 — gate failure → unfiltered
            return None

    def _resolve_eligible_snapshot_pairs(ctx, scope):
        """RAGAnything adapter needs ``(document_id, snapshot_id)``
        tuples so the bridge can compute per-snapshot workspace paths
        (``{workdir}/snapshots/{t}/{p}/{document_id}/{snapshot_id}``).
        Mirrors ``_resolve_eligible_snapshots`` but returns the
        pairs side of the eligibility result."""
        try:
            result = resolve_eligible_active_run_ids(
                ctx=ctx, scope=scope, registry=sources,
                run_store=runs_store, snapshot_store=snap_store,
            )
            return result.snapshot_pairs
        except Exception:  # noqa: BLE001 — gate failure → no fan-out
            return None

    routes: dict = {}
    if raganything_provider is not None:
        routes[RetrievalRouteKind.RAGANYTHING] = RAGAnythingAdapter(
            raganything_provider,
            eligible_snapshot_pairs_resolver=_resolve_eligible_snapshot_pairs,
        )

    # Phase 6: build the lexical-recall route on top of the canonical
    # evidence adapter (Postgres FTS by default). Ranking is
    # ``ts_rank_cd``, NOT true BM25 — the kind enum keeps the
    # ``BM25`` name for trace back-compat (see LexicalEvidenceAdapter
    # docstring).
    lexical_evidence_adapter = build_evidence_adapter(
        workspace, artifacts, sources,
    )
    routes[RetrievalRouteKind.BM25] = LexicalEvidenceAdapter(
        lexical_evidence_adapter,
        eligible_snapshot_ids_resolver=_resolve_eligible_snapshots,
    )

    def _read_artifact_bytes(record) -> str:
        try:
            path = workspace.project_root(record.project) / record.location
            return path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 — missing body is non-fatal
            return ""
    routes[RetrievalRouteKind.ARTIFACT_LOOKUP] = ArtifactLookupAdapter(
        artifacts, body_loader=_read_artifact_bytes,
    )

    def _llm_callable(req):
        """Adapt the registry's text client into the orchestrator's
        ``LLMCallable`` shape.

        The text client (``OpenAICompatTextLLMClient`` and
        compatible adapters) exposes
        ``generate(prompt, *, system_prompt=...)`` and returns
        ``(text, usage)``. The orchestrator wants a single answer
        string. Failures are caught + folded into the answer
        string so the AnswerQualityGate sees them as a refusal
        rather than crashing the orchestrator.
        """
        try_text = getattr(llm_registry, "try_text", None)
        if try_text is None:
            return ""
        client = try_text()
        if client is None:
            return ""
        try:
            text, _usage = client.generate(
                req.user_prompt,
                system_prompt=req.system_prompt,
            )
            return text or ""
        except Exception as exc:  # noqa: BLE001 — surface to gate
            return f"[llm error: {type(exc).__name__}: {exc}]"

    return SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm_callable,
    )


def build_document_cleanup_service(workspace: WorkspaceResolver):
    """Build the ``DocumentCleanupService`` for Remove + CAS-orphan
    cleanup.

    Wires the same JSONL-backed singletons + the SQLite search
    indexer + the persistent run store. Pulls the per-run
    LightRAG / MinerU workdir from ``J1_RAGANYTHING_WORKDIR``
    (matches the dev compose default) so the reset script and the
    Remove flow operate on the same filesystem tree."""
    import os
    from j1.artifacts.registry import JsonArtifactRegistry
    from j1.documents.cleanup import DocumentCleanupService
    from j1.runs.store import JsonlIngestionRunStore
    artifacts = JsonArtifactRegistry(workspace)
    sources = JsonSourceRegistry(workspace)
    # Phase 7: the cleanup service no longer constructs
    # ``SqliteSearchIndexer``. The canonical evidence index is
    # Postgres FTS; cleanup goes through the snapshot-aware
    # ``cleanup_snapshot`` path which routes to
    # ``EvidenceIndexAdapter.delete_for_snapshot``. Legacy
    # ``cleanup_run`` still works as a no-op on the FTS rows when
    # no indexer is wired — it relies on artifact + workspace
    # deletion, which is sufficient for the run-orphan-cleanup
    # case.
    run_store = JsonlIngestionRunStore(workspace)
    raganything_workdir = (
        os.environ.get("J1_RAGANYTHING_WORKDIR")
        or "/var/lib/j1/raganything"
    )
    return DocumentCleanupService(
        workspace=workspace,
        artifacts=artifacts,
        indexer=None,
        run_store=run_store,
        source_registry=sources,
        raganything_workdir=raganything_workdir,
    )


def build_review_service(workspace: WorkspaceResolver):
    """Build the `IngestionResultReviewService` for the REST adapter.

 Read-only surface over completed runs (Results > Overview etc.).
 Constructs lightweight JSONL-backed dependencies — the run store
 sits alongside `build_run_progress_surface`'s instance, and the
 artifact registry is the same JSONL the worker writes to. No
 external services. Without this wired, the REST adapter degrades
 `/ingestion-runs/{id}/summary` (and the rest of the review surface
 as it ships) to 503.

 Reads `J1_PLANNING_*` env vars at construction so the Planning
 Report projector knows the privacy caps; misconfigured values
 surface as `ConfigError` at startup rather than at request time."""
    from j1.ingestion_review import IngestionResultReviewService
    from j1.processing.planning_settings import load_planning_settings
    return IngestionResultReviewService(
        run_store=JsonlIngestionRunStore(workspace),
        artifact_registry=JsonArtifactRegistry(workspace),
        workspace=workspace,
        planning_settings=load_planning_settings(),
        # Phase 5 hardening: resume_from_checkpoint consults the
        # document's knowledge_state at the service layer so
        # detached/removed documents can't be resumed from any
        # caller path (REST + future CLI / scripted callers).
        source_registry=JsonSourceRegistry(workspace),
    )


def build_validation_service(workspace: WorkspaceResolver):
    """Build the `IngestionValidationService` for the REST adapter.

    Two surfaces post the 2026-05-14 refactor:

    * Manual Test Query — synchronous one-off questions through the
      SmartQueryOrchestrator (the detailed inspection tool).
    * Imported Test Cases — CSV upload + execute-against-active-run
      (the auxiliary Validation Tab summary).

    Returns ``None`` when the deployment doesn't have a profile
    loaded — without one, the REST adapter degrades the validation
    endpoints to 503, mirroring the ``/answer`` degradation pattern.
    """
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.profiles.loader import ProfileLoader
    from j1.validation import (
        ImportedTestCaseExecutor,
        IngestionValidationService,
        JsonlImportedTestCaseStore,
    )

    try:
        ProfileLoader().load(DEFAULT_PROFILE_ID)
    except Exception:  # noqa: BLE001 — profile is optional for validation
        return None

    artifacts = JsonArtifactRegistry(workspace)

    # Native query provider (LightRAG's hybrid ``aquery``). Built
    # best-effort: when RAGAnything settings can't be loaded the
    # native-debug endpoint reports the provider as unwired and
    # surfaces an honest "no answer attempted" response.
    native_query_provider = _build_native_query_provider_or_none()

    native_query_timeout_seconds = _env_float(
        "J1_RAG_NATIVE_QUERY_TIMEOUT_SECONDS", default=30.0,
    )
    validation_candidate_top_k = _env_int(
        "J1_VALIDATION_CANDIDATE_TOP_K", default=20,
    )

    orchestrator = _build_orchestrator_or_none(workspace)
    run_store = JsonlIngestionRunStore(workspace)
    imported_store = JsonlImportedTestCaseStore(workspace)
    imported_executor = (
        ImportedTestCaseExecutor(
            smart_query_orchestrator=orchestrator,
            run_store=run_store,
        ) if orchestrator is not None else None
    )

    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore

    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        workspace=workspace,
        # Enables ``validation_scope="active"`` on the manual-query
        # endpoint by resolving ``ActiveScope(document_id)`` against
        # the source registry → snapshot store.
        source_registry=JsonSourceRegistry(workspace),
        snapshot_store=JsonlDocumentSnapshotStore(workspace),
        smart_query_orchestrator=orchestrator,
        native_query_provider=native_query_provider,
        native_query_timeout_seconds=native_query_timeout_seconds,
        validation_candidate_top_k=validation_candidate_top_k,
        imported_test_case_store=imported_store,
        imported_test_case_executor=imported_executor,
    )


def _build_orchestrator_or_none(workspace: WorkspaceResolver):
    """Build a SmartQueryOrchestrator from the current environment.

    Returns ``None`` when the LLM registry isn't wired (orchestrator
    without an LLM would always answer with ``answer_nonempty=False``
    and that's worse than no orchestrator at all). Returns a fully-
    routed orchestrator otherwise — RAGAnything (when available),
    BM25, and ArtifactLookup adapters all wired with the same
    project workspace as the rest of the stack.

    Single helper used by both REST facade construction and worker
    spec construction so a deployment doesn't get half-wired (REST
    using the orchestrator, Temporal still on the legacy path) or
    vice versa.
    """
    import logging
    _wlog = logging.getLogger("j1.deploy.wiring")
    try:
        from j1.compose import bootstrap_from_env
    except Exception as exc:  # noqa: BLE001
        _wlog.warning(
            "SmartQueryOrchestrator unavailable: bootstrap import "
            "failed: %s", exc, exc_info=True,
        )
        return None
    try:
        boot = bootstrap_from_env()
    except Exception as exc:  # noqa: BLE001
        _wlog.warning(
            "SmartQueryOrchestrator unavailable: bootstrap failed: %s",
            exc, exc_info=True,
        )
        return None
    llm_registry = getattr(boot, "llm_registry", None)
    if llm_registry is None:
        _wlog.warning(
            "SmartQueryOrchestrator unavailable: bootstrap returned "
            "no llm_registry",
        )
        return None
    rag_provider = _build_native_query_provider_or_none()
    return build_smart_query_orchestrator(
        workspace=workspace,
        llm_registry=llm_registry,
        raganything_provider=rag_provider,
    )


def _build_native_query_provider_or_none():
    """Construct ``RAGAnythingQueryProvider`` if RAGAnything is
    installed AND an LLM registry can be loaded. Returns ``None``
    on any failure — the validation service handles None
    gracefully (mode degrades to ``bm25_quality_debug``).

    Every failure path logs a WARN so the operator can grep
    server logs to see WHY native isn't available. Previously
    these were swallowed silently, which made "I changed the
    default to lightrag_native and now every query is empty"
    impossible to diagnose without code-spelunking.
    """
    import logging
    _wlog = logging.getLogger("j1.deploy.wiring")

    try:
        from j1.compose import bootstrap_from_env
        from j1.providers.raganything.retrieval import (
            RAGAnythingQueryProvider,
        )
        from j1.providers.raganything.settings import (
            load_raganything_settings,
        )
    except Exception as exc:  # noqa: BLE001 — vendor / config not installable
        _wlog.warning(
            "native query provider unavailable: import failed: %s",
            exc, exc_info=True,
        )
        return None

    try:
        boot = bootstrap_from_env()
    except Exception as exc:  # noqa: BLE001 — bootstrap may not be available
        _wlog.warning(
            "native query provider unavailable: bootstrap failed: %s",
            exc, exc_info=True,
        )
        return None
    llm_registry = getattr(boot, "llm_registry", None)
    if llm_registry is None:
        _wlog.warning(
            "native query provider unavailable: bootstrap returned "
            "no llm_registry",
        )
        return None

    try:
        rag_settings = load_raganything_settings()
    except Exception as exc:  # noqa: BLE001
        _wlog.warning(
            "native query provider unavailable: settings load failed: %s",
            exc, exc_info=True,
        )
        return None

    try:
        return RAGAnythingQueryProvider.from_default(
            llm_registry=llm_registry,
            settings=rag_settings,
        )
    except Exception as exc:  # noqa: BLE001 — degrade silently
        _wlog.warning(
            "native query provider unavailable: from_default failed: %s",
            exc, exc_info=True,
        )
        return None


def _env_int(name: str, *, default: int) -> int:
    """Read an int env var with a default. Invalid values log a
    warning (via the underlying parse) but fall back to the
    default rather than crashing the boot."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _domain_registry_or_none():
    """Return the default domain registry, or None on import failure.
 Generator runs in generic mode when this returns None — the set
 still generates, just without the domain testing-lens overlay."""
    try:
        from j1.domains import default_registry
        return default_registry()
    except Exception:  # noqa: BLE001 — degrade quietly to generic mode
        return None


def build_run_progress_surface(
    workspace: WorkspaceResolver,
) -> tuple[IngestionRunStore, ProgressReporter]:
    """Build the dependencies the user-facing `/ingestion-runs/*` surface
 needs. Returns the run store + an audit-backed progress reporter.

 Both are lightweight (JSONL files under the workspace's audit
 area), reused by `api.py` to power:

 * `POST /ingestion-runs` — run record + progress events
 * `GET /ingestion-runs` — list view
 * `GET /ingestion-runs/{id}` — status snapshot
 * `GET /ingestion-runs/{id}/plan` — execution plan
 * `POST /ingestion-runs/{id}/confirm` — plan.confirmed event
 * `GET /ingestion-runs/{id}/events[/stream]` — historical + live events

 Without these wired, the REST adapter degrades each `/ingestion-runs/*`
 handler to 503 with `ingestion-run store not configured`. The dev
 stack always wires them — production deployments should too unless
 they intentionally don't expose the surface."""
    audit_recorder = DefaultAuditRecorder(JsonlAuditSink(workspace))
    return (
        JsonlIngestionRunStore(workspace),
        AuditProgressReporter(audit_recorder),
    )


def build_batch_run_store(workspace: WorkspaceResolver):
    """Build the JSONL-backed batch-run store. Used by
 `POST /ingestion-batches` to track multi-upload aggregations."""
    from j1.runs.batch_store import JsonlBatchRunStore
    return JsonlBatchRunStore(workspace)


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
    # shared LLM-call limiter the bootstrap built.
    # When None, the new EnrichmentModule adapters still construct;
    # they just don't gate concurrent LLM calls. (CompositeEnricher
    # behaves the same way.)
    llm_call_limiter: object | None = None,
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

    # Phase 3 retry: the snapshot service is the canonical lineage
    # surface. Every artifact stamped by ``_materialize_draft`` and
    # every terminal-success promotion routes through here. Wiring
    # it at worker boot is what flips the default runtime from the
    # legacy run-keyed path to the snapshot-centered one.
    snapshot_service, snapshot_layout, index_refs = build_snapshot_services(
        workspace,
    )
    # Phase 3 retry: pick the canonical evidence adapter. Default is
    # Postgres FTS; SQLite is selected only when the operator
    # explicitly sets ``J1_EVIDENCE_BACKEND=sqlite_fts5`` (or when
    # ``postgres_fts`` is configured but the DSN is missing in DEV —
    # ``build_evidence_adapter`` logs a structured warning before
    # falling back so the absence is loud).
    evidence_adapter = build_evidence_adapter(workspace, artifacts, sources)

    intake = DocumentIntakeService(
        workspace=workspace, registry=sources, audit_sink=audit_sink,
        max_upload_bytes=_resolve_max_upload_bytes(),
        allowed_extensions=_resolve_allowed_upload_extensions(),
    )
    # Worker-side orchestrator. Temporal's ACTIVITY_QUERY delegates
    # through ProcessingService.query, which requires the
    # orchestrator. Built once per worker so route adapters
    # (RAGAnything / BM25 / artifact) are shared across activity
    # invocations in the same process.
    smart_query_orchestrator = _build_orchestrator_or_none(workspace)
    processing = ProcessingService(
        workspace=workspace, artifact_registry=artifacts,
        audit=audit_recorder, cost=cost_recorder,
        smart_query_orchestrator=smart_query_orchestrator,
    )
    # Postgres FTS is the only supported evidence backend.
    # ``indexer`` stays ``None``; the canonical write target is
    # ``evidence_adapter`` (PostgresFtsEvidenceAdapter). Deployments
    # that want a different indexer must inject one outside this
    # wiring helper.
    indexer = None

    # Progress reporter shared by every workflow exit-point
    # activity (`run.completed`, `run.failed`, `run.cancelled`,
    # `step.skipped`). Same audit recorder the API uses, so the
    # frontend's `GET /ingestion-runs/{id}/events[/stream]` sees one
    # combined timeline regardless of whether the event was emitted
    # by the REST handler or by the worker.
    progress_reporter = AuditProgressReporter(audit_recorder)

    activities: list = []
    # Phase-1 ingestion diagnostics recorder. One instance per
    # worker process; passed to both ``RunsActivities`` (for the
    # terminal report-write hook) and ``ProcessingActivities`` (for
    # stage timing + LLM-call attribution). Optional collaborator
    # in every consumer — wiring it here turns diagnostics ON
    # globally for this worker.
    diagnostic_recorder = build_diagnostic_recorder(
        workspace=workspace, audit=audit_recorder,
    )
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
        # Document-centric promotion hook (Phase 4): when a run
        # reaches a usable terminal state (succeeded / succeeded-
        # with-warnings), the activity points the parent document's
        # `active_run_id` at the new run. Failed/cancelled runs
        # don't promote — which is exactly what makes "failed
        # reindex doesn't clobber the previous good run" hold.
        source_registry=JsonSourceRegistry(workspace),
        # Lineage hardening: after the promotion flips active_run_id,
        # the supersede sweep stamps the PREVIOUS active run's
        # artifacts with `search_state=superseded` so retrieval
        # stops surfacing them. Closes the "mixed-run retrieval
        # results after reindex" gap the latest validation reports
        # exposed.
        artifact_registry=JsonArtifactRegistry(workspace),
        # Cleanup service for CAS-orphan candidates: when a
        # candidate reindex / refresh-enrich run finishes
        # successfully but loses the CAS-promotion race (or the
        # parent document was removed mid-run), the orphan run's
        # artifacts / FTS rows / workspace dirs get dropped here
        # rather than left as queryable phantom evidence.
        cleanup_service=build_document_cleanup_service(workspace),
        diagnostic_recorder=diagnostic_recorder,
        # Phase 3 retry: wire the snapshot service into the run-
        # promotion hook so a successful terminal status ALSO
        # promotes ``document.active_snapshot_id`` (in addition to
        # the existing ``active_run_id`` denormalisation). Without
        # this, the snapshot side stays at BUILDING / READY without
        # ever becoming the canonical visibility key.
        snapshot_service=snapshot_service,
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
        indexers=dict(indexers or {}),
        # Phase 3 retry: the canonical write target. The legacy
        # ``indexer`` dispatch above still runs (for back-compat
        # readers); the evidence adapter writes the snapshot-scoped
        # row that the new query layer reads from.
        evidence_adapter=evidence_adapter,
        snapshot_service=snapshot_service,
        artifact_registry=artifacts,
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
        #  * vision → `VisualContentDescriber` (already used)
        #  * text → reserved for future LLM-backed enrichers
        #  (TableExtractor / RequirementExtractor / etc).
        #  Today's stub `_produce` methods ignore the
        #  client; wiring it now means real
        #  implementations don't have to re-plumb later.
        #  * embedding → same — reserved for any enricher that
        #  wants to compute embeddings (e.g.
        #  ConsistencyChecker comparing chunks).
        # try_* returns None when the env config is absent; the
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
        # Closure for VCD's per-image triage. Returns the full
        # `ArtifactRecord` so VCD can read `metadata["vision_decision"]`
        # and short-circuit decorative images.
        def _artifact_record_lookup(
            artifact_ctx, artifact_id: str,
        ):
            try:
                return artifacts.get(artifact_ctx, artifact_id)
            except Exception:  # noqa: BLE001 — registry miss is treated as "no triage data"
                return None

        composite = CompositeEnricher.from_default(
            profile,
            content_source=_artifact_content_source,
            artifact_lookup=_artifact_lookup,
            artifact_record_lookup=_artifact_record_lookup,
            vision_client=vision_client,
            text_client=text_client,
            embedding_client=embedding_client,
            images_enabled=images_enabled,
            tables_enabled=tables_enabled,
            diagrams_enabled=diagrams_enabled,
            scanned_pages_enabled=scanned_pages_enabled,
            # Without the limiter every LLM call inside the composite
            # bypasses ``_emit_diag_llm_call`` → diagnostic report
            # shows ``llm_call_count=0`` even though real LLM traffic
            # is happening. Wiring it here lets the limiter's per-call
            # hook fire ``j1.ingestion.llm_call.completed`` so the
            # report and the audit stream both reflect actual usage.
            llm_call_limiter=llm_call_limiter,
        )
        resolved_enrichers = {composite.kind: composite}
    else:
        resolved_enrichers = enrichers

    #  + 11A — bootstrap supplies the RAW analysis clients;
    # the enrichment activity constructs the `PerImageVisionAdapter`
    # per run so it can resolve actual image bytes from the run's
    # `compile.image` artifacts via `WorkspaceImageBytesProvider`.
    # Text client structurally matches `TextAnalysisClient` already
    # — the adapter is a thin pass-through, kept here to make the
    # dependency arrow visible at the wiring boundary.
    #
    # Production / staging deployments OUTSIDE `deploy/dev/` must
    # follow the same wiring pattern when they're built:
    #  1. `bootstrap_from_env` → `BootstrapResult` carries the
    #  `llm_registry` + `llm_call_limiter`.
    #  2. Resolve `text_client = registry.try_text` and
    #  `vision_client = registry.try_vision` — either may be
    #  None for deployments without LLM credentials.
    #  3. Optionally wrap text client in `TextLLMClientAdapter` for
    #  explicit Protocol surface (production client matches
    #  structurally either way).
    #  4. Pass the RAW vision client through (do NOT wrap at
    #  bootstrap) — the activity wraps per-run.
    #  5. Pass `llm_call_limiter=boot.llm_call_limiter` so the
    #  same limiter reaches every adapter AND per-image vision
    #  calls (the limiter acquires per-image).
    #  6. Construct `ProcessingActivities(...,
    #  enrichment_text_client=..., enrichment_vision_client=...,
    #  enrichment_llm_call_limiter=...)`.
    from j1.processing.enrichment_clients import TextLLMClientAdapter

    enrichment_text_client: object | None = None
    enrichment_vision_client: object | None = None
    if text_client is not None:
        enrichment_text_client = TextLLMClientAdapter(text_client)
    if vision_client is not None:
        # Pass the raw vision client through — the activity wraps
        # it in `PerImageVisionAdapter(raw_vision_client,
        # image_provider=WorkspaceImageBytesProvider(...))` at
        # `run_enrichment_stage` time so the provider can see the
        # current run's compile-image artifacts.
        enrichment_vision_client = vision_client

    activities += ProcessingActivities(
        processing=processing,
        sources=sources,
        artifacts=artifacts,
        compilers=dict(compilers or {}),
        enrichers=dict(resolved_enrichers),
        graph_builders=dict(graph_builders or {}),
        indexers=dict(indexers or {}),
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
        # typed analysis clients + shared limiter for
        # the new EnrichmentModule adapters (text / classification
        # / table / image). When any client is None, the matching
        # adapter SKIPs the module with "no LLM client configured"
        # so the absence is loud in the final ingestion report.
        enrichment_text_client=enrichment_text_client,
        enrichment_vision_client=enrichment_vision_client,
        enrichment_llm_call_limiter=llm_call_limiter,
        # Phase-1 ingestion diagnostics. Same instance as the
        # RunsActivities pass above — single recorder per worker
        # so the per-run aggregate sees signals from BOTH the
        # processing activities (stage timing + LLM calls) and
        # the terminal hook (report-write).
        diagnostic_recorder=diagnostic_recorder,
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
        # Phase 3 retry: every artifact materialised by
        # ``_materialize_draft`` now gets a typed ``snapshot_id`` +
        # ``created_by_run_id`` stamped onto the ``ArtifactRecord``.
        # The snapshot is allocated / reused via
        # ``get_or_create_for_run`` so retries on the same
        # (document_id, run_id) pair land in the same snapshot.
        snapshot_service=snapshot_service,
    ).all_activities()

    # `BatchOrchestrationWorkflow` is the parent that dispatches
    # per-document children sequentially via `execute_child_workflow`.
    # Both must be registered on the same worker — the parent runs
    # in the worker process, then schedules child workflow tasks
    # that the same worker (or any worker on this task queue) picks
    # up. Forgetting to register it would surface as
    # "WorkflowTypeNotFoundError" the first time `POST
    # /ingestion-batches` runs.
    from j1.orchestration.workflows.batch_orchestration import (
        BatchOrchestrationWorkflow,
    )
    return WorkerSpec(
        workflows=[
            ProjectProcessingWorkflow,
            DocumentProcessingWorkflow,
            BatchOrchestrationWorkflow,
        ],
        activities=activities,
    )
