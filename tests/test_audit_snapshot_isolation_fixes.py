"""Tests for the audit-driven snapshot isolation fixes.

Covers:

  * **Delete-run guard via snapshot lookup** — the run protected
    from deletion is the one that produced the document's currently
    active snapshot (``DocumentSnapshot.created_by_run_id`` of
    ``DocumentRecord.active_snapshot_id``), not the latest succeeded
    run by timestamp. Locks down the divergence risk the audit
    flagged in §5#2.

  * **Atomic re-index lock via ``pending_operation`` CAS** — two
    near-simultaneous reindex POSTs cannot both pass; the loser
    gets HTTP 409. Locks down the in-flight-guard race the audit
    flagged as HIGH severity.

  * **Validation-before-promotion gate (structural hook)** — when
    ``J1_REQUIRE_VALIDATION_BEFORE_PROMOTION=true`` and no validator
    is wired, the gate refuses (fails closed). Default off keeps
    existing behaviour unchanged.

The RAGAnything snapshot-scope tests live in
``tests/test_query_retrieval_routes.py`` next to the other adapter
tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest import create_rest_api
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.documents.snapshot import (
    DocumentSnapshot, SnapshotState,
)
from j1.documents.snapshot_service import DocumentSnapshotService
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.ingestion_review import IngestionResultReviewService
from j1.jobs.status import ProcessingStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def snapshot_store(workspace):
    return JsonlDocumentSnapshotStore(workspace)


@pytest.fixture
def snapshot_service(snapshot_store):
    return DocumentSnapshotService(store=snapshot_store)


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry,
        artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


@pytest.fixture
def review_service(run_store, artifact_registry, workspace):
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


@pytest.fixture
def application_facade(
    intake_service, artifact_registry, registry, audit_recorder,
):
    from j1.integration import (
        ApplicationFacade, CitationLookupService,
        DocumentIngestionService, EventPublisherService,
        RetrievalService, SourceLookupService,
    )
    return ApplicationFacade(
        ingestion=DocumentIngestionService(intake_service),
        retrieval=RetrievalService(artifact_registry),
        citation_lookup=CitationLookupService(artifact_registry),
        source_lookup=SourceLookupService(registry),
        feedback=None,
        event_publisher=EventPublisherService(audit_recorder),
        job_control=None,
    )


@pytest.fixture
def client(
    application_facade, workspace, run_store, lifecycle_service,
    review_service, snapshot_service,
):
    from j1.integration.dto import ProcessingCapabilities
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        snapshot_service=snapshot_service,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app)


def _headers(ctx):
    return {"X-Tenant-Id": ctx.tenant_id, "X-Project-Id": ctx.project_id}


def _seed_doc(registry, ctx, *, document_id="doc-1", active_snapshot_id):
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
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


def _seed_run(
    run_store, ctx, *, run_id, document_id="doc-1",
    status=RunStatus.SUCCEEDED, started_at=None,
):
    started = started_at or _NOW
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=status,
        started_at=started,
        updated_at=started,
        completed_at=started,
        metadata={},
    ))


def _seed_snapshot(snapshot_store, ctx, *, snapshot_id, run_id,
                   document_id="doc-1",
                   state=SnapshotState.READY):
    snapshot_store.upsert(ctx, DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        created_by_run_id=run_id,
        state=state,
        created_at=_NOW,
        promoted_at=_NOW if state == SnapshotState.READY else None,
    ))


# ---- Clean-up-run guard via snapshot lookup -----------------------


def test_clean_up_run_guard_uses_active_snapshot_producing_run(
    client, registry, run_store, snapshot_store, ctx,
):
    """Audit fix: the protected run is derived from
    ``active_snapshot.created_by_run_id``, NOT from the latest-
    succeeded-run heuristic. Setup picks an active snapshot whose
    producer is the OLDER run; the latest succeeded run is a later
    (non-promoting) attempt — proves the guard now consults the
    snapshot store instead of the heuristic, on the snapshot-
    centric Clean Up Run endpoint."""
    # Active snapshot was produced by r-older. A later succeeded
    # run (r-newer) exists but did NOT promote — e.g. test/preview
    # path, CAS conflict, refresh-enrich pending promotion.
    _seed_doc(registry, ctx, active_snapshot_id="snap-older")
    _seed_run(
        run_store, ctx, run_id="r-older",
        status=RunStatus.SUCCEEDED,
        started_at=_NOW - timedelta(hours=2),
    )
    _seed_run(
        run_store, ctx, run_id="r-newer",
        status=RunStatus.SUCCEEDED, started_at=_NOW,
    )
    _seed_snapshot(snapshot_store, ctx,
                   snapshot_id="snap-older", run_id="r-older")

    # The older run produced the active snapshot — guarded. The
    # snapshot-centric endpoint always returns 200; ``cleaned``
    # carries the outcome and ``reason`` carries the refusal code.
    resp_older = client.post(
        "/ingestion-runs/r-older/clean-up", headers=_headers(ctx),
    )
    assert resp_older.status_code == 200, resp_older.text
    older_body = resp_older.json()["data"]
    assert older_body["cleaned"] is False
    assert older_body["reason"] == "ACTIVE_RUN"
    assert run_store.get(ctx, "r-older") is not None

    # The newer run is NOT the active snapshot's producer — eligible.
    resp_newer = client.post(
        "/ingestion-runs/r-newer/clean-up", headers=_headers(ctx),
    )
    assert resp_newer.status_code == 200, resp_newer.text
    assert resp_newer.json()["data"]["cleaned"] is True


# ---- Atomic re-index lock (pending_operation CAS) ---------------


class _StubJobStarter:
    """Captures the (ctx, document_id, body) it was called with so
    tests can assert dispatch happened. Returns a deterministic
    workflow id."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def __call__(self, ctx, document_id, body):
        self.calls.append((ctx, document_id, body))
        return f"wf-{document_id}-{len(self.calls)}"


@pytest.fixture
def starter():
    return _StubJobStarter()


@pytest.fixture
def reindex_client(
    application_facade, workspace, run_store, lifecycle_service,
    review_service, snapshot_service, starter, ctx,
):
    """Variant of ``client`` that wires a job_starter so the reindex
    endpoint can complete (otherwise the dispatch step 503s out and
    the lock-release branch fires before the test can probe state)."""
    from j1.integration.dto import ProcessingCapabilities

    # Seed a raw file on disk so the reindex 'source exists' guard
    # passes. The doc fixture below points at this same path.
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "doc-1.pdf").write_bytes(b"%PDF-1.4 stub\n")

    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        snapshot_service=snapshot_service,
        job_starter=starter,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    return TestClient(app)


def test_reindex_lock_blocks_concurrent_request(
    reindex_client, registry, starter, ctx,
):
    """Two reindex POSTs back-to-back for the same document. The
    first acquires the ``pending_operation=reindex`` lock; the
    second sees it set and returns HTTP 409 *without* dispatching a
    parallel workflow run."""
    _seed_doc(registry, ctx, active_snapshot_id=None)

    # First reindex acquires the lock and dispatches.
    r1 = reindex_client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert r1.status_code == 200, r1.text
    assert len(starter.calls) == 1

    # Second reindex sees the lock and refuses.
    r2 = reindex_client.post(
        "/documents/doc-1/reindex", headers=_headers(ctx),
    )
    assert r2.status_code == 409, r2.text
    msg = r2.json()["error"]["message"].lower()
    assert "pending operation" in msg or "already" in msg
    # CRITICAL: no second workflow dispatch.
    assert len(starter.calls) == 1


def test_reindex_lock_released_on_starter_failure(
    application_facade, workspace, run_store, lifecycle_service,
    review_service, snapshot_service, registry, ctx,
):
    """When workflow dispatch raises BEFORE the activity-layer
    release can run, the REST handler must release the lock so the
    document doesn't stay wedged. Subsequent reindex must succeed."""
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "doc-1.pdf").write_bytes(b"%PDF-1.4 stub\n")
    _seed_doc(registry, ctx, active_snapshot_id=None)

    # First starter raises — simulates Temporal-down at dispatch time.
    class _RaisingStarter:
        def __init__(self) -> None:
            self.calls = 0
        async def __call__(self, ctx, document_id, body):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporal unavailable")
            return f"wf-{document_id}"

    raising = _RaisingStarter()
    from j1.integration.dto import ProcessingCapabilities
    app = create_rest_api(
        application_facade,
        workspace=workspace,
        ingestion_run_store=run_store,
        review_service=review_service,
        document_lifecycle_service=lifecycle_service,
        snapshot_service=snapshot_service,
        job_starter=raising,
        processing_capabilities=ProcessingCapabilities(
            default_compiler_kind="mock",
            compiler_kinds=frozenset({"mock"}),
        ),
    )
    # ``raise_server_exceptions=False`` so the test can probe the
    # HTTP response shape instead of having the TestClient re-raise
    # the simulated RuntimeError into the test body.
    client_ = TestClient(app, raise_server_exceptions=False)

    # First call: lock acquired, starter raises, lock should release.
    r1 = client_.post("/documents/doc-1/reindex", headers=_headers(ctx))
    assert r1.status_code >= 500, r1.text

    # Second call: must succeed (lock released; no wedge).
    r2 = client_.post("/documents/doc-1/reindex", headers=_headers(ctx))
    assert r2.status_code == 200, r2.text
    assert raising.calls == 2


# ---- Validation-before-promotion gate (structural hook) ---------


def test_validation_gate_default_off_promotes_normally(
    monkeypatch, snapshot_service, snapshot_store,
    registry, run_store, ctx,
):
    """Default behaviour: env unset → gate bypassed → promotion path
    runs as before. The new structural hook is invisible until
    explicitly opted into via the env var."""
    from j1.orchestration.activities.runs import RunsActivities

    monkeypatch.delenv(
        "J1_REQUIRE_VALIDATION_BEFORE_PROMOTION", raising=False,
    )
    activities = RunsActivities(
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
        artifact_registry=None,
    )
    fake_run = IngestionRun(
        run_id="r-1", document_id="doc-1",
        workflow_id="wf", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW,
        target_snapshot_id="snap-1",
        metadata={},
    )
    assert activities._validation_gate_passed_for_promotion(
        ctx, fake_run, "snap-1",
    ) is True


def test_validation_gate_required_but_unwired_fails_closed(
    monkeypatch, snapshot_service, registry, run_store, ctx,
):
    """Env set to require validation BUT no validator wired → fail
    closed (refuse promotion). A misconfigured deployment must not
    silently bypass the gate."""
    from j1.orchestration.activities.runs import RunsActivities

    monkeypatch.setenv("J1_REQUIRE_VALIDATION_BEFORE_PROMOTION", "true")
    activities = RunsActivities(
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
        artifact_registry=None,
    )
    assert not hasattr(activities, "_validation_gate") or \
        getattr(activities, "_validation_gate", None) is None
    fake_run = IngestionRun(
        run_id="r-1", document_id="doc-1",
        workflow_id="wf", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW,
        target_snapshot_id="snap-1",
        metadata={},
    )
    assert activities._validation_gate_passed_for_promotion(
        ctx, fake_run, "snap-1",
    ) is False


def test_validation_gate_required_with_passing_validator(
    monkeypatch, snapshot_service, registry, run_store, ctx,
):
    """Env set to require validation + validator returns True →
    gate passes → promotion proceeds."""
    from j1.orchestration.activities.runs import RunsActivities

    monkeypatch.setenv("J1_REQUIRE_VALIDATION_BEFORE_PROMOTION", "1")
    activities = RunsActivities(
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
        artifact_registry=None,
    )
    activities._validation_gate = lambda *_: True
    fake_run = IngestionRun(
        run_id="r-1", document_id="doc-1",
        workflow_id="wf", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW,
        target_snapshot_id="snap-1",
        metadata={},
    )
    assert activities._validation_gate_passed_for_promotion(
        ctx, fake_run, "snap-1",
    ) is True
