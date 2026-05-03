from datetime import datetime, timezone
from itertools import count
from pathlib import Path

import pytest

from j1.artifacts.registry import JsonArtifactRegistry
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.config.settings import Settings
from j1.cost.recorder import DefaultCostRecorder
from j1.cost.sink import JsonlCostSink
from j1.intake.registry import JsonSourceRegistry
from j1.intake.service import DocumentIntakeService
from j1.processing.service import ProcessingService
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def settings(data_root: Path) -> Settings:
    return Settings(data_root=data_root)


@pytest.fixture
def workspace(settings: Settings) -> WorkspaceResolver:
    return WorkspaceResolver(settings)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


@pytest.fixture
def other_ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="beta")


@pytest.fixture
def registry(workspace: WorkspaceResolver) -> JsonSourceRegistry:
    return JsonSourceRegistry(workspace)


@pytest.fixture
def artifact_registry(workspace: WorkspaceResolver) -> JsonArtifactRegistry:
    return JsonArtifactRegistry(workspace)


@pytest.fixture
def audit_sink(workspace: WorkspaceResolver) -> JsonlAuditSink:
    return JsonlAuditSink(workspace)


@pytest.fixture
def cost_sink(workspace: WorkspaceResolver) -> JsonlCostSink:
    return JsonlCostSink(workspace)


@pytest.fixture
def fixed_clock():
    return lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def id_factory():
    counter = count(1)
    return lambda: f"id-{next(counter):08d}"


@pytest.fixture
def audit_recorder(audit_sink, fixed_clock, id_factory) -> DefaultAuditRecorder:
    return DefaultAuditRecorder(audit_sink, clock=fixed_clock, id_factory=id_factory)


@pytest.fixture
def cost_recorder(cost_sink, fixed_clock, id_factory) -> DefaultCostRecorder:
    return DefaultCostRecorder(cost_sink, clock=fixed_clock, id_factory=id_factory)


@pytest.fixture
def intake_service(
    workspace: WorkspaceResolver,
    registry: JsonSourceRegistry,
    audit_sink: JsonlAuditSink,
    fixed_clock,
    id_factory,
) -> DocumentIntakeService:
    return DocumentIntakeService(
        workspace=workspace,
        registry=registry,
        audit_sink=audit_sink,
        clock=fixed_clock,
        id_factory=id_factory,
    )


@pytest.fixture
def processing_service(
    workspace: WorkspaceResolver,
    artifact_registry: JsonArtifactRegistry,
    audit_recorder: DefaultAuditRecorder,
    cost_recorder: DefaultCostRecorder,
    fixed_clock,
    id_factory,
) -> ProcessingService:
    return ProcessingService(
        workspace=workspace,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
        clock=fixed_clock,
        id_factory=id_factory,
    )
