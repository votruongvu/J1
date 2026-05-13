import threading
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
        # Per-correlation_id monotonic sequencer. Consumers tailing
        # ``/ingestion-runs/{id}/events`` use ``sequence`` to detect
        # out-of-order delivery and to reorder events that share the
        # same ``occurred_at`` millisecond — particularly common when
        # the diagnostic recorder emits ``stage.started`` + an LLM-
        # call event in the same tick. A bare counter keyed by
        # ``correlation_id`` (typically the run_id) is enough; the
        # recorder is per-worker so contention across workers is
        # avoided by Temporal's per-task-queue dispatch.
        self._sequence_lock = threading.Lock()
        self._sequence_by_corr: dict[str, int] = {}

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
        sequence: int | None = None
        if correlation_id:
            with self._sequence_lock:
                next_seq = self._sequence_by_corr.get(correlation_id, 0) + 1
                self._sequence_by_corr[correlation_id] = next_seq
                sequence = next_seq
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
            sequence=sequence,
        )
        self._sink.write(event)
        return event.event_id
