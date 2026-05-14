"""Tests for the snapshot-centered metadata types — Phase 1.

These types are pure dataclasses; the tests pin the shape so that
Phase 2 (lifecycle wiring) builds against a stable contract."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.documents.snapshot import (
    DocumentSnapshot,
    IndexKind,
    IndexRef,
    SnapshotState,
)


# ---- Index references -------------------------------------------


def test_index_ref_carries_snapshot_id_not_run_id():
    ref = IndexRef(
        snapshot_id="snap-1",
        kind=IndexKind.VECTOR,
        provider="qdrant",
        location="j1_snap-1",
        stats={"vectors": 1024},
    )
    assert ref.snapshot_id == "snap-1"
    assert ref.kind == IndexKind.VECTOR
    assert ref.provider == "qdrant"
    assert ref.location == "j1_snap-1"
    assert ref.stats["vectors"] == 1024


def test_index_ref_is_frozen():
    ref = IndexRef(
        snapshot_id="snap-1",
        kind=IndexKind.VECTOR,
        provider="qdrant",
        location="j1_snap-1",
    )
    with pytest.raises(Exception):
        ref.snapshot_id = "snap-2"  # type: ignore[misc]


# ---- Snapshot ---------------------------------------------------


def test_snapshot_default_state_and_lineage_fields():
    now = datetime.now(timezone.utc)
    snap = DocumentSnapshot(
        snapshot_id="snap-1",
        document_id="doc-1",
        tenant_id="t",
        project_id="p",
        created_by_run_id="run-1",
        state=SnapshotState.BUILDING,
        created_at=now,
    )
    assert snap.state == SnapshotState.BUILDING
    assert snap.created_by_run_id == "run-1"
    assert snap.promoted_at is None
    assert snap.superseded_at is None
    assert snap.index_refs == ()
    assert snap.summary == {}


def test_snapshot_can_carry_multiple_index_refs():
    now = datetime.now(timezone.utc)
    refs = (
        IndexRef(snapshot_id="snap-1", kind=IndexKind.VECTOR,
                 provider="qdrant", location="j1_snap-1"),
        IndexRef(snapshot_id="snap-1", kind=IndexKind.GRAPH,
                 provider="neo4j", location="snap_1_subgraph"),
        IndexRef(snapshot_id="snap-1", kind=IndexKind.EVIDENCE,
                 provider="postgres_fts", location="j1.evidence_snap_1"),
        IndexRef(snapshot_id="snap-1", kind=IndexKind.RAG,
                 provider="raganything",
                 location="/var/lib/j1/raganything/snapshots/snap-1"),
    )
    snap = DocumentSnapshot(
        snapshot_id="snap-1",
        document_id="doc-1",
        tenant_id="t",
        project_id="p",
        created_by_run_id="run-1",
        state=SnapshotState.READY,
        created_at=now,
        index_refs=refs,
    )
    kinds = {r.kind for r in snap.index_refs}
    assert kinds == {IndexKind.VECTOR, IndexKind.GRAPH,
                     IndexKind.EVIDENCE, IndexKind.RAG}


def test_snapshot_state_transitions_are_string_enum():
    """Each state name round-trips through its string value."""
    for s in SnapshotState:
        assert SnapshotState(s.value) is s


def test_snapshot_is_frozen():
    snap = DocumentSnapshot(
        snapshot_id="snap-1",
        document_id="doc-1",
        tenant_id="t",
        project_id="p",
        created_by_run_id="run-1",
        state=SnapshotState.BUILDING,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(Exception):
        snap.state = SnapshotState.READY  # type: ignore[misc]
