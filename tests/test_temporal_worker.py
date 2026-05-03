from unittest.mock import patch

from temporalio import activity, workflow

from j1.orchestration.temporal.config import TemporalSettings
from j1.orchestration.temporal.worker import (
    WorkerSpec,
    build_worker,
    run_worker,
)


@workflow.defn
class _SampleWorkflow:
    @workflow.run
    async def run(self, value: str) -> str:
        return value


@activity.defn
def _sample_activity(value: str) -> str:
    return value


def test_worker_spec_defaults_are_empty():
    spec = WorkerSpec()
    assert list(spec.workflows) == []
    assert list(spec.activities) == []


def test_build_worker_passes_registrations():
    spec = WorkerSpec(
        workflows=[_SampleWorkflow],
        activities=[_sample_activity],
    )
    settings = TemporalSettings(task_queue="test-queue")

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        build_worker(client=object(), settings=settings, spec=spec)
        worker_cls.assert_called_once()
        kwargs = worker_cls.call_args.kwargs
        assert kwargs["task_queue"] == "test-queue"
        assert kwargs["workflows"] == [_SampleWorkflow]
        assert kwargs["activities"] == [_sample_activity]


def test_build_worker_uses_settings_task_queue():
    spec = WorkerSpec(workflows=[_SampleWorkflow], activities=[_sample_activity])
    settings = TemporalSettings(task_queue="another-queue")

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        build_worker(client=object(), settings=settings, spec=spec)
        assert worker_cls.call_args.kwargs["task_queue"] == "another-queue"


def test_run_worker_awaits_run():
    import asyncio

    spec = WorkerSpec()
    settings = TemporalSettings()

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        instance = worker_cls.return_value

        async def _ok() -> None:
            return None

        instance.run.return_value = _ok()
        asyncio.run(run_worker(client=object(), settings=settings, spec=spec))
        instance.run.assert_called_once()
