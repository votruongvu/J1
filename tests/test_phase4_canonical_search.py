"""Phase 4 — canonical search/evidence path invariants.

These tests lock the Phase 4 cutover invariants:

  * ``SearchService`` accepts an ``EvidenceIndexAdapter`` and
    resolves the active-snapshot allowlist through eligibility
    before issuing a query.
  * ``SearchService`` with a registry filters by ``active_snapshot_id``
    only — never by ``active_run_id``.
  * Default ``SearchActivities`` runtime DOES NOT dual-write to SQLite.
  * ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED=true`` opts back into the
    legacy dual-write for debug / migration scenarios.
  * Chunk resolver memoizes file reads within one activity
    invocation.
  * Snapshot allocation is idempotent across retries via
    ``get_or_create_for_run``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.documents.snapshot import IndexKind, IndexRef, SnapshotState
from j1.jobs.status import ProcessingStatus
from j1.orchestration.activities.payloads import (
    ProjectScope,
    SearchIndexInput,
)
from j1.orchestration.activities.search import SearchActivities
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _doc(
    document_id="d-1",
    *,
    active_snapshot_id="snap-1",
    active_run_id="run-1",
    knowledge_state="attached",
    lifecycle_status="stable",
):
    return DocumentRecord(
        document_id=document_id,
        project=ProjectContext(tenant_id="t", project_id="p", profile=None),
        original_filename="x.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=datetime.now(timezone.utc),
        knowledge_state=knowledge_state,
        active_run_id=active_run_id,
        active_snapshot_id=active_snapshot_id,
        lifecycle_status=lifecycle_status,
    )


class _InMemoryRegistry:
    def __init__(self, docs):
        self._docs = list(docs)

    def list_documents(self, ctx):
        return list(self._docs)

    def get(self, ctx, document_id):
        for d in self._docs:
            if d.document_id == document_id:
                return d
        raise LookupError(document_id)


# ---- SearchService refactor ------------------------------------


def _adapter_returning(hits):
    """Build a minimal adapter double whose ``search`` returns the
    supplied list. Uses the real ``PostgresFtsEvidenceAdapter``
    shape so the ``isinstance`` check in SearchService accepts it."""
    from j1.search.evidence_adapter import PostgresFtsEvidenceAdapter

    class _Backend:
        def search(self, ctx, *, query, allowed_snapshot_ids, max_results):
            self.last_call = {
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "allowed_snapshot_ids": list(allowed_snapshot_ids),
                "query": query,
            }
            return list(hits)

        def index_chunks(self, chunks):
            return len(chunks)

        def delete_for_snapshot(self, ctx, snapshot_id):
            return 0

    backend = _Backend()
    adapter = PostgresFtsEvidenceAdapter(
        backend=backend, chunk_resolver=lambda *_: iter(()),
    )
    return adapter, backend


def test_search_service_filters_by_active_snapshot_allowlist(ctx):
    """The canonical Phase-4 contract: SearchService must NEVER
    query without first resolving the active-snapshot allowlist."""
    from j1.integration.services import SearchService
    from j1.search.postgres_fts import EvidenceHit

    hits = [EvidenceHit(
        artifact_id="art-1", chunk_id="c-1",
        snapshot_id="snap-1", document_id="d-1",
        tenant_id="t", project_id="p",
        content="hello", score=0.9, created_by_run_id="run-1",
    )]
    adapter, backend = _adapter_returning(hits)
    registry = _InMemoryRegistry([
        _doc(active_snapshot_id="snap-1", active_run_id="run-1"),
    ])
    svc = SearchService(adapter, registry)
    out = svc.search(ctx, "hello")
    assert backend.last_call["tenant_id"] == "t"
    assert backend.last_call["project_id"] == "p"
    assert backend.last_call["allowed_snapshot_ids"] == ["snap-1"]
    assert out[0].snapshot_id == "snap-1"


def test_search_service_returns_empty_when_no_active_snapshot(ctx):
    """A document with only ``active_run_id`` is NOT searchable —
    the eligibility filter excludes it and the adapter is never
    called."""
    from j1.integration.services import SearchService

    adapter, backend = _adapter_returning([])
    registry = _InMemoryRegistry([
        _doc(active_snapshot_id=None, active_run_id="run-1"),
    ])
    svc = SearchService(adapter, registry)
    out = svc.search(ctx, "hello")
    assert out == []
    # Adapter was NOT called — refusal short-circuits.
    assert not hasattr(backend, "last_call")


def test_search_service_excludes_detached_documents(ctx):
    """A detached document is invisible even when it has an
    ``active_snapshot_id``."""
    from j1.integration.services import SearchService

    adapter, backend = _adapter_returning([])
    registry = _InMemoryRegistry([
        _doc(active_snapshot_id="snap-1", knowledge_state="detached"),
    ])
    svc = SearchService(adapter, registry)
    out = svc.search(ctx, "hello")
    assert out == []
    assert not hasattr(backend, "last_call")


def test_search_service_only_passes_active_snapshots_to_adapter(ctx):
    """Mixed registry: some docs have snapshots, some don't.
    Only docs with ``active_snapshot_id`` AND ``knowledge_state="attached"``
    contribute to the allowlist."""
    from j1.integration.services import SearchService

    adapter, backend = _adapter_returning([])
    registry = _InMemoryRegistry([
        _doc("d-1", active_snapshot_id="snap-1"),
        _doc("d-2", active_snapshot_id=None, active_run_id="run-2"),
        _doc("d-3", active_snapshot_id="snap-3", knowledge_state="detached"),
        _doc("d-4", active_snapshot_id="snap-4"),
    ])
    svc = SearchService(adapter, registry)
    svc.search(ctx, "anything")
    assert sorted(backend.last_call["allowed_snapshot_ids"]) == [
        "snap-1", "snap-4",
    ]


# ---- SearchActivities default behavior -------------------------


def test_default_search_activity_does_not_dual_write_to_sqlite(
    audit_recorder, ctx,
):
    """Phase 4 default: ``J1_LEGACY_SQLITE_EVIDENCE_ENABLED`` is OFF.
    The activity returns success without consulting the legacy
    indexer map — even when the requested kind is missing."""
    activities = SearchActivities(
        audit=audit_recorder,
        indexers={},  # no SQLite indexer.,
    )
    result = activities.build_search_index_activity(
        SearchIndexInput(
            scope=ProjectScope.from_context(ctx),
            artifact_ids=["a"],
            processor_kind="anything",
        )
    )
    # Status is succeeded; legacy indexer was bypassed entirely.
    # Evidence adapter is also unwired in this test → count = 0.
    assert result.status == "succeeded"
    assert result.indexed_artifact_count == 0


@pytest.mark.skip(
    reason="Phase 8: legacy SQLite dual-write flag was deleted.",
)
def test_legacy_flag_re_enables_sqlite_dual_write(audit_recorder, ctx):
    """```` opts back into the old
    dual-write behaviour. Operators flip the env var when running
    a side-by-side comparison."""
    from j1.processing.results import ProcessingResult, ResultStatus

    class _Indexer:
        kind = "mock"

        def index(self, ctx, artifact_ids):
            return ProcessingResult(status=ResultStatus.SUCCEEDED)

    activities = SearchActivities(
        audit=audit_recorder,
        indexers={"mock": _Indexer()},
    )
    result = activities.build_search_index_activity(
        SearchIndexInput(
            scope=ProjectScope.from_context(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock",
        )
    )
    assert result.status == "succeeded"
    assert result.indexed_artifact_count == 2


# ---- Chunk resolver memoization --------------------------------


def test_chunk_resolver_memoizes_file_reads(tmp_path):
    """Phase 4: the same artifact_id requested twice within the
    resolver's lifetime reads the on-disk file ONCE."""
    from deploy.dev._wiring import _build_chunk_resolver
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))

    # Synthesize a chunk file on disk.
    compiled = workspace.compiled(ctx_object())
    compiled.mkdir(parents=True, exist_ok=True)
    body_path = compiled / "art-1.txt"
    body_path.write_text("snapshot scoped content", encoding="utf-8")

    read_count = {"n": 0}

    class _CountingArtifacts:
        def get(self, ctx, artifact_id):
            from j1.artifacts.models import ArtifactRecord
            from j1.jobs.status import ProcessingStatus, ReviewStatus
            read_count["n"] += 1
            return ArtifactRecord(
                artifact_id=artifact_id,
                project=ctx,
                kind="chunk",
                location=f"compiled/{artifact_id}.txt",
                content_hash="x",
                byte_size=1,
                status=ProcessingStatus.SUCCEEDED,
                review_status=ReviewStatus.NOT_REQUIRED,
                version=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata={"chunk_id": "c-1", "snapshot_id": "snap-1"},
            )

    resolver = _build_chunk_resolver(workspace, _CountingArtifacts())
    ctx = ctx_object()
    list(resolver(ctx, "art-1"))
    list(resolver(ctx, "art-1"))
    list(resolver(ctx, "art-1"))
    # The artifact registry is consulted ONCE; subsequent calls
    # hit the in-memory cache.
    assert read_count["n"] == 1


def ctx_object():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- Snapshot allocation idempotency ---------------------------


def test_get_or_create_for_run_is_idempotent_across_retries(tmp_path):
    """Phase 4 lazy-allocation contract: calling
    ``get_or_create_for_run`` twice for the same (document_id,
    run_id) returns the SAME snapshot. Activity retries don't
    create duplicate snapshots."""
    from j1.config.settings import Settings
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    store = JsonlDocumentSnapshotStore(workspace)
    service = DocumentSnapshotService(store=store)
    ctx = ctx_object()

    snap_a = service.get_or_create_for_run(
        ctx, document_id="d-1", run_id="run-X",
    )
    snap_b = service.get_or_create_for_run(
        ctx, document_id="d-1", run_id="run-X",
    )
    snap_c = service.get_or_create_for_run(
        ctx, document_id="d-1", run_id="run-X",
    )
    assert snap_a.snapshot_id == snap_b.snapshot_id == snap_c.snapshot_id


def test_get_or_create_for_run_distinguishes_different_run_ids(tmp_path):
    """Two runs for the same document → two different snapshots."""
    from j1.config.settings import Settings
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    store = JsonlDocumentSnapshotStore(workspace)
    service = DocumentSnapshotService(store=store)
    ctx = ctx_object()

    snap_x = service.get_or_create_for_run(
        ctx, document_id="d-1", run_id="run-X",
    )
    snap_y = service.get_or_create_for_run(
        ctx, document_id="d-1", run_id="run-Y",
    )
    assert snap_x.snapshot_id != snap_y.snapshot_id


# ---- Default facade wiring uses canonical adapter ---------------


def test_build_application_facade_uses_canonical_adapter(tmp_path, monkeypatch):
    """Phase 8: ``build_application_facade`` constructs a
    SearchService backed by ``PostgresFtsEvidenceAdapter``. The
    SQLite path was deleted; the builder requires a Postgres DSN +
    psycopg installed."""
    psycopg = pytest.importorskip("psycopg")
    from deploy.dev._wiring import build_application_facade
    from j1.config.settings import Settings
    from j1.search.evidence_adapter import PostgresFtsEvidenceAdapter
    from j1.workspace.resolver import WorkspaceResolver

    monkeypatch.setenv("J1_METADATA_DSN", "postgresql://j1:j1@stub/j1")
    monkeypatch.setenv("J1_EVIDENCE_BACKEND", "postgres_fts")

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    facade = build_application_facade(workspace)
    inner = facade.search._adapter
    assert isinstance(inner, PostgresFtsEvidenceAdapter), (
        f"facade.search wraps {type(inner).__name__}, expected Postgres FTS"
    )
    assert facade.search._indexer is None
