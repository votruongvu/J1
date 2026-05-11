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
from j1.orchestration.activities.accounting import AccountingActivities
from j1.orchestration.activities.knowledge import KnowledgeProcessingActivities
from j1.orchestration.activities.lifecycle import ProjectLifecycleActivities
from j1.orchestration.activities.review import ReviewActivities
from j1.orchestration.activities.search import SearchActivities
from j1.processing.service import ProcessingService
from j1.projects.context import ProjectContext
from j1.review.queue import JsonReviewQueue
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture(autouse=True)
def _stub_raganything_vlm_url(monkeypatch):
    """J1 forces MinerU into HTTP-client mode and refuses to load
 settings without `J1_RAGANYTHING_VLM_HTTP_SERVER_URL`. The
 bootstrap-triggered tests (test_compose, test_bootstrap_integration,
 etc.) call `load_raganything_settings` with no env arg → it
 reads `os.environ`. Provide a stub URL here so those tests work
 out of the box without each test setting it themselves.

 Tests that pass an explicit `env={...}` to the loader bypass
 `os.environ` entirely; those test files set the key in their
 own dicts. Tests that need to assert on the URL-missing path
 use `monkeypatch.delenv` inside the test body."""
    monkeypatch.setenv(
        "J1_RAGANYTHING_VLM_HTTP_SERVER_URL", "http://stub-vlm:1234/v1",
    )


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


@pytest.fixture
def review_queue(workspace: WorkspaceResolver) -> JsonReviewQueue:
    return JsonReviewQueue(workspace)


@pytest.fixture
def lifecycle_activities(
    workspace: WorkspaceResolver,
    intake_service: DocumentIntakeService,
    audit_recorder: DefaultAuditRecorder,
) -> ProjectLifecycleActivities:
    return ProjectLifecycleActivities(
        workspace=workspace,
        intake=intake_service,
        audit=audit_recorder,
    )


@pytest.fixture
def accounting_activities(
    workspace: WorkspaceResolver,
    audit_recorder: DefaultAuditRecorder,
) -> AccountingActivities:
    return AccountingActivities(workspace=workspace, audit=audit_recorder)


@pytest.fixture
def review_activities(
    review_queue: JsonReviewQueue,
    audit_recorder: DefaultAuditRecorder,
    fixed_clock,
    id_factory,
) -> ReviewActivities:
    return ReviewActivities(
        review_queue=review_queue,
        audit=audit_recorder,
        clock=fixed_clock,
        id_factory=id_factory,
    )
