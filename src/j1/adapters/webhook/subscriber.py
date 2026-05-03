import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

from j1.adapters.webhook.service import WebhookDeliveryService
from j1.integration.events.event import ApplicationEvent
from j1.integration.events.subscriptions import WebhookSubscriptionRegistry

_log = logging.getLogger(__name__)


class DeliveryExecutor(Protocol):
    """Anything that can run a delivery callable in the background.

    Defined as a Protocol so callers can plug in a Temporal worker
    submission, an asyncio loop, or a synchronous executor for tests.
    """

    def submit(self, fn: Callable[..., None], /, *args, **kwargs) -> Future: ...


class DirectExecutor:
    """Runs callables synchronously — handy for tests and Temporal activities.

    Wraps the result in a `Future` so the interface matches
    `concurrent.futures.Executor` without pulling in the real one.
    """

    def submit(self, fn: Callable[..., None], /, *args, **kwargs) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class ThreadPoolDeliveryExecutor:
    """Background thread pool — the default for fire-and-forget delivery.

    Wraps `concurrent.futures.ThreadPoolExecutor` so we can shut it down
    cleanly. The pool is intentionally small: webhook delivery is I/O
    bound and we don't want to flood remote endpoints.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="j1-webhook"
        )

    def submit(self, fn: Callable[..., None], /, *args, **kwargs) -> Future:
        return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


class WebhookEventSubscriber:
    """Bridges `ApplicationEventBus` events to `WebhookDeliveryService`.

    Looks up matching subscriptions for each event and submits a delivery
    job per subscription. Submission is fire-and-forget (returns the
    `Future` for callers who care, but doesn't await it). Failures are
    swallowed and logged — the event bus contract requires `handle` to
    not raise.
    """

    def __init__(
        self,
        registry: WebhookSubscriptionRegistry,
        delivery_service: WebhookDeliveryService,
        executor: DeliveryExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._delivery_service = delivery_service
        self._executor: DeliveryExecutor = executor or ThreadPoolDeliveryExecutor()

    def handle(self, event: ApplicationEvent) -> None:
        try:
            matches = self._registry.matching(event)
        except Exception:
            _log.exception(
                "webhook subscription registry lookup failed for %s/%s",
                event.type, event.id,
            )
            return
        for subscription in matches:
            try:
                self._executor.submit(
                    self._delivery_service.deliver, subscription, event,
                )
            except Exception:
                _log.exception(
                    "failed to submit webhook delivery for sub=%s event=%s/%s",
                    subscription.id, event.type, event.id,
                )
