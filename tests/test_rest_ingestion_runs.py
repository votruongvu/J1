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


class _StubJobControl:
    """Captures pause/resume/cancel calls without touching Temporal."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # [(action, job_id)]
        self.fail_on: set[str] = set()

    async def pause_job(self, _ctx, job_id: str):
        self.calls.append(("pause", job_id))
        if "pause" in self.fail_on:
            raise RuntimeError("pause signal failed")

        from j1.integration import JobActionResultDTO
        return JobActionResultDTO(job_id=job_id, action="pause")

    async def resume_job(self, _ctx, job_id: str):
        self.calls.append(("resume", job_id))
        if "resume" in self.fail_on:
            raise RuntimeError("resume signal failed")
        from j1.integration import JobActionResultDTO
        return JobActionResultDTO(job_id=job_id, action="resume")

    async def cancel_job(self, _ctx, job_id: str):
        self.calls.append(("cancel", job_id))
        if "cancel" in self.fail_on:
            raise RuntimeError("cancel signal failed")
        from j1.integration import JobActionResultDTO
        return JobActionResultDTO(job_id=job_id, action="cancel")


@pytest.fixture
def job_control() -> _StubJobControl:
    return _StubJobControl()


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store,
    audit_recorder, job_control,
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
        job_control=job_control,
    )


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(ProjectContext(
        tenant_id="acme", project_id="alpha",
    )) / "feedback.jsonl")


@pytest.fixture
def started_jobs() -> list[tuple[str, str, str]]:
    """Captures (project_id, document_id, compiler_kind) per
 job_starter call. Mirrors the pattern from test_rest_adapter.py."""
    return []


@pytest.fixture
def job_starter(started_jobs):
    async def starter(ctx, document_id, body):
        started_jobs.append((ctx.project_id, document_id, body.compiler_kind))
        return f"wf-{document_id}-{len(started_jobs)}"
    return starter


@pytest.fixture
def client(application_facade, workspace, run_store, job_starter, reporter) -> TestClient:
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
    )
    return TestClient(app)


@pytest.fixture
def client_no_reporter(application_facade, workspace, run_store, job_starter) -> TestClient:
    """Client without a progress reporter — used to verify the
 POST /ingestion-runs endpoint still works when the deployment
 hasn't wired the progress surface (records are persisted; no
 progress events are emitted)."""
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        job_starter=job_starter,
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


def test_get_run_derives_current_step_from_audit_when_record_is_empty(
    client, run_store, ctx, reporter,
):
    """Worker activities don't update the run-store record (the
 run-store is API-side state; the worker emits audit events).
 Without server-side derivation the detail endpoint's
 `currentStage` / `currentStep` would stay null for the lifetime
 of the run. This test pins down the round-2 fix: when the run
 record's own `current_*` fields are empty, the GET handler
 backfills them from the most recent `step.*` progress event."""
    now = datetime.now(timezone.utc)
    run_store.upsert(ctx, IngestionRun(
        run_id="run-derived",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=now,
        updated_at=now,
        # `current_*` deliberately left at defaults — simulates the
        # worker not writing back to the run store.
    ))

    reporter.report_run_created(ctx, run_id="run-derived", document_id="doc-1")
    reporter.report_step_started(
        ctx, run_id="run-derived", stage="COMPILE", step="LAYOUT_PREPARATION",
    )
    reporter.report_step_progress(
        ctx, run_id="run-derived", stage="COMPILE",
        step="LAYOUT_PREPARATION", progress_percent=42,
    )

    resp = client.get("/ingestion-runs/run-derived", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["currentStage"] == "COMPILE"
    assert body["currentStep"] == "LAYOUT_PREPARATION"
    assert body["progressPercent"] == 42
    assert body["lastEventType"] == "step.progress"


def test_get_run_keeps_record_values_when_present(
    client, run_store, ctx, reporter,
):
    """When the run record DOES carry `current_*` (e.g. a future
 worker-side writer fills it), the audit-derived values must not
 overwrite them. Only `lastEventType` is always derived because it
 isn't on the record."""
    run_store.upsert(ctx, _make_run("run-pinned"))
    reporter.report_step_started(
        ctx, run_id="run-pinned", stage="ENRICH", step="OTHER_STEP",
    )

    resp = client.get("/ingestion-runs/run-pinned", headers=_HEADERS)
    body = resp.json()["data"]
    # Run-record values win.
    assert body["currentStage"] == "COMPILE"
    assert body["currentStep"] == "LAYOUT_PREPARATION"
    assert body["progressPercent"] == 50
    # But lastEventType is still surfaced.
    assert body["lastEventType"] == "step.started"


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


def test_confirm_transitions_run_from_canonical_assessment_ready_to_running(
    client, run_store, ctx,
):
    """the canonical `assessment_ready` value is an alias of
 the legacy `plan_ready`. /confirm must accept both as valid entry
 states so workers writing canonical values still flip to running."""
    run = _make_run("run-confirm-canonical")
    run.status = RunStatus.ASSESSMENT_READY
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-confirm-canonical/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"
    assert run_store.get(ctx, "run-confirm-canonical").status == RunStatus.RUNNING


def test_list_status_filter_expands_canonical_to_include_legacy(
    client, run_store, ctx,
):
    """`?status=received` must match both runs persisted with the
 canonical `received` value AND legacy `created` runs. Mirrors
 `?status=assessment_ready` matching `plan_ready` too."""
    legacy_run = _make_run("run-legacy-created")
    legacy_run.status = RunStatus.CREATED
    run_store.upsert(ctx, legacy_run)

    canonical_run = _make_run("run-canonical-received")
    canonical_run.status = RunStatus.RECEIVED
    run_store.upsert(ctx, canonical_run)

    # Unrelated run that should NOT match
    other = _make_run("run-other-running")
    other.status = RunStatus.RUNNING
    run_store.upsert(ctx, other)

    resp = client.get(
        "/ingestion-runs?status=received", headers=_HEADERS,
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    run_ids = {it["runId"] for it in items}
    assert "run-legacy-created" in run_ids
    assert "run-canonical-received" in run_ids
    assert "run-other-running" not in run_ids


def test_list_status_filter_legacy_query_also_matches_canonical(
    client, run_store, ctx,
):
    """Symmetric: a FE that still queries with the legacy name must
 still see runs written under the canonical name."""
    legacy_run = _make_run("run-legacy-plan-ready")
    legacy_run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, legacy_run)

    canonical_run = _make_run("run-canonical-assessment-ready")
    canonical_run.status = RunStatus.ASSESSMENT_READY
    run_store.upsert(ctx, canonical_run)

    resp = client.get(
        "/ingestion-runs?status=plan_ready", headers=_HEADERS,
    )
    assert resp.status_code == 200
    run_ids = {it["runId"] for it in resp.json()["data"]["items"]}
    assert "run-legacy-plan-ready" in run_ids
    assert "run-canonical-assessment-ready" in run_ids


# ---- POST /ingestion-runs/{id}/compile (two-phase compile) -------


def test_compile_returns_404_for_unknown_run(client):
    resp = client.post(
        "/ingestion-runs/missing/compile", headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_compile_transitions_run_from_compile_pending_to_running(
    client, run_store, ctx,
):
    run = _make_run("run-compile")
    run.status = RunStatus.COMPILE_PENDING
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-compile/compile", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"

    after = run_store.get(ctx, "run-compile")
    assert after.status == RunStatus.RUNNING
    assert "compileTriggeredAt" in (after.metadata or {}) or "compile_triggered_at" in (after.metadata or {})


def test_compile_is_noop_for_run_not_in_compile_pending(
    client, run_store, ctx,
):
    """Idempotency: re-issuing /compile on a running run returns the
 current status and does not re-flip anything. Same behaviour for
 runs in ASSESSING / PLAN_READY — only COMPILE_PENDING is a valid
 entry state for the trigger."""
    run = _make_run("run-compile-noop")
    run.status = RunStatus.RUNNING
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-compile-noop/compile", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"


def test_compile_invokes_handler_with_ctx_and_run_id(
    application_facade, workspace, run_store, reporter, job_starter, ctx,
):
    calls: list[tuple] = []

    async def handler(c, run_id):
        calls.append((c.tenant_id, c.project_id, run_id))

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
        compile_handler=handler,
    )
    test_client = TestClient(app)

    run = _make_run("run-compile-handler")
    run.status = RunStatus.COMPILE_PENDING
    run_store.upsert(ctx, run)

    resp = test_client.post(
        "/ingestion-runs/run-compile-handler/compile", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert calls == [("acme", "alpha", "run-compile-handler")]


def test_compile_handler_failure_does_not_block_status_flip(
    application_facade, workspace, run_store, reporter, job_starter, ctx,
):
    async def broken_handler(_ctx, _run_id):
        raise RuntimeError("temporal unreachable")

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
        compile_handler=broken_handler,
    )
    test_client = TestClient(app)

    run = _make_run("run-compile-broken")
    run.status = RunStatus.COMPILE_PENDING
    run_store.upsert(ctx, run)

    resp = test_client.post(
        "/ingestion-runs/run-compile-broken/compile", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert run_store.get(ctx, "run-compile-broken").status == RunStatus.RUNNING


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
 field names used by `GET.../events`. The streamed payload must
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


# ---- POST /ingestion-runs --------------------------------------


def test_post_ingestion_run_returns_400_when_tenant_header_missing(
    client,
):
    """The Tenant/Project context contract applies to every endpoint —
 missing X-Tenant-Id is a 400, not a silent default."""
    files = {"file": ("doc.txt", b"hello", "text/plain")}
    resp = client.post(
        "/ingestion-runs", files=files,
        headers={PROJECT_HEADER: "alpha"},  # only project, missing tenant
    )
    assert resp.status_code == 400


def test_post_ingestion_run_returns_400_when_project_header_missing(client):
    files = {"file": ("doc.txt", b"hello", "text/plain")}
    resp = client.post(
        "/ingestion-runs", files=files,
        headers={TENANT_HEADER: "acme"},  # only tenant, missing project
    )
    assert resp.status_code == 400


def test_post_ingestion_run_creates_run_and_starts_workflow(
    client, run_store, ctx, started_jobs, workspace,
):
    """Composite happy path: document registered, run record
 persisted with status=CREATED, workflow started exactly once,
 progress events emitted to the audit log."""
    files = {"file": ("hello.txt", b"hello world", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        data={"compilerKind": "mock"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201

    body = resp.json()["data"]
    assert body["status"] == "created"
    assert body["runId"]
    assert body["documentId"]
    assert body["workflowId"]

    # Run record persisted under the correct (tenant, project) path.
    run = run_store.get(ctx, body["runId"])
    assert run is not None
    assert run.status == RunStatus.CREATED
    assert run.document_id == body["documentId"]

    # Workflow started exactly once with the resolved compiler kind.
    assert len(started_jobs) == 1


def test_post_ingestion_run_emits_run_created_and_document_received(
    client, ctx, workspace,
):
    """When a progress reporter is wired, POST /ingestion-runs
 emits the first two progress events (`run.created` and
 `document.received`) so the SSE stream has content from t=0."""
    files = {"file": ("hello.txt", b"hi", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        data={"compilerKind": "mock"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    run_id = resp.json()["data"]["runId"]

    events_resp = client.get(
        f"/ingestion-runs/{run_id}/events", headers=_HEADERS,
    )
    types = [e["eventType"] for e in events_resp.json()["data"]["events"]]
    assert "run.created" in types
    assert "document.received" in types


def test_post_ingestion_run_works_without_progress_reporter(
    client_no_reporter, run_store, ctx,
):
    """Backwards compat: when no reporter is wired the endpoint
 still creates the run record and starts the workflow — just
 without emitting progress events. This is the migration path
 for deployments that adopt /ingestion-runs without immediately
 wiring the progress surface."""
    files = {"file": ("hello.txt", b"hi", "text/plain")}
    resp = client_no_reporter.post(
        "/ingestion-runs",
        files=files,
        data={"compilerKind": "mock"},
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    run = run_store.get(ctx, resp.json()["data"]["runId"])
    assert run is not None


def test_post_ingestion_run_uses_correlation_id_as_run_id_when_provided(
    client, run_store, ctx,
):
    """Open question default: run_id == correlation_id == workflow_id.
 Caller-supplied correlation_id wins so the audit log + SSE
 cursor + Temporal IDs all share one identifier."""
    files = {"file": ("hello.txt", b"hi", "text/plain")}
    resp = client.post(
        "/ingestion-runs",
        files=files,
        data={
            "correlation_id": "my-correlation-123",
            "compilerKind": "mock",
        },
        headers=_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["runId"] == "my-correlation-123"
    # And the run record persists under that ID.
    assert run_store.get(ctx, "my-correlation-123") is not None


# ---- GET /ingestion-runs (list) -------------------------------


def test_list_ingestion_runs_paginates_and_orders_by_started_desc(
    client, run_store, ctx,
):
    """The list endpoint dedupes by run_id (latest snapshot wins),
 sorts by `startedAt` desc, and paginates the result set. Drives
 the All Runs page so the UI can land on a stable contract before
 the FE list code gets wired."""
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc)
    for i in range(5):
        run_store.upsert(
            ctx,
            IngestionRun(
                run_id=f"r{i}",
                document_id=f"d{i}",
                workflow_id=f"wf-{i}",
                workflow_run_id=None,
                status=RunStatus.RUNNING if i % 2 == 0 else RunStatus.SUCCEEDED,
                started_at=base + timedelta(seconds=i),
                updated_at=base + timedelta(seconds=i),
                metadata={"document_name": f"doc-{i}.pdf"},
            ),
        )

    resp = client.get(
        "/ingestion-runs", headers=_HEADERS, params={"page": 1, "pageSize": 3},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["pageSize"] == 3
    assert [item["runId"] for item in body["items"]] == ["r4", "r3", "r2"]
    # Each item carries the user-facing display fields.
    assert body["items"][0]["documentName"] == "doc-4.pdf"
    assert body["items"][0]["status"] in {"running", "succeeded"}


def test_list_ingestion_runs_carries_mode_and_policy_from_metadata(
    client, run_store, ctx,
):
    """`mode` / `policy` come from the run's metadata bag (populated
 by the upload handler). Listing items must surface them so the
 All Runs row meta line ("STANDARD · auto") matches the run-detail
 page header."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    run_store.upsert(ctx, IngestionRun(
        run_id="r1", document_id="d1", workflow_id="wf",
        workflow_run_id=None, status=RunStatus.RUNNING,
        started_at=now, updated_at=now,
        metadata={
            "document_name": "earnings.pdf",
            "mode": "FAST",
            "policy": "redact-pii",
        },
    ))
    resp = client.get("/ingestion-runs", headers=_HEADERS)
    item = resp.json()["data"]["items"][0]
    assert item["mode"] == "FAST"
    assert item["policy"] == "redact-pii"


def test_list_ingestion_runs_filters_by_status_repeats(client, run_store, ctx):
    """Repeated `?status=` query params narrow the result set; the
 FE quick-filter chips drive this."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for i, status in enumerate(
        [RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.RUNNING],
    ):
        run_store.upsert(
            ctx,
            IngestionRun(
                run_id=f"r{i}", document_id="d", workflow_id="wf",
                workflow_run_id=None, status=status,
                started_at=now, updated_at=now,
            ),
        )
    resp = client.get(
        "/ingestion-runs",
        headers=_HEADERS,
        params=[("status", "running"), ("status", "failed")],
    )
    assert resp.status_code == 200
    statuses = {item["status"] for item in resp.json()["data"]["items"]}
    assert statuses == {"running", "failed"}


def test_list_ingestion_runs_q_filter_matches_run_id_or_document_name(
    client, run_store, ctx,
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    run_store.upsert(ctx, IngestionRun(
        run_id="quarterly-Q4", document_id="d1", workflow_id="wf",
        workflow_run_id=None, status=RunStatus.SUCCEEDED,
        started_at=now, updated_at=now,
        metadata={"document_name": "earnings.pdf"},
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id="other", document_id="d2", workflow_id="wf",
        workflow_run_id=None, status=RunStatus.SUCCEEDED,
        started_at=now, updated_at=now,
        metadata={"document_name": "earnings-summary.pdf"},
    ))
    # Match on document name substring.
    resp = client.get(
        "/ingestion-runs", headers=_HEADERS, params={"q": "earnings"},
    )
    ids = {item["runId"] for item in resp.json()["data"]["items"]}
    assert ids == {"quarterly-Q4", "other"}
    # Match on run_id substring.
    resp = client.get("/ingestion-runs", headers=_HEADERS, params={"q": "Q4"})
    ids = {item["runId"] for item in resp.json()["data"]["items"]}
    assert ids == {"quarterly-Q4"}


def test_list_ingestion_runs_returns_503_when_store_not_configured(
    application_facade, workspace,
):
    app = create_rest_api(application_facade, workspace=workspace)
    test_client = TestClient(app)
    resp = test_client.get("/ingestion-runs", headers=_HEADERS)
    assert resp.status_code == 503


# ---- POST /ingestion-runs/{id}/confirm — extended behaviour ----


def test_confirm_emits_plan_confirmed_progress_event(
    client, run_store, ctx,
):
    """Confirming a parked run must surface a `plan.confirmed` event
 in the timeline so the SSE stream and the events-history
 endpoint show the operator action — no need to poll the run
 record to detect that confirmation happened."""
    run = _make_run("run-emits-confirmed")
    run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-emits-confirmed/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200

    events = client.get(
        "/ingestion-runs/run-emits-confirmed/events", headers=_HEADERS,
    ).json()["data"]["events"]
    assert any(e["eventType"] == "plan.confirmed" for e in events)


def test_confirm_persists_confirmed_at_and_confirmed_by(
    client, run_store, ctx,
):
    """Audit-trail metadata: who confirmed, when. Future workflow
 integrations can compare these against `started_at` to compute
 operator response time."""
    run = _make_run("run-meta")
    run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, run)
    resp = client.post(
        "/ingestion-runs/run-meta/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200
    after = run_store.get(ctx, "run-meta")
    assert "confirmed_at" in after.metadata
    assert "confirmed_by" in after.metadata


def test_confirm_invokes_handler_with_ctx_and_run_id(
    application_facade, workspace, run_store, reporter, job_starter, ctx,
):
    """The injected `confirm_handler` is the seam Temporal-signal
 integrations plug into. Verify the REST adapter calls it with
 (ctx, run_id) on a real confirmation transition."""
    calls: list[tuple] = []

    async def handler(c, run_id):
        calls.append((c.tenant_id, c.project_id, run_id))

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
        confirm_handler=handler,
    )
    test_client = TestClient(app)

    run = _make_run("run-handler")
    run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, run)

    resp = test_client.post(
        "/ingestion-runs/run-handler/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert calls == [("acme", "alpha", "run-handler")]


def test_confirm_handler_failure_does_not_block_status_flip(
    application_facade, workspace, run_store, reporter, job_starter, ctx,
):
    """If the downstream signal raises, the run is still marked
 RUNNING in the store — confirmation is acknowledged at the REST
 boundary even when the workflow couldn't be reached."""

    async def broken_handler(_ctx, _run_id):
        raise RuntimeError("temporal unreachable")

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
        confirm_handler=broken_handler,
    )
    test_client = TestClient(app)

    run = _make_run("run-broken-handler")
    run.status = RunStatus.PLAN_READY
    run_store.upsert(ctx, run)

    resp = test_client.post(
        "/ingestion-runs/run-broken-handler/confirm", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert run_store.get(ctx, "run-broken-handler").status == RunStatus.RUNNING


def test_confirm_does_not_invoke_handler_for_already_running_run(
    application_facade, workspace, run_store, reporter, job_starter, ctx,
):
    """Idempotency: re-issuing confirm on a running run is a noop —
 no signal is forwarded, no event is re-emitted."""
    calls: list[tuple] = []

    async def handler(_c, _run_id):
        calls.append(("called",))

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        progress_reporter=reporter,
        job_starter=job_starter,
        confirm_handler=handler,
    )
    test_client = TestClient(app)

    run = _make_run("run-already-running")
    run.status = RunStatus.RUNNING
    run_store.upsert(ctx, run)

    test_client.post(
        "/ingestion-runs/run-already-running/confirm", headers=_HEADERS,
    )
    assert calls == []


# ---- camelCase metadata on the wire ---------------------------


def test_run_failed_event_metadata_uses_camel_case(client, ctx, reporter):
    """Backend reporter writes `failure_code` / `failure_message` to
 the audit payload (Python convention). The wire format for the
 `metadata` bag must be camelCase so the frontend doesn't have to
 handle two casings — the audit-to-record translator camelizes
 keys at serialisation time."""
    reporter.report_run_failed(
        ctx, run_id="run-fail",
        failure_code="J1_INGEST_RUN_FAILED",
        failure_message="graph build failed",
    )
    resp = client.get(
        "/ingestion-runs/run-fail/events", headers=_HEADERS,
    )
    failed = next(
        e for e in resp.json()["data"]["events"]
        if e["eventType"] == "run.failed"
    )
    # Metadata bag is camelCase — no snake_case slipped through.
    assert failed["metadata"]["failureCode"] == "J1_INGEST_RUN_FAILED"
    assert failed["metadata"]["failureMessage"] == "graph build failed"
    assert "failure_code" not in failed["metadata"]
    assert "failure_message" not in failed["metadata"]


def test_step_failed_event_metadata_uses_camel_case(client, ctx, reporter):
    reporter.report_step_failed(
        ctx, run_id="run-step-fail", stage="GRAPH", step="graph.build",
        error_type="GraphBuildError", error_message="duplicate node id",
        retryable=False,
    )
    resp = client.get(
        "/ingestion-runs/run-step-fail/events", headers=_HEADERS,
    )
    failed = next(
        e for e in resp.json()["data"]["events"]
        if e["eventType"] == "step.failed"
    )
    assert failed["metadata"]["errorType"] == "GraphBuildError"
    assert failed["metadata"]["errorMessage"] == "duplicate node id"
    assert failed["metadata"]["retryable"] is False


# ---- run.cancelled SSE termination ------------------------------


def test_sse_stream_closes_on_run_cancelled(client, ctx, reporter):
    """Run cancellation must close the SSE generator like
 run.completed / run.failed do — otherwise the client idles
 against an in-flight stream until the 1h max-duration timeout."""
    reporter.report_run_created(ctx, run_id="run-cxl", document_id="doc-1")
    reporter.report_run_cancelled(
        ctx, run_id="run-cxl", reason="operator-cancelled",
    )
    with client.stream(
        "GET", "/ingestion-runs/run-cxl/events/stream", headers=_HEADERS,
    ) as resp:
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"run.cancelled" in body:
                break
    assert b"event: run.cancelled" in body
    # The reason payload travels in the metadata bag (camelCase).
    cancelled = next(
        line for line in body.decode("utf-8").splitlines()
        if line.startswith("data: ") and "run.cancelled" in line
    )
    payload = json.loads(cancelled[len("data: "):])
    assert payload["metadata"]["reason"] == "operator-cancelled"


def test_sse_stream_closes_on_human_review_required(client, ctx, reporter):
    """Same termination guarantee for the human-review terminal."""
    reporter.report_run_created(ctx, run_id="run-rev", document_id="doc-1")
    reporter.report_human_review_required(
        ctx, run_id="run-rev", gate="manual-review",
    )
    with client.stream(
        "GET", "/ingestion-runs/run-rev/events/stream", headers=_HEADERS,
    ) as resp:
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"human_review.required" in body:
                break
    assert b"event: human_review.required" in body


# ---- POST /ingestion-runs/{id}/{pause|resume|cancel} ---------


def test_pause_flips_run_record_to_paused_and_signals_workflow(
    client, run_store, ctx, job_control,
):
    """Pause endpoint must do BOTH: update the run record's status
 (so the FE polling sees PAUSED immediately) AND forward the
 Temporal signal so the workflow stops at the next gate."""
    run = _make_run("run-pause")
    run.status = RunStatus.RUNNING
    run.workflow_id = "wf-pause"
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-pause/pause", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["runId"] == "run-pause"
    assert body["action"] == "pause"
    assert body["status"] == "paused"
    assert body["message"]
    assert body["updatedAt"]

    after = run_store.get(ctx, "run-pause")
    assert after.status == RunStatus.PAUSED
    # Forwarded the signal using the run's workflow_id (not the run_id).
    assert ("pause", "wf-pause") in job_control.calls


def test_pause_uses_run_id_when_workflow_id_missing(
    client, run_store, ctx, job_control,
):
    """For per-document workflows the workflow_id == run_id; if the
 run record doesn't carry workflow_id we fall back to the run_id
 so the signal still routes."""
    run = _make_run("run-pause-fallback")
    run.status = RunStatus.RUNNING
    run.workflow_id = ""
    run_store.upsert(ctx, run)

    resp = client.post(
        "/ingestion-runs/run-pause-fallback/pause", headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert ("pause", "run-pause-fallback") in job_control.calls


def test_pause_409_when_run_is_terminal(client, run_store, ctx, job_control):
    """Cannot pause a terminal run — 409 + the signal is not sent."""
    run = _make_run("run-done")
    run.status = RunStatus.SUCCEEDED
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-done/pause", headers=_HEADERS)
    assert resp.status_code == 409
    assert job_control.calls == []


def test_pause_404_for_unknown_run(client, job_control):
    resp = client.post("/ingestion-runs/missing/pause", headers=_HEADERS)
    assert resp.status_code == 404
    assert job_control.calls == []


def test_resume_flips_paused_back_to_running(
    client, run_store, ctx, job_control,
):
    run = _make_run("run-resume")
    run.status = RunStatus.PAUSED
    run.workflow_id = "wf-resume"
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-resume/resume", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "running"
    after = run_store.get(ctx, "run-resume")
    assert after.status == RunStatus.RUNNING
    assert ("resume", "wf-resume") in job_control.calls


def test_resume_409_when_run_is_running(client, run_store, ctx, job_control):
    """Resume only legal from PAUSED — running/idle should 409 so
 the operator notices the misclick."""
    run = _make_run("run-running")
    run.status = RunStatus.RUNNING
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-running/resume", headers=_HEADERS)
    assert resp.status_code == 409
    assert job_control.calls == []


def test_cancel_flips_run_record_to_cancelling_and_signals(
    client, run_store, ctx, job_control,
):
    """Cancel is one-way: status flips to CANCELLING immediately so
 the UI shows the operator action; the workflow's terminal exit
 later flips it to CANCELLED."""
    run = _make_run("run-cancel")
    run.status = RunStatus.RUNNING
    run.workflow_id = "wf-cancel"
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-cancel/cancel", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "cancelling"
    after = run_store.get(ctx, "run-cancel")
    assert after.status == RunStatus.CANCELLING
    assert ("cancel", "wf-cancel") in job_control.calls


def test_cancel_legal_from_paused(client, run_store, ctx, job_control):
    """A paused run can still be cancelled — operators may decide to
 abandon a paused run rather than resume it."""
    run = _make_run("run-paused")
    run.status = RunStatus.PAUSED
    run_store.upsert(ctx, run)

    resp = client.post("/ingestion-runs/run-paused/cancel", headers=_HEADERS)
    assert resp.status_code == 200
    after = run_store.get(ctx, "run-paused")
    assert after.status == RunStatus.CANCELLING


def test_control_signal_failure_keeps_run_record_flipped(
    client, run_store, ctx, job_control,
):
    """If the Temporal signal fails (worker disconnected, network
 blip), the REST call still succeeds because the run-record
 update IS the authoritative FE-visible flip. Operators see the
 failure in worker logs; the FE shows the requested status."""
    run = _make_run("run-signal-fail")
    run.status = RunStatus.RUNNING
    run.workflow_id = "wf-fail"
    run_store.upsert(ctx, run)
    job_control.fail_on = {"pause"}

    resp = client.post(
        "/ingestion-runs/run-signal-fail/pause", headers=_HEADERS,
    )
    assert resp.status_code == 200
    after = run_store.get(ctx, "run-signal-fail")
    assert after.status == RunStatus.PAUSED


def test_control_persists_actor_metadata(
    client, run_store, ctx, job_control,
):
    """For the audit trail, every control action records who and
 when. The FE doesn't render this today, but cost/compliance
 tooling reads it from the run record's metadata bag."""
    run = _make_run("run-meta-pause")
    run.status = RunStatus.RUNNING
    run_store.upsert(ctx, run)

    client.post("/ingestion-runs/run-meta-pause/pause", headers=_HEADERS)
    after = run_store.get(ctx, "run-meta-pause")
    assert "pause_at" in after.metadata
    assert "pause_by" in after.metadata
