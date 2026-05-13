from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from j1.projects.context import ProjectContext


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    occurred_at: datetime
    project: ProjectContext
    actor: str
    action: str
    target_kind: str
    target_id: str
    correlation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Monotonically-increasing per-run sequence number.
    # Lets consumers detect out-of-order delivery and reorder by
    # the producer's actual emit order rather than by
    # ``occurred_at`` (which can tie when multiple events land in
    # the same millisecond). Populated by ``DefaultAuditRecorder``
    # when a sequencer is wired; ``None`` for legacy / unscoped
    # events.
    sequence: int | None = None
