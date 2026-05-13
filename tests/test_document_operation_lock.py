"""Tests for the per-document mutating-operation CAS lock.

The dev-mode refactor introduced ``pending_operation`` /
``pending_operation_run_id`` /``pending_operation_started_at`` on
DocumentRecord plus two helpers on SourceRegistry:

  * ``try_acquire_operation_lock`` — atomic CAS; returns the
    locked record on success, ``None`` if a concurrent operation
    already holds the lock.

  * ``release_operation_lock`` — idempotent release; refuses to
    clear a lock owned by a different ``run_id`` so a stale
    handler can't trample a newer operation.

These tests pin the contract end-to-end (acquire / contention /
release / idempotence / cross-document independence)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


def _add(registry, ctx, document_id="doc-1"):
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
    )
    registry.add(record)
    return record


def test_acquire_lock_sets_fields(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    locked = registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )

    assert locked is not None
    assert locked.pending_operation == "reindex"
    assert locked.pending_operation_run_id == "run-A"
    assert locked.pending_operation_started_at is not None
    # Persisted, not just in-memory.
    reread = registry.get(ctx, "doc-1")
    assert reread.pending_operation == "reindex"


def test_acquire_second_lock_returns_none_when_held(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    first = registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    second = registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="remove", run_id="run-B",
    )

    assert first is not None
    assert second is None
    # First lock is still intact — second attempt didn't tamper.
    reread = registry.get(ctx, "doc-1")
    assert reread.pending_operation == "reindex"
    assert reread.pending_operation_run_id == "run-A"


def test_release_lock_clears_fields(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    released = registry.release_operation_lock(ctx, "doc-1")

    assert released.pending_operation is None
    assert released.pending_operation_run_id is None
    assert released.pending_operation_started_at is None


def test_release_when_not_held_is_idempotent(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    released = registry.release_operation_lock(ctx, "doc-1")
    assert released.pending_operation is None


def test_release_with_wrong_expected_run_id_is_skipped(ctx, workspace):
    """A stale handler must not clear a NEWER operation's lock —
    even if it thinks the work is its own."""
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    # Pretend a stale "run-stale" handler is trying to release.
    result = registry.release_operation_lock(
        ctx, "doc-1", expected_run_id="run-stale",
    )
    # Lock is unchanged — still belongs to run-A.
    assert result.pending_operation == "reindex"
    assert result.pending_operation_run_id == "run-A"


def test_release_with_matching_run_id_succeeds(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    released = registry.release_operation_lock(
        ctx, "doc-1", expected_run_id="run-A",
    )
    assert released.pending_operation is None


def test_locks_are_per_document(ctx, workspace):
    """Two different documents must not share the same lock —
    operations on doc-1 must not block operations on doc-2."""
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx, document_id="doc-1")
    _add(registry, ctx, document_id="doc-2")

    first = registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    second = registry.try_acquire_operation_lock(
        ctx, "doc-2", operation="reindex", run_id="run-B",
    )
    assert first is not None
    assert second is not None


def test_acquire_unknown_document_raises(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    with pytest.raises(DocumentNotFoundError):
        registry.try_acquire_operation_lock(
            ctx, "ghost", operation="reindex", run_id="run-A",
        )


def test_acquire_after_release_works(ctx, workspace):
    registry = JsonSourceRegistry(workspace)
    _add(registry, ctx)

    registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="reindex", run_id="run-A",
    )
    registry.release_operation_lock(ctx, "doc-1")
    again = registry.try_acquire_operation_lock(
        ctx, "doc-1", operation="remove", run_id="run-B",
    )
    assert again is not None
    assert again.pending_operation == "remove"
    assert again.pending_operation_run_id == "run-B"
