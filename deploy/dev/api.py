"""Local-development REST API entrypoint.

Run via:

    python -m deploy.dev.api

The container's CMD wraps this. NOT a production deployment — see
`docs/architecture.md` § 17 for the full wiring story. This script
exists to give developers a runnable HTTP surface backed by the
filesystem; pluggable processors (model providers, real compilers,
etc.) are explicitly not wired here.
"""

import contextlib
import logging
import os

import uvicorn

from deploy.dev._wiring import (
    build_application_facade,
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
    build_client,
    create_rest_api,
    load_temporal_settings,
)
from j1.integration.services import ApplicationFacade

_log = logging.getLogger("j1.dev.api")


def _build_app():
    settings = build_settings()
    workspace = build_workspace(settings)
    facade = build_application_facade(workspace)
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

    async def _start_project_workflow(ctx, document_id, body) -> str:
        """`job_starter` callable — drives the per-document ingest path.

        Distinct from `job_control.start_project_job`, which is the
        project-wide variant. Both share the Temporal client built in
        the lifespan handler.
        """
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
            ),
            id=workflow_id,
            task_queue=temporal_settings.task_queue,
        )
        return workflow_id

    app = create_rest_api(
        facade_with_temporal,
        authenticator=maybe_build_authenticator(),
        workspace=workspace,
        event_bus=ApplicationEventBus(),
        job_starter=_start_project_workflow,
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
