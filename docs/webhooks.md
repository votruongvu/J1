# J1 Webhooks (CloudEvents 1.0)

J1 publishes domain events as CloudEvents and delivers them to
subscribed HTTP endpoints. The KB core never sees CloudEvents — it
emits transport-neutral [`ApplicationEvent`s](../src/j1/integration/events/event.py),
the integration layer formats them, and the
[webhook adapter](../src/j1/adapters/webhook/) does the HTTP work.

---

## 1. Layering

```
[core / activities] ── publish ApplicationEvent ──► ApplicationEventBus
 │
 ┌──────────────────────────────┴──────────────────────────────┐
 ▼ ▼
 [other subscriber] WebhookEventSubscriber
 │
 ▼
 ThreadPoolDeliveryExecutor.submit(...) ──── fire-and-forget
 │
 ▼
 WebhookDeliveryService
 ┌───────────────┴────────────────┐
 │ to_cloudevent(event) │
 │ + sign_payload(secret, body) │
 │ + retry w/ exponential backoff │
 │ + log per attempt to store │
 └───────────────┬────────────────┘
 ▼
 WebhookHttpClient.post(...)
 │
 ▼
 Receiver endpoint
```

Where things live:

| Module | Purpose |
|--------|---------|
| [`j1.integration.events.event`](../src/j1/integration/events/event.py) | `ApplicationEvent`, event-type constants |
| [`j1.integration.events.bus`](../src/j1/integration/events/bus.py) | `ApplicationEventBus`, `EventSubscriber` Protocol |
| [`j1.integration.events.cloudevents`](../src/j1/integration/events/cloudevents.py) | `to_cloudevent(event) -> dict` |
| [`j1.integration.events.subscriptions`](../src/j1/integration/events/subscriptions.py) | `WebhookSubscription`, `StaticWebhookSubscriptionRegistry` |
| [`j1.integration.events.signing`](../src/j1/integration/events/signing.py) | HMAC-SHA256 signing + verification |
| [`j1.integration.events.delivery`](../src/j1/integration/events/delivery.py) | `WebhookDeliveryRecord`, `JsonlWebhookDeliveryStore` |
| [`j1.integration.events.settings`](../src/j1/integration/events/settings.py) | `WebhookSettings`, `load_webhook_settings(env=...)` |
| [`j1.adapters.webhook.client`](../src/j1/adapters/webhook/client.py) | `WebhookHttpClient` Protocol, `HttpxWebhookClient` |
| [`j1.adapters.webhook.service`](../src/j1/adapters/webhook/service.py) | `WebhookDeliveryService` (sign + retry + log) |
| [`j1.adapters.webhook.subscriber`](../src/j1/adapters/webhook/subscriber.py) | `WebhookEventSubscriber`, `ThreadPoolDeliveryExecutor`, `DirectExecutor` |

Constraints honoured:

- **No CloudEvents inside core.** `j1.intake`, `j1.processing`, etc.
 never import the cloudevents mapper or the webhook adapter.
- **No HTTP inside core.** Only `j1.adapters.webhook` imports `httpx`.
- **Webhook failures don't break core operations.** `EventBus.publish`
 traps subscriber exceptions; `WebhookDeliveryService.deliver` traps
 transport errors and never raises; submission to the delivery
 executor is fire-and-forget.

---

## 2. Event types

The 12 first-class event types (exported as `KB_EVENT_TYPES`):

| Event type | When |
|-------------------------------------|------|
| `document.uploaded` | A document was registered into a project |
| `document.parsing_started` | Compilation kicked off for a document |
| `document.parsing_completed` | Compilation finished, compiled artifacts available |
| `document.ingestion_started` | A document- or project-wide ingestion job began |
| `document.ingestion_completed` | The ingestion job finished successfully |
| `document.ingestion_failed` | The ingestion job ended in a terminal failure |
| `document.indexing_started` | The search indexer began work on a set of artifacts |
| `document.indexing_completed` | Indexing finished |
| `knowledge.updated` | The knowledge graph or any artifact set materially changed |
| `query.completed` | A `/search` or `/retrieve` call finished |
| `answer.generated` | An `/answer` call returned a generated answer |
| `citation.validation_failed` | A citation lookup failed validation |

Subscribers may register additional custom types — the framework only
checks for string membership on the `event_types` set.

---

## 3. CloudEvents payload format

Each delivery is a CloudEvents 1.0 JSON envelope:

```json
{
 "specversion": "1.0",
 "type": "document.uploaded",
 "source": "j1/rest",
 "id": "evt-7f3c…",
 "time": "2026-05-03T08:30:21+00:00",
 "datacontenttype": "application/json",
 "subject": "doc-01J…",
 "data": { "checksum": "sha256:…", "size": 4096 },
 "kbtenantid": "acme",
 "kbcorrelationid": "9f1c…",
 "kbactor": "svc-power",
 "kbauthtype": "api_key"
}
```

Notes:

- `subject` is the entity the event is about (typically a `documentId`
 or `artifactId`).
- `kbtenantid`, `kbcorrelationid`, `kbactor`, and `kbauthtype` are
 CloudEvents 1.0 extension attributes (lowercase letters/digits, ≤20
 chars). They make tenant routing, tracing, and identity attribution
 first-class without polluting `data`.
- `kbactor` is the authenticated subject from the inbound
 [`SecurityContext`](security.md). It's omitted when the event was
 triggered by an anonymous request (or a background worker), so
 receivers can distinguish "user X did this" from "system did this".
- `kbauthtype` mirrors the auth-type that produced the event
 (`api_key` / `jwt` / `anonymous`).
- `data` is whatever the publisher attached — never a raw audit row,
 HTTP request, or other transport-bound object.

The body is `Content-Type: application/cloudevents+json`.

---

## 4. Delivery semantics

| Behaviour | Default | Configurable |
|-----------|---------|--------------|
| Method | `POST` | — |
| Body | CloudEvents 1.0 JSON | — |
| Connect/read timeout | 10 seconds | per-subscription |
| Max attempts | 5 | per-subscription |
| Initial retry delay | 1 second | per-subscription |
| Backoff factor | ×2 | per-subscription |
| Max retry delay (clamp) | 60 seconds | per-subscription |
| Retried on transport errors (DNS/connect/timeout) | yes | — |
| Retried on non-2xx HTTP status codes | yes | — |
| Per-attempt delivery log | written to JSONL store | swap store |

`WebhookDeliveryService.deliver` is synchronous and never raises;
every outcome (success, retry exhaustion, transport failure) appends a
`WebhookDeliveryRecord` to the store and the *terminal* record is
returned. The `WebhookEventSubscriber` runs deliveries through a
`ThreadPoolDeliveryExecutor` so publication doesn't block.

### Delivery headers

| Header | Meaning |
|------------------------------|---------|
| `Content-Type` | `application/cloudevents+json` |
| `X-KB-Event-Id` | The CloudEvents `id` (echo of `event.id`) |
| `X-KB-Event-Type` | The event type (e.g. `document.uploaded`) |
| `X-KB-Signature` | `sha256=<hex>` HMAC of the body — only present when the subscription has a non-empty `secret` |
| `X-Request-Id` | Echo of the application's correlation ID, when set |
| _(plus `WebhookSubscription.headers`)_ | Per-subscription static headers — handy for receiver-side auth tokens |

---

## 5. Signature verification

Webhook receivers should compute the HMAC-SHA256 of the raw request
body using the configured secret and compare against the value in
`X-KB-Signature`. The framework ships
[`verify_signature(secret, payload, signature)`](../src/j1/integration/events/signing.py)
for the common case:

```python
from j1 import verify_signature, SIGNATURE_HEADER

def receiver(request):
 body = request.body # raw bytes — do NOT re-serialise
 sig = request.headers.get(SIGNATURE_HEADER, "")
 if not verify_signature(SHARED_SECRET, body, sig):
 return 401...
```

`verify_signature` uses `hmac.compare_digest` for constant-time
comparison.

---

## 6. Subscription model

```python
@dataclass(frozen=True)
class WebhookSubscription:
 id: str
 url: str
 event_types: frozenset[str] # may include "*"
 secret: str = "" # empty -> unsigned
 enabled: bool = True
 tenant_id: str | None = None # None -> matches every tenant
 timeout_seconds: float = 10.0
 retry_max_attempts: int = 5
 retry_initial_delay_seconds: float = 1.0
 retry_backoff: float = 2.0
 retry_max_delay_seconds: float = 60.0
 headers: dict[str, str] =... # static headers added to every request
```

A subscription matches an event when:
1. `enabled` is `True`, **and**
2. `tenant_id` is `None` or equals `event.tenant_id`, **and**
3. `"*"` ∈ `event_types` or `event.type` ∈ `event_types`.

### Configuration

Today the framework ships **static configuration only** —
subscriptions live in env / file or are wired programmatically. An API-
managed subscription registry is a planned future addition; it would
implement the same `WebhookSubscriptionRegistry` Protocol so the
delivery service stays unchanged.

| Variable | Notes |
|-------------------------------------|-------|
| `J1_WEBHOOK_ENABLED` | `true`/`false`. Currently advisory — adapters opt in by passing the subscriber. |
| `J1_WEBHOOK_SUBSCRIPTIONS` | Inline JSON array of subscription objects. Convenient for local dev. |
| `J1_WEBHOOK_SUBSCRIPTIONS_FILE` | Path to a JSON file with the same shape. Mount from your secrets store. Mutually exclusive with `J1_WEBHOOK_SUBSCRIPTIONS`. |
| `J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS`| Default `timeout_seconds` for entries that don't override it. |
| `J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS` | Default `retry_max_attempts`. |

Subscription objects accept either snake_case or camelCase keys.
Example file:

```json
[
 {
 "id": "billing-sink",
 "url": "https://hooks.example.com/billing",
 "event_types": ["answer.generated", "document.ingestion_completed"],
 "secret": "topsecret",
 "tenant_id": "acme",
 "timeout_seconds": 15.0,
 "retry_max_attempts": 3,
 "headers": { "Authorization": "Bearer rcv-token" }
 },
 {
 "id": "audit-sink",
 "url": "https://hooks.example.com/all",
 "event_types": ["*"]
 }
]
```

---

## 7. Wiring example

```python
from j1 import (
 ApplicationEventBus, HttpxWebhookClient,
 JsonlWebhookDeliveryStore, StaticWebhookSubscriptionRegistry,
 ThreadPoolDeliveryExecutor, WebhookDeliveryService,
 WebhookEventSubscriber, create_rest_api, load_webhook_settings,
)

settings = load_webhook_settings
if not settings.subscriptions:
 bus = ApplicationEventBus # nothing to deliver
else:
 registry = StaticWebhookSubscriptionRegistry(settings.subscriptions)
 store = JsonlWebhookDeliveryStore(workspace_runtime / "webhook_deliveries.jsonl")
 service = WebhookDeliveryService(client=HttpxWebhookClient, store=store)
 subscriber = WebhookEventSubscriber(
 registry, service, executor=ThreadPoolDeliveryExecutor(max_workers=4),
 )
 bus = ApplicationEventBus(subscribers=[subscriber])

# Hand the bus to the REST adapter — protected handlers (POST /documents,
# POST /documents/{id}/ingest, POST /ingestion-jobs, POST /search,
# POST /retrieve, POST /answer) will publish on it automatically with
# the authenticated actor attached.
app = create_rest_api(
 facade,
 authenticator=authenticator,
 event_bus=bus,
)
```

When `event_bus=None` (the default), no events are emitted — keeps
existing test deployments working unchanged.

### Manual publication from non-REST code

Background workers and Temporal activities can publish to the same bus.
They typically have no `SecurityContext`, so `actor` and `auth_type`
should stay `None` — receivers see that and treat the event as
system-triggered.

```python
from j1 import ApplicationEvent, EVENT_DOCUMENT_INDEXING_COMPLETED
import uuid
from datetime import datetime, timezone

bus.publish(ApplicationEvent(
 id=uuid.uuid4.hex,
 type=EVENT_DOCUMENT_INDEXING_COMPLETED,
 occurred_at=datetime.now(timezone.utc),
 source="j1/search-indexer",
 subject=artifact.artifact_id,
 tenant_id=ctx.tenant_id,
 correlation_id=workflow_id,
 data={"artifactCount": 12},
))
```

---

## 8. Failure handling

Webhook failure must never break core operations. The framework enforces
this at three levels:

1. **`ApplicationEventBus.publish`** wraps every subscriber call in a
 try/except — a misbehaving subscriber doesn't stop the others, and
 nothing propagates back to the publisher.
2. **`WebhookEventSubscriber.handle`** wraps registry lookups and
 executor submissions, so an exhausted thread pool or a misbehaving
 registry doesn't reach the bus.
3. **`WebhookDeliveryService.deliver`** wraps the HTTP call and the
 retry loop. Transport errors become `WebhookTransportError`, which
 are recorded as failed attempts. The method always returns; it never
 raises.

A persistent `WebhookDeliveryStore` (the JSONL one, by default) keeps
the history of every attempt so failed deliveries can be analysed or
re-driven offline.

---

## 9. Queue / event-broker integration

For high-volume enterprise integration use the
broker-neutral `EventPublisher` abstraction documented in
[event-integration.md](event-integration.md). It runs over the same
`ApplicationEventBus` and the same 12 event-type names, so consumers
can subscribe via webhook *or* via Kafka / RabbitMQ / SQS / NATS
without code branching. The AsyncAPI 3.0 contract for the queue
surface lives at
[`docs/asyncapi/kb-events.asyncapi.yaml`](asyncapi/kb-events.asyncapi.yaml).

---

## 10. Optional Temporal-backed delivery

Today's `WebhookDeliveryService` is synchronous + thread-pool. If the
deployment already runs the J1 Temporal worker, deliveries can be moved
to a workflow with three steps:

1. The `WebhookEventSubscriber` swaps its executor for one that calls
 `client.start_workflow(WebhookDeliveryWorkflow,...)` instead of
 submitting to a thread pool.
2. `WebhookDeliveryWorkflow` runs a single `deliver_webhook_activity`
 that wraps `WebhookDeliveryService.deliver` (still synchronous —
 activities are the right layer for blocking I/O).
3. Temporal's built-in retry policy and history give you durable
 resumable delivery without changing the integration layer.

Until that wiring lands, the in-process thread pool is the right
default — simple, isolated, and good enough for a single-writer
deployment.

---

## 11. Testability

Both layers are designed for stub-driven tests:

- `WebhookDeliveryService` accepts a `client`, a `store`, a `clock`, a
 `sleeper`, and a `delivery_id_factory`. None of them need real
 network or wall-clock time.
- `WebhookEventSubscriber` accepts a `DeliveryExecutor`. Use
 [`DirectExecutor`](../src/j1/adapters/webhook/subscriber.py) to run
 deliveries synchronously in tests, or
 `ThreadPoolDeliveryExecutor` for production.
- See [`tests/test_events.py`](../tests/test_events.py) for primitive
 coverage and [`tests/test_webhook_delivery.py`](../tests/test_webhook_delivery.py)
 for end-to-end delivery, retries, signing, and the failure-isolation
 guarantees.
