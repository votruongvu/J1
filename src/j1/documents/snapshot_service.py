"""DocumentSnapshotService — orchestrates the snapshot lifecycle.

Phase 2 introduces this service as the ONLY way to change a
document's visibility state. The lifecycle is strictly forward-only:

    BUILDING  ── (compile / enrich / graph all succeed) ──▶  READY
        │                                                      │
        │ (any stage fails)                                    │ (promote-on-success)
        ▼                                                      ▼
    FAILED                                            (active_snapshot_id ← snap)
                                                       previous active ──▶ SUPERSEDED

Why a service (not a free function): promotion is CAS-guarded
against the document's current ``active_snapshot_id``, and the
supersede step touches the previous snapshot's state — all three
writes (snapshot.READY → active, previous → SUPERSEDED, document
update) must be coordinated. The service is the single place that
coordinates them and emits the audit trail.

run_id appears here only as ``created_by_run_id`` on the snapshot
record. The service never reads ``run_id`` to make a visibility
decision."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from j1.documents.snapshot import (
    DocumentSnapshot,
    IndexRef,
    SnapshotState,
)
from j1.documents.snapshot_store import (
    DocumentSnapshotStore,
    SnapshotConflictError,
)
from j1.projects.context import ProjectContext


# ---- Errors ----------------------------------------------------


class InvalidSnapshotTransitionError(Exception):
    """Raised when a state-change call doesn't match the snapshot's
    current state. Callers should treat as a programming error and
    not retry without reconciling first."""


# ---- Service ---------------------------------------------------


@dataclass
class DocumentSnapshotService:
    """All snapshot state changes go through here. Constructed with
    its store + an optional clock injection (tests use a fixed
    datetime; production uses real wall-clock)."""

    store: DocumentSnapshotStore
    clock: "Clock | None" = None

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    # ---- Create -------------------------------------------------

    def create_candidate(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        created_by_run_id: str,
        snapshot_id: str | None = None,
    ) -> DocumentSnapshot:
        """Allocate a new ``BUILDING`` snapshot. Caller stamps the
        returned ``snapshot_id`` onto the ``IngestionRun``'s
        ``target_snapshot_id`` so the run knows which candidate it's
        producing."""
        sid = snapshot_id or _new_snapshot_id()
        snap = DocumentSnapshot(
            snapshot_id=sid,
            document_id=document_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            created_by_run_id=created_by_run_id,
            state=SnapshotState.BUILDING,
            created_at=self._now(),
        )
        self.store.upsert(ctx, snap)
        return snap

    def get_or_create_for_run(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        run_id: str,
    ) -> DocumentSnapshot:
        """Phase-3 helper for the Temporal activity layer.

        Returns the BUILDING/READY/SUPERSEDED snapshot created by
        ``run_id`` for ``document_id`` if one exists; otherwise
        creates a fresh BUILDING candidate. Activities call this on
        every artifact-materialise event so the snapshot allocation
        is lazy + idempotent across retries.
        """
        for snap in self.store.list_for_document(
            ctx, document_id=document_id,
        ):
            if snap.created_by_run_id == run_id:
                return snap
        return self.create_candidate(
            ctx,
            document_id=document_id,
            created_by_run_id=run_id,
        )

    # ---- Transitions --------------------------------------------

    def attach_index_ref(
        self,
        ctx: ProjectContext,
        *,
        snapshot_id: str,
        ref: IndexRef,
    ) -> DocumentSnapshot:
        """Append an index ref to a snapshot's embedded ``index_refs``
        tuple. The external ``IndexRefStore`` is the canonical
        source — this method keeps the embedded copy in sync so the
        snapshot record alone is enough for diagnostics."""
        snap = self._require(ctx, snapshot_id)
        if snap.state != SnapshotState.BUILDING:
            raise InvalidSnapshotTransitionError(
                f"cannot attach index ref to snapshot in state "
                f"{snap.state.value!r}"
            )
        new_refs = tuple(
            r for r in snap.index_refs
            if (r.kind, r.provider) != (ref.kind, ref.provider)
        ) + (ref,)
        updated = _replace(snap, index_refs=new_refs)
        self.store.upsert(ctx, updated)
        return updated

    def mark_ready(
        self,
        ctx: ProjectContext,
        *,
        snapshot_id: str,
        summary: dict | None = None,
    ) -> DocumentSnapshot:
        """Transition ``BUILDING → READY``. Caller invokes after
        compile / enrich / graph all succeed and the index refs are
        attached. The snapshot is now eligible for promotion."""
        snap = self._require(ctx, snapshot_id)
        if snap.state != SnapshotState.BUILDING:
            raise InvalidSnapshotTransitionError(
                f"mark_ready: snapshot {snapshot_id!r} is in state "
                f"{snap.state.value!r}, expected BUILDING"
            )
        updated = _replace(
            snap,
            state=SnapshotState.READY,
            summary=dict(snap.summary or {}, **(summary or {})),
        )
        self.store.upsert(ctx, updated)
        return updated

    def mark_failed(
        self,
        ctx: ProjectContext,
        *,
        snapshot_id: str,
        reason: str | None = None,
    ) -> DocumentSnapshot:
        """Transition ``BUILDING → FAILED``. Failed candidates never
        become active; the document's existing ``active_snapshot_id``
        (if any) is untouched."""
        snap = self._require(ctx, snapshot_id)
        if snap.state not in {SnapshotState.BUILDING, SnapshotState.FAILED}:
            raise InvalidSnapshotTransitionError(
                f"mark_failed: snapshot {snapshot_id!r} is in state "
                f"{snap.state.value!r}, expected BUILDING"
            )
        summary = dict(snap.summary or {})
        if reason:
            summary["failure_reason"] = reason
        updated = _replace(
            snap, state=SnapshotState.FAILED, summary=summary,
        )
        self.store.upsert(ctx, updated)
        return updated

    # ---- Promotion ----------------------------------------------

    def promote(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        snapshot_id: str,
        previous_active_snapshot_id: str | None,
    ) -> tuple[DocumentSnapshot, DocumentSnapshot | None]:
        """Atomically promote ``snapshot_id`` to the active position.

        The CAS precondition is ``previous_active_snapshot_id`` — the
        caller's view of which snapshot is currently active. When
        the on-disk record says something different (another promote
        ran concurrently), raise ``SnapshotConflictError`` and let
        the caller retry.

        Returns ``(new_active_snapshot, previous_active_snapshot)``.
        ``previous_active_snapshot`` is None for the first promotion
        on a document.
        """
        snap = self._require(ctx, snapshot_id)
        if snap.document_id != document_id:
            raise InvalidSnapshotTransitionError(
                f"promote: snapshot {snapshot_id!r} belongs to "
                f"document {snap.document_id!r}, not {document_id!r}"
            )
        if snap.state != SnapshotState.READY:
            raise InvalidSnapshotTransitionError(
                f"promote: snapshot {snapshot_id!r} is in state "
                f"{snap.state.value!r}, expected READY"
            )

        # Resolve the *actual* current active by scanning the
        # document's snapshots for state=PROMOTED. We don't trust the
        # DocumentRecord here — the snapshot store is authoritative,
        # and the DocumentRecord field is a denormalisation the
        # caller updates after this returns.
        actual_active = self._find_active(ctx, document_id)
        actual_id = actual_active.snapshot_id if actual_active else None
        if actual_id != previous_active_snapshot_id:
            raise SnapshotConflictError(
                f"promote: expected active={previous_active_snapshot_id!r}, "
                f"on-disk active={actual_id!r}"
            )

        now = self._now()
        # 1. The previous active (if any) → SUPERSEDED.
        prev_updated: DocumentSnapshot | None = None
        if actual_active is not None:
            prev_updated = _replace(
                actual_active,
                state=SnapshotState.SUPERSEDED,
                superseded_at=now,
            )
            self.store.upsert(ctx, prev_updated)

        # 2. The new candidate → PROMOTED (we keep state=READY +
        #    stamp ``promoted_at``; PROMOTED isn't a distinct state
        #    because the snapshot model captures "active" via
        #    DocumentRecord.active_snapshot_id, not via snapshot.state.
        #    A SUPERSEDED snapshot is one that WAS active; a READY
        #    snapshot with promoted_at set IS active).
        new_active = _replace(snap, promoted_at=now)
        self.store.upsert(ctx, new_active)
        return new_active, prev_updated

    # ---- Helpers -----------------------------------------------

    def _require(
        self, ctx: ProjectContext, snapshot_id: str,
    ) -> DocumentSnapshot:
        snap = self.store.get(ctx, snapshot_id)
        if snap is None:
            raise InvalidSnapshotTransitionError(
                f"snapshot {snapshot_id!r} not found"
            )
        return snap

    def _find_active(
        self, ctx: ProjectContext, document_id: str,
    ) -> DocumentSnapshot | None:
        """The ACTIVE snapshot for a document = the READY snapshot
        with the latest ``promoted_at`` that hasn't been superseded.
        Returns None when no snapshot has ever been promoted."""
        candidates = [
            s for s in self.store.list_for_document(
                ctx, document_id=document_id,
            )
            if s.state == SnapshotState.READY and s.promoted_at is not None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.promoted_at, reverse=True)
        return candidates[0]


# ---- Clock injection point -------------------------------------


class Clock:
    """Tests inject a fixed clock; production uses the default."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# ---- Internal helpers ------------------------------------------


def _new_snapshot_id() -> str:
    """Random snapshot id. Prefix flags Phase-2 origin in operator
    grep'ing through audit logs."""
    return f"snap_{uuid.uuid4().hex[:16]}"


def _replace(snap: DocumentSnapshot, **kwargs) -> DocumentSnapshot:
    """``dataclasses.replace`` shim that preserves the frozen
    semantics of ``DocumentSnapshot``."""
    from dataclasses import replace
    return replace(snap, **kwargs)


__all__ = [
    "Clock",
    "DocumentSnapshotService",
    "InvalidSnapshotTransitionError",
    "SnapshotConflictError",
]
