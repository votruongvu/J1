"""PostgresFtsAdapter — Phase 3 SQL-correctness tests.

Uses an in-memory connection double that records every executed
SQL statement + binding so we can verify:

  * Schema bootstrap issues the right DDL (idempotent).
  * Insert path UPSERTs by the unique-chunk key.
  * Delete path always filters by tenant + project + snapshot_id.
  * Search refuses without an explicit allowlist.
  * Search produces the expected WHERE clause shape (tenant,
    project, snapshot allowlist, tsvector match).

The test double does NOT execute the SQL — it captures it. That's
enough to lock the contract; the docker-compose postgres tests
that exercise real SQL belong to the integration tier.
"""

from __future__ import annotations

import pytest

from j1.projects.context import ProjectContext
from j1.search.postgres_fts import (
    EvidenceChunk,
    PostgresFtsAdapter,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, tuple(params or ())))
        self.rowcount = self._conn.next_rowcount
        return self

    def executemany(self, sql, rows):
        rows = list(rows)
        for r in rows:
            self._conn.executed.append((sql, tuple(r)))
        self.rowcount = len(rows)
        return self

    def fetchall(self):
        return list(self._conn.fetch_queue)

    def fetchone(self):
        if not self._conn.fetch_queue:
            return None
        return self._conn.fetch_queue[0]

    def close(self):
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.committed = 0
        self.closed = False
        self.next_rowcount = 0
        self.fetch_queue: list[tuple] = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_factory():
    """Returns ``(factory, conns)``. ``conns`` is the list of
    connections that the factory has handed out — tests inspect
    its executed SQL after the call."""
    conns: list[_FakeConn] = []

    def _factory():
        c = _FakeConn()
        conns.append(c)
        return c

    return _factory, conns


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t-1", project_id="p-1", profile=None)


# ---- Schema --------------------------------------------------------


def test_ensure_schema_emits_idempotent_ddl(fake_factory):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    adapter.ensure_schema()
    sqls = [s for s, _ in conns[0].executed]
    joined = "\n".join(sqls)
    assert "CREATE SCHEMA IF NOT EXISTS j1" in joined
    assert "CREATE TABLE IF NOT EXISTS j1.evidence_chunks" in joined
    assert "content_tsvector tsvector GENERATED ALWAYS" in joined
    assert "USING GIN(content_tsvector)" in joined
    assert "tenant_id, project_id, document_id, snapshot_id" in joined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in joined


def test_ensure_schema_is_idempotent_skip_after_first(fake_factory):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    adapter.ensure_schema()
    adapter.ensure_schema()
    # Second call must not issue another DDL — the adapter caches
    # the "ready" flag in process memory.
    assert len(conns) == 1


# ---- Index ---------------------------------------------------------


def test_index_chunks_emits_upsert_with_snapshot_natural_key(
    fake_factory, ctx,
):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    chunks = [
        EvidenceChunk(
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            document_id="doc-1",
            snapshot_id="snap-1",
            artifact_id="art-1",
            chunk_id="c-1",
            content="hello world",
            created_by_run_id="run-1",
            metadata={"kind": "chunk"},
        ),
    ]
    rows = adapter.index_chunks(chunks)
    assert rows == 1
    # ``ensure_schema`` runs on the first call and uses one
    # connection; the INSERT path uses a fresh one. Walk every
    # connection looking for the INSERT.
    insert_calls = [
        (sql, params)
        for conn in conns
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO j1.evidence_chunks")
    ]
    assert insert_calls, "expected an INSERT statement"
    sql, params = insert_calls[0]
    assert "ON CONFLICT (tenant_id, project_id, snapshot_id, artifact_id" in sql
    assert "DO UPDATE SET" in sql
    # Parameter order matches the column list in the INSERT.
    assert params[0:4] == ("t-1", "p-1", "doc-1", "snap-1")
    assert params[4] == "art-1"
    assert params[6] == "hello world"


def test_index_chunks_empty_input_is_noop(fake_factory):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    assert adapter.index_chunks([]) == 0
    # No connection acquired for an empty input.
    assert conns == []


# ---- Delete --------------------------------------------------------


def test_delete_filters_by_tenant_project_snapshot(fake_factory, ctx):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    conns_before = list(conns)
    # First call triggers ensure_schema; reset rowcount for the
    # DELETE we're checking.
    adapter.delete_for_snapshot(ctx, "snap-1")
    delete_calls = [
        (sql, params) for sql, params in conns[-1].executed
        if sql.startswith("DELETE FROM j1.evidence_chunks")
    ]
    assert delete_calls
    sql, params = delete_calls[0]
    assert "WHERE tenant_id = %s AND project_id = %s AND snapshot_id = %s" in sql
    assert params == ("t-1", "p-1", "snap-1")


# ---- Search --------------------------------------------------------


def test_search_refuses_without_allowlist(fake_factory, ctx):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    hits = adapter.search(
        ctx, query="anything", allowed_snapshot_ids=[], max_results=10,
    )
    assert hits == []
    # No connection used — the visibility refusal short-circuits.
    assert conns == []


def test_search_emits_tsvector_clause_with_snapshot_allowlist(
    fake_factory, ctx,
):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)
    adapter.search(
        ctx,
        query="design phases",
        allowed_snapshot_ids=["snap-a", "snap-b"],
        max_results=5,
    )
    select_calls = [
        (sql, params) for sql, params in conns[-1].executed
        if sql.startswith("SELECT")
    ]
    assert select_calls
    sql, params = select_calls[0]
    assert "WHERE tenant_id = %s AND project_id = %s" in sql
    # Two snapshot ids → two placeholders.
    assert "snapshot_id IN (%s, %s)" in sql
    assert "content_tsvector @@ plainto_tsquery('english', %s)" in sql
    assert "ORDER BY score DESC" in sql
    # Param order: tsquery, tenant, project, snap-a, snap-b, tsquery, limit.
    assert params[0] == "design phases"
    assert params[1:3] == ("t-1", "p-1")
    assert tuple(params[3:5]) == ("snap-a", "snap-b")
    assert params[5] == "design phases"
    assert params[6] == 5


def test_search_decodes_rows_into_evidence_hits(fake_factory, ctx):
    factory, conns = fake_factory
    adapter = PostgresFtsAdapter(factory)

    # Pre-populate the fetch queue on the connection that the
    # search call will receive.
    def _factory_with_rows():
        c = _FakeConn()
        c.fetch_queue = [(
            "art-1", "chunk-1", "snap-1", "doc-1",
            "t-1", "p-1", "design phases body", 0.85,
            "run-1", '{"kind": "chunk"}',
        )]
        conns.append(c)
        return c

    adapter_with_rows = PostgresFtsAdapter(_factory_with_rows)
    hits = adapter_with_rows.search(
        ctx, query="design",
        allowed_snapshot_ids=["snap-1"],
        max_results=10,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert hit.artifact_id == "art-1"
    assert hit.chunk_id == "chunk-1"
    assert hit.snapshot_id == "snap-1"
    assert hit.score == pytest.approx(0.85)
    assert hit.created_by_run_id == "run-1"
    assert hit.metadata == {"kind": "chunk"}
