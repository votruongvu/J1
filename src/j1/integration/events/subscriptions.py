from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from j1.integration.events.event import ApplicationEvent

WILDCARD_EVENT_TYPE = "*"


@dataclass(frozen=True)
class WebhookSubscription:
    """Static webhook configuration record.

 `event_types` may include the literal `"*"` to subscribe to every event.
 `tenant_id=None` matches all tenants; otherwise the subscription only
 fires for events tagged with that tenant.

 Retry policy fields use the same shape as `RetryPolicySpec` (initial
 delay, exponential backoff, max attempts) so callers can carry the
 same numbers through.
 """

    id: str
    url: str
    event_types: frozenset[str]
    secret: str = ""
    enabled: bool = True
    tenant_id: str | None = None
    timeout_seconds: float = 10.0
    retry_max_attempts: int = 5
    retry_initial_delay_seconds: float = 1.0
    retry_backoff: float = 2.0
    retry_max_delay_seconds: float = 60.0
    headers: dict[str, str] = field(default_factory=dict)

    def accepts(self, event: ApplicationEvent) -> bool:
        if not self.enabled:
            return False
        if self.tenant_id is not None and self.tenant_id != event.tenant_id:
            return False
        return (
            WILDCARD_EVENT_TYPE in self.event_types
            or event.type in self.event_types
        )


class WebhookSubscriptionRegistry(Protocol):
    """Looks up subscriptions matching an event."""

    def matching(
        self, event: ApplicationEvent
    ) -> list[WebhookSubscription]: ...


class StaticWebhookSubscriptionRegistry:
    """In-memory registry — appropriate for env/file-driven configuration.

 A future API-managed registry would implement the same protocol and
 persist subscriptions to a registry file or database. The webhook
 delivery service depends on the protocol, not this implementation.
 """

    def __init__(self, subscriptions: Iterable[WebhookSubscription]) -> None:
        self._subscriptions: list[WebhookSubscription] = list(subscriptions)

    def matching(
        self, event: ApplicationEvent
    ) -> list[WebhookSubscription]:
        return [s for s in self._subscriptions if s.accepts(event)]

    def list(self) -> list[WebhookSubscription]:
        return list(self._subscriptions)
