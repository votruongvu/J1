from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from j1.processing.status import ResultStatus


@dataclass(frozen=True)
class CostBreakdown:
    vendor: str
    model: str
    unit_kind: str
    units: int
    amount: Decimal
    currency: str = "USD"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CostResult:
    event_id: str
    status: ResultStatus = ResultStatus.SUCCEEDED
