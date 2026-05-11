"""End-to-end tests for the webhook adapter (`j1.adapters.webhook`).

Verifies:
- WebhookDeliveryService formats CloudEvents + signs + sends
- 2xx responses are recorded as 'succeeded' with no further attempts
- non-2xx responses retry up to retry_max_attempts then terminate as 'failed'
- transport errors are retried then terminate as 'failed'
- Retry delays follow exponential backoff (clamped to retry_max_delay_seconds)
- WebhookEventSubscriber dispatches per-subscription deliveries via the executor
- Subscriber failures NEVER raise out of `handle` (core operation isolation)
- JsonlWebhookDeliveryStore round-trips records
"""

from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from j1.adapters.webhook import (
    DirectExecutor,
    WebhookDeliveryService,
    WebhookEventSubscriber,
    WebhookHttpClient,
    WebhookResponse,
    WebhookTransportError,
)
from j1.integration.events import (
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_RETRYING,
    DELIVERY_STATUS_SUCCEEDED,
    EVENT_ANSWER_GENERATED,
    EVENT_DOCUMENT_UPLOADED,
    InMemoryWebhookDeliveryStore,
    JsonlWebhookDeliveryStore,
    SIGNATURE_HEADER,
    StaticWebhookSubscriptionRegistry,
    WebhookDeliveryRecord,
    WebhookSubscription,
    sign_payload,
)
from j1.integration.events.event import ApplicationEvent


# ---- Test doubles ---------------------------------------------------


class _FakeClient:
    """Programmable HTTP client that records every POST.

 `responses` is a list of `WebhookResponse | Exception`. The Nth call
 returns / raises the Nth element. If the list is exhausted, the
 last entry is replayed (handy for "always 200" or "always raises").
 """

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def post(
        self, url: str, *, body: bytes,
        headers: Mapping[str, str], timeout: float,
    ) -> WebhookResponse:
        idx = min(len(self.calls), len(self.responses) - 1)
        outcome = self.responses[idx]
        self.calls.append({
            "url": url, "body": body,
            "headers": dict(headers), "timeout": timeout,
        })
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _now() -> datetime:
    return datetime(2026, 5, 3, 8, 30, 21, tzinfo=timezone.utc)


def _event(**overrides) -> ApplicationEvent:
    base = dict(
        id="evt-1",
        type=EVENT_DOCUMENT_UPLOADED,
        occurred_at=_now(),
        source="j1/test",
        subject="doc-1",
        tenant_id="acme",
        correlation_id="run-1",
        data={"size": 42},
    )
    base.update(overrides)
    return ApplicationEvent(**base)


def _sub(**overrides) -> WebhookSubscription:
    base = dict(
        id="sub-1",
        url="https://example.com/hook",
        event_types=frozenset({EVENT_DOCUMENT_UPLOADED}),
        secret="topsecret",
        retry_max_attempts=3,
        retry_initial_delay_seconds=0.1,
        retry_backoff=2.0,
        retry_max_delay_seconds=10.0,
        timeout_seconds=5.0,
    )
    base.update(overrides)
    return WebhookSubscription(**base)


@pytest.fixture
def store() -> InMemoryWebhookDeliveryStore:
    return InMemoryWebhookDeliveryStore()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def service(store, sleeps) -> WebhookDeliveryService:
    return WebhookDeliveryService(
        client=_FakeClient([WebhookResponse(200)]),
        store=store,
        clock=_now,
        sleeper=sleeps.append,
        delivery_id_factory=lambda: "del-fixed",
    )


# ---- Successful delivery --------------------------------------------


def test_delivery_succeeds_on_2xx(store, sleeps):
    client = _FakeClient([WebhookResponse(200)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    record = svc.deliver(_sub(), _event())
    assert record.status == DELIVERY_STATUS_SUCCEEDED
    assert record.response_status == 200
    assert record.attempt == 1
    assert sleeps == []  # no retries → no sleep


def test_delivery_records_request_metadata(store, sleeps):
    client = _FakeClient([WebhookResponse(202)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(_sub(), _event())
    call = client.calls[0]
    assert call["url"] == "https://example.com/hook"
    assert call["timeout"] == 5.0
    # CloudEvents content type
    assert call["headers"]["Content-Type"] == "application/cloudevents+json"
    assert call["headers"]["X-KB-Event-Id"] == "evt-1"
    assert call["headers"]["X-KB-Event-Type"] == EVENT_DOCUMENT_UPLOADED
    assert call["headers"]["X-Request-Id"] == "run-1"


def test_delivery_signs_payload(store, sleeps):
    client = _FakeClient([WebhookResponse(200)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(_sub(secret="topsecret"), _event())
    call = client.calls[0]
    expected = sign_payload("topsecret", call["body"])
    assert call["headers"][SIGNATURE_HEADER] == expected


def test_delivery_omits_signature_when_no_secret(store, sleeps):
    client = _FakeClient([WebhookResponse(200)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(_sub(secret=""), _event())
    assert SIGNATURE_HEADER not in client.calls[0]["headers"]


def test_delivery_uses_subscription_headers_to_override(store, sleeps):
    client = _FakeClient([WebhookResponse(200)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(
        _sub(headers={"Authorization": "Bearer rcv-token", "X-KB-Event-Id": "ignored"}),
        _event(),
    )
    headers = client.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer rcv-token"
    # Subscription-supplied headers override defaults
    assert headers["X-KB-Event-Id"] == "ignored"


def test_payload_is_cloudevents_envelope(store, sleeps):
    client = _FakeClient([WebhookResponse(200)])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    import json as _json
    svc.deliver(_sub(), _event())
    body = _json.loads(client.calls[0]["body"])
    assert body["specversion"] == "1.0"
    assert body["type"] == EVENT_DOCUMENT_UPLOADED
    assert body["data"] == {"size": 42}
    assert body["kbtenantid"] == "acme"


# ---- Failed delivery + retries --------------------------------------


def test_non_2xx_retries_then_fails(store, sleeps):
    client = _FakeClient([
        WebhookResponse(500),
        WebhookResponse(503),
        WebhookResponse(500),
    ])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    record = svc.deliver(_sub(retry_max_attempts=3), _event())
    assert record.status == DELIVERY_STATUS_FAILED
    assert len(client.calls) == 3
    # 3 attempt records + 1 terminal-failed promotion
    statuses = [r.status for r in store.list_all()]
    assert statuses[-1] == DELIVERY_STATUS_FAILED
    assert statuses.count(DELIVERY_STATUS_RETRYING) == 2


def test_transport_errors_are_retried(store, sleeps):
    client = _FakeClient([
        WebhookTransportError("conn refused"),
        WebhookTransportError("timeout"),
        WebhookResponse(200),
    ])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    record = svc.deliver(_sub(retry_max_attempts=3), _event())
    assert record.status == DELIVERY_STATUS_SUCCEEDED
    assert record.attempt == 3
    # Failed attempts have response_status=None
    failures = [r for r in store.list_all() if r.status == DELIVERY_STATUS_RETRYING]
    assert len(failures) == 2
    assert all(r.response_status is None for r in failures)


def test_retry_uses_exponential_backoff(store, sleeps):
    client = _FakeClient([
        WebhookResponse(500), WebhookResponse(500),
        WebhookResponse(500), WebhookResponse(500),
    ])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(
        _sub(
            retry_max_attempts=4,
            retry_initial_delay_seconds=1.0,
            retry_backoff=2.0,
            retry_max_delay_seconds=10.0,
        ),
        _event(),
    )
    # 4 attempts → 3 sleeps with exponential growth: 1, 2, 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_delay_clamped_to_max(store, sleeps):
    client = _FakeClient([
        WebhookResponse(500), WebhookResponse(500),
        WebhookResponse(500), WebhookResponse(500),
        WebhookResponse(500),
    ])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    svc.deliver(
        _sub(
            retry_max_attempts=5,
            retry_initial_delay_seconds=10.0,
            retry_backoff=10.0,
            retry_max_delay_seconds=15.0,
        ),
        _event(),
    )
    # Without clamping: 10, 100, 1000, 10000. With clamp to 15: 10, 15, 15, 15.
    assert sleeps == [10.0, 15.0, 15.0, 15.0]


def test_deliver_never_raises_on_transport_error(store, sleeps):
    """Webhook failure must not break the caller — that's the core promise."""
    client = _FakeClient([WebhookTransportError("dns fail")])
    svc = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    # Even with retry_max_attempts=1 the call returns rather than raising.
    record = svc.deliver(_sub(retry_max_attempts=1), _event())
    assert record.status == DELIVERY_STATUS_FAILED
    assert record.error == "dns fail"


# ---- Subscriber dispatch + isolation --------------------------------


class _CountingService:
    """Drop-in replacement for WebhookDeliveryService used by subscriber tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def deliver(self, subscription, event):
        self.calls.append((subscription.id, event.type))
        return None


def test_subscriber_dispatches_per_matching_subscription():
    registry = StaticWebhookSubscriptionRegistry([
        _sub(id="a", event_types=frozenset({EVENT_DOCUMENT_UPLOADED})),
        _sub(id="b", event_types=frozenset({EVENT_DOCUMENT_UPLOADED})),
        _sub(id="other", event_types=frozenset({EVENT_ANSWER_GENERATED})),
    ])
    service = _CountingService()
    sub = WebhookEventSubscriber(registry, service, executor=DirectExecutor())
    sub.handle(_event(type=EVENT_DOCUMENT_UPLOADED))
    assert sorted(c[0] for c in service.calls) == ["a", "b"]


def test_subscriber_skips_when_no_match():
    registry = StaticWebhookSubscriptionRegistry([
        _sub(event_types=frozenset({EVENT_ANSWER_GENERATED})),
    ])
    service = _CountingService()
    sub = WebhookEventSubscriber(registry, service, executor=DirectExecutor())
    sub.handle(_event(type=EVENT_DOCUMENT_UPLOADED))
    assert service.calls == []


def test_subscriber_handle_never_raises_on_delivery_failure():
    """A delivery raising must not propagate out of subscriber.handle."""

    class _BoomService:
        def deliver(self, subscription, event):
            raise RuntimeError("delivery exploded")

    # DirectExecutor will set the exception on the future, which surfaces
    # only if someone awaits future.result. The subscriber must NOT.
    registry = StaticWebhookSubscriptionRegistry([_sub()])
    sub = WebhookEventSubscriber(
        registry, _BoomService(), executor=DirectExecutor()
    )
    # Should not raise:
    sub.handle(_event())


def test_subscriber_handle_never_raises_on_registry_failure():
    class _BoomRegistry:
        def matching(self, event):
            raise RuntimeError("registry exploded")

    sub = WebhookEventSubscriber(
        _BoomRegistry(), _CountingService(), executor=DirectExecutor()
    )
    # Should not raise:
    sub.handle(_event())


# ---- Bus → subscriber → delivery, end to end -----------------------


def test_event_bus_to_webhook_end_to_end(store, sleeps):
    """Publishing on the bus triggers HTTP delivery via the subscriber."""
    from j1.integration.events import ApplicationEventBus

    client = _FakeClient([WebhookResponse(200)])
    delivery = WebhookDeliveryService(
        client=client, store=store, clock=_now,
        sleeper=sleeps.append, delivery_id_factory=lambda: "del-1",
    )
    registry = StaticWebhookSubscriptionRegistry([
        _sub(id="a", event_types=frozenset({EVENT_DOCUMENT_UPLOADED})),
    ])
    bus = ApplicationEventBus()
    bus.subscribe(WebhookEventSubscriber(
        registry, delivery, executor=DirectExecutor(),
    ))

    bus.publish(_event())

    assert len(client.calls) == 1
    assert store.list_all()[0].status == DELIVERY_STATUS_SUCCEEDED


def test_bus_publish_does_not_raise_when_webhook_dies(store, sleeps):
    """The headline guarantee: a webhook delivery exception cannot break
 publication. Caller code keeps running."""
    from j1.integration.events import ApplicationEventBus

    class _ExplodingService:
        def deliver(self, subscription, event):
            raise RuntimeError("dead")

    bus = ApplicationEventBus()
    bus.subscribe(WebhookEventSubscriber(
        StaticWebhookSubscriptionRegistry([_sub()]),
        _ExplodingService(),
        executor=DirectExecutor(),
    ))
    # Publication completes cleanly:
    bus.publish(_event())


# ---- JsonlWebhookDeliveryStore --------------------------------------


def test_jsonl_store_round_trip(tmp_path):
    path = tmp_path / "deliveries.jsonl"
    store = JsonlWebhookDeliveryStore(path)
    record = WebhookDeliveryRecord(
        delivery_id="d-1",
        subscription_id="s-1",
        event_id="e-1",
        event_type=EVENT_DOCUMENT_UPLOADED,
        attempted_at=_now(),
        attempt=1,
        status=DELIVERY_STATUS_SUCCEEDED,
        response_status=200,
        elapsed_ms=42,
        tenant_id="acme",
        correlation_id="c-1",
    )
    store.append(record)
    out = store.list_all()
    assert len(out) == 1
    assert out[0] == record


def test_jsonl_store_filters_by_subscription(tmp_path):
    path = tmp_path / "deliveries.jsonl"
    store = JsonlWebhookDeliveryStore(path)
    for sub_id in ("s-1", "s-2", "s-1"):
        store.append(WebhookDeliveryRecord(
            delivery_id=f"d-{sub_id}",
            subscription_id=sub_id,
            event_id="e", event_type=EVENT_DOCUMENT_UPLOADED,
            attempted_at=_now(), attempt=1,
            status=DELIVERY_STATUS_SUCCEEDED,
        ))
    matched = store.list_for_subscription("s-1")
    assert len(matched) == 2
    assert all(r.subscription_id == "s-1" for r in matched)
