"""Tests for the JSONL batch-run store + status derivation.

The batch store is a parallel of `IngestionRunStore` for multi-upload
aggregations. Status is derived at read-time from the child runs —
never persisted, so the aggregate view never goes stale.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.runs.batch_store import (
    BatchRun,
    JsonlBatchRunStore,
    derive_batch_status,
)


@pytest.fixture
def store(workspace) -> JsonlBatchRunStore:
    return JsonlBatchRunStore(workspace)


def _now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_batch_store_persists_and_reads_back(store, ctx):
    batch = BatchRun(
        batch_run_id="batch-1",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_ids=["r1", "r2", "r3"],
        file_count=3,
        started_at=_now(),
        actor="ops@example.com",
        metadata={"source": "ui-multi-upload"},
    )
    store.upsert(ctx, batch)

    fetched = store.get(ctx, "batch-1")
    assert fetched is not None
    assert fetched.run_ids == ["r1", "r2", "r3"]
    assert fetched.file_count == 3
    assert fetched.actor == "ops@example.com"
    assert fetched.metadata == {"source": "ui-multi-upload"}


def test_batch_store_returns_none_for_unknown_id(store, ctx):
    assert store.get(ctx, "missing") is None


def test_batch_store_list_returns_latest_per_id_sorted_by_recency(store, ctx):
    earlier = BatchRun(
        batch_run_id="batch-old",
        tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        run_ids=["r1"], file_count=1,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    later = BatchRun(
        batch_run_id="batch-new",
        tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        run_ids=["r2"], file_count=1,
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    store.upsert(ctx, earlier)
    store.upsert(ctx, later)

    listing = store.list(ctx)
    assert [b.batch_run_id for b in listing] == ["batch-new", "batch-old"]


def test_batch_store_upsert_overwrites_via_latest_snapshot(store, ctx):
    """JSONL append-only contract: re-upserting the same id appends a
 fresh snapshot; reads return the latest."""
    initial = BatchRun(
        batch_run_id="batch-1",
        tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        run_ids=["r1"], file_count=1,
        started_at=_now(),
        metadata={"v": 1},
    )
    store.upsert(ctx, initial)
    updated = BatchRun(
        batch_run_id="batch-1",
        tenant_id=ctx.tenant_id, project_id=ctx.project_id,
        run_ids=["r1", "r2"], file_count=2,
        started_at=_now(),
        metadata={"v": 2},
    )
    store.upsert(ctx, updated)

    fetched = store.get(ctx, "batch-1")
    assert fetched.metadata == {"v": 2}
    assert fetched.file_count == 2


# ---- Status derivation ----------------------------------------------


def test_derive_batch_status_running_when_any_child_active():
    assert derive_batch_status(["succeeded", "running"]) == "running"
    assert derive_batch_status(["failed", "assessing"]) == "running"
    assert derive_batch_status(["paused"]) == "running"


def test_derive_batch_status_completed_when_all_succeeded():
    assert derive_batch_status(["succeeded", "succeeded"]) == "completed"


def test_derive_batch_status_completed_with_warnings_when_any_warning():
    assert derive_batch_status([
        "succeeded", "succeeded_with_warnings",
    ]) == "completed_with_warnings"


def test_derive_batch_status_partially_failed_for_mixed():
    assert derive_batch_status(["succeeded", "failed"]) == "partially_failed"
    assert derive_batch_status([
        "succeeded_with_warnings", "failed",
    ]) == "partially_failed"


def test_derive_batch_status_failed_when_all_failed_or_cancelled():
    assert derive_batch_status(["failed", "failed"]) == "failed"
    assert derive_batch_status(["failed", "cancelled"]) == "failed"


def test_derive_batch_status_deleted_when_all_deleted():
    assert derive_batch_status(["deleted", "deleted"]) == "deleted"


def test_derive_batch_status_running_when_empty():
    """Defensive: empty child list = treat as running so the FE
 polls again rather than reporting a misleading terminal status."""
    assert derive_batch_status([]) == "running"
