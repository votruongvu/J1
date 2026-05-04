from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from concurrent.futures import Executor

from temporalio.client import Client
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from j1.orchestration.temporal.config import TemporalSettings


@dataclass(frozen=True)
class WorkerSpec:
    workflows: Sequence[type] = field(default_factory=tuple)
    activities: Sequence[Callable] = field(default_factory=tuple)


# Modules that are imported during workflow validation but are safe to
# share with the host runtime (i.e. they don't introduce non-determinism
# inside workflow code, and they include C-extension or threading.local
# tricks that the sandbox can't proxy).
#
# The chain that forced this list: J1's top-level `j1/__init__.py`
# eagerly re-exports the REST adapter surface, which transitively
# imports `fastapi → starlette → anyio → sniffio`. `sniffio` does
# `class _ThreadLocal(threading.local)` at module load, which the
# sandbox's `threading.local` proxy refuses to subclass. Telling the
# sandbox to passthrough sniffio + its peers (and the optional vendor
# stack pulled in by `[all-providers]`) makes those imports go through
# normally; the sandbox still inspects WORKFLOW CODE itself for
# determinism violations (`random.random()`, `datetime.now()`,
# uncontrolled file I/O, etc.).
_DEFAULT_PASSTHROUGH_MODULES: tuple[str, ...] = (
    # Async I/O machinery pulled by FastAPI / httpx / openai / etc.
    "anyio", "sniffio", "httpx", "httpcore", "h11",
    # FastAPI / starlette (re-exported by `j1.adapters.rest`).
    "fastapi", "starlette", "uvicorn",
    # LLM clients in `[all-providers]`.
    "openai", "anthropic", "ollama", "google", "google_genai",
    "langchain_core", "langchain_openai", "langchain_anthropic",
    "langchain_google_genai", "langchain_ollama",
    # RAGAnything + its heavy stack.
    "raganything", "lightrag", "lightrag_hku",
    "torch", "transformers", "mineru", "cv2", "scipy",
    "skimage", "PIL", "numpy", "pandas",
    # Graphify + tree-sitter language grammars.
    "graphify", "tree_sitter",
    # YAML loader (J1 core dep).
    "yaml",
)


def default_workflow_runner(
    *, extra_passthrough_modules: Sequence[str] = (),
) -> WorkflowRunner:
    """Build a `SandboxedWorkflowRunner` with passthrough for J1's
    transitive heavy deps.

    Workflow code itself is still inspected for non-determinism — the
    passthrough only relaxes module-load proxying for the named
    modules. Pass `extra_passthrough_modules=` to add deployment-
    specific vendor packages.
    """
    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        *_DEFAULT_PASSTHROUGH_MODULES,
        *extra_passthrough_modules,
    )
    return SandboxedWorkflowRunner(restrictions=restrictions)


def build_worker(
    client: Client,
    settings: TemporalSettings,
    spec: WorkerSpec,
    *,
    activity_executor: Executor | None = None,
    max_concurrent_activities: int | None = None,
    workflow_runner: WorkflowRunner | None = None,
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

    `workflow_runner` defaults to `default_workflow_runner()` — a
    `SandboxedWorkflowRunner` that knows about J1's transitive
    dependency surface. Pass your own if you need custom passthrough
    or want to disable the sandbox entirely
    (`UnsandboxedWorkflowRunner()`).
    """
    kwargs: dict = {
        "task_queue": settings.task_queue,
        "workflows": list(spec.workflows),
        "activities": list(spec.activities),
        "workflow_runner": workflow_runner or default_workflow_runner(),
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
    workflow_runner: WorkflowRunner | None = None,
) -> None:
    worker = build_worker(
        client, settings, spec,
        activity_executor=activity_executor,
        max_concurrent_activities=max_concurrent_activities,
        workflow_runner=workflow_runner,
    )
    await worker.run()
