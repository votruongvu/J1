"""Real Postgres FTS evidence index — Phase 3.

Replaces the Phase-2 stub. Writes evidence chunks into a single
shared table ``j1.evidence_chunks`` (one row per chunk per
snapshot) and queries via ``tsvector @@ plainto_tsquery``.

Why one shared table and not per-snapshot tables: tsvector indexes
work best on stable schemas. Per-snapshot tables would multiply the
CREATE / VACUUM surface and make cross-snapshot queries painful
without buying anything — the natural-key filter
``(tenant_id, project_id, snapshot_id)`` is already a sub-second
index lookup on a properly indexed shared table.

Schema (idempotent CREATE on first use):

    CREATE SCHEMA IF NOT EXISTS j1;

    CREATE TABLE IF NOT EXISTS j1.evidence_chunks (
        id                 BIGSERIAL PRIMARY KEY,
        tenant_id          TEXT NOT NULL,
        project_id         TEXT NOT NULL,
        document_id        TEXT NOT NULL,
        snapshot_id        TEXT NOT NULL,
        artifact_id        TEXT NOT NULL,
        chunk_id           TEXT,
        content            TEXT NOT NULL,
        content_tsvector   tsvector
            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
        created_by_run_id  TEXT,
        metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS evidence_chunks_tsv_idx
        ON j1.evidence_chunks USING GIN(content_tsvector);
    CREATE INDEX IF NOT EXISTS evidence_chunks_scope_idx
        ON j1.evidence_chunks (tenant_id, project_id, document_id, snapshot_id);
    CREATE INDEX IF NOT EXISTS evidence_chunks_snapshot_idx
        ON j1.evidence_chunks (snapshot_id);
    CREATE UNIQUE INDEX IF NOT EXISTS evidence_chunks_unique_chunk_idx
        ON j1.evidence_chunks
        (tenant_id, project_id, snapshot_id, artifact_id, COALESCE(chunk_id, ''));

Reads ALWAYS filter by ``(tenant_id, project_id)`` plus an
explicit allowlist of ``snapshot_ids``. The lifecycle's eligibility
gate produces the allowlist; the adapter refuses queries without
one (no "read every snapshot" code path exists).

Why a connection factory: production wires this to
``psycopg.connect``; tests inject an in-memory stub that records
the SQL. The adapter never imports ``psycopg`` at module-load time
so the test suite runs without the optional dependency.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from j1.documents.snapshot import IndexKind, IndexRef
from j1.projects.context import ProjectContext

_log = logging.getLogger("j1.search.postgres_fts")


# ---- Connection seam --------------------------------------------


class _PgCursor(Protocol):
    """Minimal cursor surface this adapter uses. Matches
    ``psycopg.Cursor`` and ``psycopg.AsyncCursor`` (sync-mode only
    here) for the subset of methods we need; the test double
    implements the same shape."""

    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...
    def executemany(self, query: str, params_seq: Iterable[Sequence[Any]]) -> Any: ...
    def fetchall(self) -> list[tuple]: ...
    def fetchone(self) -> tuple | None: ...
    def close(self) -> None: ...


class _PgConnection(Protocol):
    def cursor(self) -> _PgCursor: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


class PostgresConnectionFactory(Protocol):
    """Hands the adapter a connection ready for use. The adapter
    consumes one connection per call and lets the factory decide
    pooling vs. per-call connect."""

    def __call__(self) -> _PgConnection: ...


# ---- Evidence chunk record --------------------------------------


@dataclass(frozen=True)
class EvidenceChunk:
    """One unit of indexed text. Producers (the compile adapter or a
    dedicated chunker) materialise these from artifact bodies."""

    tenant_id: str
    project_id: str
    document_id: str
    snapshot_id: str
    artifact_id: str
    chunk_id: str | None
    content: str
    created_by_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceHit:
    """One search hit. Carries snapshot lineage so the answer
    citations can resolve back to a document + snapshot."""

    artifact_id: str
    chunk_id: str | None
    snapshot_id: str
    document_id: str
    tenant_id: str
    project_id: str
    content: str
    score: float
    created_by_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---- Adapter ----------------------------------------------------


class PostgresFtsAdapter:
    """Real Postgres FTS evidence index.

    Methods:
      * ``ensure_schema()`` — idempotent DDL. Safe to call on every
        worker boot; the ``IF NOT EXISTS`` clauses make repeated
        calls cheap.
      * ``index_chunks(chunks)`` — UPSERT-style insert. The unique
        index on ``(tenant, project, snapshot, artifact, chunk_id)``
        makes re-runs of the same snapshot idempotent.
      * ``delete_for_snapshot(ctx, snapshot_id)`` — DELETE by the
        snapshot index. O(rows) but fenced by the BTREE.
      * ``search(...)`` — tsvector match scoped by tenant + project
        + allowed snapshots. Refuses to run without an explicit
        snapshot allowlist.
    """

    def __init__(
        self,
        factory: PostgresConnectionFactory,
        *,
        schema: str = "j1",
        table: str = "evidence_chunks",
    ) -> None:
        self._factory = factory
        self._schema = schema
        self._table = table
        self._schema_ready = False

    # ---- Schema bootstrap ---------------------------------------

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        ddl = [
            f"CREATE SCHEMA IF NOT EXISTS {self._schema}",
            (
                f"CREATE TABLE IF NOT EXISTS {self._schema}.{self._table} ("
                "id BIGSERIAL PRIMARY KEY, "
                "tenant_id TEXT NOT NULL, "
                "project_id TEXT NOT NULL, "
                "document_id TEXT NOT NULL, "
                "snapshot_id TEXT NOT NULL, "
                "artifact_id TEXT NOT NULL, "
                "chunk_id TEXT, "
                "content TEXT NOT NULL, "
                "content_tsvector tsvector GENERATED ALWAYS AS "
                "(to_tsvector('english', content)) STORED, "
                "created_by_run_id TEXT, "
                "metadata JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            ),
            (
                f"CREATE INDEX IF NOT EXISTS {self._table}_tsv_idx "
                f"ON {self._schema}.{self._table} USING GIN(content_tsvector)"
            ),
            (
                f"CREATE INDEX IF NOT EXISTS {self._table}_scope_idx "
                f"ON {self._schema}.{self._table} "
                "(tenant_id, project_id, document_id, snapshot_id)"
            ),
            (
                f"CREATE INDEX IF NOT EXISTS {self._table}_snapshot_idx "
                f"ON {self._schema}.{self._table} (snapshot_id)"
            ),
            (
                f"CREATE UNIQUE INDEX IF NOT EXISTS "
                f"{self._table}_unique_chunk_idx "
                f"ON {self._schema}.{self._table} "
                "(tenant_id, project_id, snapshot_id, artifact_id, "
                "COALESCE(chunk_id, ''))"
            ),
        ]
        conn = self._factory()
        try:
            cur = conn.cursor()
            try:
                for stmt in ddl:
                    cur.execute(stmt)
                conn.commit()
            finally:
                cur.close()
        finally:
            conn.close()
        self._schema_ready = True

    # ---- Writes --------------------------------------------------

    def index_chunks(self, chunks: Sequence[EvidenceChunk]) -> int:
        """UPSERT (on the unique chunk index) each row. Returns the
        rowcount the cursor reports — typically equal to ``len(chunks)``."""
        if not chunks:
            return 0
        self.ensure_schema()
        sql = (
            f"INSERT INTO {self._schema}.{self._table} ("
            "tenant_id, project_id, document_id, snapshot_id, "
            "artifact_id, chunk_id, content, created_by_run_id, metadata"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            f"ON CONFLICT (tenant_id, project_id, snapshot_id, "
            f"artifact_id, COALESCE(chunk_id, '')) "
            "DO UPDATE SET "
            "content = EXCLUDED.content, "
            "metadata = EXCLUDED.metadata, "
            "created_by_run_id = EXCLUDED.created_by_run_id"
        )
        rows = [
            (
                c.tenant_id, c.project_id, c.document_id, c.snapshot_id,
                c.artifact_id, c.chunk_id, c.content,
                c.created_by_run_id,
                json.dumps(c.metadata or {}),
            )
            for c in chunks
        ]
        conn = self._factory()
        try:
            cur = conn.cursor()
            try:
                cur.executemany(sql, rows)
                conn.commit()
                return len(chunks)
            finally:
                cur.close()
        finally:
            conn.close()

    def delete_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> int:
        """Drop every row for one snapshot. Always filters by tenant
        + project as well so a misaligned snapshot_id can't reach
        across projects."""
        self.ensure_schema()
        sql = (
            f"DELETE FROM {self._schema}.{self._table} "
            "WHERE tenant_id = %s AND project_id = %s AND snapshot_id = %s"
        )
        conn = self._factory()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, (
                    ctx.tenant_id, ctx.project_id, snapshot_id,
                ))
                conn.commit()
                # psycopg's cursor.rowcount is the affected rows.
                return int(getattr(cur, "rowcount", 0) or 0)
            finally:
                cur.close()
        finally:
            conn.close()

    # ---- Reads ---------------------------------------------------

    def search(
        self,
        ctx: ProjectContext,
        *,
        query: str,
        allowed_snapshot_ids: Sequence[str],
        max_results: int = 20,
    ) -> list[EvidenceHit]:
        """Lexical search over the evidence index. The allowlist is
        REQUIRED — the adapter refuses to run without it so a caller
        can never silently bypass the snapshot visibility boundary.

        Ranking: ``ts_rank_cd`` over the GIN-indexed tsvector with
        ``plainto_tsquery`` (handles operator-free user queries
        safely; no SQL injection surface)."""
        if not allowed_snapshot_ids:
            # Empty allowlist = no visible snapshots = empty result.
            # A caller that genuinely wants project-wide access has
            # to enumerate the document → snapshot mapping first;
            # the adapter never decides visibility on its own.
            return []
        if not query.strip():
            return []
        self.ensure_schema()
        placeholders = ", ".join(["%s"] * len(allowed_snapshot_ids))
        sql = (
            "SELECT artifact_id, chunk_id, snapshot_id, document_id, "
            "tenant_id, project_id, content, "
            "ts_rank_cd(content_tsvector, plainto_tsquery('english', %s)) AS score, "
            "created_by_run_id, metadata "
            f"FROM {self._schema}.{self._table} "
            "WHERE tenant_id = %s AND project_id = %s "
            f"AND snapshot_id IN ({placeholders}) "
            "AND content_tsvector @@ plainto_tsquery('english', %s) "
            "ORDER BY score DESC, id ASC "
            "LIMIT %s"
        )
        params: list[Any] = [query, ctx.tenant_id, ctx.project_id]
        params.extend(allowed_snapshot_ids)
        params.append(query)
        params.append(int(max_results))

        conn = self._factory()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            finally:
                cur.close()
        finally:
            conn.close()
        return [_row_to_hit(r) for r in rows]


def _row_to_hit(row: tuple) -> EvidenceHit:
    (
        artifact_id, chunk_id, snapshot_id, document_id,
        tenant_id, project_id, content, score, created_by_run_id, meta,
    ) = row
    if isinstance(meta, str):
        try:
            metadata = json.loads(meta)
        except json.JSONDecodeError:
            metadata = {}
    elif isinstance(meta, dict):
        metadata = meta
    else:
        metadata = {}
    return EvidenceHit(
        artifact_id=str(artifact_id),
        chunk_id=str(chunk_id) if chunk_id is not None else None,
        snapshot_id=str(snapshot_id),
        document_id=str(document_id),
        tenant_id=str(tenant_id),
        project_id=str(project_id),
        content=str(content or ""),
        score=float(score or 0.0),
        created_by_run_id=(
            str(created_by_run_id) if created_by_run_id is not None else None
        ),
        metadata=metadata,
    )


# ---- Default connection factory (psycopg-backed) ---------------


def make_psycopg_connection_factory(dsn: str):
    """Build a connection factory that returns a fresh psycopg
    connection on each call. Returns None when psycopg isn't
    installed — caller decides whether to fall back to SQLite or
    fail.

    Each call opens + closes one connection. For low-volume worker
    activity load this is fine; swap for ``psycopg_pool.ConnectionPool``
    when production traffic justifies it."""
    try:
        import psycopg  # noqa: F401 — lazy import so the optional dep stays optional
    except ImportError:
        return None

    def _factory():
        import psycopg
        return psycopg.connect(dsn)

    return _factory


__all__ = [
    "EvidenceChunk",
    "EvidenceHit",
    "PostgresConnectionFactory",
    "PostgresFtsAdapter",
    "make_psycopg_connection_factory",
]
