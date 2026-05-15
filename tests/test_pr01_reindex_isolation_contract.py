"""PR-01 contract — Re-index isolation + active-snapshot safety.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-01, J1
MUST guarantee four behaviours when a document is re-indexed:

  1. A successful re-index creates a new isolated snapshot — no
     compile / chunk / enrichment / alias artifact from the prior
     snapshot is reused.
  2. A failed re-index does NOT promote — the previously active
     snapshot stays in place.
  3. Default-scope queries do not see an unpromoted candidate
     snapshot — eligibility resolves to the promoted snapshot only.
  4. New runs do not carry forward IDs from prior runs — metadata
     keys reserved for reuse paths are absent on reindex.

Each behaviour has historical coverage spread across multiple
test files. This module is the single navigable contract pin: if
any of the four breaks in a future refactor, the failure surfaces
here first, with a clear name. Adjacent regression tests still
exist for finer-grained edge cases; this file is the operator's
"is PR-01 still shipped?" smoke test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import (
    PROJECT_HEADER,
    TENANT_HEADER,
    create_rest_api,
)
from j1.documents.models import DocumentRecord, ProcessingStatus
from j1.documents.snapshot import DocumentSnapshot, SnapshotState
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
from j1.orchestration.activities.payloads import ProjectScope
from j1.orchestration.activities.runs import (
    ReportRunTerminalInput, RunsActivities,
)
from j1.projects.context import ProjectContext
from j1.query.eligibility import resolve_eligible_active_run_ids
from j1.query.scope import ActiveScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_HEADERS = {TENANT_HEADER: "acme", PROJECT_HEADER: "alpha"}
_CTX = ProjectContext(tenant_id="acme", project_id="alpha")
_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---- Fixtures ----------------------------------------------------


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def snapshot_store(workspace):
    return JsonlDocumentSnapshotStore(workspace)


@pytest.fixture
def snapshot_service(snapshot_store):
    from j1.documents.snapshot_service import DocumentSnapshotService
    return DocumentSnapshotService(store=snapshot_store)


@pytest.fixture
def feedback_store(workspace):
    from j1.integration import JsonlFeedbackStore
    return JsonlFeedbackStore(workspace.audit(_CTX) / "feedback.jsonl")


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, feedback_store,
    audit_recorder,
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


@pytest.fixture
def runs_activities(run_store, registry, snapshot_service):
    return RunsActivities(
        progress_reporter=None,
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
    )


def _seed_doc(
    *, registry, workspace, document_id: str,
    active_snapshot_id: str | None = None, create_file: bool = True,
) -> None:
    registry.add(DocumentRecord(
        document_id=document_id,
        project=_CTX,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
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
) -> None:
    run_store.upsert(_CTX, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=_NOW,
        updated_at=_NOW,
        target_snapshot_id=target_snapshot_id,
    ))


def _seed_snapshot(
    *, snapshot_store, document_id: str, snapshot_id: str, run_id: str,
    state: SnapshotState = SnapshotState.READY,
) -> None:
    snapshot_store.upsert(_CTX, DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        tenant_id=_CTX.tenant_id,
        project_id=_CTX.project_id,
        created_by_run_id=run_id,
        state=state,
        created_at=_NOW,
        promoted_at=_NOW if state == SnapshotState.READY else None,
    ))


def _terminate_run(
    activities: RunsActivities, *, run_id: str, final_status: str,
) -> None:
    activities._persist_run_terminal(
        _CTX,
        ReportRunTerminalInput(
            scope=ProjectScope.from_context(_CTX),
            run_id=run_id,
            final_status=final_status,
        ),
    )


# ---- Contract 1: successful re-index creates new isolated snapshot


def test_contract_1_successful_reindex_allocates_isolated_snapshot(
    client, registry, run_store, started_jobs, workspace,
):
    """Re-index dispatches a NEW run with a NEW target snapshot id,
    distinct from the document's current active snapshot. The
    workflow starter sees the new run id — proving the dispatch
    is fresh rather than restarting the prior run."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-iso-1", active_snapshot_id="snap-old",
    )
    _seed_run(
        run_store=run_store, document_id="doc-iso-1",
        run_id="run-old", target_snapshot_id="snap-old",
    )

    resp = client.post("/documents/doc-iso-1/reindex", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    new_run_id = resp.json()["data"]["reindexRunId"]

    persisted = run_store.get(_CTX, new_run_id)
    assert persisted.run_type == "reindex"
    assert persisted.parent_run_id == "run-old"
    # New target snapshot, distinct from the prior active.
    assert persisted.target_snapshot_id is not None
    assert persisted.target_snapshot_id != "snap-old", (
        "reindex must allocate a NEW snapshot — reusing the prior "
        "active snapshot would let a failed compile overwrite the "
        "queryable state"
    )

    # Starter saw the new run; one dispatch, not a restart.
    assert len(started_jobs) == 1
    assert started_jobs[0]["correlation_id"] == new_run_id


# ---- Contract 2: failed re-index does not promote ---------------


def test_contract_2_failed_reindex_does_not_promote(
    runs_activities, run_store, registry, snapshot_service,
):
    """When a reindex run terminates ``failed``, the document's
    ``active_snapshot_id`` MUST remain pinned at the prior good
    snapshot. Promotion happens ONLY on terminal-success.

    Sequence:
      1. Seed a document with NO active snapshot.
      2. Run a successful initial ingestion → promotes snap-A.
      3. Run a failed reindex → must NOT promote snap-B.
      4. Assert active_snapshot_id == snap-A's id.
    """
    document_id = "doc-iso-2"
    registry.add(DocumentRecord(
        document_id=document_id, project=_CTX,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=None,
    ))
    # Initial good run → promotes.
    snap_a = snapshot_service.create_candidate(
        _CTX, document_id=document_id, created_by_run_id="run-initial",
    )
    run_store.upsert(_CTX, IngestionRun(
        run_id="run-initial", document_id=document_id,
        workflow_id="wf-initial", workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=_NOW, updated_at=_NOW,
        target_snapshot_id=snap_a.snapshot_id,
    ))
    _terminate_run(
        runs_activities, run_id="run-initial", final_status="succeeded",
    )
    promoted = registry.get(_CTX, document_id).active_snapshot_id
    assert promoted is not None, (
        "initial successful run must promote — preconditions wrong"
    )

    # Failed reindex → must NOT promote.
    snap_b = snapshot_service.create_candidate(
        _CTX, document_id=document_id, created_by_run_id="run-reindex",
    )
    run_store.upsert(_CTX, IngestionRun(
        run_id="run-reindex", document_id=document_id,
        workflow_id="wf-reindex", workflow_run_id=None,
        status=RunStatus.RUNNING,
        started_at=_NOW, updated_at=_NOW,
        target_snapshot_id=snap_b.snapshot_id,
        parent_run_id="run-initial",
    ))
    _terminate_run(
        runs_activities, run_id="run-reindex", final_status="failed",
    )
    after = registry.get(_CTX, document_id).active_snapshot_id
    assert after == promoted, (
        f"failed reindex must not change active_snapshot_id; "
        f"was {promoted!r}, became {after!r}"
    )
    assert after != snap_b.snapshot_id, (
        "the failed candidate snapshot must NOT be promoted"
    )


# ---- Contract 3: default queries don't see unpromoted snapshots --


def test_contract_3_unpromoted_snapshot_not_queryable_via_default_scope(
    registry, workspace, snapshot_store,
):
    """The eligibility resolver — the single chokepoint every query
    path consults — MUST return only the document's
    ``active_snapshot_id``. A candidate snapshot that has been
    allocated but not promoted (still in BUILDING state) cannot
    leak into the default-scope query."""
    # Document has snap-active as the promoted snapshot. snap-cand
    # is allocated (created_by_run_id points at a candidate run)
    # but the document's active_snapshot_id stays pinned at
    # snap-active.
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-iso-3", active_snapshot_id="snap-active",
    )
    _seed_snapshot(
        snapshot_store=snapshot_store, document_id="doc-iso-3",
        snapshot_id="snap-active", run_id="run-active",
        state=SnapshotState.READY,
    )
    _seed_snapshot(
        snapshot_store=snapshot_store, document_id="doc-iso-3",
        snapshot_id="snap-cand", run_id="run-candidate",
        state=SnapshotState.BUILDING,
    )

    # ActiveScope query → only the promoted snapshot is eligible.
    active_result = resolve_eligible_active_run_ids(
        ctx=_CTX,
        scope=ActiveScope(document_id="doc-iso-3"),
        registry=registry,
    )
    assert active_result.snapshot_ids == frozenset({"snap-active"})
    assert "snap-cand" not in active_result.snapshot_ids, (
        "default-scope query MUST NOT surface a candidate snapshot — "
        "operators querying the document expect only the promoted "
        "knowledge version"
    )

    # WorkspaceScope (project-wide) query → same contract.
    workspace_result = resolve_eligible_active_run_ids(
        ctx=_CTX, scope=WorkspaceScope(), registry=registry,
    )
    assert workspace_result.snapshot_ids == frozenset({"snap-active"})


# ---- Contract 4: new runs do not carry forward reuse metadata ---


def test_contract_4_new_reindex_run_does_not_carry_reuse_metadata(
    client, registry, run_store, workspace,
):
    """Compile reuse fires only when ``metadata.reused_compile_from_run_id``
    is set on the new run record (the manual-domain-enrichment
    flow uses this). Reindex MUST NOT set it — pinning here so a
    future refactor that adds compile-cache reuse to reindex can't
    do it silently."""
    _seed_doc(
        registry=registry, workspace=workspace,
        document_id="doc-iso-4", active_snapshot_id="snap-old",
    )
    _seed_run(
        run_store=run_store, document_id="doc-iso-4",
        run_id="run-old", target_snapshot_id="snap-old",
    )
    resp = client.post("/documents/doc-iso-4/reindex", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    new_run_id = resp.json()["data"]["reindexRunId"]

    persisted = run_store.get(_CTX, new_run_id)
    metadata = dict(persisted.metadata or {})
    forbidden_reuse_keys = (
        "reused_compile_from_run_id",
        "carry_forward_artifact_ids",
        "resume_artifact_ids",
        "chunk_artifact_ids",
        "manual_action_source_run_id",
    )
    leaked = [k for k in forbidden_reuse_keys if k in metadata]
    assert not leaked, (
        f"reindex run metadata leaked reuse keys {leaked!r} — "
        "compile must start from the original file, not the prior "
        "run's outputs"
    )
    # And the parent pointer is structural (audit lineage), not a
    # reuse signal.
    assert persisted.parent_run_id == "run-old"
