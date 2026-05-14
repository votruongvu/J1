"""EvidenceIndexAdapter tests — Phase 2."""

from __future__ import annotations

import pytest

from j1.documents.snapshot import IndexKind
from j1.projects.context import ProjectContext
from j1.search.evidence_adapter import (
    EvidenceIndexRequest,
    PostgresFtsEvidenceAdapter,
    select_evidence_adapter,
)


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _req(ctx, artifact_ids=("a-1", "a-2"), snapshot_id="snap-1"):
    return EvidenceIndexRequest(
        ctx=ctx,
        document_id="doc-1",
        snapshot_id=snapshot_id,
        created_by_run_id="run-1",
        artifact_ids=artifact_ids,
    )


# ---- Postgres FTS adapter (Phase 3 — real backend) --------------


class _FakePostgresBackend:
    """In-memory stand-in for ``PostgresFtsAdapter`` so tests can
    exercise the EvidenceIndexAdapter wiring without psycopg."""

    def __init__(self) -> None:
        self.indexed: list = []
        self.deleted: list[tuple] = []

    def index_chunks(self, chunks):
        self.indexed.extend(chunks)
        return len(chunks)

    def delete_for_snapshot(self, ctx, snapshot_id):
        self.deleted.append((ctx.tenant_id, ctx.project_id, snapshot_id))
        return 1

    def search(self, ctx, *, query, allowed_snapshot_ids, max_results):
        return []


def _stub_chunk_resolver(ctx, artifact_id):
    yield {"chunk_id": f"chunk-{artifact_id}", "content": f"body of {artifact_id}"}


def test_postgres_adapter_passes_chunks_to_backend(ctx):
    backend = _FakePostgresBackend()
    adapter = PostgresFtsEvidenceAdapter(
        backend=backend, chunk_resolver=_stub_chunk_resolver,
    )
    result = adapter.index(_req(ctx))
    assert result.success is True
    assert result.indexed_count == 2
    assert result.index_ref.provider == "postgres_fts"
    assert "snapshot=snap-1" in result.index_ref.location
    # Backend saw chunks scoped by snapshot + tenant + project.
    assert {c.snapshot_id for c in backend.indexed} == {"snap-1"}
    assert {c.tenant_id for c in backend.indexed} == {"t"}


def test_postgres_adapter_skips_empty_bodies(ctx):
    backend = _FakePostgresBackend()

    def _resolver(_ctx, artifact_id):
        yield {"chunk_id": artifact_id, "content": "   "}

    adapter = PostgresFtsEvidenceAdapter(
        backend=backend, chunk_resolver=_resolver,
    )
    result = adapter.index(_req(ctx))
    assert result.success is True
    assert result.indexed_count == 0
    assert backend.indexed == []


def test_postgres_adapter_delete_routes_to_backend(ctx):
    backend = _FakePostgresBackend()
    adapter = PostgresFtsEvidenceAdapter(
        backend=backend, chunk_resolver=_stub_chunk_resolver,
    )
    removed = adapter.delete_for_snapshot(ctx, "snap-1")
    assert removed == 1
    assert backend.deleted == [("t", "p", "snap-1")]


def test_postgres_adapter_surfaces_backend_exceptions(ctx):
    class _BoomBackend:
        def index_chunks(self, *_):
            raise RuntimeError("connection refused")

    adapter = PostgresFtsEvidenceAdapter(
        backend=_BoomBackend(), chunk_resolver=_stub_chunk_resolver,
    )
    result = adapter.index(_req(ctx))
    assert result.success is False
    assert "connection refused" in (result.error or "")


# ---- Dispatch -----------------------------------------------------


def test_select_evidence_adapter_routes_postgres_only():
    pg = select_evidence_adapter(
        "postgres_fts",
        postgres_backend=_FakePostgresBackend(),
        postgres_chunk_resolver=_stub_chunk_resolver,
    )
    assert pg.name == "postgres_fts"


def test_select_evidence_adapter_rejects_sqlite_in_phase_8():
    """Phase 8: ``sqlite_fts5`` is no longer a supported backend."""
    with pytest.raises(ValueError, match="unsupported evidence backend"):
        select_evidence_adapter("sqlite_fts5")


def test_select_evidence_adapter_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unsupported evidence backend"):
        select_evidence_adapter("opensearch")


def test_select_evidence_adapter_requires_postgres_dependencies():
    with pytest.raises(ValueError, match="postgres_fts evidence backend"):
        select_evidence_adapter("postgres_fts")
