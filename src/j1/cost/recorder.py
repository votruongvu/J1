import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from j1.cost.breakdown import CostBreakdown, CostResult
from j1.cost.events import CostEvent
from j1.cost.sink import CostSink
from j1.projects.context import ProjectContext


class CostRecorder(Protocol):
    def record(
        self,
        ctx: ProjectContext,
        breakdown: CostBreakdown,
        *,
        job_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CostResult: ...


class DefaultCostRecorder:
    def __init__(
        self,
        sink: CostSink,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def record(
        self,
        ctx: ProjectContext,
        breakdown: CostBreakdown,
        *,
        job_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CostResult:
        event = CostEvent(
            event_id=self._id_factory(),
            occurred_at=self._clock(),
            project=ctx,
            vendor=breakdown.vendor,
            model=breakdown.model,
            unit_kind=breakdown.unit_kind,
            units=breakdown.units,
            amount=breakdown.amount,
            currency=breakdown.currency,
            job_id=job_id,
            correlation_id=correlation_id,
            metadata=dict(breakdown.metadata),
        )
        self._sink.write(event)
        return CostResult(event_id=event.event_id)
