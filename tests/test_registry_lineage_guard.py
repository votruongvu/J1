"""Tests for the registry-level lineage guard in
``j1.artifacts.registry.JsonArtifactRegistry``.

The latest validation report flagged 7 NEW ``graph_json`` orphans
(``metadata.run_id=None``) — DIFFERENT IDs from the 7 in the
previous report. The producer fix
(``_graph_drafts_from_storage`` stamping run_id) prevents most
paths from producing orphans, and the legacy ``_register_draft``
hard-fails graph_json without run_id, but a future adapter calling
``artifacts.add()`` directly would bypass both. The registry layer
is the ONE choke point every write goes through — this guard is
the hermetic last line of defense.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import (
    JsonArtifactRegistry,
    RegistryLineageError,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


@pytest.fixture
def registry(tmp_path):
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver

    settings = Settings(data_root=tmp_path)
    workspace = WorkspaceResolver(settings)
    return JsonArtifactRegistry(workspace)


def _record(*, ctx, artifact_id, kind, metadata):
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"area/{artifact_id}.json",
        content_hash=f"sha256:{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        metadata=dict(metadata),
    )


# ---- Headline regression -----------------------------------------


def test_graph_json_without_run_id_is_rejected(registry, ctx):
    """The HERMETIC guard: a graph_json with no ``metadata.run_id``
    cannot be written via ``registry.add()`` regardless of which
    upstream path called it."""
    record = _record(
        ctx=ctx, artifact_id="bad-graph-1", kind="graph_json",
        metadata={},  # no run_id — the bug pattern
    )
    with pytest.raises(RegistryLineageError) as exc_info:
        registry.add(record)
    # Operator-actionable error message.
    assert "graph_json" in str(exc_info.value)
    assert "run_id" in str(exc_info.value)
    assert "metadata.run_id" in str(exc_info.value)


def test_graph_json_with_empty_string_run_id_is_rejected(registry, ctx):
    """Empty-string run_id is just as bad as missing — the guard
    enforces non-empty truthiness, not just presence."""
    record = _record(
        ctx=ctx, artifact_id="bad-graph-2", kind="graph_json",
        metadata={"run_id": ""},
    )
    with pytest.raises(RegistryLineageError):
        registry.add(record)


def test_graph_json_with_explicit_none_run_id_is_rejected(registry, ctx):
    """``metadata={"run_id": None}`` is the exact shape that slipped
    through earlier registration helpers (``if run_id and ...``
    skipped the overwrite, then the guard saw None)."""
    record = _record(
        ctx=ctx, artifact_id="bad-graph-3", kind="graph_json",
        metadata={"run_id": None},
    )
    with pytest.raises(RegistryLineageError):
        registry.add(record)


def test_graph_json_with_valid_run_id_is_accepted(registry, ctx):
    """Happy path: ``metadata.run_id`` set → registration succeeds."""
    record = _record(
        ctx=ctx, artifact_id="good-graph-1", kind="graph_json",
        metadata={"run_id": "run-x"},
    )
    registry.add(record)
    stored = registry.get(ctx, "good-graph-1")
    assert stored.metadata["run_id"] == "run-x"


def test_non_lineage_required_kind_can_have_no_run_id(registry, ctx):
    """Kinds OUTSIDE the registry's required set legitimately have
    no run_id (operator uploads, raw files, doc-status snapshots).
    The guard must not over-reach."""
    record = _record(
        ctx=ctx, artifact_id="raw-1", kind="raw_upload",
        metadata={},
    )
    registry.add(record)
    stored = registry.get(ctx, "raw-1")
    assert stored.kind == "raw_upload"


def test_chunk_kind_currently_allowed_without_run_id(registry, ctx):
    """``chunk`` is lineage-required at the higher-level guards but
    NOT at the registry layer (yet). The registry's tight catalogue
    targets the specific production failure mode (graph_json); a
    future expansion can add more kinds, but we don't want to
    break test fixtures that rely on the current envelope."""
    record = _record(
        ctx=ctx, artifact_id="chunk-1", kind="chunk", metadata={},
    )
    # No raise — current envelope.
    registry.add(record)


def test_registry_lineage_error_is_subclass_of_j1_error():
    """Existing callers that catch ``J1Error`` continue to handle
    lineage failures — no new exception leaks out of the registry
    surface."""
    from j1.errors.exceptions import J1Error
    assert issubclass(RegistryLineageError, J1Error)
