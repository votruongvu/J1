import logging
from collections.abc import Iterable
from typing import Protocol

from j1.integration.events.event import ApplicationEvent

_log = logging.getLogger(__name__)


class EventSubscriber(Protocol):
    """Receives an `ApplicationEvent`. Implementations must not raise.

    Subscribers are called synchronously from `ApplicationEventBus.publish`,
    but the bus traps exceptions and logs them so a failing subscriber never
    breaks publication. Long-running work (network I/O, retries) belongs in
    a thread pool / worker behind the subscriber, not in `handle` itself.
    """

    def handle(self, event: ApplicationEvent) -> None: ...


class ApplicationEventBus:
    """In-process pub/sub for `ApplicationEvent`.

    Trivially fan-out: every registered subscriber sees every event. Type
    filtering is the subscriber's job (so subscriptions can be matched by
    tenant + secret + retry policy in one place — see WebhookSubscription).
    """

    def __init__(self, subscribers: Iterable[EventSubscriber] | None = None) -> None:
        self._subscribers: list[EventSubscriber] = list(subscribers or ())

    def subscribe(self, subscriber: EventSubscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, event: ApplicationEvent) -> None:
        for sub in self._subscribers:
            try:
                sub.handle(event)
            except Exception:
                _log.exception(
                    "event subscriber %r failed for event %s/%s",
                    type(sub).__name__, event.type, event.id,
                )

    @property
    def subscribers(self) -> tuple[EventSubscriber, ...]:
        return tuple(self._subscribers)
