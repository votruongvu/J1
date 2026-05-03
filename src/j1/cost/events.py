from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from j1.projects.context import ProjectContext


@dataclass(frozen=True)
class CostEvent:
    event_id: str
    occurred_at: datetime
    project: ProjectContext
    vendor: str
    model: str
    unit_kind: str
    units: int
    amount: Decimal
    currency: str
    job_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
