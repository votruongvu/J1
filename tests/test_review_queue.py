from datetime import datetime, timezone

import pytest

from j1.errors.exceptions import J1Error
from j1.jobs.status import ReviewStatus
from j1.review.models import ReviewItem
from j1.review.queue import (
    REVIEW_QUEUE_FILENAME,
    JsonReviewQueue,
    ReviewItemNotFoundError,
)


def _item(ctx, *, item_id="r-1", status=ReviewStatus.PENDING) -> ReviewItem:
    return ReviewItem(
        review_item_id=item_id,
        project=ctx,
        target_kind="artifact",
        target_id="art-1",
        review_status=status,
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_empty_queue(review_queue, ctx):
    assert review_queue.list_items(ctx) == []
    assert review_queue.list_pending(ctx) == []


def test_add_then_get(review_queue, ctx):
    item = _item(ctx)
    review_queue.add(item)
    assert review_queue.get(ctx, item.review_item_id) == item


def test_get_missing_raises(review_queue, ctx):
    with pytest.raises(ReviewItemNotFoundError):
        review_queue.get(ctx, "missing")


def test_duplicate_id_rejected(review_queue, ctx):
    review_queue.add(_item(ctx))
    with pytest.raises(J1Error):
        review_queue.add(_item(ctx))


def test_list_pending_filters_by_status(review_queue, ctx):
    review_queue.add(_item(ctx, item_id="r-1", status=ReviewStatus.PENDING))
    review_queue.add(_item(ctx, item_id="r-2", status=ReviewStatus.APPROVED))
    pending = review_queue.list_pending(ctx)
    assert [i.review_item_id for i in pending] == ["r-1"]


def test_update_status(review_queue, ctx):
    review_queue.add(_item(ctx))
    review_queue.update_status(
        ctx, "r-1", ReviewStatus.APPROVED, actor="reviewer", notes="ok"
    )
    item = review_queue.get(ctx, "r-1")
    assert item.review_status is ReviewStatus.APPROVED
    assert item.actor == "reviewer"
    assert item.notes == "ok"


def test_update_missing_raises(review_queue, ctx):
    with pytest.raises(ReviewItemNotFoundError):
        review_queue.update_status(ctx, "missing", ReviewStatus.APPROVED)


def test_persistence_roundtrip(workspace, ctx):
    a = JsonReviewQueue(workspace)
    a.add(_item(ctx))

    b = JsonReviewQueue(workspace)
    assert b.get(ctx, "r-1").review_item_id == "r-1"


def test_isolates_projects(review_queue, ctx, other_ctx):
    review_queue.add(_item(ctx, item_id="r-a"))
    review_queue.add(_item(other_ctx, item_id="r-b"))
    assert [i.review_item_id for i in review_queue.list_items(ctx)] == ["r-a"]
    assert [i.review_item_id for i in review_queue.list_items(other_ctx)] == ["r-b"]


def test_queue_file_lives_in_runtime(review_queue, workspace, ctx):
    review_queue.add(_item(ctx))
    assert (workspace.runtime(ctx) / REVIEW_QUEUE_FILENAME).is_file()
