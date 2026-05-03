"""Local-development REST API entrypoint.

Run via:

    python -m deploy.dev.api

The container's CMD wraps this. NOT a production deployment — see
`docs/architecture.md` § 17 for the full wiring story. This script
exists to give developers a runnable HTTP surface backed by the
filesystem; pluggable processors (model providers, real compilers,
etc.) are explicitly not wired here.
"""

import asyncio
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

    # Lazy Temporal client — connect on the first job-control / job-
    # status request rather than blocking startup. Useful when the
    # Temporal server comes up after the API.
    _client_state: dict = {"client": None}

    async def _client_provider():
        if _client_state["client"] is None:
            _client_state["client"] = await build_client(temporal_settings)
        return _client_state["client"]

    def _sync_client_provider():
        # `TemporalJobControlService` calls this synchronously from an
        # async context. Use a small adapter that runs the coroutine on
        # the running loop.
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.ensure_future(_client_provider())
        return loop.run_until_complete(_client_provider())

    job_control = TemporalJobControlService(
        client_provider=_sync_client_provider,
        task_queue=temporal_settings.task_queue,
    )
    job_status = TemporalJobStatusService(client_provider=_sync_client_provider)

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

    async def _start_project_workflow(ctx, request) -> str:
        client = await _client_provider()
        scope = ProjectScope.from_context(ctx)
        workflow_id = f"j1-{ctx.tenant_id}-{ctx.project_id}-{request.compiler_kind}"
        await client.start_workflow(
            ProjectProcessingWorkflow.run,
            ProjectProcessingRequest(
                scope=scope,
                compiler_kind=request.compiler_kind,
                enricher_kind=request.enricher_kind,
                graph_builder_kind=request.graph_builder_kind,
                indexer_kind=request.indexer_kind,
                actor=request.actor,
                correlation_id=request.correlation_id,
            ),
            id=workflow_id,
            task_queue=temporal_settings.task_queue,
        )
        return workflow_id

    return create_rest_api(
        facade_with_temporal,
        authenticator=maybe_build_authenticator(),
        workspace=workspace,
        event_bus=ApplicationEventBus(),
        # `job_starter` is the per-document ingest hook, distinct from
        # `job_control.start_project_job`. Wired here for completeness.
        job_starter=_start_project_workflow,
        version=os.environ.get("J1_API_VERSION", "0.0.1-dev"),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("J1_API_PORT", "8000"))
    _log.info("starting J1 dev API on port %d", port)
    app = _build_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
