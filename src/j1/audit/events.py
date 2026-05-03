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
