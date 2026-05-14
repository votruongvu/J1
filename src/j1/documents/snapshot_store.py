"""JSONL-backed persistence for ``DocumentSnapshot``.

Mirrors ``JsonlIngestionRunStore`` — every upsert appends a fresh
record; readers reconstruct latest-per-snapshot by replaying the
log. Same retention / backup semantics as the audit area.

Why JSONL: writes are O(1) (open-append-close, no locks needed
beyond a single fsync) and reads scan a small file (typical
projects have dozens of snapshots, not millions). The state
machine is also strictly forward-only (``building → ready →
superseded`` or ``building → failed``) so an append log is the
right shape — every transition is preserved.

For deployments that outgrow JSONL the ``DocumentSnapshotStore``
Protocol stays the read-write seam; swap the implementation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Protocol

from j1._serialization import to_jsonable
from j1.documents.snapshot import (
    DocumentSnapshot,
    IndexRef,
    SnapshotState,
)
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

SNAPSHOTS_FILENAME = "document_snapshots.jsonl"


class SnapshotConflictError(Exception):
    """Raised by ``promote`` when the CAS precondition doesn't match
    the on-disk state. Caller decides whether to refresh and retry
    or give up."""


class DocumentSnapshotStore(Protocol):
    def upsert(self, ctx: ProjectContext, snapshot: DocumentSnapshot) -> None: ...

    def get(self, ctx: ProjectContext, snapshot_id: str) -> DocumentSnapshot | None: ...

    def list_for_document(
        self, ctx: ProjectContext, *, document_id: str,
    ) -> list[DocumentSnapshot]: ...

    def list(
        self,
        ctx: ProjectContext,
        *,
        states: Iterable[SnapshotState] | None = None,
    ) -> list[DocumentSnapshot]: ...

    def purge(self, ctx: ProjectContext, snapshot_id: str) -> bool: ...


# ---- JSONL implementation ----------------------------------------


class JsonlDocumentSnapshotStore:
    """JSONL append-only store. Latest-per-snapshot_id wins on read."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    # ---- Path ----------------------------------------------------

    def _path(self, ctx: ProjectContext):
        return (
            self._workspace.area(ctx, WorkspaceArea.AUDIT) / SNAPSHOTS_FILENAME
        )

    # ---- Writes --------------------------------------------------

    def upsert(self, ctx: ProjectContext, snapshot: DocumentSnapshot) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(snapshot), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    # ---- Reads ---------------------------------------------------

    def get(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> DocumentSnapshot | None:
        latest: DocumentSnapshot | None = None
        for snap in self._iter_all(ctx):
            if snap.snapshot_id == snapshot_id:
                latest = snap
        return latest

    def list_for_document(
        self, ctx: ProjectContext, *, document_id: str,
    ) -> list[DocumentSnapshot]:
        latest_by_id: dict[str, DocumentSnapshot] = {}
        for snap in self._iter_all(ctx):
            if snap.document_id != document_id:
                continue
            latest_by_id[snap.snapshot_id] = snap
        out = list(latest_by_id.values())
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def list(
        self,
        ctx: ProjectContext,
        *,
        states: Iterable[SnapshotState] | None = None,
    ) -> list[DocumentSnapshot]:
        latest_by_id: dict[str, DocumentSnapshot] = {}
        for snap in self._iter_all(ctx):
            latest_by_id[snap.snapshot_id] = snap
        out = list(latest_by_id.values())
        if states is not None:
            allowed = set(states)
            out = [s for s in out if s.state in allowed]
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    # ---- Mutating sweeps -----------------------------------------

    def purge(self, ctx: ProjectContext, snapshot_id: str) -> bool:
        path = self._path(ctx)
        if not path.exists():
            return False
        kept: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(stripped)
                    continue
                if str(payload.get("snapshot_id")) == snapshot_id:
                    removed += 1
                    continue
                kept.append(stripped)
        if removed == 0:
            return False
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line)
                fh.write("\n")
        tmp.replace(path)
        return True

    # ---- Iteration -----------------------------------------------

    def _iter_all(self, ctx: ProjectContext):
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                yield _deserialize(payload)


# ---- (De)serialisation ------------------------------------------


def _deserialize(payload: dict) -> DocumentSnapshot:
    """Rehydrate a snapshot from the JSON payload. Missing optional
    fields fall back to safe defaults so old log lines deserialise
    cleanly."""
    return DocumentSnapshot(
        snapshot_id=str(payload["snapshot_id"]),
        document_id=str(payload["document_id"]),
        tenant_id=str(payload["tenant_id"]),
        project_id=str(payload["project_id"]),
        created_by_run_id=str(payload["created_by_run_id"]),
        state=SnapshotState(payload["state"]),
        created_at=_dt(payload.get("created_at")),
        promoted_at=_dt(payload.get("promoted_at"), allow_none=True),
        superseded_at=_dt(payload.get("superseded_at"), allow_none=True),
        index_refs=tuple(_index_ref(r) for r in payload.get("index_refs", ())),
        summary=dict(payload.get("summary", {})),
    )


def _index_ref(payload: dict) -> IndexRef:
    from j1.documents.snapshot import IndexKind
    return IndexRef(
        snapshot_id=str(payload["snapshot_id"]),
        kind=IndexKind(payload["kind"]),
        provider=str(payload["provider"]),
        location=str(payload["location"]),
        stats=dict(payload.get("stats", {})),
    )


def _dt(value, *, allow_none: bool = False) -> datetime:
    if value is None:
        if allow_none:
            return None  # type: ignore[return-value]
        raise ValueError("missing required datetime field on snapshot row")
    if isinstance(value, datetime):
        return value
    # Accept ISO-8601 strings (the to_jsonable default for datetimes).
    return datetime.fromisoformat(str(value))


__all__ = [
    "DocumentSnapshotStore",
    "JsonlDocumentSnapshotStore",
    "SNAPSHOTS_FILENAME",
    "SnapshotConflictError",
]
