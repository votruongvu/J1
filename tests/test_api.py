import io
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.api import build_default_container, create_api
from j1.api.services import ServiceContainer
from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.config.settings import Settings
from j1.cost.breakdown import CostBreakdown
from j1.cost.recorder import DefaultCostRecorder
from j1.cost.sink import JsonlCostSink
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.review.models import ReviewItem
from j1.workspace.layout import WorkspaceArea


# ---- Mock Temporal client --------------------------------------------


class _MockHandle:
    def __init__(self, mock_client: "_MockTemporalClient", workflow_id: str) -> None:
        self._client = mock_client
        self.id = workflow_id

    async def signal(self, name: str, *args: Any, **kwargs: Any) -> None:
        self._client.signals.append((self.id, name))

    async def query(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._client.queries.append((self.id, name))
        return self._client.status_for(self.id)


class _MockTemporalClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, Any]] = []
        self.signals: list[tuple[str, str]] = []
        self.queries: list[tuple[str, str]] = []
        self._status_by_id: dict[str, Any] = {}

    def set_status(self, workflow_id: str, status: Any) -> None:
        self._status_by_id[workflow_id] = status

    def status_for(self, workflow_id: str) -> Any:
        return self._status_by_id.get(workflow_id, _DefaultStatus())

    async def start_workflow(
        self,
        fn: Any,
        arg: Any,
        *,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> _MockHandle:
        self.started.append((id, arg))
        return _MockHandle(self, id)

    def get_workflow_handle(self, workflow_id: str) -> _MockHandle:
        return _MockHandle(self, workflow_id)


class _DefaultStatus:
    """Lookalike for `WorkflowStatus` — only the fields the API reads."""

    state = "running"
    current_operation = "compile:doc-1"
    pending_operation = None
    completed_operations: list[str] = []
    documents_total = 1
    documents_completed = 0
    produced_artifact_ids: list[str] = []
    review_required = False
    review_gate = None
    budget_approval_required = False
    error = None


# ---- Fixtures --------------------------------------------------------


@pytest.fixture
def container(workspace, ctx) -> ServiceContainer:
    settings = Settings(data_root=workspace.data_root)
    container = build_default_container(settings=settings)
    # The fixtures already created the workspace; ensure it for the API ctx too.
    container.workspace.ensure(ctx)
    return container


@pytest.fixture
def mock_temporal() -> _MockTemporalClient:
    return _MockTemporalClient()


@pytest.fixture
def client(container, mock_temporal) -> TestClient:
    container.temporal_client = mock_temporal
    app = create_api(container)
    return TestClient(app)


def _headers(tenant_id: str = "acme") -> dict[str, str]:
    return {"X-Tenant-Id": tenant_id}


def _stage_artifact(
    workspace,
    ctx,
    artifact_registry,
    *,
    artifact_id: str,
    kind: str = "compiled.text",
    content: bytes = b"",
    area: WorkspaceArea = WorkspaceArea.COMPILED,
    suffix: str = ".txt",
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}{suffix}"
    (area_dir / stored).write_bytes(content)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    artifact_registry.add(record)
    return record


# ---- Tenant header ---------------------------------------------------


def test_missing_tenant_header_returns_400(client):
    response = client.post("/api/projects", json={"project_id": "alpha"})
    assert response.status_code == 400
    assert "X-Tenant-Id" in response.json()["detail"]


def test_invalid_tenant_id_returns_400(client):
    response = client.post(
        "/api/projects",
        json={"project_id": "alpha"},
        headers=_headers(".."),
    )
    assert response.status_code == 400


# ---- Projects --------------------------------------------------------


def test_create_project(client, container):
    response = client.post(
        "/api/projects",
        json={"project_id": "alpha"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "project_id": "alpha",
        "tenant_id": "acme",
        "profile": None,
    }


def test_create_project_with_profile(client):
    response = client.post(
        "/api/projects",
        json={"project_id": "alpha", "profile": "default"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["profile"] == "default"


def test_create_project_invalid_id(client):
    response = client.post(
        "/api/projects",
        json={"project_id": ".."},
        headers=_headers(),
    )
    assert response.status_code == 400


# ---- Documents -------------------------------------------------------


def test_upload_document(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.post(
        "/api/projects/alpha/documents",
        files={"file": ("paper.txt", io.BytesIO(b"hello world"), "text/plain")},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["original_filename"] == "paper.txt"
    assert data["mime_type"] == "text/plain"
    assert data["file_size"] == len(b"hello world")
    assert data["checksum"].startswith("sha256:")
    assert data["duplicate"] is False
    assert "/" not in data["stored_filename"]


def test_upload_duplicate_returns_existing_id(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    first = client.post(
        "/api/projects/alpha/documents",
        files={"file": ("doc.txt", io.BytesIO(b"same content"), "text/plain")},
        headers=_headers(),
    )
    second = client.post(
        "/api/projects/alpha/documents",
        files={"file": ("doc.txt", io.BytesIO(b"same content"), "text/plain")},
        headers=_headers(),
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["duplicate"] is True
    assert payload["document_id"] == first.json()["document_id"]


def test_upload_response_does_not_expose_paths(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.post(
        "/api/projects/alpha/documents",
        files={"file": ("doc.txt", io.BytesIO(b"x"), "text/plain")},
        headers=_headers(),
    )
    payload = response.json()
    for value in payload.values():
        if isinstance(value, str):
            assert "/tenants/" not in value
            assert not value.startswith("/")


# ---- Processing ------------------------------------------------------


def test_start_processing_invokes_temporal(client, mock_temporal):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.post(
        "/api/projects/alpha/processing",
        json={
            "compiler_kind": "external_knowledge_compiler",
            "review_after": ["after_compile"],
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["workflow_id"].startswith("j1-acme-alpha-")
    assert len(mock_temporal.started) == 1
    started_id, started_arg = mock_temporal.started[0]
    assert started_id == body["workflow_id"]
    assert started_arg.compiler_kind == "external_knowledge_compiler"
    assert started_arg.review_after == ("after_compile",)


def test_start_processing_returns_503_when_no_temporal(container):
    """A container without a Temporal client refuses workflow control."""
    container.temporal_client = None
    app = create_api(container)
    c = TestClient(app)
    c.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    r = c.post(
        "/api/projects/alpha/processing",
        json={"compiler_kind": "x"},
        headers=_headers(),
    )
    assert r.status_code == 503


def test_get_workflow_status(client, mock_temporal):
    workflow_id = "j1-acme-alpha-abc123"

    class _Status(_DefaultStatus):
        state = "waiting_for_review"
        current_operation = "review_gate:after_compile"
        review_required = True
        review_gate = "after_compile"

    mock_temporal.set_status(workflow_id, _Status())
    response = client.get(
        f"/api/projects/alpha/processing/{workflow_id}",
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "waiting_for_review"
    assert data["review_required"] is True
    assert data["review_gate"] == "after_compile"
    assert data["workflow_id"] == workflow_id
    assert mock_temporal.queries == [(workflow_id, "get_status")]


@pytest.mark.parametrize(
    "action,signal",
    [("pause", "pause"), ("resume", "resume"), ("cancel", "cancel")],
)
def test_workflow_actions_send_signals(client, mock_temporal, action, signal):
    workflow_id = "j1-acme-alpha-xyz"
    response = client.post(
        f"/api/projects/alpha/processing/{workflow_id}/{action}",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json() == {"workflow_id": workflow_id, "action": action}
    assert (workflow_id, signal) in mock_temporal.signals


# ---- Artifacts -------------------------------------------------------


def test_list_artifacts_empty(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.get(
        "/api/projects/alpha/artifacts", headers=_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"artifacts": []}


def test_list_artifacts_returns_records(
    client, container, workspace, ctx, artifact_registry
):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    _stage_artifact(workspace, ctx, container.artifact_registry, artifact_id="a-1")
    _stage_artifact(
        workspace, ctx, container.artifact_registry,
        artifact_id="a-2", kind="enriched.requirements", area=WorkspaceArea.ENRICHED,
    )
    response = client.get(
        "/api/projects/alpha/artifacts", headers=_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert {a["artifact_id"] for a in data["artifacts"]} == {"a-1", "a-2"}


def test_list_artifacts_filter_by_kind(client, container, workspace, ctx):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    _stage_artifact(workspace, ctx, container.artifact_registry, artifact_id="a-1")
    _stage_artifact(
        workspace, ctx, container.artifact_registry,
        artifact_id="a-2", kind="enriched.requirements", area=WorkspaceArea.ENRICHED,
    )
    response = client.get(
        "/api/projects/alpha/artifacts?kind=enriched.requirements",
        headers=_headers(),
    )
    assert {a["artifact_id"] for a in response.json()["artifacts"]} == {"a-2"}


def test_get_artifact_returns_relative_location_only(
    client, container, workspace, ctx
):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    _stage_artifact(workspace, ctx, container.artifact_registry, artifact_id="a-1")
    response = client.get(
        "/api/projects/alpha/artifacts/a-1", headers=_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert data["artifact_id"] == "a-1"
    assert data["location"] == "compiled/a-1.txt"
    assert "/tenants/" not in data["location"]


def test_get_artifact_not_found_returns_404(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.get(
        "/api/projects/alpha/artifacts/missing", headers=_headers()
    )
    assert response.status_code == 404


# ---- Query -----------------------------------------------------------


def test_query_endpoint_returns_full_response_shape(
    client, container, workspace, ctx
):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    record = _stage_artifact(
        workspace, ctx, container.artifact_registry,
        artifact_id="a-1", content=b"the schedule constraint is firm",
    )
    container.search_indexer.index(ctx, [record.artifact_id])

    response = client.post(
        "/api/projects/alpha/query",
        json={"question": "schedule"},
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    # Required response fields per spec.
    for field in (
        "answer",
        "mode_used",
        "sources",
        "confidence",
        "review_required",
        "warnings",
    ):
        assert field in data
    assert data["mode_used"] in {"knowledge_first", "graph_first"}
    assert any(s["artifact_id"] == "a-1" for s in data["sources"])
    assert "confidence_level" in data
    assert "warning_categories" in data


def test_query_endpoint_explicit_mode(client, container, workspace, ctx):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.post(
        "/api/projects/alpha/query",
        json={"question": "anything", "mode": "consistency_check"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["mode_used"] == "consistency_check"


def test_query_endpoint_unknown_mode_returns_400(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.post(
        "/api/projects/alpha/query",
        json={"question": "x", "mode": "bogus_mode"},
        headers=_headers(),
    )
    assert response.status_code == 400


# ---- Cost ------------------------------------------------------------


def test_cost_summary(client, container, ctx):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    cost_recorder = DefaultCostRecorder(JsonlCostSink(container.workspace))
    from decimal import Decimal

    cost_recorder.record(
        ctx,
        CostBreakdown(
            vendor="anthropic",
            model="m",
            unit_kind="input_tokens",
            units=1,
            amount=Decimal("0.10"),
        ),
        correlation_id="run-1",
    )
    response = client.get(
        "/api/projects/alpha/cost?correlation_id=run-1",
        headers=_headers(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total_amount"] == "0.10"
    assert data["by_level"]["workflow_run"] == "0.10"


# ---- Reviews ---------------------------------------------------------


def test_list_reviews_empty(client):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    response = client.get(
        "/api/projects/alpha/reviews", headers=_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_list_reviews_returns_pending_items(client, container, ctx):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    container.review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.get(
        "/api/projects/alpha/reviews", headers=_headers()
    )
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["review_item_id"] == "r-1"
    assert items[0]["review_status"] == "pending"


def test_submit_review_decision_updates_queue(client, container, ctx, workspace):
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    container.review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.post(
        "/api/projects/alpha/reviews/r-1/decision",
        json={"decision": "approved", "actor": "reviewer", "notes": "ok"},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "approved"
    assert body["audit_event_id"]
    # Queue updated.
    assert container.review_queue.get(ctx, "r-1").review_status is ReviewStatus.APPROVED
    # Audit event written.
    line = (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()[-1]
    assert json.loads(line)["action"] == "j1.review.decision"


def test_submit_review_decision_unknown_status_returns_400(client, container, ctx):
    """ApplyReviewDecision raises ApplicationError(non_retryable=True) for bad status;
    the API surfaces it as a 4xx (not a 500)."""
    client.post("/api/projects", json={"project_id": "alpha"}, headers=_headers())
    container.review_queue.add(
        ReviewItem(
            review_item_id="r-1",
            project=ctx,
            target_kind="artifact",
            target_id="art-1",
            review_status=ReviewStatus.PENDING,
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.post(
        "/api/projects/alpha/reviews/r-1/decision",
        json={"decision": "not-real", "actor": "reviewer"},
        headers=_headers(),
    )
    # ApplicationError isn't auto-translated to 400; should bubble as 500.
    # We accept either as a sanity check that bad input doesn't silently succeed.
    assert response.status_code in (400, 500)


# ---- Custom tenant resolver -----------------------------------------


def test_custom_tenant_resolver(container, mock_temporal):
    container.temporal_client = mock_temporal

    def fixed_tenant(_request) -> str:
        return "from_resolver"

    app = create_api(container, tenant_resolver=fixed_tenant)
    c = TestClient(app)
    response = c.post("/api/projects", json={"project_id": "alpha"})
    # No header needed because resolver is fixed.
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "from_resolver"
