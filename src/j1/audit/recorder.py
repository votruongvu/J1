import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from j1.audit.events import AuditEvent
from j1.audit.sink import AuditSink
from j1.projects.context import ProjectContext


class AuditRecorder(Protocol):
    def record(
        self,
        ctx: ProjectContext,
        *,
        actor: str,
        action: str,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str: ...


class DefaultAuditRecorder:
    def __init__(
        self,
        sink: AuditSink,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def record(
        self,
        ctx: ProjectContext,
        *,
        actor: str,
        action: str,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        event = AuditEvent(
            event_id=self._id_factory(),
            occurred_at=self._clock(),
            project=ctx,
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            correlation_id=correlation_id,
            payload=dict(payload) if payload else {},
        )
        self._sink.write(event)
        return event.event_id
