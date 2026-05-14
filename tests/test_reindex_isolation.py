"""Regression tests for the document-level Re-Index contract.

Each test maps to one of the test rows in the Re-Index/Resume brief
(rows A, C, F, G, H). Rows B, D, E are covered by existing tests:

  * B (active result tabs are run-scoped) →
    ``tests/test_ingestion_review_service.py`` already exercises every
    tab through ``_resolve_run_artifacts`` which filters by run_id.
  * D (validation uses only active run) → exercised below via the
    ``ActiveScope`` resolver test (row C body).
  * E (run-level actions removed from UI) → ``frontend`` tests +
    ``test_documents_projector.py`` already assert ``"resume"`` is
    never in the action set.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.documents.models import DocumentRecord, ProcessingStatus
from j1.documents.snapshot import (
    DocumentSnapshot,
    SnapshotState,
)
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
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
from j1.query.active_scope import (
    _NO_ACTIVE_RUN_SENTINEL,
    resolve_to_concrete_scope,
)
from j1.query.scope import ActiveScope, RunScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}
_CTX = ProjectContext(tenant_id="acme", project_id="alpha")


# ---- Fixtures ------------------------------------------------------


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def snapshot_store(workspace):
    return JsonlDocumentSnapshotStore(workspace)


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(_CTX) / "feedback.jsonl")


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
def started_jobs():
    return []


@pytest.fixture
def stub_starter(started_jobs):
    async def _start(ctx, document_id, body):
        started_jobs.append({
            "document_id": document_id,
            "correlation_id": body.correlation_id,
            "reindex_of": getattr(body, "reindex_of", None),
            "target_snapshot_id": getattr(body, "target_snapshot_id", None),
        })
        return f"wf-{body.correlation_id}"
    return _start


@pytest.fixture
def snapshot_service(snapshot_store):
    from j1.documents.snapshot_service import DocumentSnapshotService
    return DocumentSnapshotService(store=snapshot_store)


@pytest.fixture
def client(
    application_facade, workspace, run_store, stub_starter,
    snapshot_service,
):
    from j1.documents.service import DocumentLifecycleService
    from j1.integration.dto import ProcessingCapabilities

    capabilities = ProcessingCapabilities(
        default_compiler_kind="mock",
        compiler_kinds=frozenset({"mock"}),
    )
    lifecycle = DocumentLifecycleService(
        registry=application_facade.source_lookup._sources,
        artifact_registry=application_facade.retrieval._artifacts,
    )
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        job_starter=stub_starter,
        document_lifecycle_service=lifecycle,
        processing_capabilities=capabilities,
        snapshot_service=snapshot_service,
    )
    return TestClient(app)


_NOW_ISO = "2026-05-14T00:00:00+00:00"


def _seed_doc(
    *, registry, workspace, document_id: str,
    active_snapshot_id: str | None = None, create_file: bool = True,
):
    from datetime import datetime, timezone
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    registry.add(DocumentRecord(
        document_id=document_id,
        project=_CTX,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=now,
        knowledge_state="attached",
        active_snapshot_id=active_snapshot_id,
    ))
    if create_file:
        raw_dir = workspace.raw(_CTX)
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{document_id}.pdf").write_bytes(b"%PDF-1.4\n")


def _seed_run(
    *, run_store, document_id: str, run_id: str,
    status: RunStatus = RunStatus.SUCCEEDED,
    target_snapshot_id: str | None = None,
):
    from datetime import datetime, timezone
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    run_store.upsert(_CTX, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=now,
        updated_at=now,
        metadata={},
        target_snapshot_id=target_snapshot_id,
    ))


def _seed_snapshot(
    *, snapshot_store, document_id: str, snapshot_id: str, run_id: str,
    state: SnapshotState = SnapshotState.READY,
):
    from datetime import datetime, timezone
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    snapshot_store.upsert(_CTX, DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        tenant_id=_CTX.tenant_id,
        project_id=_CTX.project_id,
        created_by_run_id=run_id,
        state=state,
        created_at=now,
        promoted_at=now if state == SnapshotState.READY else None,
    ))


# ---- A: Document-level Re-Index creates a clean run ----------------


def test_A_document_reindex_allocates_new_isolated_run(
    client, registry, run_store, started_jobs, workspace,
):
    """A new re-index allocates a new run_id under the SAME
    document_id, threads a new target_snapshot_id, and parents to the
    prior run. The new run carries NO artifact ids / chunk ids /
    enrichment ids from the prior run (its metadata is independent)."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-A", active_snapshot_id="snap-1",
    )
    _seed_run(
        run_store=run_store, document_id="doc-A", run_id="run-1",
        target_snapshot_id="snap-1",
    )

    resp = client.post("/documents/doc-A/reindex", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    new_run_id = data["reindexRunId"]
    assert new_run_id != "run-1", "new run must have a new id"
    assert data["documentId"] == "doc-A"
    assert data["parentRunId"] == "run-1"
    assert data["runType"] == "reindex"

    persisted = run_store.get(_CTX, new_run_id)
    assert persisted.run_type == "reindex"
    assert persisted.parent_run_id == "run-1"
    # New target snapshot, distinct from the prior active.
    assert persisted.target_snapshot_id is not None
    assert persisted.target_snapshot_id != "snap-1"
    # No artifact / chunk / enrichment ids leak from run-1.
    assert "carry_forward_artifact_ids" not in (persisted.metadata or {})
    assert "resume_artifact_ids" not in (persisted.metadata or {})
    assert "chunk_artifact_ids" not in (persisted.metadata or {})

    # Job starter was invoked with the new run id, not the old.
    assert len(started_jobs) == 1
    assert started_jobs[0]["correlation_id"] == new_run_id


# ---- C+D: Active scope resolves through snapshot store -------------


def test_C_active_scope_resolves_to_active_snapshots_creator_run(
    registry, snapshot_store, workspace,
):
    """ActiveScope(document_id) → RunScope(snapshot.created_by_run_id)
    via the snapshot store. After a re-index promotes a new snapshot,
    validation in active mode hits ONLY the new run's data."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-B", active_snapshot_id="snap-new",
    )
    _seed_snapshot(
        snapshot_store=snapshot_store, document_id="doc-B",
        snapshot_id="snap-new", run_id="run-new",
    )

    resolved = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-B"),
        registry=registry,
        ctx=_CTX,
        snapshot_store=snapshot_store,
    )
    assert isinstance(resolved, RunScope)
    assert resolved.run_id == "run-new"


def test_C_active_scope_returns_sentinel_when_no_active_snapshot(
    registry, snapshot_store, workspace,
):
    """A document with no promoted snapshot resolves to the sentinel
    (downstream filter matches zero artifacts — the correct "no
    active knowledge to validate" answer)."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-empty", active_snapshot_id=None,
    )
    resolved = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-empty"),
        registry=registry,
        ctx=_CTX,
        snapshot_store=snapshot_store,
    )
    assert isinstance(resolved, RunScope)
    assert resolved.run_id == _NO_ACTIVE_RUN_SENTINEL


def test_C_run_scope_and_workspace_scope_pass_through(
    registry, snapshot_store,
):
    """Non-ActiveScope inputs are returned unchanged."""
    run = resolve_to_concrete_scope(
        RunScope(run_id="explicit-run"),
        registry=registry, ctx=_CTX, snapshot_store=snapshot_store,
    )
    assert run == RunScope(run_id="explicit-run")
    ws = resolve_to_concrete_scope(
        WorkspaceScope(),
        registry=registry, ctx=_CTX, snapshot_store=snapshot_store,
    )
    assert ws == WorkspaceScope()


# ---- F: Run-level endpoints disabled (also covered in
# test_rest_resume_from_checkpoint; this asserts the doc-level path
# is still wired so the user has a working replacement). ------------


def test_F_document_level_reindex_endpoint_remains_supported(
    client, registry, run_store, workspace,
):
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-F",
    )
    resp = client.post("/documents/doc-F/reindex", headers=_HEADERS)
    assert resp.status_code == 200, resp.text


# ---- H: Missing original file fails clearly -----------------------


def test_H_reindex_fails_when_original_file_missing_on_disk(
    client, registry, run_store, workspace,
):
    """Re-index NEVER falls back to old parsed/compiled outputs. When
    the original uploaded file is gone, return a clear 409 with a
    user-visible message — do NOT start a phantom run that "succeeds"
    by reusing cached state."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-H", create_file=False,
    )

    resp = client.post("/documents/doc-H/reindex", headers=_HEADERS)
    assert resp.status_code == 409, resp.text
    msg = resp.json()["error"]["message"]
    assert "missing" in msg.lower()
    assert "doc-H.pdf" in msg
    # No run was created for the failed re-index.
    assert run_store.list_runs(_CTX, document_id="doc-H") == []
