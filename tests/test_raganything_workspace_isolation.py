"""Snapshot-scoped workspace isolation tests.

Phase 5 retired ``workspace_path_for_run`` from the public API. The
isolation contract that test file used to pin — "two runs of the
same document write into different scoped directories" — now
belongs to ``SnapshotLayout``. The tests below verify the same
guarantees against the snapshot-centred path:

  * Two snapshots for the same document get distinct workspace
    roots.
  * A snapshot subtree can be pruned atomically (one rm -rf
    deletes every stage directory).
  * Sibling documents are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from j1.documents.snapshot_layout import SnapshotArea, SnapshotLayout
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


def test_two_snapshots_for_same_document_get_distinct_paths(
    tmp_path: Path, ctx,
):
    """The Phase-2 storage rule: two snapshots for the same document
    write into different roots. Without this, a re-ingest would
    overwrite the previous snapshot's graphml + chunks."""
    layout = SnapshotLayout(data_root=tmp_path)
    snap_a = layout.snapshot_root(ctx, "doc-a", "snap-1")
    snap_b = layout.snapshot_root(ctx, "doc-a", "snap-2")
    assert snap_a != snap_b
    assert snap_a.parent == snap_b.parent  # same /documents/{doc}/snapshots/


def test_two_snapshot_compile_dirs_do_not_overwrite_each_other(
    tmp_path: Path, ctx,
):
    """Simulate two snapshot-scoped compile writes; both files
    survive."""
    layout = SnapshotLayout(data_root=tmp_path)
    snap1 = layout.compile(ctx, "doc-a", "snap-1")
    snap2 = layout.compile(ctx, "doc-a", "snap-2")
    snap1.mkdir(parents=True, exist_ok=True)
    snap2.mkdir(parents=True, exist_ok=True)
    (snap1 / "graph.graphml").write_text("snap-1 content")
    (snap2 / "graph.graphml").write_text("snap-2 content")
    assert (snap1 / "graph.graphml").read_text() == "snap-1 content"
    assert (snap2 / "graph.graphml").read_text() == "snap-2 content"


def test_snapshot_root_namespace_layout(tmp_path: Path, ctx):
    """Snapshot root shape: ``data/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}``.

    Four levels of namespace so retention / detach / remove can
    prune by deleting the appropriate subtree. NEVER contains a
    ``runs/`` segment."""
    layout = SnapshotLayout(data_root=tmp_path)
    path = layout.snapshot_root(ctx, "doc-a", "snap-1")
    assert path == (
        tmp_path
        / "tenants" / "t1"
        / "projects" / "p1"
        / "documents" / "doc-a"
        / "snapshots" / "snap-1"
    )
    assert "runs" not in path.parts


def test_per_document_subtree_can_be_pruned_atomically(tmp_path: Path, ctx):
    """Detach/remove cleanup: deleting ``{document_id}/`` removes
    every snapshot's data for that document in one rm -rf. Sibling
    documents survive."""
    layout = SnapshotLayout(data_root=tmp_path)
    snap1 = layout.ensure(ctx, "doc-a", "snap-1")
    snap2 = layout.ensure(ctx, "doc-a", "snap-2")
    sibling = layout.ensure(ctx, "doc-b", "snap-1")
    (snap1 / SnapshotArea.COMPILE.value / "g.txt").write_text("a")
    (snap2 / SnapshotArea.COMPILE.value / "g.txt").write_text("b")
    (sibling / SnapshotArea.COMPILE.value / "g.txt").write_text("c")

    import shutil
    shutil.rmtree(snap1.parent)  # documents/doc-a/snapshots/

    assert not snap1.exists()
    assert not snap2.exists()
    assert sibling.exists()
    assert (sibling / SnapshotArea.COMPILE.value / "g.txt").read_text() == "c"
