from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from j1.jobs.status import ReviewStatus
from j1.projects.context import ProjectContext


@dataclass
class ReviewItem:
    review_item_id: str
    project: ProjectContext
    target_kind: str
    target_id: str
    review_status: ReviewStatus
    requested_at: datetime
    actor: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewDecision:
    """Record of who decided what on which review item, and when."""

    review_item_id: str
    decision: ReviewStatus
    actor: str
    decided_at: datetime
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
