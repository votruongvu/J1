"""JsonlDocumentSnapshotStore tests — Phase 2."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.config.settings import Settings
from j1.documents.snapshot import (
    DocumentSnapshot,
    IndexKind,
    IndexRef,
    SnapshotState,
)
from j1.documents.snapshot_store import (
    JsonlDocumentSnapshotStore,
    SNAPSHOTS_FILENAME,
)
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture
def workspace(tmp_path) -> WorkspaceResolver:
    return WorkspaceResolver(Settings(data_root=tmp_path))


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _snap(snapshot_id: str, *, state=SnapshotState.BUILDING) -> DocumentSnapshot:
    return DocumentSnapshot(
        snapshot_id=snapshot_id,
        document_id="doc-1",
        tenant_id="t",
        project_id="p",
        created_by_run_id="run-1",
        state=state,
        created_at=datetime.now(timezone.utc),
    )


def test_upsert_then_get_round_trips_state_and_lineage(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("snap-1"))
    got = store.get(ctx, "snap-1")
    assert got is not None
    assert got.snapshot_id == "snap-1"
    assert got.created_by_run_id == "run-1"
    assert got.state == SnapshotState.BUILDING


def test_upsert_appends_and_latest_wins(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("snap-1", state=SnapshotState.BUILDING))
    store.upsert(ctx, _snap("snap-1", state=SnapshotState.READY))
    got = store.get(ctx, "snap-1")
    assert got is not None
    assert got.state == SnapshotState.READY


def test_jsonl_file_lives_in_audit_area(workspace, ctx, tmp_path):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("snap-1"))
    path = workspace.area(ctx, WorkspaceArea.AUDIT) / SNAPSHOTS_FILENAME
    assert path.exists()


def test_list_for_document_returns_latest_per_snapshot_sorted(
    workspace, ctx,
):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("snap-1"))
    store.upsert(ctx, _snap("snap-2"))
    items = store.list_for_document(ctx, document_id="doc-1")
    ids = {s.snapshot_id for s in items}
    assert ids == {"snap-1", "snap-2"}


def test_list_filters_by_state(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("snap-a", state=SnapshotState.BUILDING))
    store.upsert(ctx, _snap("snap-b", state=SnapshotState.READY))
    items = store.list(ctx, states=[SnapshotState.READY])
    assert [s.snapshot_id for s in items] == ["snap-b"]


def test_purge_rewrites_jsonl_without_target_snapshot(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("keep"))
    store.upsert(ctx, _snap("drop"))
    assert store.purge(ctx, "drop") is True
    assert store.get(ctx, "drop") is None
    assert store.get(ctx, "keep") is not None


def test_purge_is_idempotent_when_snapshot_absent(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    store.upsert(ctx, _snap("keep"))
    assert store.purge(ctx, "ghost") is False


def test_index_refs_round_trip(workspace, ctx):
    store = JsonlDocumentSnapshotStore(workspace)
    snap = _snap("snap-1")
    refs = (
        IndexRef(
            snapshot_id="snap-1",
            kind=IndexKind.VECTOR,
            provider="qdrant",
            location="j1_snap-1",
            stats={"vectors": 32},
        ),
    )
    from dataclasses import replace
    store.upsert(ctx, replace(snap, index_refs=refs))
    got = store.get(ctx, "snap-1")
    assert got is not None
    assert len(got.index_refs) == 1
    assert got.index_refs[0].provider == "qdrant"
    assert got.index_refs[0].stats == {"vectors": 32}
