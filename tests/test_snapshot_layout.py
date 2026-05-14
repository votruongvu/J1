"""SnapshotLayout — Phase 2 path-resolver tests."""

from __future__ import annotations

from pathlib import Path

from j1.documents.snapshot_layout import SnapshotArea, SnapshotLayout
from j1.projects.context import ProjectContext


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t-1", project_id="p-1", profile=None)


def test_snapshot_root_uses_tenant_project_document_snapshot_naming(tmp_path):
    layout = SnapshotLayout(data_root=tmp_path)
    root = layout.snapshot_root(_ctx(), "doc-1", "snap-1")
    assert root == (
        tmp_path
        / "tenants" / "t-1"
        / "projects" / "p-1"
        / "documents" / "doc-1"
        / "snapshots" / "snap-1"
    )


def test_snapshot_root_never_contains_run_id_dimension(tmp_path):
    """Locks in the Phase-2 storage rule: run_id must NOT appear in
    the snapshot path. Two re-indexes of the same document must
    land in different snapshot dirs, not the same run dir."""
    layout = SnapshotLayout(data_root=tmp_path)
    root = layout.snapshot_root(_ctx(), "doc-1", "snap-1")
    assert "runs" not in str(root).split("/"), (
        f"snapshot root contained a /runs/ segment: {root}"
    )


def test_each_stage_lives_under_the_snapshot_root(tmp_path):
    layout = SnapshotLayout(data_root=tmp_path)
    root = layout.snapshot_root(_ctx(), "doc-1", "snap-1")
    for area in SnapshotArea:
        sub = layout.area(_ctx(), "doc-1", "snap-1", area)
        assert sub.parent == root
        assert sub.name == area.value


def test_ensure_creates_every_stage_directory(tmp_path):
    layout = SnapshotLayout(data_root=tmp_path)
    root = layout.ensure(_ctx(), "doc-1", "snap-1")
    assert root.is_dir()
    for area in SnapshotArea:
        assert (root / area.value).is_dir()


def test_stage_helpers_return_per_area_paths(tmp_path):
    layout = SnapshotLayout(data_root=tmp_path)
    src = layout.source(_ctx(), "doc-1", "snap-1")
    compile_p = layout.compile(_ctx(), "doc-1", "snap-1")
    enrichment = layout.enrichment(_ctx(), "doc-1", "snap-1")
    assert src.name == "source"
    assert compile_p.name == "compile"
    assert enrichment.name == "enrichment"


def test_two_snapshots_for_same_document_get_distinct_roots(tmp_path):
    layout = SnapshotLayout(data_root=tmp_path)
    a = layout.snapshot_root(_ctx(), "doc-1", "snap-a")
    b = layout.snapshot_root(_ctx(), "doc-1", "snap-b")
    assert a != b
    assert a.parent == b.parent  # same /documents/{doc}/snapshots/ parent
