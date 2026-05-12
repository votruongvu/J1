"""Tests for ``invalidate_lineage_missing_artifacts`` — the
project-wide sweep that catches lineage-required artifacts (graph_json,
chunk, compiled.text, …) with ``metadata.run_id=None``.

Why this exists separately from ``invalidate_orphan_artifacts``:
the document-scoped sweep filters by ``source_document_ids``, but
LightRAG-produced graph artifacts are workspace-aggregate and
frequently have empty ``source_document_ids``. The user's validation
report showed 7 such ``graph_json`` rows leaking into retrieval
with ``run_id=None``; the per-document sweep can't find them.

These tests assert the project-wide sweep does.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.artifact_state import (
    SEARCH_STATE_ACTIVE,
    SEARCH_STATE_INVALID,
    invalidate_lineage_missing_artifacts,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


class _Registry:
    """In-memory registry with the methods the sweep needs."""

    def __init__(self, records: list[ArtifactRecord]):
        self._by_id = {r.artifact_id: r for r in records}

    def list_artifacts(self, ctx, *, kind=None):  # noqa: ARG002
        if kind is None:
            return list(self._by_id.values())
        return [r for r in self._by_id.values() if r.kind == kind]

    def update_metadata(self, ctx, artifact_id, new_metadata):  # noqa: ARG002
        prev = self._by_id[artifact_id]
        self._by_id[artifact_id] = ArtifactRecord(
            **{**prev.__dict__, "metadata": dict(new_metadata)},
        )

    def get(self, artifact_id):
        return self._by_id[artifact_id]


def _art(*, artifact_id, kind, ctx, metadata=None, source_documents=None):
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"area/{artifact_id}.bin",
        content_hash=f"hash-{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=list(source_documents or []),
        metadata=dict(metadata or {}),
    )


def test_graph_json_with_no_run_id_and_no_source_documents_is_invalidated(ctx):
    """The production failure mode: a graph_json row with empty
    ``source_document_ids`` AND no ``run_id``. The document-scoped
    sweep misses it; the project-wide sweep catches it."""
    registry = _Registry([
        _art(
            artifact_id="g-orphan-1",
            kind="graph_json",
            ctx=ctx,
            metadata={},  # no run_id
            source_documents=[],  # no document binding either
        ),
    ])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )

    assert invalidated == 1
    rec = registry.get("g-orphan-1")
    assert rec.metadata["search_state"] == SEARCH_STATE_INVALID
    assert rec.metadata["invalid_reason"] == "missing_run_id"


def test_already_invalid_artifacts_are_idempotent(ctx):
    """Re-running the sweep is a no-op on artifacts that are already
    invalidated. Important for the repair endpoint, which operators
    may hit repeatedly during corpus cleanup."""
    registry = _Registry([
        _art(
            artifact_id="g-1",
            kind="graph_json",
            ctx=ctx,
            metadata={
                "search_state": SEARCH_STATE_INVALID,
                "invalid_reason": "missing_run_id",
            },
        ),
    ])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )

    assert invalidated == 0


def test_artifacts_with_run_id_are_left_alone(ctx):
    """Lineage-intact artifacts must NOT be flipped to invalid. The
    sweep is a cleanup, not a destructive operation."""
    registry = _Registry([
        _art(
            artifact_id="g-good-1",
            kind="graph_json",
            ctx=ctx,
            metadata={"run_id": "run-xyz", "search_state": SEARCH_STATE_ACTIVE},
        ),
    ])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )

    assert invalidated == 0
    rec = registry.get("g-good-1")
    assert rec.metadata["search_state"] == SEARCH_STATE_ACTIVE
    assert rec.metadata["run_id"] == "run-xyz"


def test_non_lineage_kinds_skipped_even_without_run_id(ctx):
    """Generic blob/document kinds NOT in ``_LINEAGE_REQUIRED_KINDS``
    are legitimately allowed to have no run_id (operator uploads, raw
    files). The sweep leaves them alone."""
    registry = _Registry([
        _art(
            artifact_id="raw-1",
            kind="raw_upload",  # not in required-kinds set
            ctx=ctx,
            metadata={},  # no run_id, but OK for this kind
        ),
    ])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )

    assert invalidated == 0


def test_mixed_corpus_only_required_orphans_flipped(ctx):
    """End-to-end: a corpus with a mix of good/bad/non-required
    artifacts. The sweep flips exactly the required-kind orphans
    and nothing else."""
    registry = _Registry([
        # Required + no run_id → invalidate.
        _art(artifact_id="g-bad-1", kind="graph_json", ctx=ctx, metadata={}),
        _art(artifact_id="c-bad-1", kind="chunk", ctx=ctx, metadata={}),
        # Required + has run_id → leave alone.
        _art(
            artifact_id="g-good-1", kind="graph_json", ctx=ctx,
            metadata={"run_id": "r-ok"},
        ),
        # Non-required kind, no run_id → leave alone.
        _art(artifact_id="raw-1", kind="raw_upload", ctx=ctx, metadata={}),
    ])

    invalidated = invalidate_lineage_missing_artifacts(
        ctx=ctx, artifacts=registry,
    )

    assert invalidated == 2
    assert registry.get("g-bad-1").metadata["search_state"] == SEARCH_STATE_INVALID
    assert registry.get("c-bad-1").metadata["search_state"] == SEARCH_STATE_INVALID
    assert "search_state" not in registry.get("g-good-1").metadata
    assert "search_state" not in registry.get("raw-1").metadata
