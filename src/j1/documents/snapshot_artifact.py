"""Snapshot-scoped artifact registration — Phase 2.

The existing artifact registry stamps ``metadata["run_id"]`` for
lineage. Phase 2 adds the snapshot dimension WITHOUT removing the
run_id metadata (Phase 3 retires the run_id key once every reader
has migrated).

This helper is the one place every producer should go through when
attaching an artifact to a snapshot. It:

  1. Stamps ``snapshot_id`` + ``created_by_run_id`` on the record
     (typed fields, not metadata).
  2. Keeps ``metadata["run_id"]`` + ``metadata["snapshot_id"]`` in
     sync so the legacy readers (SqliteSearchIndexer, validation
     filters) still find lineage where they expect it.
  3. Delegates to the underlying registry's ``add()``.

Why a free function (not a method on the registry): the registry
Protocol is small and shared across many adapters (in-memory test
double, the production JSON store). Adding snapshot fields to the
protocol would force every adapter to know about snapshots, which
Phase 3 will undo when the legacy fields go. The helper keeps the
seam narrow.
"""

from __future__ import annotations

from datetime import datetime, timezone

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.projects.context import ProjectContext


def register_snapshot_artifact(
    registry: ArtifactRegistry,
    ctx: ProjectContext,
    *,
    record: ArtifactRecord,
    snapshot_id: str,
    created_by_run_id: str,
) -> ArtifactRecord:
    """Stamp + register one artifact. Returns the updated record so
    the caller can inspect the final shape.

    The function mutates ``record`` in place (typed fields +
    metadata mirror) because every producer we call expects the
    same instance back; copying would invite drift between the
    snapshot-shaped copy and a downstream registry read.
    """
    record.snapshot_id = snapshot_id
    record.created_by_run_id = created_by_run_id
    # Mirror into metadata so legacy readers (SqliteSearchIndexer's
    # FTS write path, validation filters, ingestion-review service)
    # still see the lineage they expect.
    if record.metadata is None:
        record.metadata = {}
    record.metadata["snapshot_id"] = snapshot_id
    # Keep run_id metadata key intact for backwards compat — Phase 3
    # removes this assignment + the readers that depend on it.
    record.metadata.setdefault("run_id", created_by_run_id)
    if record.updated_at is None:
        record.updated_at = datetime.now(timezone.utc)
    registry.add(record)
    return record


__all__ = ["register_snapshot_artifact"]
