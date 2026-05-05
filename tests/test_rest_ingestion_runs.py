"""End-to-end tests for the user-facing /ingestion-runs/* endpoints.

Covers:
  * `GET /ingestion-runs/{id}` — status snapshot
  * `GET /ingestion-runs/{id}/plan` — execution plan view
  * `GET /ingestion-runs/{id}/events` — historical progress events
  * `GET /ingestion-runs/{id}/events/stream` — SSE shape
  * `POST /ingestion-runs/{id}/confirm` — confirmation transition

The endpoints sit alongside `/ingestion-jobs/*` (technical surface)
without breaking it.
"""

from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.config.settings import Settings
from j1.projects.context import ProjectContext
from j1.runs import (
    AuditProgressReporter,
    IngestionRun,
    JsonlIngestionRunStore,
    RunStatus,
)
from j1.workspace.resolver import WorkspaceResolver


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def reporter(workspace) -> AuditProgressReporter:
    return AuditProgressReporter(DefaultAuditRecorder(JsonlAuditSink(workspace)))


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store,
    audit_recorder,
):
    """Minimal facade for the run-related endpoints. Mirrors the
    constructor shape used in `test_rest_adapter.py` but skips the
    search / answer / temporal services we don't exercise here."""
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        FeedbackService, RetrievalService, SourceLookupService,
    )

    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(ProjectContext(
        tenant_id="acme", project_id="alpha",
    )) / "feedback.jsonl")


@pytest.fixture
def client(application_facade, workspace, run_store) -> TestClient:
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
    )
    return TestClient(app)


# ---- GET /ingestion-runs/{id} ------------------------------------


def _make_run(run_id: str = "run-1") -> IngestionRun:
    now = datetime.now(timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id="wfr-1",
        status=RunStatus.RUNNING,
        started_at=now,
        updated_at=now,
        current_stage="COMPILE",
        current_step="LAYOUT_PREPARATION",
        progress_percent=50,
    )


def test_get_run_returns_404_for_unknown_run(client):
    resp = client.get("/ingestion-runs/missing", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_run_returns_status_snapshot(client, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    resp = client.get("/ingestion-runs/run-1", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["runId"] == "run-1"
    assert body["status"] == "running"
    assert body["currentStage"] == "COMPILE"
    assert body["progressPercent"] == 50


def test_get_run_returns_503_when_store_not_configured(application_facade, workspace):
    """The endpoint degrades gracefully when no run store is wired —
    deployments that don't use the runs surface aren't required to."""
    app = create_rest_api(application_facade, workspace=workspace)
    test_client = TestClient(app)
    resp = test_client.get("/ingestion-runs/run-1", headers=_HEADERS)
    assert resp.status_code == 503


# ---- GET /ingestion-runs/{id}/events --------------------------


def test_get_run_events_returns_progress_entries_only(client, ctx, reporter):
    """Only `j1.progress.*` audit entries with matching correlation_id
    surface as ProgressEvents — other audit actions stay invisible to
    the runs surface."""
    reporter.report_run_created(ctx, run_id="run-2", document_id="doc-x")
    reporter.report_step_started(
        ctx, run_id="run-2", stage="COMPILE", step="parse",
    )
    reporter.report_step_progress(
        ctx, run_id="run-2", stage="COMPILE", step="parse",
        progress_percent=50, current=22, total=44,
        message="Layout: 22/44 pages", engine="MinerU",
    )

    resp = client.get("/ingestion-runs/run-2/events", headers=_HEADERS)
    assert resp.status_code == 200
    events = resp.json()["data"]["events"]
    types = [e["eventType"] for e in events]
    assert "run.created" in types
    assert "step.started" in types
    assert "step.progress" in types
    progress = next(e for e in events if e["eventType"] == "step.progress")
    assert progress["progressPercent"] == 50
    assert progress["current"] == 22
    assert progress["total"] == 44
    assert progress["engine"] == "MinerU"


def test_get_run_events_filters_unrelated_runs(client, ctx, reporter):
    """Events for run B must not appear in run A's timeline — the
    correlation_id filter is what makes the runs view sane in a
    workspace with many concurrent ingestions."""
    reporter.report_run_created(ctx, run_id="run-A", document_id="doc-A")
    reporter.report_run_created(ctx, run_id="run-B", document_id="doc-B")

    resp = client.get("/ingestion-runs/run-A/events", headers=_HEADERS)
    events = resp.json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["runId"] == "run-A"


# ---- GET /ingestion-runs/{id}/plan -----------------------------


def test_get_run_plan_returns_404_when_no_plan_recorded(client):
    resp = client.get("/ingestion-runs/no-plan/plan", headers=_HEADERS)
    assert resp.status_code == 404


def test_get_run_plan_returns_execution_plan_shape(client, ctx, reporter):
    """The plan endpoint must reshape the latest `plan.generated`
    audit payload into the frontend's ExecutionPlan record with
    per-step decisions."""
    plan_payload = {
        "document_id": "doc-1",
        "mode": "TEXT_ONLY",
        "policy": "auto",
        "confidence": 0.95,
        "estimated_cost_level": "low",
        "fast_llm_used": False,
        "warnings": [],
        "steps": [
            {
                "name": "compile", "step_id": "compile", "stage": "COMPILE",
                "decision": "RUN", "required": True, "source": "planner",
                "dependency_step_ids": [], "estimated_cost_tier": "MEDIUM",
                "risk_level": "low",
            },
            {
                "name": "graph", "step_id": "graph", "stage": "GRAPH",
                "decision": "SKIP", "required": False, "source": "planner",
                "reason": "TEXT_ONLY mode",
                "dependency_step_ids": ["compile", "enrich"],
                "estimated_cost_tier": "HIGH", "risk_level": "low",
            },
        ],
        "profile": {"extension": ".txt", "page_count": 1},
    }
    reporter.report_plan_generated(ctx, run_id="run-3", plan_payload=plan_payload)

    resp = client.get("/ingestion-runs/run-3/plan", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["mode"] == "TEXT_ONLY"
    assert body["confidence"] == 0.95
    decisions = {s["stepId"]: s["decision"] for s in body["steps"]}
    assert decisions == {"compile": "RUN", "graph": "SKIP"}
    graph = next(s for s in body["steps"] if s["stepId"] == "graph")
    assert graph["reason"] == "TEXT_ONLY mode"


# ---- POST /ingestion-runs/{id}/confirm -----------------------


def test_confirm_transitions_run_from_plan_ready_to_running(
    client, run_store, ctx,
):
    run = _make_run("run-confirm")
    run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-confirm/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"

    after = run_store.get(ctx, "run-confirm")
    assert after.status == RunStatus.RUNNING


def test_confirm_is_noop_for_already_running_run(client, run_store, ctx):
    run = _make_run("run-already")
    run.status = RunStatus.RUNNING
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-already/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200  # idempotent
    assert resp.json()["data"]["status"] == "running"


# ---- GET /ingestion-runs/{id}/events/stream (SSE) -------------


def test_sse_stream_emits_text_event_stream_content_type(client, ctx, reporter):
    """SSE response must have the right content type so browsers /
    EventSource client libs accept it."""
    reporter.report_run_created(ctx, run_id="run-sse", document_id="doc-1")
    reporter.report_run_completed(
        ctx, run_id="run-sse", final_status="succeeded",
    )

    with client.stream(
        "GET", "/ingestion-runs/run-sse/events/stream", headers=_HEADERS,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # Read until the run.completed event closes the stream.
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"run.completed" in body:
                break

    assert b"id: " in body                 # event-id resume cursor
    assert b"event: run.created" in body
    assert b"event: run.completed" in body
    # Each data: line should be valid JSON.
    for line in body.decode("utf-8").splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            assert "eventId" in payload
            assert "runId" in payload
            assert "eventType" in payload


def test_sse_stream_data_payload_uses_camel_case(client, ctx, reporter):
    """Frontend code consuming the stream expects the same camelCase
    field names used by `GET .../events`. The streamed payload must
    match — operators shouldn't have to handle two casings."""
    reporter.report_run_created(ctx, run_id="run-camel", document_id="doc-1")
    reporter.report_step_progress(
        ctx, run_id="run-camel", stage="COMPILE", step="LAYOUT_PREPARATION",
        progress_percent=50, current=22, total=44, engine="MinerU",
    )
    reporter.report_run_completed(
        ctx, run_id="run-camel", final_status="succeeded",
    )

    with client.stream(
        "GET", "/ingestion-runs/run-camel/events/stream", headers=_HEADERS,
    ) as resp:
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"run.completed" in body:
                break

    # Find the step.progress data line.
    lines = body.decode("utf-8").splitlines()
    progress_data = next(
        (json.loads(l[len("data: "):]) for l in lines
         if l.startswith("data: ") and "step.progress" in l),
        None,
    )
    assert progress_data is not None
    assert "progressPercent" in progress_data
    assert "eventId" in progress_data
    assert "runId" in progress_data
    # No snake_case slipped through.
    assert "progress_percent" not in progress_data
    assert "event_id" not in progress_data
