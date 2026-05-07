"""Local-development REST API entrypoint.

Run via:

    python -m deploy.dev.api

The container's CMD wraps this. NOT a production deployment — see
`docs/architecture.md` § 17 for the full wiring story. Calls
`bootstrap_from_env()` so the API can:

  * Default omitted `compilerKind` request fields to
    `J1_DEFAULT_COMPILER`.
  * Reject unknown processor kinds at the API boundary with a clear
    400 instead of letting them surface as workflow failures later.
"""

import contextlib
import logging
import os

import uvicorn

from deploy.dev._wiring import (
    build_application_facade,
    build_review_service,
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
from temporalio.common import WorkflowIDConflictPolicy
from j1.integration.services import ApplicationFacade
from j1.search.indexer import SqliteSearchIndexer

_log = logging.getLogger("j1.dev.api")


def make_per_document_starter(
    *,
    client_provider,
    task_queue: str,
    planner_enabled: bool,
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
            ),
            id=workflow_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return workflow_id

    return _start


def _build_app():
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
        answer=facade.answer,
        project_admin=facade.project_admin,
        job_control=job_control,
        cost_summary=facade.cost_summary,
        review=facade.review,
    )

    # User-facing flow: should each upload run the planner? Default
    # ON for the dev stack so the FE's run-detail page sees a
    # populated execution plan immediately. Operators who don't want
    # adaptive planning can set `J1_INGEST_PLANNER_ENABLED=false`.
    planner_enabled = (
        os.environ.get("J1_INGEST_PLANNER_ENABLED", "true").lower()
        not in {"false", "0", "no", "off"}
    )

    _start_project_workflow = make_per_document_starter(
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
    # Surface the worker's registered processor kinds so the REST
    # adapter can both (a) validate caller-supplied kinds at the API
    # boundary and (b) auto-default omitted kinds via the new
    # `_resolve_optional_processor_kind` rule. Keep this in lockstep
    # with `build_worker_spec`'s registrations — if a kind ships in
    # the worker but isn't surfaced here, FE uploads won't auto-pick
    # it and the corresponding stage stays unrunnable.
    from j1.enrichers import COMPOSITE_ENRICHER_KIND
    capabilities = capabilities_from_bootstrap(
        boot,
        enricher_kinds=frozenset({COMPOSITE_ENRICHER_KIND}),
        indexer_kinds=frozenset({SqliteSearchIndexer.kind}),
    )

    app = create_rest_api(
        facade_with_temporal,
        authenticator=maybe_build_authenticator(),
        workspace=workspace,
        event_bus=ApplicationEventBus(),
        job_starter=_start_project_workflow,
        processing_capabilities=capabilities,
        # User-facing ingestion-runs surface — without these the
        # frontend's All Runs page, run detail, and SSE timeline all
        # 503. Wiring them is cheap (JSONL files under the workspace
        # audit area).
        ingestion_run_store=run_store,
        progress_reporter=progress_reporter,
        # Read-only review surface for completed runs (Results tabs).
        # Without this the FE's `/ingestion-runs/{id}/summary` 503s.
        review_service=build_review_service(workspace),
        # Manual test query (Phase 1 validation). Returns None when
        # no profile is loaded — the REST endpoint then 503s, which
        # is fine because the FE's Validation tab availability gate
        # in `availableViews.validation` will already be off.
        validation_service=build_validation_service(workspace),
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("J1_API_PORT", "8000"))
    _log.info("starting J1 dev API on port %d", port)
    app = _build_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
