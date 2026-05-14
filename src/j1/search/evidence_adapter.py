"""EvidenceIndexAdapter — Phase 2 seam for lexical / evidence search.

The Phase 1 policy: Postgres FTS is the strategic default; SQLite
BM25 stays as legacy / debug / manual comparison only and is
explicitly NOT the primary answer engine. Phase 2 introduces the
adapter so the lifecycle can talk to "an evidence index" without
caring which backend implements it.

Two adapters ship today:

* ``SqliteEvidenceAdapter`` — wraps the existing
  ``SqliteSearchIndexer``. Tags each indexed artifact with its
  ``snapshot_id`` (was: ``run_id``). The underlying FTS5 table now
  stores both columns side-by-side; Phase 3 retires ``run_id``.

* ``PostgresFtsEvidenceAdapter`` — stub. The connection plumbing
  comes online when Phase 2's docker-compose postgres is wired into
  the test harness; today the adapter just records the operation
  for diagnostics and returns success. Stubbing here lets the
  lifecycle exercise the snapshot-scoped path even before the
  backend lands.

The adapter MUST NOT become the primary answer engine — the
SmartQueryOrchestrator stays in charge. Evidence index is a *
recall* surface, not the synthesizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from j1.documents.snapshot import IndexKind, IndexRef
from j1.projects.context import ProjectContext


# ---- Request / result shapes -----------------------------------


@dataclass(frozen=True)
class EvidenceIndexRequest:
    ctx: ProjectContext
    document_id: str
    snapshot_id: str
    created_by_run_id: str
    artifact_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceIndexResult:
    success: bool
    indexed_count: int
    index_ref: IndexRef
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---- Adapter protocol ------------------------------------------


class EvidenceIndexAdapter(Protocol):
    """Index a snapshot's artifacts into a lexical store + delete
    them by snapshot. Provider field on the returned ``IndexRef``
    tells the cleanup path which adapter to dispatch to."""

    name: str

    def index(self, request: EvidenceIndexRequest) -> EvidenceIndexResult: ...

    def delete_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> int: ...


# ---- Postgres FTS adapter (canonical) --------------------------


class PostgresFtsEvidenceAdapter:
    """Real Postgres FTS evidence index — Phase 3.

    Delegates the actual SQL to :class:`PostgresFtsAdapter` (from
    ``j1.search.postgres_fts``), which writes evidence chunks into
    the shared ``j1.evidence_chunks`` table behind a GIN index.

    The adapter's job at this layer is the translation:
      * Walk ``request.artifact_ids`` against the artifact registry
        + chunk-body resolver to materialise a list of ``EvidenceChunk``
        rows.
      * Hand them to the underlying backend.
      * Return an ``IndexRef`` so the snapshot lifecycle can route
        cleanup back through ``delete_for_snapshot``.

    The chunk resolver is injected (``chunk_resolver``) so production
    can read bodies off the artifact registry while tests can pass a
    deterministic stub.
    """

    name = "postgres_fts"

    def __init__(
        self,
        *,
        backend,
        chunk_resolver,
        schema: str = "j1",
    ) -> None:
        # ``backend`` is a ``PostgresFtsAdapter`` (or any test-double
        # that exposes ``index_chunks`` / ``delete_for_snapshot`` /
        # ``search``). Keeping the import lazy means the package can
        # be imported in environments without psycopg installed.
        self._backend = backend
        self._chunk_resolver = chunk_resolver
        self._schema = schema

    def index(
        self, request: EvidenceIndexRequest,
    ) -> EvidenceIndexResult:
        from j1.search.postgres_fts import EvidenceChunk
        try:
            chunks: list[EvidenceChunk] = []
            for artifact_id in request.artifact_ids:
                for entry in self._chunk_resolver(
                    request.ctx, artifact_id,
                ):
                    body = (entry.get("content") or "").strip()
                    if not body:
                        continue
                    chunks.append(EvidenceChunk(
                        tenant_id=request.ctx.tenant_id,
                        project_id=request.ctx.project_id,
                        document_id=request.document_id,
                        snapshot_id=request.snapshot_id,
                        artifact_id=str(artifact_id),
                        chunk_id=entry.get("chunk_id"),
                        content=body,
                        created_by_run_id=request.created_by_run_id,
                        metadata=dict(entry.get("metadata") or {}),
                    ))
            indexed = self._backend.index_chunks(chunks)
        except Exception as exc:  # noqa: BLE001 — surface backend failure
            return EvidenceIndexResult(
                success=False,
                indexed_count=0,
                index_ref=self._ref(request, indexed=0),
                error=f"{type(exc).__name__}: {exc}",
            )
        return EvidenceIndexResult(
            success=True,
            indexed_count=indexed,
            index_ref=self._ref(request, indexed=indexed),
            metadata={
                "backend": "postgres_fts",
                "schema": self._schema,
            },
        )

    def delete_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> int:
        return int(self._backend.delete_for_snapshot(ctx, snapshot_id))

    def search(
        self,
        ctx: ProjectContext,
        *,
        query: str,
        allowed_snapshot_ids,
        max_results: int = 20,
    ):
        """Pass-through to the backend's ``search`` so callers that
        already hold the adapter can run lexical queries without
        importing ``postgres_fts`` directly."""
        return self._backend.search(
            ctx,
            query=query,
            allowed_snapshot_ids=list(allowed_snapshot_ids),
            max_results=max_results,
        )

    def _ref(
        self,
        request: EvidenceIndexRequest,
        *,
        indexed: int,
    ) -> IndexRef:
        location = (
            f"{self._schema}.evidence_chunks"
            f"#tenant={request.ctx.tenant_id}"
            f"&project={request.ctx.project_id}"
            f"&snapshot={request.snapshot_id}"
        )
        return IndexRef(
            snapshot_id=request.snapshot_id,
            kind=IndexKind.EVIDENCE,
            provider="postgres_fts",
            location=location,
            stats={"indexed_chunks": indexed},
        )


# ---- Dispatch helper -------------------------------------------


def select_evidence_adapter(
    backend: str,
    *,
    postgres_backend=None,
    postgres_chunk_resolver=None,
    postgres_schema: str = "j1",
) -> EvidenceIndexAdapter:
    """Phase 8: PostgreSQL FTS is the only supported evidence
    backend. ``sqlite_fts5`` was deleted.

    Required injections:
      * ``postgres_backend`` — a ``PostgresFtsAdapter``.
      * ``postgres_chunk_resolver`` — a callable that yields
        chunks per artifact_id.

    Any other backend value raises ``ValueError``.
    """
    if backend != "postgres_fts":
        raise ValueError(
            f"unsupported evidence backend: {backend!r}. Phase 8 "
            "only supports 'postgres_fts'."
        )
    if postgres_backend is None or postgres_chunk_resolver is None:
        raise ValueError(
            "postgres_fts evidence backend requires both "
            "postgres_backend and postgres_chunk_resolver"
        )
    return PostgresFtsEvidenceAdapter(
        backend=postgres_backend,
        chunk_resolver=postgres_chunk_resolver,
        schema=postgres_schema,
    )


__all__ = [
    "EvidenceIndexAdapter",
    "EvidenceIndexRequest",
    "EvidenceIndexResult",
    "PostgresFtsEvidenceAdapter",
    "select_evidence_adapter",
]
