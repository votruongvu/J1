"""Local-development REST API entrypoint.

Run via:

 python -m deploy.dev.api

The container's CMD wraps this. NOT a production deployment — see
`docs/architecture.md` § 17 for the full wiring story. Calls
`bootstrap_from_env` so the API can:

 * Default omitted `compilerKind` request fields to
 `J1_DEFAULT_COMPILER`.
 * Reject unknown processor kinds at the API boundary with a clear
 400 instead of letting them surface as workflow failures later.
"""

import contextlib
import logging
import os
from pathlib import Path

import uvicorn


def _load_local_dotenv() -> None:
    """Best-effort: pull the project-root .env into ``os.environ``.

    Same config that docker-compose picks up via ``env_file`` is
    then visible when running the API locally with
    ``python -m deploy.dev.api`` (or uvicorn directly). Without
    this, the .env-only ``J1_TEXT_LLM_*`` keys are missing from
    ``os.environ`` and the validation generator silently falls
    back to the heuristic question producer ("heuristic (no LLM)"
    trace on the FE).

    Only called from the ``__main__`` guard below so importing
    ``deploy.dev.api`` from tests (or any other consumer) does
    NOT leak ``.env`` into the process — tests that exercise the
    bootstrap config-error path depend on the env staying clean.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        # python-dotenv is a dev dependency; if missing, the user
        # is expected to source the env file before launching.
        return
    root = Path(__file__).resolve().parents[2]
    dotenv = root / ".env"
    if dotenv.is_file():
        load_dotenv(dotenv, override=False)

from deploy.dev._wiring import (
    _build_orchestrator_or_none,
    build_application_facade,
    build_batch_run_store,
    build_document_lifecycle_service,
    build_review_service,
    build_snapshot_services,
    build_validation_service,
    build_run_progress_surface,
    build_settings,
    build_workspace,
    maybe_build_authenticator,
)
from j1 import (
    ApplicationEventBus,
    ProjectProcessingRequest,
    ProjectProcessingWorkflow,
    ProjectScope,
    TemporalJobControlService,
    TemporalJobStatusService,
    bootstrap_from_env,
    build_client,
    capabilities_from_bootstrap,
    create_rest_api,
    load_temporal_settings,
)
from j1.orchestration.workflows.batch_orchestration import (
    BatchChildSpec,
    BatchOrchestrationRequest,
    BatchOrchestrationWorkflow,
)
from j1.processing.execution_profile_policy import (
    load_execution_profile_policy,
)
from temporalio.common import WorkflowIDConflictPolicy
from j1.integration.services import ApplicationFacade

_log = logging.getLogger("j1.dev.api")


def _requested_capabilities_dict(body) -> dict | None:
    """Project ``IngestRequest.requested_capabilities`` onto a
    plain dict for the workflow payload.

    Returns ``None`` when the field is absent — the workflow then
    falls back to the deterministic planner's defaults. Returns a
    three-key dict when present so downstream code can index it
    without a None check.
    """
    raw = getattr(body, "requested_capabilities", None)
    if raw is None:
        return None
    # Pydantic model → dict via ``model_dump``; tolerate plain dicts
    # passed in by test wirings.
    dump = (
        raw.model_dump() if hasattr(raw, "model_dump")
        else dict(raw)
    )
    return {
        "image_processing": bool(dump.get("image_processing", False)),
        "table_processing": bool(dump.get("table_processing", False)),
        "equation_processing": bool(dump.get("equation_processing", False)),
    }


# Renamed from ``J1_INGEST_PLANNER_ENABLED`` in Phase 2. The legacy
# name is honoured for one release cycle with a deprecation warning;
# remove the fallback once operators have migrated.
_ENV_ASSESSMENT_ENABLED = "J1_ASSESSMENT_ENABLED"
_ENV_LEGACY_PLANNER_ENABLED = "J1_INGEST_PLANNER_ENABLED"
_FALSY_VALUES = frozenset({"false", "0", "no", "off"})


def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in _FALSY_VALUES


def resolve_assessment_enabled(env) -> bool:
    """Resolve the pre-compile assessment toggle.

    Honours the renamed ``J1_ASSESSMENT_ENABLED``; falls back to the
    legacy ``J1_INGEST_PLANNER_ENABLED`` with a deprecation warning
    when the legacy name is the only source. When both are set, the
    new name wins and a conflict warning fires so operators clean up
    their env.

    Default (neither set) is ``True`` — the dev stack ships with the
    assessment stage enabled.
    """
    new_raw = env.get(_ENV_ASSESSMENT_ENABLED)
    legacy_raw = env.get(_ENV_LEGACY_PLANNER_ENABLED)
    if new_raw is not None and legacy_raw is not None:
        _log.warning(
            "Both %s and the legacy %s are set; honouring %s. "
            "Remove %s from your env — it will be ignored.",
            _ENV_ASSESSMENT_ENABLED, _ENV_LEGACY_PLANNER_ENABLED,
            _ENV_ASSESSMENT_ENABLED, _ENV_LEGACY_PLANNER_ENABLED,
        )
        return _truthy(new_raw)
    if new_raw is not None:
        return _truthy(new_raw)
    if legacy_raw is not None:
        _log.warning(
            "%s is deprecated; use %s instead. Value honoured for "
            "this run; the legacy name will be removed in a future "
            "release.",
            _ENV_LEGACY_PLANNER_ENABLED, _ENV_ASSESSMENT_ENABLED,
        )
        return _truthy(legacy_raw)
    return True


def make_per_document_starter(
    *,
    client_provider,
    task_queue: str,
    planner_enabled: bool,
    assessment_failure_policy: str = "fail_open",
):
    """Build the `JobStarter` closure used by `POST /documents/{id}/ingest`.

 Lifted out of `_build_app` so its behaviour (deterministic
 workflow id, single-document scoping, USE_EXISTING conflict
 policy) is unit-testable without standing up the entire app.

 Crucial:

 * Scopes the workflow to the SINGLE document just uploaded
 (`target_document_ids=(document_id,)`). Without this filter
 the workflow would call `list_pending_documents` and re-
 process every PENDING document in the project — once on the
 first upload, twice on the second, three times on the third,
 … The bulk-job path (`job_control.start_project_job`)
 intentionally leaves the filter empty.

 * Workflow id is `j1-{tenant_id}-{project_id}-{document_id}` —
 deterministic per (tenant, project, document). Combined with
 intake's checksum-based dedup, re-uploading the same file
 always lands on the same workflow id.

 * `id_conflict_policy=USE_EXISTING`. If a workflow with this
 id is already running (re-upload of an in-flight file), do
 NOT spawn a parallel run — return the existing handle.
 Combined with `ProcessingResultCache`, this means a single
 physical file is never parsed twice in parallel and never
 re-parsed once already completed.
 """

    async def _start(ctx, document_id, body) -> str:
        client = client_provider()
        scope = ProjectScope.from_context(ctx)
        # Default deterministic workflow_id: lets repeated uploads of
        # the same checksum re-attach to the in-flight workflow via
        # USE_EXISTING. The `-reindex-` suffix prevents USE_EXISTING
        # from re-attaching when the user explicitly re-indexes a
        # document.
        if getattr(body, "reindex_of", None):
            workflow_id = (
                f"j1-{ctx.tenant_id}-{ctx.project_id}-"
                f"{document_id}-reindex-{body.correlation_id}"
            )
        else:
            workflow_id = (
                f"j1-{ctx.tenant_id}-{ctx.project_id}-{document_id}"
            )
        await client.start_workflow(
            ProjectProcessingWorkflow.run,
            ProjectProcessingRequest(
                scope=scope,
                compiler_kind=body.compiler_kind,
                enricher_kind=body.enricher_kind,
                graph_builder_kind=body.graph_builder_kind,
                indexer_kind=body.indexer_kind,
                actor=body.actor,
                correlation_id=body.correlation_id,
                target_document_ids=(document_id,),
                planner_enabled=planner_enabled,
                assessment_failure_policy=assessment_failure_policy,
                target_snapshot_id=getattr(
                    body, "target_snapshot_id", None,
                ),
                reindex_of=getattr(body, "reindex_of", None),
                # User-selected execution profile from the FE picker.
                # `getattr` guards legacy callers that built IngestRequest
                # before the field was added; they keep working with
                # the workflow-side `DEFAULT_PROFILE` fallback.
                selected_execution_profile=getattr(
                    body, "selected_profile", None,
                ),
                # Persisted AssessmentDecision (validated by REST
                # adapter). When supplied, the workflow uses it
                # verbatim and skips its rebuild path. ``getattr``
                # keeps legacy IngestRequests working with the
                # workflow-side rebuild fallback.
                assessment_decision_payload=getattr(
                    body, "assessment_decision_payload", None,
                ),
                assessment_decision_warnings=tuple(getattr(
                    body, "assessment_decision_warnings", (),
                ) or ()),
                # User-selected per-modality capability checkboxes
                # from the Knowledge Index picker. None when the
                # FE didn't supply them (legacy callers / bulk-job
                # dispatch); the workflow falls back to the
                # deterministic planner's defaults in that case.
                requested_capabilities=_requested_capabilities_dict(
                    body,
                ),
            ),
            id=workflow_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return workflow_id

    return _start


def make_batch_starter(
    *,
    client_provider,
    task_queue: str,
    planner_enabled: bool,
):
    """Build the `BatchStarter` closure used by `POST /ingestion-batches`.

 The closure dispatches ONE `BatchOrchestrationWorkflow` per call;
 that parent fans out the child workflows sequentially via
 `execute_child_workflow`. Replaces the previous "REST handler
 fans out N concurrent workflows" pattern (which depended on the
 worker-wide `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1` env var to
 serialize). With the parent workflow, multiple batches can run
 in parallel — each batch is internally sequential, batches are
 independent.

 Parent workflow id: `j1-batch-{batch_run_id}` so operators can
 spot batches in the Temporal UI vs single-doc workflows. The
 batch_run_id is the FE-facing identifier; using it directly
 avoids the need for a follow-up "what's the workflow id for
 batch X" lookup.
 """

    async def _start(ctx, batch_run_id, child_specs) -> str:
        client = client_provider()
        scope = ProjectScope.from_context(ctx)
        # Hydrate the dict child specs from the REST endpoint into
        # frozen dataclass instances so the workflow input is
        # Temporal-data-converter friendly + locked at dispatch time.
        specs = tuple(
            BatchChildSpec(
                workflow_id=str(s["workflow_id"]),
                document_id=str(s["document_id"]),
                correlation_id=str(s["correlation_id"]),
                compiler_kind=str(s["compiler_kind"]),
                enricher_kind=s.get("enricher_kind"),
                graph_builder_kind=s.get("graph_builder_kind"),
                indexer_kind=s.get("indexer_kind"),
                actor=str(s.get("actor", "system")),
                planner_enabled=planner_enabled,
                target_snapshot_id=s.get("target_snapshot_id"),
            )
            for s in child_specs
        )
        parent_workflow_id = f"j1-batch-{batch_run_id}"
        await client.start_workflow(
            BatchOrchestrationWorkflow.run,
            BatchOrchestrationRequest(
                scope=scope,
                batch_run_id=batch_run_id,
                child_specs=specs,
                actor="system",
            ),
            id=parent_workflow_id,
            task_queue=task_queue,
            # USE_EXISTING — a duplicate POST (operator double-click
            # before getting a response) attaches to the in-flight
            # parent instead of dispatching a parallel batch. The
            # batch_run_id is freshly allocated per request so this
            # only matters under network retries.
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return parent_workflow_id

    return _start


def _build_app():
    # Phase 1: validate the unified runtime config FIRST. In PROD this
    # fails fast on missing providers; in DEV the validator is lenient
    # so the legacy local seams still come up.
    from deploy.dev._wiring import build_runtime_config
    build_runtime_config()
    settings = build_settings()
    workspace = build_workspace(settings)
    facade = build_application_facade(workspace)
    # The user-facing `/ingestion-runs/*` surface needs an
    # `IngestionRunStore` + `ProgressReporter`; without them every
    # endpoint there 503s with "ingestion-run store not configured".
    # Both are JSONL-backed under the workspace's audit area.
    run_store, progress_reporter = build_run_progress_surface(workspace)
    temporal_settings = load_temporal_settings()

    # The Temporal client is constructed once inside the FastAPI
    # lifespan (so it lives on the same event loop as the request
    # handlers). `client_provider` is a sync lambda returning the
    # already-connected client — that's the contract
    # `TemporalJobControlService` and `TemporalJobStatusService`
    # expect.
    _client_box: dict = {"client": None}

    def client_provider():
        client = _client_box["client"]
        if client is None:
            # Hit only if a request lands before lifespan startup
            # finishes — extremely rare but worth a clear error.
            raise RuntimeError(
                "Temporal client not yet initialised — startup in progress"
            )
        return client

    job_control = TemporalJobControlService(
        client_provider=client_provider,
        task_queue=temporal_settings.task_queue,
        # Idempotent bulk-job start: a duplicate `POST /ingestion-jobs`
        # (operator double-click, retry after a network blip) attaches
        # to the in-flight workflow instead of spawning a parallel
        # one. Without this, two parallel workflows would re-process
        # every PENDING document — doubling parse cost and racing the
        # registry writes.
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    job_status = TemporalJobStatusService(client_provider=client_provider)

    facade_with_temporal = ApplicationFacade(
        ingestion=facade.ingestion,
        retrieval=facade.retrieval,
        citation_lookup=facade.citation_lookup,
        source_lookup=facade.source_lookup,
        feedback=facade.feedback,
        event_publisher=facade.event_publisher,
        job_status=job_status,
        search=facade.search,
        project_admin=facade.project_admin,
        job_control=job_control,
        cost_summary=facade.cost_summary,
        review=facade.review,
    )

    # Should each upload run the pre-compile assessment (profile +
    # AssessmentPlan)? Default ON for the dev stack so the FE's
    # run-detail page sees a populated assessment immediately.
    # Operators who don't want adaptive assessment can set
    # ``J1_ASSESSMENT_ENABLED=false`` — the workflow then falls back
    # to ``settings.parse_method`` without LLM-driven plan choice.
    #
    # Legacy-name compatibility: the var was renamed from
    # ``J1_INGEST_PLANNER_ENABLED`` in Phase 2. We honour the legacy
    # name for one release cycle, log a warning when it is the only
    # source, and prefer the new name when both are set. See
    # ``resolve_assessment_enabled`` for the contract.
    planner_enabled = resolve_assessment_enabled(os.environ)

    # Read once at startup — passing through the workflow request
    # rather than reading inside the workflow (which would violate
    # Temporal's deterministic-replay contract).
    from j1.processing.assessment import load_assessment_failure_policy
    assessment_failure_policy = load_assessment_failure_policy()

    _start_project_workflow = make_per_document_starter(
        client_provider=client_provider,
        task_queue=temporal_settings.task_queue,
        planner_enabled=planner_enabled,
        assessment_failure_policy=assessment_failure_policy,
    )
    # Parent-workflow dispatcher for multi-upload batches. Replaces
    # the previous "REST handler fans out N concurrent workflows"
    # pattern (which depended on `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1`
    # to serialize). Multiple batches now run concurrently — each
    # batch is internally sequential.
    _start_batch_workflow = make_batch_starter(
        client_provider=client_provider,
        task_queue=temporal_settings.task_queue,
        planner_enabled=planner_enabled,
    )

    # Compose the env-declared providers so the API can default
    # `compilerKind` and validate unknown kinds. The same `boot`
    # value is what `worker.py` consumes — keeping API + worker on
    # one bootstrap means clients that omit `compilerKind` get the
    # selection the worker actually wired.
    boot = bootstrap_from_env()

    # LLM connectivity probe at API startup. Same warn-only contract
    # as the worker — failures populate the cached results that
    # `/healthz/llm` reads, so the FE can render the banner without
    # the API itself going down. The API can still serve cached run
    # history, audit-log endpoints, and the run-detail UI even when
    # LLM is unreachable; only NEW uploads are gated at the FE.
    from j1.llm.probe import (
        cache_probe_results,
        llm_probe_enabled,
        probe_registry,
        start_health_monitor,
    )
    if llm_probe_enabled() and getattr(boot, "llm_registry", None) is not None:
        _log.info("LLM startup probe: starting (5s deadline per role)")
        results = probe_registry(boot.llm_registry)
        cache_probe_results(results)
        failures = [r for r in results if not r.ok]
        if failures:
            for f in failures:
                _log.warning(
                    "LLM probe FAILED: role=%s provider=%s model=%s error=%s",
                    f.role, f.provider, f.model, f.error,
                )
            _log.warning(
                "API booting WITH unreachable LLM roles. /healthz/llm "
                "will report the failure; FE shows the banner.",
            )
        else:
            _log.info(
                "LLM startup probe: all %d configured roles reachable",
                len(results),
            )
        # Background re-probe loop (daemon thread, separate from the
        # request-handling loop). Refreshes the cached `/healthz/llm`
        # state on a bounded interval so the FE banner clears /
        # appears automatically when the LLM goes up / down without
        # an operator restart.
        start_health_monitor(boot.llm_registry)
    # Surface the worker's registered processor kinds so the REST
    # adapter can both (a) validate caller-supplied kinds at the API
    # boundary and (b) auto-default omitted kinds via the new
    # `_resolve_optional_processor_kind` rule. Keep this in lockstep
    # with `build_worker_spec`'s registrations — if a kind ships in
    # the worker but isn't surfaced here, FE uploads won't auto-pick
    # it and the corresponding stage stays unrunnable.
    #
    # When `J1_ENRICH_ENABLED=false`, omit the enricher kind entirely
    # so `_resolve_optional_processor_kind` returns None and the
    # workflow's `_stage_enabled` skips enrich. Without this gate,
    # the env var only feeds startup diagnostics — the auto-pick still
    # selects the registered kind and enrich runs anyway.
    from j1.enrichers import COMPOSITE_ENRICHER_KIND
    enricher_kinds = (
        frozenset({COMPOSITE_ENRICHER_KIND})
        if boot.enrichment.enabled
        else frozenset()
    )
    capabilities = capabilities_from_bootstrap(
        boot,
        enricher_kinds=enricher_kinds,
        # Phase 8: SQLite indexer deleted; no indexer kinds
        # registered by default. Evidence flows through the
        # canonical Postgres FTS adapter via the activity layer.
        indexer_kinds=frozenset(),
    )

    # Preload the domain registry in the API process so both the
    # generic and civil-engineering packs are visible at
    # ``/assessment-plan`` time. Without this, the standalone dev
    # API (no worker in-process) would never import
    # ``j1.domains.civil_engineering`` and the registry singleton
    # would carry only the generic pack — domain-specific rules
    # would silently never fire.
    from j1.domains import default_registry as _preload_default_registry
    try:
        _preload_default_registry()
    except Exception:  # noqa: BLE001 — degrade if a pack fails to load
        pass

    # Persistent ``AssessmentDecision`` store. Lives under the same
    # workspace audit area as ``ingestion_runs.jsonl`` so a single
    # backup covers run records + snapshots + decisions.
    from j1.processing.assessment_decision import (
        JsonlAssessmentDecisionStore,
    )
    assessment_decision_store = JsonlAssessmentDecisionStore(workspace)

    # Optional LLM Advanced Assessment service — operator-triggered
    # only. Disabled by default; flip on via env once a deployment
    # has an LLM provider + cost budget for the picker-time
    # assessment. The factory returns ``None`` when no LLM registry
    # is wired so the REST endpoint surfaces a structured refusal
    # ("Advanced Assessment is not configured in this deployment.")
    # instead of 5xx.
    from j1.processing.llm_advanced_assessment import (
        LLMAdvancedAssessmentService,
    )
    from j1.processing.llm_advanced_assessment_settings import (
        load_llm_advanced_assessment_settings,
    )
    advanced_assessment_settings = load_llm_advanced_assessment_settings()

    def _build_llm_advanced_assessment_service():
        if not advanced_assessment_settings.enabled:
            # Build a no-op service so the FE list still shows the
            # button and the operator gets a clear refusal payload
            # instead of a missing endpoint.
            return LLMAdvancedAssessmentService(
                settings=advanced_assessment_settings,
                llm_call=None,
            )
        registry = getattr(boot, "llm_registry", None)
        if registry is None:
            return LLMAdvancedAssessmentService(
                settings=advanced_assessment_settings,
                llm_call=None,
            )
        def _call(prompt: str, system_prompt: str) -> str:
            try_text = getattr(registry, "try_text", None)
            if try_text is None:
                return ""
            client = try_text()
            if client is None:
                return ""
            text, _usage = client.generate(
                prompt, system_prompt=system_prompt,
            )
            return text or ""
        return LLMAdvancedAssessmentService(
            settings=advanced_assessment_settings,
            llm_call=_call,
        )

    llm_advanced_assessment_service = (
        _build_llm_advanced_assessment_service()
    )

    # Deployment-level workspace default domain. Read at boot to
    # avoid per-request env access. ``None`` keeps the resolver on
    # the generic pack until a per-project override lands.
    workspace_default_domain_id = (
        os.environ.get("J1_WORKSPACE_DEFAULT_DOMAIN") or None
    )

    app = create_rest_api(
        facade_with_temporal,
        authenticator=maybe_build_authenticator(),
        workspace=workspace,
        event_bus=ApplicationEventBus(),
        job_starter=_start_project_workflow,
        batch_starter=_start_batch_workflow,
        processing_capabilities=capabilities,
        # User-facing ingestion-runs surface — without these the
        # frontend's All Runs page, run detail, and SSE timeline all
        # 503. Wiring them is cheap (JSONL files under the workspace
        # audit area).
        ingestion_run_store=run_store,
        # Multi-upload batch store — sits alongside the ingestion-run
        # store. Without this, `POST /ingestion-batches` 503s.
        batch_run_store=build_batch_run_store(workspace),
        progress_reporter=progress_reporter,
        # Read-only review surface for completed runs (Results tabs).
        # Without this the FE's `/ingestion-runs/{id}/summary` 503s.
        review_service=build_review_service(workspace),
        # Manual test query ( validation). Returns None when
        # no profile is loaded — the REST endpoint then 503s, which
        # is fine because the FE's Validation tab availability gate
        # in `availableViews.validation` will already be off.
        validation_service=build_validation_service(workspace),
        # SmartQueryOrchestrator for the ``POST /dev/query-trace``
        # operator surface. Same instance the validation service +
        # processing service consume; built once at the deployment
        # boundary via ``build_smart_query_orchestrator``. Returns
        # ``None`` when no LLM registry is wired — endpoint then
        # returns 503 with a wiring hint.
        smart_query_orchestrator=_build_orchestrator_or_none(workspace),
        # Document-centric attach / detach / remove flow. Without
        # this the three ``POST /documents/{id}/<action>``
        # endpoints 503 — operators clicked "Remove from Knowledge"
        # in the Document UI and got an immediate "Remove failed"
        # toast because the service wasn't wired.
        document_lifecycle_service=build_document_lifecycle_service(workspace),
        # Hand the LLM registry to the REST adapter so `POST
        # /healthz/llm/refresh` (the FE banner's "Retry now" button)
        # can re-probe synchronously instead of waiting for the next
        # 30s background tick.
        llm_registry=getattr(boot, "llm_registry", None),
        # Phase 5: snapshot service injected so every
        # ``IngestionRun`` created via the REST surface allocates
        # its target ``DocumentSnapshot`` UP-FRONT (instead of
        # leaving the activity layer to do it lazily on first
        # artifact write).
        snapshot_service=build_snapshot_services(workspace)[0],
        # Phase C: deployment-level execution-profile safety policy.
        # Read from env at boot (`J1_DEFAULT_INGEST_PROFILE`,
        # `J1_ALLOW_{MINIMUM_QUERYABLE,STANDARD,ADVANCED}_INGEST`).
        # Invalid env config raises `InvalidProfilePolicyError`
        # synchronously so the dev process refuses to start rather
        # than booting with a degraded policy — exactly the
        # contract we want for operator-visible misconfiguration.
        execution_profile_policy=load_execution_profile_policy(),
        # Persistent recommendation store + workspace default domain
        # — the two seams that turn ``/assessment-plan`` into a real
        # source of truth instead of a one-shot recommendation. See
        # ``j1.processing.assessment_decision`` for the validation
        # contract consumers must honour.
        assessment_decision_store=assessment_decision_store,
        workspace_default_domain_id=workspace_default_domain_id,
        # Optional LLM Advanced Assessment service. Operator-
        # triggered only — never runs automatically. Disabled
        # deployments still expose the endpoint; it just returns
        # a structured refusal.
        llm_advanced_assessment_service=llm_advanced_assessment_service,
        # `confirm_handler` intentionally left None — no workflow in
        # the dev stack listens for the confirm signal yet, so the
        # endpoint just flips status and emits `plan.confirmed`.
        # When a workflow with a `confirm_run` signal ships, plug a
        # handler here that calls `client.get_workflow_handle(...).
        # signal(...)`.
        version=os.environ.get("J1_API_VERSION", "0.0.1-dev"),
    )

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        _log.info(
            "connecting to Temporal target=%s namespace=%s",
            temporal_settings.target, temporal_settings.namespace,
        )
        _client_box["client"] = await build_client(temporal_settings)
        _log.info("Temporal client ready")
        try:
            yield
        finally:
            # The Temporal Python SDK's Client has no explicit close;
            # the underlying gRPC channel is reaped when the process
            # exits.
            _client_box["client"] = None

    # `create_rest_api` doesn't expose a lifespan param yet; attach
    # ours after construction. Standard Starlette/FastAPI pattern.
    app.router.lifespan_context = _lifespan
    return app


def main() -> None:
    # Pull .env into os.environ before any wiring runs so local
    # ``python -m deploy.dev.api`` invocations see the same
    # config docker compose's ``env_file`` injects. Has no effect
    # under docker (.env already in environment) or under tests
    # (this path doesn't run on import).
    _load_local_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("J1_API_PORT", "8000"))
    _log.info("starting J1 dev API on port %d", port)
    app = _build_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
