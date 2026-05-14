"""Snapshot-scoped cleanup — Phase 2.

The Phase-1 ``DocumentCleanupService`` deletes by ``run_id``. That's
correct for the run-centered model: every artifact + workspace
path is named after the run. The snapshot-centered model moves the
naming key to ``snapshot_id``, so the cleanup primitives need a
matching surface.

This module ships the primitives WITHOUT modifying the existing
service — both code paths coexist during the migration. Phase 3
deletes the run-keyed primitives once every caller has switched.

Cleanup policy (matches Phase 1 ``CleanupConfig``):

  * DEV: hard-delete on, no retention window. Failed snapshots get
    cleaned immediately so dev volumes don't fill up.
  * PROD: hard-delete off by default; SUPERSEDED snapshots are kept
    until the retention window elapses.

A snapshot in state ``BUILDING`` is allowed to be cleaned at any
time (the run that owned it failed). ``READY`` snapshots that
aren't promoted are kept (operator may want to inspect why a good
build didn't promote). ``PROMOTED`` (i.e. ``READY`` +
``promoted_at`` set + matching ``DocumentRecord.active_snapshot_id``)
snapshots are NEVER cleaned by routine policy.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from j1.documents.index_refs import IndexRefStore
from j1.documents.snapshot import SnapshotState
from j1.documents.snapshot_layout import SnapshotLayout
from j1.documents.snapshot_store import DocumentSnapshotStore
from j1.projects.context import ProjectContext

_log = logging.getLogger("j1.documents.snapshot_cleanup")


@dataclass
class SnapshotCleanupResult:
    snapshot_id: str
    removed_path: Path | None
    removed_index_refs: int
    purged_from_store: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class SnapshotCleanupService:
    """Coordinated cleanup of one snapshot:

      1. Delete workspace dir under ``{snapshot_root}``.
      2. Drop every ``IndexRef`` for the snapshot from the IndexRefStore.
         (Adapters that own physical indexes — Qdrant collection,
         Neo4j subgraph — handle their own DROP from the index_refs
         list returned here.)
      3. Purge the snapshot record from the JSONL store.

    Refuses to clean a promoted snapshot (``state == READY`` AND
    ``promoted_at != None``) unless ``force=True`` is passed —
    operators get an explicit override knob, routine policy can't
    nuke the active state by accident.
    """

    layout: SnapshotLayout
    store: DocumentSnapshotStore
    index_refs: IndexRefStore

    def cleanup_snapshot(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        snapshot_id: str,
        force: bool = False,
    ) -> SnapshotCleanupResult:
        snap = self.store.get(ctx, snapshot_id)
        result = SnapshotCleanupResult(
            snapshot_id=snapshot_id,
            removed_path=None,
            removed_index_refs=0,
            purged_from_store=False,
        )
        if snap is None:
            # Idempotent: nothing to clean.
            return result
        # Refuse to clean a promoted snapshot unless forced.
        is_promoted = (
            snap.state == SnapshotState.READY and snap.promoted_at is not None
        )
        if is_promoted and not force:
            result.errors.append(
                f"snapshot {snapshot_id!r} is promoted; refusing to "
                "clean without force=True"
            )
            return result

        # 1. Workspace dir.
        snap_root = self.layout.snapshot_root(
            ctx, document_id, snapshot_id,
        )
        if snap_root.exists():
            try:
                shutil.rmtree(snap_root)
                result.removed_path = snap_root
            except OSError as exc:
                result.errors.append(
                    f"workspace rmtree failed: {exc}"
                )

        # 2. Index refs (the adapters dispose of physical indexes
        # using these refs; here we just drop the registry rows so
        # cleanup is the same shape across providers).
        try:
            removed = self.index_refs.delete_for_snapshot(
                ctx, snapshot_id,
            )
            result.removed_index_refs = int(removed)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"index ref delete failed: {exc}"
            )

        # 3. Snapshot store row.
        try:
            result.purged_from_store = self.store.purge(ctx, snapshot_id)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"snapshot store purge failed: {exc}"
            )

        return result

    def cleanup_failed_snapshots(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
    ) -> list[SnapshotCleanupResult]:
        """Sweep every FAILED snapshot for a document. DEV policy
        default — operator-triggered in PROD."""
        out: list[SnapshotCleanupResult] = []
        for snap in self.store.list_for_document(
            ctx, document_id=document_id,
        ):
            if snap.state != SnapshotState.FAILED:
                continue
            out.append(self.cleanup_snapshot(
                ctx,
                document_id=document_id,
                snapshot_id=snap.snapshot_id,
            ))
        return out


__all__ = [
    "SnapshotCleanupResult",
    "SnapshotCleanupService",
]
