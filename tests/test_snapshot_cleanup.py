"""SnapshotCleanupService — Phase 2 cleanup-policy tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.config.settings import Settings
from j1.documents.index_refs import JsonIndexRefStore
from j1.documents.snapshot import IndexKind, IndexRef, SnapshotState
from j1.documents.snapshot_cleanup import SnapshotCleanupService
from j1.documents.snapshot_layout import SnapshotLayout
from j1.documents.snapshot_service import DocumentSnapshotService
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver


class _FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        out = self._now
        self._now += timedelta(seconds=1)
        return out


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


@pytest.fixture
def harness(tmp_path):
    ws = WorkspaceResolver(Settings(data_root=tmp_path))
    store = JsonlDocumentSnapshotStore(ws)
    layout = SnapshotLayout(data_root=tmp_path)
    refs = JsonIndexRefStore(ws)
    service = DocumentSnapshotService(store=store, clock=_FixedClock())
    cleanup = SnapshotCleanupService(
        layout=layout, store=store, index_refs=refs,
    )
    return service, cleanup, refs, layout, tmp_path


def test_cleanup_removes_workspace_dir_and_index_refs(harness, ctx):
    service, cleanup, refs, layout, tmp_path = harness
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    root = layout.ensure(ctx, "doc-1", snap.snapshot_id)
    (root / "compile" / "test.txt").write_text("payload")
    refs.register(ctx, IndexRef(
        snapshot_id=snap.snapshot_id,
        kind=IndexKind.EVIDENCE,
        provider="stub",
        location="stub://loc",
    ))
    service.mark_failed(ctx, snapshot_id=snap.snapshot_id, reason="x")

    result = cleanup.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id=snap.snapshot_id,
    )
    assert result.errors == []
    assert result.removed_path == root
    assert not root.exists()
    assert result.removed_index_refs == 1
    assert result.purged_from_store is True
    assert service.store.get(ctx, snap.snapshot_id) is None


def test_cleanup_refuses_promoted_snapshot_without_force(harness, ctx):
    service, cleanup, _, _, _ = harness
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    service.mark_ready(ctx, snapshot_id=snap.snapshot_id)
    service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=snap.snapshot_id,
        previous_active_snapshot_id=None,
    )

    result = cleanup.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id=snap.snapshot_id,
    )
    assert any("promoted" in e for e in result.errors)
    # The snapshot row is still there.
    assert service.store.get(ctx, snap.snapshot_id) is not None


def test_cleanup_with_force_clears_promoted_snapshot(harness, ctx):
    service, cleanup, _, layout, _ = harness
    snap = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-1",
    )
    service.mark_ready(ctx, snapshot_id=snap.snapshot_id)
    service.promote(
        ctx,
        document_id="doc-1",
        snapshot_id=snap.snapshot_id,
        previous_active_snapshot_id=None,
    )
    layout.ensure(ctx, "doc-1", snap.snapshot_id)

    result = cleanup.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id=snap.snapshot_id, force=True,
    )
    assert result.errors == []
    assert result.purged_from_store is True


def test_cleanup_failed_snapshots_drops_only_failed_ones(harness, ctx):
    service, cleanup, _, layout, _ = harness
    failed = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-bad",
    )
    service.mark_failed(ctx, snapshot_id=failed.snapshot_id, reason="x")
    layout.ensure(ctx, "doc-1", failed.snapshot_id)

    ready = service.create_candidate(
        ctx, document_id="doc-1", created_by_run_id="run-good",
    )
    service.mark_ready(ctx, snapshot_id=ready.snapshot_id)
    layout.ensure(ctx, "doc-1", ready.snapshot_id)

    results = cleanup.cleanup_failed_snapshots(ctx, document_id="doc-1")
    cleaned_ids = {r.snapshot_id for r in results}
    assert cleaned_ids == {failed.snapshot_id}
    # The READY snapshot is untouched.
    assert service.store.get(ctx, ready.snapshot_id) is not None


def test_cleanup_is_idempotent_for_missing_snapshot(harness, ctx):
    _, cleanup, _, _, _ = harness
    result = cleanup.cleanup_snapshot(
        ctx, document_id="doc-1", snapshot_id="ghost",
    )
    assert result.errors == []
    assert result.purged_from_store is False
