from collections.abc import Callable, Sequence
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
) -> Worker:
    return Worker(
        client,
        task_queue=settings.task_queue,
        workflows=list(spec.workflows),
        activities=list(spec.activities),
    )


async def run_worker(
    client: Client,
    settings: TemporalSettings,
    spec: WorkerSpec,
) -> None:
    worker = build_worker(client, settings, spec)
    await worker.run()
