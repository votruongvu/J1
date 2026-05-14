"""Live PostgreSQL FTS integration test — Phase 3 retry.

Runs against a real PostgreSQL database when ``J1_TEST_POSTGRES_DSN``
is set in the environment; otherwise SKIPS. CI and most contributors
will see the skip line; operators verifying the Postgres path before
shipping run it explicitly.

How to run
----------

1. Start a postgres (the dev docker-compose stack works):

    docker compose -f deploy/dev/docker-compose.yml up -d postgres

2. Export the DSN:

    export J1_TEST_POSTGRES_DSN=postgresql://j1:j1@localhost:5432/j1

3. Run the test:

    .venv/bin/pytest tests/integration/test_postgres_fts_live.py -v

The test creates the schema, inserts rows for two tenants × two
projects × two snapshots, runs the search with every combination
of filters, and asserts that the snapshot allowlist is honored
end-to-end.

The test uses a transactional table prefix (``test_evidence_chunks_*``)
so it can run repeatedly against the same database without
clashing with another caller's data. The test cleans up its
fixture rows on exit.
"""

from __future__ import annotations

import os
import uuid

import pytest

from j1.projects.context import ProjectContext

# Skip the entire module when there's no DSN — keeps CI green.
_DSN = os.environ.get("J1_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "J1_TEST_POSTGRES_DSN not set; live Postgres FTS test "
        "is skipped. See module docstring for setup."
    ),
)

# Imports are guarded so the module imports cleanly when psycopg
# isn't installed (skipif fires before any psycopg call happens).
psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def factory():
    def _factory():
        return psycopg.connect(_DSN)
    return _factory


@pytest.fixture
def schema_suffix():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def adapter(factory, schema_suffix):
    from j1.search.postgres_fts import PostgresFtsAdapter
    # Use a unique schema per test run so concurrent runs / leftover
    # rows don't collide. The dynamic schema name is harmless because
    # the adapter takes ``schema=`` at construction; tests don't share
    # an adapter across processes.
    a = PostgresFtsAdapter(
        factory,
        schema=f"j1_test_{schema_suffix}",
        table="evidence_chunks",
    )
    yield a
    # Cleanup: drop the test schema.
    conn = factory()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DROP SCHEMA IF EXISTS j1_test_{schema_suffix} CASCADE",
            )
            conn.commit()
    finally:
        conn.close()


def _chunk(
    *,
    tenant_id: str,
    project_id: str,
    document_id: str,
    snapshot_id: str,
    artifact_id: str,
    chunk_id: str | None,
    content: str,
):
    from j1.search.postgres_fts import EvidenceChunk
    return EvidenceChunk(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
        snapshot_id=snapshot_id,
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        content=content,
        created_by_run_id="run-1",
    )


def test_live_postgres_round_trip_respects_every_filter(adapter):
    """One single end-to-end exercise of the schema bootstrap +
    UPSERT + search path. Verifies tenant + project + snapshot
    filters are all honored on the live database."""
    chunks = [
        _chunk(
            tenant_id="t-a", project_id="p-1", document_id="doc-1",
            snapshot_id="snap-1", artifact_id="art-1", chunk_id="c-1",
            content="design phase deliverables include drawings",
        ),
        _chunk(
            tenant_id="t-a", project_id="p-1", document_id="doc-1",
            snapshot_id="snap-2", artifact_id="art-2", chunk_id="c-2",
            content="design phase deliverables include specifications",
        ),
        _chunk(
            tenant_id="t-a", project_id="p-2", document_id="doc-2",
            snapshot_id="snap-3", artifact_id="art-3", chunk_id="c-3",
            content="design phase deliverables include final set",
        ),
        _chunk(
            tenant_id="t-b", project_id="p-1", document_id="doc-4",
            snapshot_id="snap-4", artifact_id="art-4", chunk_id="c-4",
            content="design phase deliverables include site survey",
        ),
    ]
    adapter.index_chunks(chunks)

    ctx_a = ProjectContext(tenant_id="t-a", project_id="p-1", profile=None)
    ctx_b = ProjectContext(tenant_id="t-b", project_id="p-1", profile=None)

    # 1. Snapshot allowlist controls visibility.
    hits = adapter.search(
        ctx_a, query="design phase",
        allowed_snapshot_ids=["snap-1"], max_results=10,
    )
    assert [h.snapshot_id for h in hits] == ["snap-1"]
    assert "drawings" in hits[0].content

    # 2. Empty allowlist returns nothing without touching the DB.
    hits = adapter.search(
        ctx_a, query="design phase",
        allowed_snapshot_ids=[], max_results=10,
    )
    assert hits == []

    # 3. Tenant filter: t-b's snapshot is in the same query but
    # belongs to a different tenant — filtered out.
    hits = adapter.search(
        ctx_a, query="design phase",
        allowed_snapshot_ids=["snap-4"], max_results=10,
    )
    assert hits == []

    # 4. Project filter: t-a/p-2 snapshot can't be reached from
    # t-a/p-1 context.
    hits = adapter.search(
        ctx_a, query="design phase",
        allowed_snapshot_ids=["snap-3"], max_results=10,
    )
    assert hits == []

    # 5. Multi-snapshot allowlist within the same tenant + project.
    hits = adapter.search(
        ctx_a, query="design phase",
        allowed_snapshot_ids=["snap-1", "snap-2"], max_results=10,
    )
    assert {h.snapshot_id for h in hits} == {"snap-1", "snap-2"}

    # 6. Cross-tenant query reaches the right tenant's snapshot.
    hits = adapter.search(
        ctx_b, query="design phase",
        allowed_snapshot_ids=["snap-4"], max_results=10,
    )
    assert [h.snapshot_id for h in hits] == ["snap-4"]
    assert "site survey" in hits[0].content


def test_live_postgres_delete_for_snapshot_only_drops_the_snapshot(adapter):
    """``delete_for_snapshot`` must drop ONLY the rows it targets."""
    chunks = [
        _chunk(
            tenant_id="t-a", project_id="p-1", document_id="doc-1",
            snapshot_id="snap-a", artifact_id="art-1", chunk_id=None,
            content="alpha content",
        ),
        _chunk(
            tenant_id="t-a", project_id="p-1", document_id="doc-1",
            snapshot_id="snap-b", artifact_id="art-2", chunk_id=None,
            content="beta content",
        ),
    ]
    adapter.index_chunks(chunks)
    ctx = ProjectContext(tenant_id="t-a", project_id="p-1", profile=None)
    removed = adapter.delete_for_snapshot(ctx, "snap-a")
    assert removed == 1
    # snap-b survives.
    hits = adapter.search(
        ctx, query="alpha", allowed_snapshot_ids=["snap-a"], max_results=10,
    )
    assert hits == []
    hits = adapter.search(
        ctx, query="beta", allowed_snapshot_ids=["snap-b"], max_results=10,
    )
    assert len(hits) == 1
    assert hits[0].snapshot_id == "snap-b"


def test_live_postgres_upsert_replaces_content_for_same_natural_key(adapter):
    """Re-indexing the same (tenant, project, snapshot, artifact,
    chunk_id) tuple must REPLACE the content, not duplicate it."""
    base = dict(
        tenant_id="t-a", project_id="p-1", document_id="doc-1",
        snapshot_id="snap-1", artifact_id="art-1", chunk_id="c-1",
    )
    adapter.index_chunks([_chunk(**base, content="first body")])
    adapter.index_chunks([_chunk(**base, content="second body")])
    ctx = ProjectContext(tenant_id="t-a", project_id="p-1", profile=None)
    hits = adapter.search(
        ctx, query="second", allowed_snapshot_ids=["snap-1"], max_results=10,
    )
    assert len(hits) == 1
    assert hits[0].content == "second body"
