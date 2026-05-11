"""Broker-neutral event publisher abstraction.

Three implementations ship with the framework:

 * `NoopEventPublisher` — the safe default. Accepts events but emits
 nothing. Production deployments that haven't opted into a broker
 integration get this.
 * `InMemoryEventPublisher` — stores published events in a list for
 tests, local development, and deterministic verification.
 * `BusEventPublisher` — bridges into the existing
 `ApplicationEventBus` so the same events flow to webhook
 subscribers. This is how the queue/event surface and the existing
 webhook surface share *one* application event model.

Real broker adapters (Kafka / RabbitMQ / SQS / NATS / Redis Streams)
should live in `j1.adapters.<broker>/` packages and implement this
same `EventPublisher` Protocol — see docs/event-integration.md for
the recipe. The framework intentionally ships **no** broker code.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from j1.integration.events.bus import ApplicationEventBus
from j1.integration.events.channels import channel_for
from j1.integration.events.event import ApplicationEvent

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishedEnvelope:
    """In-memory record of one publication.

 Carries the resolved logical `channel` alongside the event so
 consumers can assert routing without having to recompute it.
 `headers` mirrors what a real broker adapter would set on the
 transport (e.g. Kafka headers, AMQP properties): `eventId`,
 `eventType`, `correlationId`, `tenantId`, `producer`,
 `schemaVersion`, optional `idempotencyKey`.
 """

    event: ApplicationEvent
    channel: str
    headers: dict[str, str] = field(default_factory=dict)


class EventPublisher(Protocol):
    """Publish an `ApplicationEvent` to a broker-neutral logical channel.

 Implementations must not raise — publication failure should never
 break the caller's primary work. `publish` returns nothing; durable
 delivery / retry semantics are the adapter's responsibility (and
 are documented per-adapter; the framework makes no exactly-once
 claims).
 """

    def publish(self, event: ApplicationEvent) -> None: ...


# ---- No-op (safe default) ------------------------------------------


class NoopEventPublisher:
    """Accepts every event, emits nothing.

 Production-safe — chosen by `select_publisher` when event
 publication is disabled or no transport is wired. A debug-level log
 line is emitted so operators can confirm wiring while leaving INFO
 quiet.
 """

    def publish(self, event: ApplicationEvent) -> None:
        _log.debug(
            "noop publisher received event id=%s type=%s tenant=%s",
            event.id, event.type, event.tenant_id,
        )


# ---- In-memory (tests + local dev) ---------------------------------


class InMemoryEventPublisher:
    """Records every published event for assertion-driven tests.

 Not suitable for production use — events live in process memory and
 are lost on restart. Provides `published`, `by_channel`, and
 `by_event_type` accessors so tests can assert routing + ordering.
 """

    def __init__(
        self,
        producer: str = "j1",
        schema_version: str = "1.0",
        idempotency_key: Callable[[ApplicationEvent], str] | None = None,
    ) -> None:
        self._producer = producer
        self._schema_version = schema_version
        self._idempotency_key = idempotency_key
        self._envelopes: list[PublishedEnvelope] = []

    @property
    def published(self) -> tuple[PublishedEnvelope, ...]:
        return tuple(self._envelopes)

    def by_channel(self, channel: str) -> list[PublishedEnvelope]:
        return [e for e in self._envelopes if e.channel == channel]

    def by_event_type(self, event_type: str) -> list[PublishedEnvelope]:
        return [e for e in self._envelopes if e.event.type == event_type]

    def clear(self) -> None:
        self._envelopes.clear()

    def publish(self, event: ApplicationEvent) -> None:
        envelope = PublishedEnvelope(
            event=event,
            channel=channel_for(event.type),
            headers=_build_headers(
                event,
                producer=self._producer,
                schema_version=self._schema_version,
                idempotency_key=self._idempotency_key,
            ),
        )
        self._envelopes.append(envelope)


# ---- Bus bridge (reuse webhook subscribers) ------------------------


class BusEventPublisher:
    """Publishes through an existing `ApplicationEventBus`.

 The bus' subscriber list — typically including
 `WebhookEventSubscriber` — receives every event without code
 changes. This is the canonical way for a deployment to make the
 queue/event surface and the webhook surface deliver from the same
 source of truth.

 Failure handling is delegated to the bus (which already swallows
 subscriber exceptions and logs them). Belt-and-suspenders: this
 publisher itself wraps the call so a misbehaving bus cannot
 surface to the caller.
 """

    def __init__(self, bus: ApplicationEventBus) -> None:
        self._bus = bus

    def publish(self, event: ApplicationEvent) -> None:
        try:
            self._bus.publish(event)
        except Exception:
            _log.exception(
                "bus publisher failed for event id=%s type=%s",
                event.id, event.type,
            )


# ---- Composite (fan out to multiple publishers) --------------------


class CompositeEventPublisher:
    """Calls every delegate publisher; never raises on a delegate failure.

 Useful for "publish to my in-memory recorder for tests AND to the
 bus for webhook delivery" or "publish to two brokers in parallel
 during a migration".
 """

    def __init__(self, delegates: Iterable[EventPublisher]) -> None:
        self._delegates = list(delegates)

    def publish(self, event: ApplicationEvent) -> None:
        for delegate in self._delegates:
            try:
                delegate.publish(event)
            except Exception:
                _log.exception(
                    "delegate %r failed for event id=%s type=%s",
                    type(delegate).__name__, event.id, event.type,
                )


# ---- Header construction (shared) ----------------------------------


def _build_headers(
    event: ApplicationEvent,
    *,
    producer: str,
    schema_version: str,
    idempotency_key: Callable[[ApplicationEvent], str] | None = None,
) -> dict[str, str]:
    """Common message headers a transport adapter should set.

 Aligned with the CloudEvents extension attributes already used by
 the webhook layer (`kbtenantid`, `kbcorrelationid`, `kbactor`,
 `kbauthtype`) so consumers can read either transport without
 branching.
 """
    headers: dict[str, str] = {
        "eventId": event.id,
        "eventType": event.type,
        "occurredAt": event.occurred_at.isoformat(),
        "producer": producer,
        "schemaVersion": schema_version,
    }
    if event.correlation_id:
        headers["correlationId"] = event.correlation_id
        headers["requestId"] = event.correlation_id  # alias
    if event.tenant_id:
        headers["tenantId"] = event.tenant_id
    if event.actor:
        headers["actor"] = event.actor
    if event.auth_type:
        headers["authType"] = event.auth_type
    if idempotency_key is not None:
        try:
            key = idempotency_key(event)
        except Exception:
            key = None
        if key:
            headers["idempotencyKey"] = key
    else:
        # Reasonable default: `<eventType>:<subject>` so duplicates of
        # the same logical happening collapse on consumer-side
        # idempotency stores. Falls back to the event id (always unique)
        # when there's no subject.
        headers["idempotencyKey"] = (
            f"{event.type}:{event.subject}"
            if event.subject
            else event.id
        )
    return headers
