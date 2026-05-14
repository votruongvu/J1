"""Verifies the `j1.ops.*` audit events emitted by the operator-
facing endpoints (soft-delete, purge, batch dispatch).

Each test invokes the endpoint, then reads `events.jsonl` and
asserts on the action string + payload. The endpoint's response is
secondary — the audit log is the historical record operators rely
on for "who did what when," so it has to be solid.

The run-level resume / rebuild-index / full-reindex endpoints were
removed: re-processing a document always goes through
``POST /documents/{document_id}/reindex``, and the audit event for
that path is ``ACTION_OPS_RUN_REINDEXED`` keyed on the new run id.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink, AUDIT_LOG_FILENAME
from j1.ingestion_review import IngestionResultReviewService
from j1.ingestion_review.audit_actions import (
    ACTION_OPS_BATCH_DISPATCHED,
    ACTION_OPS_RUN_DELETED,
    ACTION_OPS_RUN_PURGED,
    TARGET_KIND_INGESTION_BATCH,
    TARGET_KIND_INGESTION_RUN,
)
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
_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}


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
        # Critical: the ops endpoints publish events through this
        # service. Without it (or with a no-op stub), the audit
        # events under test never get written.
        event_publisher=EventPublisherService(audit_recorder),
    )


@pytest.fixture
def starter_calls():
    return []


@pytest.fixture
def stub_starter(starter_calls):
    async def _start(ctx, document_id, body) -> str:
        starter_calls.append({
            "document_id": document_id,
            "correlation_id": body.correlation_id,
            "reindex_of": getattr(body, "reindex_of", None),
        })
        suffix = "reindex" if getattr(body, "reindex_of", None) else "single"
        return f"wf-{document_id}-{suffix}-{body.correlation_id}"
    return _start


@pytest.fixture
def stub_batch_starter():
    async def _start(ctx, batch_run_id, child_specs) -> str:
        return f"j1-batch-{batch_run_id}"
    return _start


@pytest.fixture
def client(
    application_facade, workspace, run_store, review_service,
    stub_starter, stub_batch_starter,
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
        batch_starter=stub_batch_starter,
        processing_capabilities=capabilities,
    )
    return TestClient(app)


def _read_audit(workspace, ctx) -> list[dict]:
    """Read every event from the run's audit log. Mirrors the
 helper in test_activities_lifecycle.py — kept inline so this
 test file stays self-contained."""
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines() if line.strip()
    ]


def _events_with_action(workspace, ctx, action: str) -> list[dict]:
    return [e for e in _read_audit(workspace, ctx) if e["action"] == action]


# ---- Setup helpers ------------------------------------------------


def _seed_run(
    run_store,
    *,
    run_id="run-prior",
    status: RunStatus = RunStatus.SUCCEEDED,
    completed_steps: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    artifact_kinds: list[str] | None = None,
):
    # ``completed_steps`` / ``artifact_ids`` / ``artifact_kinds`` are
    # accepted but ignored; older tests passed them when seeding a
    # resume snapshot. Kept as parameters for caller compatibility
    # while the (no-op) resume context is being deleted upstream.
    del completed_steps, artifact_ids, artifact_kinds
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    run = IngestionRun(
        run_id=run_id,
        document_id="doc-A",
        workflow_id="wf-prior",
        workflow_run_id="wfr-prior",
        status=status,
        started_at=now,
        updated_at=now + timedelta(minutes=2),
        completed_at=now + timedelta(minutes=2),
        metadata={
            "policy": "auto", "mode": "STANDARD",
            "document_name": "doc-A.pdf",
        },
    )
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    run_store.upsert(ctx, run)


def _seed_document(registry, *, document_id="doc-A"):
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


# ---- Tests --------------------------------------------------------


def test_delete_endpoint_emits_ops_run_deleted(
    client, run_store, workspace,
):
    _seed_run(run_store, status=RunStatus.SUCCEEDED)
    resp = client.delete("/ingestion-runs/run-prior", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    events = _events_with_action(workspace, ctx, ACTION_OPS_RUN_DELETED)
    assert len(events) == 1
    e = events[0]
    assert e["target_kind"] == TARGET_KIND_INGESTION_RUN
    assert e["target_id"] == "run-prior"
    assert e["correlation_id"] == "run-prior"
    assert "tombstoned_artifact_count" in e["payload"]
    assert e["payload"]["was_already_deleted"] is False


def test_purge_endpoint_emits_ops_run_purged(
    client, run_store, workspace,
):
    _seed_run(run_store, status=RunStatus.DELETED)
    resp = client.post(
        "/ingestion-runs/run-prior/purge", headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    events = _events_with_action(workspace, ctx, ACTION_OPS_RUN_PURGED)
    assert len(events) == 1
    e = events[0]
    assert e["target_id"] == "run-prior"
    assert e["payload"]["snapshots_removed"] == 1
    assert "files_deleted" in e["payload"]
    assert "files_missing" in e["payload"]


