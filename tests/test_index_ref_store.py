"""IndexRefStore — Phase 2 persistence + dispatch tests."""

from __future__ import annotations

import pytest

from j1.config.settings import Settings
from j1.documents.index_refs import JsonIndexRefStore, INDEX_REFS_FILENAME
from j1.documents.snapshot import IndexKind, IndexRef
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture
def store(tmp_path):
    ws = WorkspaceResolver(Settings(data_root=tmp_path))
    return JsonIndexRefStore(ws), ws


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _ref(
    snapshot_id="snap-1",
    kind=IndexKind.VECTOR,
    provider="qdrant",
    location="loc-1",
) -> IndexRef:
    return IndexRef(
        snapshot_id=snapshot_id,
        kind=kind,
        provider=provider,
        location=location,
    )


def test_register_writes_under_runtime_area(store, ctx):
    s, ws = store
    s.register(ctx, _ref())
    path = ws.area(ctx, WorkspaceArea.RUNTIME) / INDEX_REFS_FILENAME
    assert path.exists()


def test_register_replaces_same_triple_key(store, ctx):
    s, _ = store
    s.register(ctx, _ref(location="loc-a"))
    s.register(ctx, _ref(location="loc-b"))
    refs = s.list_for_snapshot(ctx, "snap-1")
    assert len(refs) == 1
    assert refs[0].location == "loc-b"


def test_register_keeps_multiple_kinds_for_one_snapshot(store, ctx):
    s, _ = store
    s.register(ctx, _ref(kind=IndexKind.VECTOR, provider="qdrant"))
    s.register(ctx, _ref(kind=IndexKind.GRAPH, provider="neo4j"))
    s.register(ctx, _ref(kind=IndexKind.EVIDENCE, provider="postgres_fts"))
    refs = s.list_for_snapshot(ctx, "snap-1")
    kinds = {r.kind for r in refs}
    assert kinds == {IndexKind.VECTOR, IndexKind.GRAPH, IndexKind.EVIDENCE}


def test_list_by_provider_filters_correctly(store, ctx):
    s, _ = store
    s.register(ctx, _ref(provider="qdrant", kind=IndexKind.VECTOR))
    s.register(ctx, _ref(
        snapshot_id="snap-2", provider="qdrant", kind=IndexKind.VECTOR,
    ))
    s.register(ctx, _ref(provider="neo4j", kind=IndexKind.GRAPH))
    qdrant = s.list_by_provider(ctx, "qdrant")
    assert len(qdrant) == 2
    qdrant_vec = s.list_by_provider(ctx, "qdrant", kind=IndexKind.VECTOR)
    assert len(qdrant_vec) == 2


def test_delete_for_snapshot_drops_every_kind(store, ctx):
    s, _ = store
    s.register(ctx, _ref(kind=IndexKind.VECTOR, provider="qdrant"))
    s.register(ctx, _ref(kind=IndexKind.GRAPH, provider="neo4j"))
    s.register(ctx, _ref(
        snapshot_id="snap-keep",
        kind=IndexKind.VECTOR, provider="qdrant",
    ))
    removed = s.delete_for_snapshot(ctx, "snap-1")
    assert removed == 2
    assert s.list_for_snapshot(ctx, "snap-1") == []
    assert len(s.list_for_snapshot(ctx, "snap-keep")) == 1
