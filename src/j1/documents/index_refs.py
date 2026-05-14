"""IndexRef store — Phase 2.

Tracks which physical indexes (Qdrant collection / Neo4j subgraph /
Postgres FTS partition / RAGAnything workspace) belong to which
snapshot. The store is the single source of truth for the
"snapshot → index pointers" join the cleanup + query paths need.

Why separate from ``DocumentSnapshotStore``: the snapshot record
embeds an ``index_refs`` tuple for convenience, but index refs are
written by **adapters** (each provider knows its own location
shape) at different points in the lifecycle than the snapshot
record. Decoupling lets adapters append refs without rewriting the
snapshot, and lets the cleanup path query refs by provider for
dispatch without scanning every snapshot.

Layout: JSON blob under the workspace's runtime area, keyed by
(snapshot_id, kind, provider). Append/replace semantics are
explicit (``register`` always overwrites the (snapshot, kind,
provider) triple).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Protocol

from j1._serialization import to_jsonable
from j1.documents.snapshot import IndexKind, IndexRef
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

INDEX_REFS_FILENAME = "index_refs.json"
INDEX_REFS_VERSION = 1


# ---- Protocol --------------------------------------------------


class IndexRefStore(Protocol):
    def register(self, ctx: ProjectContext, ref: IndexRef) -> None: ...

    def list_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> list[IndexRef]: ...

    def list_by_provider(
        self,
        ctx: ProjectContext,
        provider: str,
        *,
        kind: IndexKind | None = None,
    ) -> list[IndexRef]: ...

    def delete_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> int: ...


# ---- JSON implementation ---------------------------------------


class JsonIndexRefStore:
    """JSON-blob store. Reads + writes the whole file because refs
    are small (a few dozen per project) and the read patterns want
    multi-key joins (snapshot×provider×kind).

    File shape:
        {
          "version": 1,
          "refs": [
            {"snapshot_id": "...", "kind": "vector", "provider": "qdrant", "location": "...", "stats": {...}},
            ...
          ]
        }
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    # ---- Path ----------------------------------------------------

    def _path(self, ctx: ProjectContext) -> Path:
        return (
            self._workspace.area(ctx, WorkspaceArea.RUNTIME) / INDEX_REFS_FILENAME
        )

    # ---- Reads ---------------------------------------------------

    def _read(self, ctx: ProjectContext) -> list[IndexRef]:
        path = self._path(ctx)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        refs = payload.get("refs", [])
        return [_deserialize(r) for r in refs if isinstance(r, dict)]

    def list_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> list[IndexRef]:
        return [r for r in self._read(ctx) if r.snapshot_id == snapshot_id]

    def list_by_provider(
        self,
        ctx: ProjectContext,
        provider: str,
        *,
        kind: IndexKind | None = None,
    ) -> list[IndexRef]:
        out = [r for r in self._read(ctx) if r.provider == provider]
        if kind is not None:
            out = [r for r in out if r.kind == kind]
        return out

    # ---- Writes --------------------------------------------------

    def register(self, ctx: ProjectContext, ref: IndexRef) -> None:
        """Register or replace the ref for ``(snapshot_id, kind,
        provider)``. The triple is the natural key — there's only
        one physical index per (snapshot, kind, provider)."""
        refs = self._read(ctx)
        key = (ref.snapshot_id, ref.kind, ref.provider)
        without_key = [
            r for r in refs
            if (r.snapshot_id, r.kind, r.provider) != key
        ]
        without_key.append(ref)
        self._write(ctx, without_key)

    def delete_for_snapshot(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> int:
        refs = self._read(ctx)
        kept = [r for r in refs if r.snapshot_id != snapshot_id]
        removed = len(refs) - len(kept)
        if removed == 0:
            return 0
        self._write(ctx, kept)
        return removed

    def _write(self, ctx: ProjectContext, refs: Iterable[IndexRef]) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_REFS_VERSION,
            "refs": [to_jsonable(r) for r in refs],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)


# ---- Deserialisation -------------------------------------------


def _deserialize(payload: dict) -> IndexRef:
    return IndexRef(
        snapshot_id=str(payload["snapshot_id"]),
        kind=IndexKind(payload["kind"]),
        provider=str(payload["provider"]),
        location=str(payload["location"]),
        stats=dict(payload.get("stats", {})),
    )


__all__ = [
    "INDEX_REFS_FILENAME",
    "INDEX_REFS_VERSION",
    "IndexRefStore",
    "JsonIndexRefStore",
]
