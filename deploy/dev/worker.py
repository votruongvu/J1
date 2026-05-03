"""Local-development Temporal worker entrypoint.

Run via:

    python -m deploy.dev.worker

The container's CMD wraps this. Registers the framework's two
shipped workflows (`ProjectProcessingWorkflow`,
`DocumentProcessingWorkflow`) and every activity class whose
constructor doesn't need a vendor-specific processor.

Pluggable processor maps (compilers, enrichers, graph builders) are
intentionally empty — the dev stack confirms the worker loop runs and
workflows are dispatched. Real processor wiring is deployment-
specific (model providers, external compiler binaries, etc.) and
goes in a separate worker entrypoint forked from this file.
"""

import asyncio
import logging
import os
import signal
import sys

from deploy.dev._wiring import build_settings, build_workspace, build_worker_spec
from j1 import build_client, load_temporal_settings, run_worker

_log = logging.getLogger("j1.dev.worker")


async def _run() -> None:
    settings = build_settings()
    workspace = build_workspace(settings)
    temporal_settings = load_temporal_settings()

    _log.info(
        "connecting to Temporal target=%s namespace=%s task_queue=%s",
        temporal_settings.target, temporal_settings.namespace,
        temporal_settings.task_queue,
    )
    client = await build_client(temporal_settings)
    spec = build_worker_spec(workspace)

    _log.info(
        "registering %d activities + %d workflows; "
        "max_concurrent_activities=%s",
        len(spec.activities), len(spec.workflows),
        os.environ.get("J1_WORKER_MAX_CONCURRENT_ACTIVITIES", "default"),
    )
    await run_worker(client, temporal_settings, spec)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: signal handlers via add_signal_handler aren't supported.
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        _log.info("worker interrupted; shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
