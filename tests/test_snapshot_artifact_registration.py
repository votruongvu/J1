"""Snapshot-scoped artifact registration — Phase 2."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry, ArtifactNotFoundError
from j1.documents.snapshot_artifact import register_snapshot_artifact
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


class _InMemoryRegistry:
    """Minimal in-memory ``ArtifactRegistry`` test double."""

    def __init__(self) -> None:
        self.added: list[ArtifactRecord] = []

    def add(self, record):
        self.added.append(record)

    def get(self, ctx, artifact_id):
        for r in self.added:
            if r.artifact_id == artifact_id:
                return r
        raise ArtifactNotFoundError(artifact_id)

    def find_by_content_hash(self, ctx, content_hash):
        return None

    def list_artifacts(self, ctx, *, kind=None):
        if kind is None:
            return list(self.added)
        return [r for r in self.added if r.kind == kind]

    def update_metadata(self, ctx, artifact_id, metadata):
        rec = self.get(ctx, artifact_id)
        rec.metadata = dict(metadata)

    def delete_by_artifact_id(self, ctx, artifact_id):
        before = len(self.added)
        self.added = [r for r in self.added if r.artifact_id != artifact_id]
        return len(self.added) < before


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _record(kind: str = "chunk", *, metadata=None) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="art-1",
        project=ProjectContext(tenant_id="t", project_id="p", profile=None),
        kind=kind,
        location=f"compiled/art-1.json",
        content_hash="0xdead",
        byte_size=16,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.PENDING,
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata=dict(metadata or {}),
    )


def test_stamps_typed_snapshot_id_and_run_id(ctx):
    reg = _InMemoryRegistry()
    rec = register_snapshot_artifact(
        reg, ctx,
        record=_record(),
        snapshot_id="snap-1",
        created_by_run_id="run-1",
    )
    assert rec.snapshot_id == "snap-1"
    assert rec.created_by_run_id == "run-1"


def test_mirrors_into_metadata_for_legacy_readers(ctx):
    reg = _InMemoryRegistry()
    rec = register_snapshot_artifact(
        reg, ctx,
        record=_record(),
        snapshot_id="snap-1",
        created_by_run_id="run-1",
    )
    # Legacy SqliteSearchIndexer + validation filters still expect
    # metadata["run_id"] — Phase 2 keeps that key so they don't
    # need rewrites this phase.
    assert rec.metadata["run_id"] == "run-1"
    assert rec.metadata["snapshot_id"] == "snap-1"


def test_run_id_metadata_is_not_overwritten_when_caller_set_explicit_value(ctx):
    reg = _InMemoryRegistry()
    rec = register_snapshot_artifact(
        reg, ctx,
        record=_record(metadata={"run_id": "preset-by-caller"}),
        snapshot_id="snap-1",
        created_by_run_id="run-zzz",
    )
    # Caller wins for metadata["run_id"] — typed field still reflects
    # the canonical execution id.
    assert rec.metadata["run_id"] == "preset-by-caller"
    assert rec.created_by_run_id == "run-zzz"


def test_register_actually_adds_to_registry(ctx):
    reg = _InMemoryRegistry()
    register_snapshot_artifact(
        reg, ctx,
        record=_record(),
        snapshot_id="snap-1",
        created_by_run_id="run-1",
    )
    assert len(reg.added) == 1
    assert reg.added[0].snapshot_id == "snap-1"
