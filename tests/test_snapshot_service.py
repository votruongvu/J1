"""DocumentSnapshotService — Phase 2 promote / supersede tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.config.settings import Settings
from j1.documents.snapshot import IndexKind, IndexRef, SnapshotState
from j1.documents.snapshot_service import (
    DocumentSnapshotService,
    InvalidSnapshotTransitionError,
    SnapshotConflictError,
)
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver


class _FixedClock:
    """Deterministic clock for promotion-ordering tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._step = timedelta(seconds=1)

    def now(self) -> datetime:
        out = self._now
        self._now += self._step
        return out


@pytest.fixture
def service(tmp_path):
    ws = WorkspaceResolver(Settings(data_root=tmp_path))
    store = JsonlDocumentSnapshotStore(ws)
    clock = _FixedClock(datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc))
    return DocumentSnapshotService(store=store, clock=clock)


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- Create + state machine ------------------------------------


def test_create_candidate_starts_in_building_state(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    assert snap.state == SnapshotState.BUILDING
    assert snap.created_by_run_id == "run-1"
    assert snap.snapshot_id.startswith("snap_")


def test_mark_ready_transitions_only_from_building(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    ready = service.mark_ready(ctx, snapshot_id=snap.snapshot_id)
    assert ready.state == SnapshotState.READY
    # Calling mark_ready twice is rejected.
    with pytest.raises(InvalidSnapshotTransitionError):
        service.mark_ready(ctx, snapshot_id=snap.snapshot_id)


def test_mark_failed_transitions_from_building(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    failed = service.mark_failed(
        ctx, snapshot_id=snap.snapshot_id, reason="compile crashed",
    )
    assert failed.state == SnapshotState.FAILED
    assert failed.summary.get("failure_reason") == "compile crashed"


def test_attach_index_ref_keeps_one_per_kind_provider_pair(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    ref_a = IndexRef(
        snapshot_id=snap.snapshot_id,
        kind=IndexKind.VECTOR,
        provider="qdrant",
        location="loc-a",
        stats={"v": 1},
    )
    ref_b = IndexRef(
        snapshot_id=snap.snapshot_id,
        kind=IndexKind.VECTOR,
        provider="qdrant",
        location="loc-b",
        stats={"v": 2},
    )
    service.attach_index_ref(ctx, snapshot_id=snap.snapshot_id, ref=ref_a)
    updated = service.attach_index_ref(
        ctx, snapshot_id=snap.snapshot_id, ref=ref_b,
    )
    # Same (kind, provider) replaces the previous; the snapshot has
    # exactly one ref of that kind.
    refs = [
        r for r in updated.index_refs
        if r.kind == IndexKind.VECTOR and r.provider == "qdrant"
    ]
    assert len(refs) == 1
    assert refs[0].location == "loc-b"


# ---- Promotion -------------------------------------------------


def test_promote_first_snapshot_makes_it_active(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    service.mark_ready(ctx, snapshot_id=snap.snapshot_id)
    new_active, prev = service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=snap.snapshot_id,
        previous_active_snapshot_id=None,
    )
    assert new_active.promoted_at is not None
    assert prev is None


def test_promote_demotes_previous_active_to_superseded(service, ctx):
    first = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-a",
    )
    service.mark_ready(ctx, snapshot_id=first.snapshot_id)
    service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=first.snapshot_id,
        previous_active_snapshot_id=None,
    )

    second = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-b",
    )
    service.mark_ready(ctx, snapshot_id=second.snapshot_id)
    new_active, prev = service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=second.snapshot_id,
        previous_active_snapshot_id=first.snapshot_id,
    )
    assert new_active.snapshot_id == second.snapshot_id
    assert prev is not None
    assert prev.snapshot_id == first.snapshot_id
    assert prev.state == SnapshotState.SUPERSEDED
    assert prev.superseded_at is not None


def test_promote_rejects_snapshot_not_in_ready(service, ctx):
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    # Still BUILDING.
    with pytest.raises(InvalidSnapshotTransitionError):
        service.promote(
            ctx,
            document_id="doc-1",
            snapshot_id=snap.snapshot_id,
            previous_active_snapshot_id=None,
        )


def test_promote_cas_conflict_when_expected_does_not_match(service, ctx):
    """Locks the CAS rule: if another writer promoted concurrently,
    the caller's stale ``previous_active_snapshot_id`` makes the
    promote raise ``SnapshotConflictError``."""
    first = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-a",
    )
    service.mark_ready(ctx, snapshot_id=first.snapshot_id)
    service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=first.snapshot_id,
        previous_active_snapshot_id=None,
    )

    second = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-b",
    )
    service.mark_ready(ctx, snapshot_id=second.snapshot_id)
    # Caller thinks active is None but on-disk says ``first.snapshot_id``.
    with pytest.raises(SnapshotConflictError):
        service.promote(
            ctx,
            document_id="doc-1",
            snapshot_id=second.snapshot_id,
            previous_active_snapshot_id=None,
        )


def test_failed_candidate_does_not_replace_active(service, ctx):
    """Critical invariant: a FAILED candidate snapshot is never
    promoted; the previous active stays the active."""
    good = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-good",
    )
    service.mark_ready(ctx, snapshot_id=good.snapshot_id)
    service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=good.snapshot_id,
        previous_active_snapshot_id=None,
    )

    bad = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-bad",
    )
    service.mark_failed(ctx, snapshot_id=bad.snapshot_id, reason="boom")

    # ``promote`` is only valid for READY snapshots; FAILED can't go
    # active even if the caller tries.
    with pytest.raises(InvalidSnapshotTransitionError):
        service.promote(
            ctx,
            document_id="doc-1",
            snapshot_id=bad.snapshot_id,
            previous_active_snapshot_id=good.snapshot_id,
        )
    # And the active didn't change — `good` is still the active.
    active = service._find_active(ctx, "doc-1")
    assert active is not None
    assert active.snapshot_id == good.snapshot_id
