import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from temporalio import activity
from temporalio.exceptions import ApplicationError

from j1.audit.recorder import AuditRecorder
from j1.jobs.status import ReviewStatus
from j1.orchestration.activities.payloads import (
    ApplyReviewDecisionInput,
    ApplyReviewDecisionResult,
    CreatedReviewItem,
    CreateReviewItemsInput,
    CreateReviewItemsResult,
)
from j1.review.models import ReviewItem
from j1.review.queue import ReviewQueue

ACTIVITY_CREATE_REVIEW_ITEMS = "j1.review.create_items"
ACTIVITY_APPLY_REVIEW_DECISION = "j1.review.apply_decision"

STATUS_SUCCEEDED = "succeeded"

ACTION_REVIEW_REQUESTED = "j1.review.requested"
ACTION_REVIEW_DECISION = "j1.review.decision"

TARGET_REVIEW_ITEM = "review_item"


class ReviewActivities:
    def __init__(
        self,
        review_queue: ReviewQueue,
        audit: AuditRecorder,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._queue = review_queue
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def all_activities(self) -> list:
        return [
            self.create_review_items_activity,
            self.apply_review_decision_activity,
        ]

    @activity.defn(name=ACTIVITY_CREATE_REVIEW_ITEMS)
    def create_review_items_activity(
        self, input: CreateReviewItemsInput
    ) -> CreateReviewItemsResult:
        ctx = input.scope.to_context()
        created: list[CreatedReviewItem] = []
        for spec in input.items:
            review_item_id = self._id_factory()
            item = ReviewItem(
                review_item_id=review_item_id,
                project=ctx,
                target_kind=spec.target_kind,
                target_id=spec.target_id,
                review_status=ReviewStatus.PENDING,
                requested_at=self._clock(),
                actor=input.actor,
                notes=spec.notes,
                metadata=dict(spec.metadata),
            )
            self._queue.add(item)
            self._audit.record(
                ctx,
                actor=input.actor,
                action=ACTION_REVIEW_REQUESTED,
                target_kind=spec.target_kind,
                target_id=spec.target_id,
                correlation_id=input.correlation_id,
                payload={
                    "review_item_id": review_item_id,
                    "notes": spec.notes,
                },
            )
            created.append(
                CreatedReviewItem(
                    review_item_id=review_item_id,
                    target_kind=spec.target_kind,
                    target_id=spec.target_id,
                )
            )
        return CreateReviewItemsResult(status=STATUS_SUCCEEDED, items=created)

    @activity.defn(name=ACTIVITY_APPLY_REVIEW_DECISION)
    def apply_review_decision_activity(
        self, input: ApplyReviewDecisionInput
    ) -> ApplyReviewDecisionResult:
        ctx = input.scope.to_context()
        try:
            review_status = ReviewStatus(input.decision)
        except ValueError as exc:
            raise ApplicationError(
                f"unknown review decision: {input.decision!r}",
                non_retryable=True,
            ) from exc

        self._queue.update_status(
            ctx,
            input.review_item_id,
            review_status,
            actor=input.actor,
            notes=input.notes,
        )
        event_id = self._audit.record(
            ctx,
            actor=input.actor,
            action=ACTION_REVIEW_DECISION,
            target_kind=TARGET_REVIEW_ITEM,
            target_id=input.review_item_id,
            correlation_id=input.correlation_id,
            payload={
                "decision": review_status.value,
                "notes": input.notes,
            },
        )
        return ApplyReviewDecisionResult(
            status=STATUS_SUCCEEDED,
            review_item_id=input.review_item_id,
            review_status=review_status.value,
            audit_event_id=event_id,
        )
