from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import (
    ARTIFACT_REGISTRY_FILENAME,
    ArtifactNotFoundError,
    JsonArtifactRegistry,
)
from j1.errors.exceptions import J1Error
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


def _make(
    *,
    artifact_id: str = "art-1",
    project: ProjectContext | None = None,
    kind: str = "compiled.text",
    content_hash: str = "sha256:aaa",
    sources_doc: list[str] | None = None,
) -> ArtifactRecord:
    project = project or ProjectContext(tenant_id="acme", project_id="alpha")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=project,
        kind=kind,
        location=f"compiled/{artifact_id}.txt",
        content_hash=content_hash,
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=sources_doc or [],
    )


def test_empty_returns_empty(artifact_registry, ctx):
    assert artifact_registry.list_artifacts(ctx) == []


def test_add_and_get(artifact_registry, ctx):
    record = _make(project=ctx)
    artifact_registry.add(record)
    assert artifact_registry.get(ctx, record.artifact_id) == record


def test_get_missing_raises(artifact_registry, ctx):
    with pytest.raises(ArtifactNotFoundError):
        artifact_registry.get(ctx, "missing")


def test_duplicate_artifact_id_rejected(artifact_registry, ctx):
    artifact_registry.add(_make(project=ctx))
    with pytest.raises(J1Error):
        artifact_registry.add(_make(project=ctx, content_hash="sha256:bbb"))


def test_list_filters_by_kind(artifact_registry, ctx):
    artifact_registry.add(_make(project=ctx, artifact_id="a", kind="compiled.text"))
    artifact_registry.add(
        _make(project=ctx, artifact_id="b", kind="enriched.entities", content_hash="sha256:bbb")
    )
    listed = artifact_registry.list_artifacts(ctx, kind="enriched.entities")
    assert [r.artifact_id for r in listed] == ["b"]


def test_find_by_content_hash(artifact_registry, ctx):
    record = _make(project=ctx, content_hash="sha256:zzz")
    artifact_registry.add(record)
    assert artifact_registry.find_by_content_hash(ctx, "sha256:zzz") == record
    assert artifact_registry.find_by_content_hash(ctx, "sha256:nope") is None


def test_persistence_roundtrip(workspace, ctx):
    a = JsonArtifactRegistry(workspace)
    record = _make(project=ctx, sources_doc=["doc-1"])
    a.add(record)

    b = JsonArtifactRegistry(workspace)
    listed = b.list_artifacts(ctx)
    assert listed == [record]


def test_isolates_projects(artifact_registry, ctx, other_ctx):
    artifact_registry.add(_make(artifact_id="a", project=ctx, content_hash="sha256:a"))
    artifact_registry.add(_make(artifact_id="b", project=other_ctx, content_hash="sha256:b"))
    assert [r.artifact_id for r in artifact_registry.list_artifacts(ctx)] == ["a"]
    assert [r.artifact_id for r in artifact_registry.list_artifacts(other_ctx)] == ["b"]


def test_registry_file_lives_in_runtime(artifact_registry, workspace, ctx):
    artifact_registry.add(_make(project=ctx))
    assert (workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME).is_file()
