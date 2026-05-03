from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field

from temporalio.client import Client
from temporalio.worker import Worker

from j1.orchestration.temporal.config import TemporalSettings


@dataclass(frozen=True)
class WorkerSpec:
    workflows: Sequence[type] = field(default_factory=tuple)
    activities: Sequence[Callable] = field(default_factory=tuple)


def build_worker(
    client: Client,
    settings: TemporalSettings,
    spec: WorkerSpec,
    *,
    activity_executor: Executor | None = None,
    max_concurrent_activities: int | None = None,
) -> Worker:
    """Construct a Temporal worker.

    `activity_executor` is required by the Temporal SDK whenever any
    registered activity is synchronous. Every activity J1 ships is
    synchronous (regular `def`, not `async def`) — the worker MUST
    have an executor when registering them. Pass a
    `concurrent.futures.ThreadPoolExecutor` (or process-pool, but
    sync activities expect to share state with the registering
    object instance, so threads are the conventional choice).

    `max_concurrent_activities` is forwarded to the Temporal SDK and
    bounds in-flight activity count regardless of executor capacity.
    """
    kwargs: dict = {
        "task_queue": settings.task_queue,
        "workflows": list(spec.workflows),
        "activities": list(spec.activities),
    }
    if activity_executor is not None:
        kwargs["activity_executor"] = activity_executor
    if max_concurrent_activities is not None:
        kwargs["max_concurrent_activities"] = max_concurrent_activities
    return Worker(client, **kwargs)


async def run_worker(
    client: Client,
    settings: TemporalSettings,
    spec: WorkerSpec,
    *,
    activity_executor: Executor | None = None,
    max_concurrent_activities: int | None = None,
) -> None:
    worker = build_worker(
        client, settings, spec,
        activity_executor=activity_executor,
        max_concurrent_activities=max_concurrent_activities,
    )
    await worker.run()
