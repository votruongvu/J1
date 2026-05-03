# J1 Event Integration

J1 publishes domain events through a broker-neutral
[`EventPublisher`](../src/j1/integration/events/publisher.py) Protocol.
The framework ships **no** broker dependencies — Kafka, RabbitMQ,
SQS/SNS/EventBridge, NATS, Redis Streams, and friends are integrated
via deployment-supplied adapters that implement the same Protocol.

The same internal `ApplicationEvent` model drives both the existing
[webhook + CloudEvents](webhooks.md) layer and any queue/event
adapter — webhooks deliver events over HTTP, queue adapters publish
them to broker infrastructure, but the contract on the wire is the
same shape and the same set of event-type names.

---

## 1. AsyncAPI specification

The contract is documented at:

- [`docs/asyncapi/kb-events.asyncapi.yaml`](asyncapi/kb-events.asyncapi.yaml)
  — AsyncAPI 3.0 spec covering all 7 logical channels, all 12 event
  types, common headers, and security/sensitivity policy.

Validate the spec with the AsyncAPI CLI (no runtime dep — opt-in
tooling):

```bash
npx -y @asyncapi/cli validate docs/asyncapi/kb-events.asyncapi.yaml
```

`tests/test_asyncapi.py` keeps the spec structurally consistent with
the publisher's channel/event registry — every shipped event type is
asserted to have a matching message definition, and every logical
channel is asserted to be present.

---

## 2. Logical channels

Seven channels — broker-neutral. Physical topic / queue / stream
names are an infrastructure-config concern; an adapter is free to map
`kb.documents` to a Kafka topic, an AMQP exchange, an SQS queue URL,
etc.

| Channel          | What flows here                                       |
|------------------|--------------------------------------------------------|
| `kb.documents`   | Document lifecycle — `document.uploaded`               |
| `kb.ingestion`   | Parse / ingest pipeline events                         |
| `kb.indexing`    | Search-index + knowledge-graph updates                 |
| `kb.query`       | Search / retrieve completion events                    |
| `kb.answer`      | Answer-generation completion events                    |
| `kb.citation`    | Citation lifecycle events                              |
| `kb.audit`       | Catch-all — custom / unknown event types route here    |

The mapping lives in
[`j1.integration.events.channels`](../src/j1/integration/events/channels.py)
as `EVENT_TYPE_TO_CHANNEL`. `channel_for("custom.x")` returns
`kb.audit` so unknown events always have a home.

---

## 3. Event types (reused from the webhook layer)

The 12 event-type names are exactly those in `KB_EVENT_TYPES`:

```
document.uploaded            document.parsing_started
document.parsing_completed   document.ingestion_started
document.ingestion_completed document.ingestion_failed
document.indexing_started    document.indexing_completed
knowledge.updated            query.completed
answer.generated             citation.validation_failed
```

Webhooks and queue adapters both use these names — receivers branch
on `eventType`, never on transport.

---

## 4. The publisher abstraction

```python
class EventPublisher(Protocol):
    def publish(self, event: ApplicationEvent) -> None: ...
```

`publish()` MUST NOT raise. Publication failure must never break the
caller's primary work — the framework's own implementations all wrap
their delegate calls in try/except and log.

### Built-in implementations

| Class | Purpose |
|---|---|
| [`NoopEventPublisher`](../src/j1/integration/events/publisher.py) | The safe default. Production-safe when event publication is disabled or no transport is wired. Debug-level log only. |
| [`InMemoryEventPublisher`](../src/j1/integration/events/publisher.py) | Records every published event for assertion-driven tests. `published`, `by_channel`, `by_event_type`, `clear` accessors. **Not for production**. |
| [`BusEventPublisher`](../src/j1/integration/events/publisher.py) | Bridges into the existing `ApplicationEventBus` so the same event flows to webhook subscribers. The canonical way to share one event source between transports. |
| [`CompositeEventPublisher`](../src/j1/integration/events/publisher.py) | Fans out to multiple delegates. Failure-isolated. |

Each `publish()` constructs a [`PublishedEnvelope`](../src/j1/integration/events/publisher.py)
with the resolved logical `channel` and standard `headers` — adapters
should set those headers as broker-native metadata (Kafka headers,
AMQP properties, SQS message attributes, …).

### Standard headers an adapter sets

| Header | Required | Notes |
|---|:---:|---|
| `eventId` | yes | UUID hex, stable for the lifetime of an event |
| `eventType` | yes | One of the 12 event-type names (or a custom string) |
| `occurredAt` | yes | ISO-8601 timestamp |
| `producer` | yes | Logical producer identifier (default `"j1"`) |
| `schemaVersion` | yes | Bump on incompatible payload changes |
| `correlationId` | when set | Caller-supplied correlation / trace id |
| `requestId` | when set | Alias of `correlationId` for HTTP-sourced events |
| `tenantId` | when set | Tenant scope from the inbound `SecurityContext` |
| `actor` | when set | Authenticated subject (omitted for anonymous traffic) |
| `authType` | when set | `api_key` / `jwt` / `anonymous` |
| `idempotencyKey` | always | Default `<eventType>:<subject>`, falls back to `eventId` |

These align with the CloudEvents extension attributes the webhook
layer already sets (`kbtenantid`, `kbcorrelationid`, `kbactor`,
`kbauthtype`) so consumers can read either transport without
branching logic.

---

## 5. Configuration

Read from the environment in the existing `j1.*` style:

| Variable | Default | Notes |
|---|---|---|
| `J1_EVENT_PUBLISHER_TYPE` | `noop` | One of `noop` / `memory` / `bus`. Real broker adapters bring their own type strings. |
| `J1_EVENT_PUBLISHER_PRODUCER` | `j1` | Logical producer identifier emitted on every event header |
| `J1_EVENT_PUBLISHER_SCHEMA_VERSION` | `1.0` | Bump on incompatible payload changes |
| `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS` | `false` | When false (default), publishers/mappers must omit potentially sensitive fields. Set `true` only for trusted internal brokers — see § 8. |

`load_event_publisher_settings(env=...)` returns an
`EventPublisherSettings` dataclass; deployment code constructs the
matching publisher.

```python
from j1 import (
    InMemoryEventPublisher, NoopEventPublisher, BusEventPublisher,
    PUBLISHER_TYPE_BUS, PUBLISHER_TYPE_MEMORY, PUBLISHER_TYPE_NOOP,
    load_event_publisher_settings,
)

settings = load_event_publisher_settings()
publisher = {
    PUBLISHER_TYPE_NOOP:   NoopEventPublisher(),
    PUBLISHER_TYPE_MEMORY: InMemoryEventPublisher(
        producer=settings.producer,
        schema_version=settings.schema_version,
    ),
    PUBLISHER_TYPE_BUS:    BusEventPublisher(application_event_bus),
}[settings.publisher_type]
```

Default behaviour is deliberately safe: **publisher disabled (no-op),
no external broker required, no sensitive payload expansion**.

---

## 6. Webhook ↔ queue: one event model, two transports

| Concern | Webhook delivery | Queue/event adapter |
|---|---|---|
| Use case | Simple HTTP callbacks | High-volume enterprise integration |
| Wire format | CloudEvents 1.0 JSON over HTTPS | Broker-native (Kafka / AMQP / SQS / …) |
| Source of truth | Same `ApplicationEvent` | Same `ApplicationEvent` |
| Event-type names | Same | Same |
| Headers | CloudEvents extension attrs (`kbactor`, …) | Standard message headers (`actor`, `authType`, …) — same semantic meaning |
| Failure isolation | Bus + executor + delivery service | Publisher + delegate(s) — all `publish()` calls trap |
| Delivery guarantees | At-least-once retries with backoff | Adapter-defined; default contract is **at-least-once, no ordering** |

The recommended deployment pattern when both surfaces are needed:

```
domain code → ApplicationEventBus
                     ├─ WebhookEventSubscriber   (HTTP delivery)
                     └─ <broker>EventSubscriber  (queue delivery)
```

…or equivalently, use `CompositeEventPublisher([
BusEventPublisher(bus), KafkaEventPublisher(...)])` if a deployment
prefers an explicit fan-out at the publisher boundary.

---

## 7. Adding a real broker adapter (the recipe)

The framework ships **no** broker code — adding one is purely
deployment-side work and never modifies the integration layer. Place
the adapter in a sibling `j1.adapters.<broker>` package.

```python
# j1/adapters/kafka/publisher.py  (deployment-owned package)
import json
from j1 import (
    ApplicationEvent, EventPublisher, channel_for, to_cloudevent,
)

class KafkaEventPublisher:
    """Maps logical `kb.*` channels to configured Kafka topics."""

    def __init__(self, kafka_producer, topic_for_channel: dict[str, str]) -> None:
        self._producer = kafka_producer
        self._topic_for_channel = topic_for_channel

    def publish(self, event: ApplicationEvent) -> None:
        try:
            channel = channel_for(event.type)
            topic = self._topic_for_channel.get(channel, "kb.audit")
            payload = json.dumps(to_cloudevent(event)).encode("utf-8")
            headers = [
                ("eventId", event.id.encode()),
                ("eventType", event.type.encode()),
                # ... rest from the standard header table
            ]
            # Recommended ordering key: subject (typically a documentId)
            self._producer.send(
                topic, value=payload, headers=headers,
                key=(event.subject or event.id).encode("utf-8"),
            )
        except Exception:
            # Same contract: publisher.publish must never raise.
            logging.getLogger(__name__).exception(
                "Kafka publish failed for event id=%s type=%s",
                event.id, event.type,
            )
```

The same skeleton works for RabbitMQ (`channel.basic_publish` with
exchange = topic-channel mapping, properties carrying the headers),
SQS (`MessageAttributes`), NATS (`subject` = channel), and Redis
Streams (`xadd` with the headers as fields).

What stays in the deployment, **not** in the framework:

- The broker client library import (e.g. `aiokafka`, `pika`, `boto3`).
- The physical topic / queue / stream naming policy.
- Connection-credential handling — typically via the deployment's
  existing secret manager, never via env vars in code.
- Per-broker batching / partitioning / DLQ wiring.

---

## 8. Security

Sensitive-payload policy — **enforced by current publishers and tests**:

| Field | Default behaviour |
|---|---|
| Full document content | Never published |
| Raw extracted text | Never published |
| Object-storage keys / internal storage paths | Never published |
| Embedding vectors | Never published (n/a today — no vector pipeline) |
| Internal stack traces / exception messages | Never published — failure events use the `errorCode` + masked `errorMessage` shape |
| Full answer text | Never published — `answer.generated` carries metadata only (`modeUsed`, `citationCount`, `confidence`, `reviewRequired`) |
| Search / question text | Published by default (already public via REST). Set `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS=false` and supply a custom payload mapper to elide. |
| Tenant scope | Always set on every event header *and* in payload data — multi-tenant consumers MUST filter by `tenantId` |
| Actor | Set when the event was triggered by an authenticated request; omitted for anonymous traffic |

Operational hygiene:

- Event publishing is **opt-in** — defaulting to `noop` means a
  misconfigured deployment can't accidentally ship events anywhere.
- Broker credentials live in the deployment's existing secret
  system, not in env vars carried alongside event config.
- Logs include `eventId` / `correlationId` / `tenantId` for triage,
  but never raw payloads — `NoopEventPublisher` only logs at
  `DEBUG`.
- Tenant isolation: a single `EventPublisher` instance is process-
  wide, but every event carries `tenantId`. Adapters MUST NOT route
  events across tenants without the deployment's explicit topic /
  permission model.

---

## 9. Idempotency, ordering, delivery guarantees

The framework makes **no exactly-once claims**. Adapters typically
provide at-least-once delivery; consumers MUST be idempotent.

- **Idempotency key.** Set on every published envelope as
  `<eventType>:<subject>`, falling back to `eventId` when there's no
  subject. Consumers can use this as a dedup key in their own state
  store. A custom `idempotency_key` callable can be supplied to
  `InMemoryEventPublisher` for non-default schemes.
- **Recommended ordering key.** `event.subject` (typically a
  `documentId` or `artifactId`) — Kafka / Kinesis-style partitioning
  on this key gives per-document order without a global serialiser.
- **No global ordering.** Events about different documents may
  arrive in any order. Events about the *same* document arrive in
  publish order only when the underlying broker provides per-key
  ordering and the adapter uses subject as the partition key.
- **Retries.** Delegated to the broker adapter (Kafka producer
  acks/retries, AMQP publisher confirms, SQS retries via
  visibility timeout, …). Webhook delivery already has its own
  exponential backoff — see [webhooks.md](webhooks.md) § 4.

---

## 10. Dead-letter behaviour

Today the framework provides:

- **Webhook delivery**: persistent `WebhookDeliveryStore` (JSONL) of
  every attempt — succeeded / retrying / failed — see
  [webhooks.md](webhooks.md). This is the closest thing to a DLQ J1
  ships natively.
- **Queue adapter**: dead-letter behaviour is the broker adapter's
  responsibility. Recommended convention: a `kb.deadletter` channel
  carrying `{"reason": "...", "originalEventId": "...",
  "originalEventType": "...", "errorCode": "...",
  "errorMessage": "...", "retryable": false}` payloads.

The integration layer doesn't implement a generic DLQ because it
would couple to a specific broker — define one in the adapter when
needed.

---

## 11. Tests

| File | Covers |
|---|---|
| [tests/test_event_publisher.py](../tests/test_event_publisher.py) | Channel mapping totality + safe-default fallback; noop / in-memory / bus / composite publisher behaviour and failure-isolation; header construction (incl. anonymous events, custom idempotency-key factory, factory-failure swallowing); env-var settings loader + unknown-type rejection; sensitive-payload absence from default payloads. |
| [tests/test_asyncapi.py](../tests/test_asyncapi.py) | Spec exists, parses, declares AsyncAPI 3, has every logical channel + every event type, common headers schema is complete and aligned with the security layer, and message descriptions document the no-full-text + sensitivity policies. |

Run only the event-integration tests:

```bash
.venv/bin/pytest tests/test_event_publisher.py tests/test_asyncapi.py
```

---

## 12. Known limitations

- **No bundled broker adapter.** The framework intentionally ships
  none — see § 7 for the recipe. Future adapter packages
  (`j1.adapters.kafka`, etc.) implement the same `EventPublisher`
  Protocol without changing the integration layer.
- **No automatic publication from REST handlers.** Today the
  publisher abstraction is exposed and tested but not yet wired
  into `create_rest_api`. The recommended wiring is to construct a
  `BusEventPublisher(application_event_bus)` and let the existing
  webhook bridge in `j1.adapters.rest.events` continue to publish to
  the bus — the queue adapter automatically picks up the same
  events. A future PR can also pass `event_publisher=` to
  `create_rest_api` directly when a deployment wants to skip the
  bus.
- **No DLQ implementation.** Documented as a per-broker concern.
- **Synchronous `publish()`.** Built-in publishers are sync.
  Adapters around async broker libraries should fire-and-forget
  through their own executor (the same pattern as
  `WebhookEventSubscriber`'s `ThreadPoolDeliveryExecutor`) so they
  honour the no-raise contract.
