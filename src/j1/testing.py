from dataclasses import dataclass
from pathlib import Path

from j1.artifacts.registry import JsonArtifactRegistry
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.config.settings import Settings
from j1.cost.aggregator import CostAggregator
from j1.cost.recorder import DefaultCostRecorder
from j1.cost.sink import JsonlCostSink
from j1.intake.registry import JsonSourceRegistry
from j1.intake.service import DocumentIntakeService
from j1.locks import WorkspaceLock
from j1.processing.service import ProcessingService
from j1.projects.context import ProjectContext
from j1.review.queue import JsonReviewQueue
from j1.workspace.resolver import WorkspaceResolver


@dataclass
class TestEnvironment:
    """A fully wired-up J1 environment for downstream test suites.

    All registries point at the same `data_root`, all recorders share the
    same audit/cost sinks. Use this from any test that needs more than one
    J1 service hooked together.
    """

    # Tell pytest not to collect this dataclass as a test class.
    __test__ = False

    settings: Settings
    workspace: WorkspaceResolver
    ctx: ProjectContext
    audit_sink: JsonlAuditSink
    audit_recorder: DefaultAuditRecorder
    cost_sink: JsonlCostSink
    cost_recorder: DefaultCostRecorder
    source_registry: JsonSourceRegistry
    artifact_registry: JsonArtifactRegistry
    review_queue: JsonReviewQueue
    intake_service: DocumentIntakeService
    processing_service: ProcessingService
    cost_aggregator: CostAggregator
    workspace_lock: WorkspaceLock


def make_test_environment(
    data_root: Path,
    *,
    tenant_id: str = "acme",
    project_id: str = "alpha",
    profile_id: str | None = None,
    ensure_workspace: bool = True,
) -> TestEnvironment:
    """Build a fresh, isolated `TestEnvironment` rooted at `data_root`.

    `data_root` must be absolute. Any `tmp_path`-style fixture works: just
    pass `tmp_path.resolve()`. The workspace directories are created by
    default — set `ensure_workspace=False` to skip.
    """
    settings = Settings(data_root=data_root.resolve())
    workspace = WorkspaceResolver(settings)
    ctx = ProjectContext(
        tenant_id=tenant_id, project_id=project_id, profile=profile_id
    )
    if ensure_workspace:
        workspace.ensure(ctx)

    audit_sink = JsonlAuditSink(workspace)
    audit_recorder = DefaultAuditRecorder(audit_sink)
    cost_sink = JsonlCostSink(workspace)
    cost_recorder = DefaultCostRecorder(cost_sink)

    source_registry = JsonSourceRegistry(workspace)
    artifact_registry = JsonArtifactRegistry(workspace)
    review_queue = JsonReviewQueue(workspace)

    intake_service = DocumentIntakeService(
        workspace=workspace,
        registry=source_registry,
        audit_sink=audit_sink,
    )
    processing_service = ProcessingService(
        workspace=workspace,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
    )
    cost_aggregator = CostAggregator(workspace)
    workspace_lock = WorkspaceLock(workspace)

    return TestEnvironment(
        settings=settings,
        workspace=workspace,
        ctx=ctx,
        audit_sink=audit_sink,
        audit_recorder=audit_recorder,
        cost_sink=cost_sink,
        cost_recorder=cost_recorder,
        source_registry=source_registry,
        artifact_registry=artifact_registry,
        review_queue=review_queue,
        intake_service=intake_service,
        processing_service=processing_service,
        cost_aggregator=cost_aggregator,
        workspace_lock=workspace_lock,
    )
