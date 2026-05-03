import json

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.jobs.status import ReviewStatus
from j1.orchestration.activities.payloads import (
    CreateReviewItemsInput,
    ProjectScope,
    ReviewItemSpec,
)
from j1.orchestration.activities.review import ACTIVITY_CREATE_REVIEW_ITEMS


def test_activity_name(review_activities):
    name = review_activities.create_review_items_activity.__temporal_activity_definition.name
    assert name == ACTIVITY_CREATE_REVIEW_ITEMS


def test_create_review_items_adds_to_queue(
    review_activities, review_queue, ctx, workspace
):
    result = review_activities.create_review_items_activity(
        CreateReviewItemsInput(
            scope=ProjectScope.from_context(ctx),
            items=[
                ReviewItemSpec(
                    target_kind="artifact", target_id="art-1", notes="please verify"
                ),
                ReviewItemSpec(target_kind="artifact", target_id="art-2"),
            ],
            actor="reviewer",
            correlation_id="run-1",
        )
    )
    assert result.status == "succeeded"
    assert len(result.items) == 2

    queue_items = review_queue.list_pending(ctx)
    assert len(queue_items) == 2
    assert {i.target_id for i in queue_items} == {"art-1", "art-2"}
    assert queue_items[0].review_status is ReviewStatus.PENDING
    assert queue_items[0].actor == "reviewer"


def test_create_review_items_audits_each_item(
    review_activities, ctx, workspace
):
    review_activities.create_review_items_activity(
        CreateReviewItemsInput(
            scope=ProjectScope.from_context(ctx),
            items=[
                ReviewItemSpec(target_kind="artifact", target_id="art-1"),
                ReviewItemSpec(target_kind="artifact", target_id="art-2"),
            ],
        )
    )
    events = [
        json.loads(line)
        for line in (workspace.audit(ctx) / AUDIT_LOG_FILENAME).read_text().splitlines()
        if line.strip()
    ]
    review_events = [e for e in events if e["action"] == "j1.review.requested"]
    assert len(review_events) == 2
    assert {e["target_id"] for e in review_events} == {"art-1", "art-2"}


def test_create_review_items_empty_input(review_activities, ctx):
    result = review_activities.create_review_items_activity(
        CreateReviewItemsInput(
            scope=ProjectScope.from_context(ctx),
            items=[],
        )
    )
    assert result.status == "succeeded"
    assert result.items == []
