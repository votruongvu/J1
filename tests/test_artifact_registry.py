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


def test_snapshot_id_round_trips_through_json_reader(workspace, ctx):
    """Snapshot identity must survive the JSON write/read round-trip.

    The serializer already emits ``snapshot_id`` + ``created_by_run_id``
    (``to_jsonable`` walks every dataclass field), but for a long
    time the reader silently dropped both — the typed fields became
    ``None`` on read even though the JSON payload still carried the
    values. That asymmetry was masked by production code stamping
    ``metadata["snapshot_id"]`` in parallel; downstream consumers
    that read the typed field directly (Unified Memory Resolver,
    snapshot-aware graph/index paths) needed the round-trip to be
    lossless. This test pins it.
    """
    from j1.artifacts.models import ArtifactRecord
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id="art-snap",
        project=ctx,
        kind="compiled.text",
        location="compiled/art-snap.txt",
        content_hash="sha256:snap",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=["doc-1"],
        # Typed fields written explicitly. NO metadata stamping —
        # the round-trip must survive on the typed fields alone.
        snapshot_id="snap-active",
        created_by_run_id="run-baseline",
    )
    a = JsonArtifactRegistry(workspace)
    a.add(record)

    # Fresh registry instance forces a read from disk.
    b = JsonArtifactRegistry(workspace)
    listed = b.list_artifacts(ctx)
    assert len(listed) == 1
    read_back = listed[0]
    assert read_back.snapshot_id == "snap-active"
    assert read_back.created_by_run_id == "run-baseline"
    # Reading back via ``get()`` (alternative entry point) must
    # surface the same values — both paths go through the same
    # reader so this pins both.
    assert b.get(ctx, "art-snap").snapshot_id == "snap-active"
    assert b.get(ctx, "art-snap").created_by_run_id == "run-baseline"


def test_legacy_artifact_without_typed_snapshot_id_loads_safely(
    workspace, ctx,
):
    """Backward compatibility: artifacts written before the typed
    field round-trip fix often only carried ``snapshot_id`` inside
    ``metadata`` (the production stamping path). The reader must
    still surface the value on the typed field so the resolver
    doesn't have to special-case legacy records.
    """
    import json
    a = JsonArtifactRegistry(workspace)
    # Hand-write a legacy payload — no top-level ``snapshot_id`` /
    # ``created_by_run_id``, just metadata.
    path = workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "artifacts": [{
            "artifact_id": "art-legacy",
            "project": {
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "profile": None,
            },
            "kind": "compiled.text",
            "location": "compiled/legacy.txt",
            "content_hash": "sha256:legacy",
            "byte_size": 4,
            "status": "succeeded",
            "review_status": "not_required",
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "source_document_ids": ["doc-1"],
            "source_artifact_ids": [],
            "metadata": {
                "snapshot_id": "snap-legacy",
                "run_id": "run-legacy",
            },
            # Note: ``snapshot_id`` + ``created_by_run_id`` deliberately
            # OMITTED at the top level (legacy artifact pre-dating
            # the typed fields).
        }],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    listed = a.list_artifacts(ctx)
    assert len(listed) == 1
    read_back = listed[0]
    # Reader filled in the typed field from metadata fallback so
    # downstream consumers see a uniform shape regardless of when
    # the artifact was written.
    assert read_back.snapshot_id == "snap-legacy"
    assert read_back.created_by_run_id == "run-legacy"


def test_artifact_without_any_snapshot_id_loads_safely(
    workspace, ctx,
):
    """The oldest legacy artifacts pre-date snapshots entirely.
    They must still load (``snapshot_id=None``) — the reader must
    not raise on the missing field.
    """
    import json
    a = JsonArtifactRegistry(workspace)
    path = workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "artifacts": [{
            "artifact_id": "art-ancient",
            "project": {
                "tenant_id": ctx.tenant_id,
                "project_id": ctx.project_id,
                "profile": None,
            },
            "kind": "compiled.text",
            "location": "compiled/ancient.txt",
            "content_hash": "sha256:ancient",
            "byte_size": 4,
            "status": "succeeded",
            "review_status": "not_required",
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "source_document_ids": ["doc-1"],
            "source_artifact_ids": [],
            "metadata": {},
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    listed = a.list_artifacts(ctx)
    assert len(listed) == 1
    assert listed[0].snapshot_id is None
    assert listed[0].created_by_run_id is None


def test_isolates_projects(artifact_registry, ctx, other_ctx):
    artifact_registry.add(_make(artifact_id="a", project=ctx, content_hash="sha256:a"))
    artifact_registry.add(_make(artifact_id="b", project=other_ctx, content_hash="sha256:b"))
    assert [r.artifact_id for r in artifact_registry.list_artifacts(ctx)] == ["a"]
    assert [r.artifact_id for r in artifact_registry.list_artifacts(other_ctx)] == ["b"]


def test_registry_file_lives_in_runtime(artifact_registry, workspace, ctx):
    artifact_registry.add(_make(project=ctx))
    assert (workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME).is_file()
