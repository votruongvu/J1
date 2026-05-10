"""End-to-end tests for the POST /ingestion-runs/{id}/resume-from-checkpoint
endpoint. Verifies:

  * 200 happy path: terminal run with snapshot → new run dispatched,
    response carries the resume metadata + reused step list.
  * 404 for unknown run.
  * 409 when the original is still active.
  * 412 with structured diff when settings drifted.
  * 412 (no diff) when snapshot is absent (legacy run / cancelled).

The starter is a stub that records the body it receives so we can
assert the workflow request carries the right resume context.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.ingestion_review import IngestionResultReviewService
from j1.integration.services import (
    ApplicationFacade,
    CitationLookupService,
    DocumentIngestionService,
    EventPublisherService,
    FeedbackService,
    RetrievalService,
    SourceLookupService,
)
from j1.projects.context import ProjectContext
from j1.runs import (
    AuditProgressReporter,
    IngestionRun,
    JsonlIngestionRunStore,
    RunStatus,
)
from j1.runs.resume import compute_settings_hash


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}

_PRIOR_SETTINGS = {
    "compiler_kind": "raganything",
    "enricher_kind": "composite_enricher",
    "graph_builder_kind": "lightrag_graph",
    "indexer_kind": "sqlite_search",
    "planner_enabled": True,
    "policy": "auto",
    "domain_override": None,
    "workspace_default_domain": None,
    "failure_policy": "fail_fast",
}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def reporter(workspace) -> AuditProgressReporter:
    return AuditProgressReporter(DefaultAuditRecorder(JsonlAuditSink(workspace)))


@pytest.fixture
def review_service(run_store, artifact_registry, workspace) -> IngestionResultReviewService:
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(ProjectContext(
        tenant_id="acme", project_id="alpha",
    )) / "feedback.jsonl")


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store, audit_recorder,
):
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=FeedbackService(feedback_store, audit_recorder),
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def starter_calls():
    """Captures every body the starter receives so tests can assert
    the resume context was threaded into the workflow request."""
    return []


@pytest.fixture
def stub_starter(starter_calls):
    async def _start(ctx, document_id, body) -> str:
        starter_calls.append({
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "document_id": document_id,
            "resume_of": getattr(body, "resume_of", None),
            "resume_completed_steps": getattr(body, "resume_completed_steps", ()),
            "resume_artifact_ids": getattr(body, "resume_artifact_ids", ()),
            "rebuild_index_only": getattr(body, "rebuild_index_only", False),
            "indexer_kind": getattr(body, "indexer_kind", None),
            "correlation_id": body.correlation_id,
        })
        suffix = (
            "rebuild" if getattr(body, "rebuild_index_only", False)
            else "resume"
        )
        return f"wf-{document_id}-{suffix}-{body.correlation_id}"
    return _start


@pytest.fixture
def client(
    application_facade, workspace, run_store, review_service, stub_starter,
) -> TestClient:
    from j1.integration.dto import ProcessingCapabilities
    capabilities = ProcessingCapabilities(
        default_compiler_kind="raganything",
        compiler_kinds=frozenset({"raganything"}),
        enricher_kinds=frozenset({"composite_enricher"}),
        graph_builder_kinds=frozenset({"lightrag_graph"}),
        indexer_kinds=frozenset({"sqlite_search"}),
    )
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        job_starter=stub_starter,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


def _seed_terminal_run(
    run_store,
    *,
    run_id: str = "run-prior",
    document_id: str = "doc-A",
    status: RunStatus = RunStatus.FAILED,
    completed_steps: list[str] | None = None,
    settings: dict | None = None,
    snapshot_present: bool = True,
):
    """Drop a terminal run with (optionally) a resume snapshot into
    the JSONL store. Mirrors what the workflow's `_emit_run_terminal`
    + `_persist_run_terminal` would write."""
    snap_settings = settings or _PRIOR_SETTINGS
    metadata: dict = {
        "policy": "auto",
        "mode": "STANDARD",
        "document_name": "doc-A.pdf",
    }
    if snapshot_present:
        metadata["resume_snapshot"] = {
            "settings_hash": compute_settings_hash(snap_settings),
            "settings_snapshot": snap_settings,
            "completed_steps": list(completed_steps or ["compile", "enrich"]),
            "failed_steps": ["graph"],
            "produced_artifact_ids": ["a-compile", "a-enrich"],
            "produced_artifact_kinds": ["chunk", "enriched.tables"],
            "snapshot_at": "2026-05-10T12:00:00+00:00",
            "failure_code": "REQUIRED_STEP_FAILED",
            "failure_message": "graph failed",
        }
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    run = IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf-prior",
        workflow_run_id="wfr-prior",
        status=status,
        started_at=now,
        updated_at=now + timedelta(minutes=2),
        completed_at=now + timedelta(minutes=2),
        metadata=metadata,
    )
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    run_store.upsert(ctx, run)
    return run


def _seed_document(registry, *, document_id: str = "doc-A"):
    """Register a stub document so SourceLookupService can resolve it."""
    from j1.documents.models import DocumentRecord
    from j1.jobs.status import ProcessingStatus
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1024,
        checksum=f"h-{document_id}",
        status=ProcessingStatus.PENDING,
        created_at=now,
    ))


def test_resume_endpoint_dispatches_new_run_with_carry_forward(
    client, run_store, registry, starter_calls,
):
    """Happy path: terminal run with snapshot + matching settings →
    201/200 + new run id + carry-forward state on the workflow request."""
    _seed_terminal_run(run_store, completed_steps=["compile", "enrich"])
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/resume-from-checkpoint",
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["originalRunId"] == "run-prior"
    assert body["documentId"] == "doc-A"
    assert body["resumeRunId"] != "run-prior"
    assert body["resumedSteps"] == ["enrich"]  # graph wasn't completed
    assert body["carryForwardArtifactCount"] == 2
    # Starter received the resume context — the workflow will skip
    # the LLM-cost stages that completed.
    assert len(starter_calls) == 1
    call = starter_calls[0]
    assert call["resume_of"] == "run-prior"
    assert call["resume_completed_steps"] == ("enrich",)
    assert tuple(call["resume_artifact_ids"]) == ("a-compile", "a-enrich")
    # New run record persisted with `resume_of` lineage.
    new_run = run_store.get(
        ProjectContext(tenant_id="acme", project_id="alpha"),
        body["resumeRunId"],
    )
    assert new_run is not None
    assert new_run.metadata.get("resume_of") == "run-prior"
    assert new_run.metadata.get("resumed_steps") == ["enrich"]


def test_resume_endpoint_404_for_unknown_run(client):
    resp = client.post(
        "/ingestion-runs/missing-run/resume-from-checkpoint",
        headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_resume_endpoint_409_when_run_still_active(client, run_store, registry):
    _seed_terminal_run(run_store, status=RunStatus.RUNNING)
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/resume-from-checkpoint",
        headers=_HEADERS,
    )
    assert resp.status_code == 409


def test_resume_endpoint_412_when_snapshot_missing(client, run_store, registry):
    """A FAILED run with no resume_snapshot (legacy / cancelled) →
    operator must full-reindex instead. Surfaces as 412."""
    _seed_terminal_run(run_store, snapshot_present=False)
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/resume-from-checkpoint",
        headers=_HEADERS,
    )
    assert resp.status_code == 412


def test_resume_endpoint_412_with_diff_when_settings_drifted(
    client, run_store, registry,
):
    """Endpoint resolves candidate settings from the deployment's
    `processing_capabilities` (current registered kinds). When the
    prior snapshot claims a kind the current deployment no longer
    registers, settings_diff fires and the response carries a
    structured diff so the FE can render exactly what changed."""
    # Snapshot claims `enricher_kind="ancient_enricher"` but the test
    # client's capabilities only register `composite_enricher` — the
    # candidate resolution can't fall back to the snapshot's value
    # because the snapshot field IS what we need to compare against.
    drifted_snapshot = {**_PRIOR_SETTINGS, "enricher_kind": "ancient_enricher"}
    _seed_terminal_run(run_store, settings=drifted_snapshot)
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/resume-from-checkpoint",
        headers=_HEADERS,
    )
    assert resp.status_code == 412, resp.text
    body = resp.json()
    err = body.get("error") or {}
    assert err.get("code") == "RESUME_INCOMPATIBLE"
    diff = (err.get("details") or {}).get("diff") or {}
    assert "enricher_kind" in diff
    assert diff["enricher_kind"]["before"] == "ancient_enricher"
    assert diff["enricher_kind"]["after"] == "composite_enricher"


# ---- Rebuild index endpoint --------------------------------------


def _seed_terminal_run_with_chunks(run_store, *, run_id="run-prior"):
    """Seed a SUCCEEDED run whose snapshot includes chunk artifacts —
    the rebuild-index endpoint reads `produced_artifact_ids` filtered
    by `chunk` kind."""
    snap = {
        "settings_hash": compute_settings_hash(_PRIOR_SETTINGS),
        "settings_snapshot": _PRIOR_SETTINGS,
        "completed_steps": ["compile", "enrich", "graph", "index"],
        "failed_steps": [],
        "produced_artifact_ids": [
            "chunk-1", "chunk-2", "chunk-3", "graph-1",
        ],
        "produced_artifact_kinds": [
            "chunk", "chunk", "chunk", "graph_json",
        ],
        "snapshot_at": "2026-05-10T12:00:00+00:00",
    }
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    run = IngestionRun(
        run_id=run_id,
        document_id="doc-A",
        workflow_id="wf-prior",
        workflow_run_id="wfr-prior",
        status=RunStatus.SUCCEEDED,
        started_at=now,
        updated_at=now + timedelta(minutes=2),
        completed_at=now + timedelta(minutes=2),
        metadata={
            "policy": "auto", "mode": "STANDARD",
            "document_name": "doc-A.pdf",
            "resume_snapshot": snap,
        },
    )
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    run_store.upsert(ctx, run)


def test_rebuild_index_endpoint_dispatches_index_only_run(
    client, run_store, registry, starter_calls,
):
    """Happy path: terminal run with chunks → 200 + new run +
    `rebuild_index_only=True` on the workflow request +
    chunk-only carry forward."""
    _seed_terminal_run_with_chunks(run_store)
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/rebuild-index", headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["originalRunId"] == "run-prior"
    assert body["rebuildRunId"] != "run-prior"
    assert body["carryForwardChunkCount"] == 3
    assert body["indexerKind"] == "sqlite_search"
    # The workflow received the rebuild flag + chunk-only carry.
    assert len(starter_calls) == 1
    call = starter_calls[0]
    assert call["rebuild_index_only"] is True
    assert tuple(call["resume_artifact_ids"]) == ("chunk-1", "chunk-2", "chunk-3")
    assert call["indexer_kind"] == "sqlite_search"
    # New run record records the lineage so the FE can render the
    # relationship without a follow-up call.
    new_run = run_store.get(
        ProjectContext(tenant_id="acme", project_id="alpha"),
        body["rebuildRunId"],
    )
    assert new_run is not None
    assert new_run.metadata.get("rebuild_of") == "run-prior"


def test_rebuild_index_endpoint_404_for_unknown_run(client):
    resp = client.post(
        "/ingestion-runs/missing/rebuild-index", headers=_HEADERS,
    )
    assert resp.status_code == 404


def test_rebuild_index_endpoint_409_when_run_active(client, run_store, registry):
    _seed_terminal_run(run_store, status=RunStatus.RUNNING)
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/rebuild-index", headers=_HEADERS,
    )
    assert resp.status_code == 409


def test_rebuild_index_endpoint_412_when_no_chunks(client, run_store, registry):
    """A run that produced no chunks (snapshot has only graph
    artifacts) — nothing to re-index. 412 + actionable message
    pointing at full-reindex."""
    snap = {
        "settings_hash": compute_settings_hash(_PRIOR_SETTINGS),
        "settings_snapshot": _PRIOR_SETTINGS,
        "completed_steps": ["compile"],
        "failed_steps": [],
        "produced_artifact_ids": ["graph-1"],
        "produced_artifact_kinds": ["graph_json"],
        "snapshot_at": "2026-05-10T12:00:00+00:00",
    }
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    run_store.upsert(
        ProjectContext(tenant_id="acme", project_id="alpha"),
        IngestionRun(
            run_id="run-prior",
            document_id="doc-A",
            workflow_id="wf-prior",
            workflow_run_id="wfr-prior",
            status=RunStatus.SUCCEEDED,
            started_at=now,
            updated_at=now,
            completed_at=now,
            metadata={
                "policy": "auto", "mode": "STANDARD",
                "document_name": "doc-A.pdf",
                "resume_snapshot": snap,
            },
        ),
    )
    _seed_document(registry)
    resp = client.post(
        "/ingestion-runs/run-prior/rebuild-index", headers=_HEADERS,
    )
    assert resp.status_code == 412
