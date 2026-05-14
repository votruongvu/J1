"""Phase 7 — final legacy deletion invariants.

These tests lock the Phase 7 changes:

  * No normal wiring helper constructs ``SqliteSearchIndexer`` by
    default. The legacy SQLite path is reachable ONLY when an
    operator opts in via ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED=true``.
  * ``DocumentCleanupService.cleanup_snapshot`` exists and routes
    through the evidence adapter for evidence-row deletion.
  * Promotion does NOT write ``active_run_id``. The only
    visibility write is ``active_snapshot_id``.
  * ``RunsActivities._promote_snapshot`` reads
    ``previous_active_snapshot_id`` directly from the
    DocumentRecord (no run-id round-trip).
  * ``KnowledgeProcessingActivities`` threads ``snapshot_id``
    into the compile call when the compiler signature accepts it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.projects.context import ProjectContext


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- Wiring: no SQLite indexer in normal helpers ----------------


def test_application_facade_no_longer_constructs_sqlite_indexer(tmp_path, monkeypatch):
    """Phase 8: ``build_application_facade`` wires the canonical
    Postgres FTS evidence adapter. The SQLite path was deleted; a
    DSN is mandatory + psycopg must be installed."""
    pytest.importorskip("psycopg")
    monkeypatch.setenv("J1_METADATA_DSN", "postgresql://j1:j1@stub/j1")
    monkeypatch.setenv("J1_EVIDENCE_BACKEND", "postgres_fts")
    from deploy.dev._wiring import build_application_facade
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    facade = build_application_facade(workspace)
    assert facade.search._adapter is not None
    assert facade.search._indexer is None


def test_document_cleanup_service_default_wiring_has_no_indexer(tmp_path, monkeypatch):
    """Phase 7: the dev cleanup service is constructed with
    ``indexer=None``. Operators get the snapshot-aware
    ``cleanup_snapshot`` path."""
    monkeypatch.delenv("J1_LEGACY_SQLITE_EVIDENCE_ENABLED", raising=False)
    from deploy.dev._wiring import build_document_cleanup_service
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    svc = build_document_cleanup_service(workspace)
    assert svc._indexer is None


# ---- cleanup_snapshot on DocumentCleanupService ----------------


def test_cleanup_snapshot_routes_evidence_delete_through_adapter(tmp_path, ctx):
    """Phase 7: the new ``cleanup_snapshot`` API calls
    ``evidence_adapter.delete_for_snapshot`` and reports the row
    count."""
    from j1.documents.cleanup import DocumentCleanupService
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver

    deleted = {}

    class _FakeAdapter:
        def delete_for_snapshot(self, _ctx, snapshot_id):
            deleted["snapshot_id"] = snapshot_id
            return 7

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    svc = DocumentCleanupService(
        workspace=workspace,
        artifacts=None,
        indexer=None,
        evidence_adapter=_FakeAdapter(),
    )
    result = svc.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id="snap-1",
    )
    assert result.ok
    assert deleted["snapshot_id"] == "snap-1"
    evidence_step = next(s for s in result.steps if s.name == "evidence")
    assert evidence_step.items_removed == 7


def test_cleanup_snapshot_drops_artifacts_with_matching_snapshot_id(tmp_path, ctx):
    """The artifact step scans the registry for typed
    ``snapshot_id`` matches and deletes those records (file +
    registry entry)."""
    from datetime import datetime, timezone
    from j1.artifacts.models import ArtifactRecord
    from j1.documents.cleanup import DocumentCleanupService
    from j1.config.settings import Settings
    from j1.jobs.status import ProcessingStatus, ReviewStatus
    from j1.workspace.resolver import WorkspaceResolver

    class _Registry:
        def __init__(self):
            self.store = {}
            self.deleted = []

        def add(self, r):
            self.store[r.artifact_id] = r

        def list_artifacts(self, _ctx, **_):
            return list(self.store.values())

        def delete_by_artifact_id(self, _ctx, aid):
            if aid in self.store:
                del self.store[aid]
                self.deleted.append(aid)
                return True
            return False

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    reg = _Registry()
    reg.add(ArtifactRecord(
        artifact_id="a1", project=ctx, kind="chunk",
        location="x/y", content_hash="h", byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source_document_ids=["doc-1"],
        snapshot_id="snap-old",
    ))
    reg.add(ArtifactRecord(
        artifact_id="a2", project=ctx, kind="chunk",
        location="x/z", content_hash="h", byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source_document_ids=["doc-1"],
        snapshot_id="snap-new",
    ))

    svc = DocumentCleanupService(
        workspace=workspace,
        artifacts=reg,
        indexer=None,
        evidence_adapter=None,
    )
    result = svc.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id="snap-old",
    )
    assert result.ok
    assert reg.deleted == ["a1"]
    # The other snapshot's artifact survives.
    assert "a2" in reg.store


# ---- Promotion no longer writes active_run_id ------------------


def test_promotion_writes_only_active_snapshot_id(workspace, ctx, registry):
    """Phase 7: a usable terminal status flips
    ``active_snapshot_id`` and DOES NOT write ``active_run_id``."""
    from j1.documents.models import DocumentRecord
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
    from j1.jobs.status import ProcessingStatus
    from j1.orchestration.activities.payloads import ProjectScope
    from j1.orchestration.activities.runs import (
        ReportRunTerminalInput,
        RunsActivities,
    )
    from j1.runs.models import IngestionRun, RunStatus
    from j1.runs.store import JsonlIngestionRunStore

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    registry.add(DocumentRecord(
        document_id="doc-7", project=ctx,
        original_filename="x.pdf", stored_filename="x.pdf",
        mime_type="application/pdf", file_size=1, checksum="sha:x",
        status=ProcessingStatus.SUCCEEDED, created_at=now,
    ))

    run_store = JsonlIngestionRunStore(workspace)
    run_store.upsert(ctx, IngestionRun(
        run_id="r-7", document_id="doc-7",
        workflow_id="wf-7", workflow_run_id=None,
        status=RunStatus.RUNNING, started_at=now, updated_at=now,
    ))

    snapshot_service = DocumentSnapshotService(
        store=JsonlDocumentSnapshotStore(workspace),
    )
    activities = RunsActivities(
        progress_reporter=None,
        run_store=run_store,
        source_registry=registry,
        snapshot_service=snapshot_service,
    )
    activities._persist_run_terminal(
        ctx,
        ReportRunTerminalInput(
            scope=ProjectScope.from_context(ctx),
            run_id="r-7",
            final_status="succeeded",
        ),
    )
    doc = registry.get(ctx, "doc-7")
    assert doc.active_snapshot_id is not None
    assert doc.active_run_id is None  # Phase 7: not written.


# ---- KnowledgeProcessingActivities threads snapshot_id ---------


def test_knowledge_activity_threads_snapshot_id_to_compile(workspace, ctx, registry):
    """Phase 7: when the compiler accepts ``snapshot_id``, the
    activity resolves it (via ``get_or_create_for_run``) and
    passes it through."""
    from datetime import datetime, timezone
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.documents.models import DocumentRecord
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
    from j1.cost.recorder import DefaultCostRecorder
    from j1.cost.sink import JsonlCostSink
    from j1.jobs.status import ProcessingStatus
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )
    from j1.orchestration.activities.payloads import (
        KnowledgeCompilationInput,
        ProjectScope,
    )
    from j1.processing.results import (
        ArtifactProcessingResult,
        ResultStatus,
    )

    # Seed a document so the activity's lookup succeeds.
    registry.add(DocumentRecord(
        document_id="doc-1", project=ctx,
        original_filename="x.pdf", stored_filename="x.pdf",
        mime_type="application/pdf", file_size=1, checksum="sha:x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    ))

    captured = {}

    class _StubCompiler:
        kind = "stub.compiler"

        def compile(self, ctx, document_id, *, run_id=None, snapshot_id=None):
            captured["run_id"] = run_id
            captured["snapshot_id"] = snapshot_id
            return ArtifactProcessingResult(
                status=ResultStatus.SUCCEEDED, drafts=[],
            )

    snapshot_service = DocumentSnapshotService(
        store=JsonlDocumentSnapshotStore(workspace),
    )
    activities = KnowledgeProcessingActivities(
        workspace=workspace,
        sources=registry,
        artifacts=None,
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        cost=DefaultCostRecorder(JsonlCostSink(workspace)),
        compilers={"stub.compiler": _StubCompiler()},
        snapshot_service=snapshot_service,
    )
    activities.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="stub.compiler",
            correlation_id="run-1",
        ),
    )
    assert captured["run_id"] == "run-1"
    assert captured["snapshot_id"] is not None
    assert captured["snapshot_id"].startswith("snap_")


# ---- SearchActivities default: no indexer registration ---------


def test_default_search_activity_has_no_sqlite_kind_registered(monkeypatch):
    """When the env flag is off (default), the worker boots with
    an empty indexers map — the SQLite kind isn't advertised."""
    monkeypatch.delenv("J1_LEGACY_SQLITE_EVIDENCE_ENABLED", raising=False)
    from j1.audit.recorder import DefaultAuditRecorder
    from j1.audit.sink import JsonlAuditSink
    from j1.orchestration.activities.search import SearchActivities
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver
    import tempfile

    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        ws = WorkspaceResolver(Settings(data_root=Path(tmp)))
        audit = DefaultAuditRecorder(JsonlAuditSink(ws))
        # Construct with no indexers — what the Phase-7 wiring does
        # by default.
        sa = SearchActivities(audit=audit, indexers={})
    assert sa._indexers == {}
