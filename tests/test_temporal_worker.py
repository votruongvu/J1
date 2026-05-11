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


def test_build_worker_forwards_activity_executor():
    """Sync activities require an executor — the framework MUST allow
 deployments to plug one in without subclassing the worker."""
    from concurrent.futures import ThreadPoolExecutor

    spec = WorkerSpec(workflows=[_SampleWorkflow], activities=[_sample_activity])
    settings = TemporalSettings()

    with ThreadPoolExecutor(max_workers=2) as executor:
        with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
            build_worker(
                client=object(), settings=settings, spec=spec,
                activity_executor=executor,
                max_concurrent_activities=10,
            )
            kwargs = worker_cls.call_args.kwargs
            assert kwargs["activity_executor"] is executor
            assert kwargs["max_concurrent_activities"] == 10


def test_build_worker_omits_optional_kwargs_by_default():
    """Back-compat: existing callers that don't pass an executor must
 still get a Worker constructed (no `activity_executor=None` leak)."""
    spec = WorkerSpec(workflows=[_SampleWorkflow], activities=[_sample_activity])
    settings = TemporalSettings()

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        build_worker(client=object(), settings=settings, spec=spec)
        kwargs = worker_cls.call_args.kwargs
        # Optional fields are absent — let the SDK use its defaults.
        assert "activity_executor" not in kwargs
        assert "max_concurrent_activities" not in kwargs


# ---- Workflow-runner / sandbox -------------------------------------


def test_build_worker_supplies_default_workflow_runner():
    """The sandbox runner is always wired so heavy transitive deps
 (FastAPI/anyio/sniffio/openai/raganything/...) don't crash
 workflow validation. See `default_workflow_runner` for the
 passthrough list."""
    from j1.orchestration.temporal.worker import (
        default_workflow_runner,
    )
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    spec = WorkerSpec(workflows=[_SampleWorkflow], activities=[_sample_activity])
    settings = TemporalSettings()

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        build_worker(client=object(), settings=settings, spec=spec)
        runner = worker_cls.call_args.kwargs["workflow_runner"]
        assert isinstance(runner, SandboxedWorkflowRunner)


def test_build_worker_accepts_custom_workflow_runner():
    """Deployments can override the runner — e.g. UnsandboxedWorkflowRunner
 when they explicitly opt out of sandboxing."""
    from temporalio.worker import UnsandboxedWorkflowRunner

    spec = WorkerSpec(workflows=[_SampleWorkflow], activities=[_sample_activity])
    settings = TemporalSettings()
    custom = UnsandboxedWorkflowRunner()

    with patch("j1.orchestration.temporal.worker.Worker") as worker_cls:
        build_worker(
            client=object(), settings=settings, spec=spec,
            workflow_runner=custom,
        )
        assert worker_cls.call_args.kwargs["workflow_runner"] is custom


def test_default_runner_validates_shipped_workflows():
    """Regression: J1's bundled workflows must pass sandbox validation
 against the default runner.

 The failure mode this guards against: when `[all-providers]` is
 installed, `j1/__init__.py` transitively imports FastAPI/anyio/
 sniffio/openai/raganything/etc. The sandbox refuses to proxy
 `sniffio._impl._ThreadLocal(threading.local)` and other low-level
 tricks at module load. The `default_workflow_runner` passthroughs
 those modules so the bundled workflows validate cleanly. If this
 test ever fails, either:
 * a new transitive dep needs adding to
 `_DEFAULT_PASSTHROUGH_MODULES` in `worker.py`, or
 * the workflow itself imports something it shouldn't.
 """
    import asyncio

    from j1 import (
        DocumentProcessingWorkflow,
        ProjectProcessingWorkflow,
    )
    from j1.orchestration.temporal.worker import default_workflow_runner
    from temporalio.workflow import _Definition

    runner = default_workflow_runner()

    async def go() -> None:
        for wf in (ProjectProcessingWorkflow, DocumentProcessingWorkflow):
            defn = _Definition.must_from_class(wf)
            # `prepare_workflow` is the validator that fires inside
            # Temporal's `_WorkflowWorker.__init__`. If it raises,
            # the worker can't start.
            runner.prepare_workflow(defn)

    asyncio.run(go())
