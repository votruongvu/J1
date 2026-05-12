"""Regression tests for ``_record_to_source`` propagating
``run_id`` + ``chunk_id`` from the artifact's metadata into the
projected ``SourceReference``.

Bug fixed:
   Earlier ``_record_to_source`` (in ``j1.query.providers``)
   built a ``SourceReference`` with ``run_id=None`` and
   ``chunk_id=None`` regardless of what was in
   ``record.metadata``. Validation's
   ``retrieved_chunks_belong_to_run`` /
   ``citations_belong_to_run`` checks read ``run_id`` straight
   from ``SourceReference`` via
   ``_retrieved_chunks_from_response``. So every record-backed
   source (graph_json, consistency, report providers) projected
   with ``run_id=None``, making the validator flag them as run-id
   orphans even when ``metadata.run_id`` was correctly stamped at
   registration. Classification:
   ``retrieval_mapper_missing_run_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.query.providers import _record_to_source


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _record(*, ctx, kind, metadata):
    return ArtifactRecord(
        artifact_id="art-1",
        project=ctx,
        kind=kind,
        location=f"graph/art-1.json",
        content_hash="sha256:art-1",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        metadata=dict(metadata),
    )


def test_run_id_propagated_to_source_reference(ctx):
    """Headline regression: ``metadata.run_id`` → ``SourceReference.run_id``."""
    record = _record(
        ctx=ctx, kind="graph_json",
        metadata={"run_id": "run-xyz"},
    )
    source = _record_to_source(record)
    assert source.run_id == "run-xyz"


def test_chunk_id_propagated_to_source_reference(ctx):
    """``metadata.chunk_id`` → ``SourceReference.chunk_id``. Used by
    the validation runner's per-chunk identity checks."""
    record = _record(
        ctx=ctx, kind="chunk",
        metadata={"run_id": "run-xyz", "chunk_id": "ch-a"},
    )
    source = _record_to_source(record)
    assert source.chunk_id == "ch-a"
    assert source.run_id == "run-xyz"


def test_missing_run_id_projects_as_none(ctx):
    """If the metadata genuinely lacks ``run_id``, the source is None
    — same as before. The propagation is "preserve, not invent"."""
    record = _record(ctx=ctx, kind="report", metadata={})
    source = _record_to_source(record)
    assert source.run_id is None


def test_existing_fields_still_propagated(ctx):
    """The fix is additive — title, source_document_id,
    source_location keep working."""
    record = ArtifactRecord(
        artifact_id="art-2",
        project=ctx,
        kind="graph_json",
        location="graph/art-2.json",
        content_hash="sha256:art-2",
        byte_size=100,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=["doc-a"],
        metadata={
            "run_id": "run-xyz",
            "title": "Knowledge graph",
            "source_location": "graph/file.json",
        },
    )
    source = _record_to_source(record)
    assert source.run_id == "run-xyz"
    assert source.title == "Knowledge graph"
    assert source.source_document_id == "doc-a"
    assert source.source_location == "graph/file.json"


def test_no_run_id_orphan_misclassification_no_longer_happens(ctx):
    """Integration-shape: a graph_json artifact with proper
    ``metadata.run_id`` set must NOT project to a source with
    ``run_id=None``. This was the exact symptom operators saw in
    the validation report: artifacts had run_id in metadata but
    validation said run_id=None on the citation."""
    record = _record(
        ctx=ctx, kind="graph_json",
        metadata={"run_id": "74549b9efe95437a91592d3949b9edc0"},
    )
    source = _record_to_source(record)
    assert source.run_id == "74549b9efe95437a91592d3949b9edc0"
    # Defensive: check the field is the EXACT value, not stringified None
    # or coerced to empty.
    assert source.run_id != "None"
    assert source.run_id != ""
