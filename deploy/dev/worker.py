"""Local-development Temporal worker entrypoint.

Run via:

    python -m deploy.dev.worker

The container's CMD wraps this. Calls `bootstrap_from_env()` so the
selection env vars (`J1_DEFAULT_COMPILER`, `J1_DEFAULT_GRAPH_PROVIDER`,
`J1_DEFAULT_RETRIEVAL_PROVIDER`) actually take effect — the dev
stack defaults to `mock` for all three, which wires the bundled
deterministic mock adapters and lets a brand-new `docker compose up`
run a complete workflow end-to-end with no vendor credentials.

Switch any selection to `raganything` (and provide
`J1_TEXT_LLM_*` / `J1_EMBEDDING_*` credentials) to drive real
processing through the same entrypoint.
"""

import asyncio
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from deploy.dev._wiring import build_settings, build_workspace, build_worker_spec
from j1 import (
    bootstrap_from_env,
    build_client,
    load_temporal_settings,
    run_worker,
)

_log = logging.getLogger("j1.dev.worker")


async def _run() -> None:
    settings = build_settings()
    workspace = build_workspace(settings)
    temporal_settings = load_temporal_settings()

    # Compose the env-declared providers. With the bundled
    # `.env.example` this produces a registry of mock adapters
    # so the entire pipeline runs end-to-end out of the box.
    boot = bootstrap_from_env()
    _log.info(
        "bootstrap selection: compiler=%s graph=%s retrieval=%s",
        boot.selection.compiler, boot.selection.graph, boot.selection.retrieval,
    )

    _log.info(
        "connecting to Temporal target=%s namespace=%s task_queue=%s",
        temporal_settings.target, temporal_settings.namespace,
        temporal_settings.task_queue,
    )
    client = await build_client(temporal_settings)
    spec = build_worker_spec(
        workspace,
        compilers=boot.compilers,
        graph_builders=boot.graph_builders,
        query_providers=boot.retrieval_providers,
        # Pass the LLM registry through so the auto-registered
        # composite enricher gets the configured vision client. Without
        # this, `VisualContentDescriber` constructs with
        # `vision_client=None` and the FE's Results > Assets tab shows
        # the 'No vision LLM configured' markdown stub for every run
        # even when J1_VISION_LLM_* is set.
        llm_registry=boot.llm_registry,
    )

    # Every J1 activity is synchronous; the Temporal SDK requires
    # a ThreadPoolExecutor to dispatch sync activities. Size it to
    # the configured concurrency cap (with a small floor).
    max_concurrent = int(
        os.environ.get("J1_WORKER_MAX_CONCURRENT_ACTIVITIES", "5")
    )
    pool_size = max(max_concurrent, 4)

    _log.info(
        "registering %d activities + %d workflows; "
        "max_concurrent_activities=%d, pool_size=%d",
        len(spec.activities), len(spec.workflows),
        max_concurrent, pool_size,
    )
    with ThreadPoolExecutor(
        max_workers=pool_size, thread_name_prefix="j1-activity"
    ) as executor:
        await run_worker(
            client, temporal_settings, spec,
            activity_executor=executor,
            max_concurrent_activities=max_concurrent,
        )


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
