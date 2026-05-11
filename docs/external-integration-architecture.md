# External Integration Architecture

The single map of every way external systems talk to J1. Every per-area
guide ([rest-api.md](rest-api.md), [security.md](security.md),
[webhooks.md](webhooks.md), [event-integration.md](event-integration.md),
[bulk.md](bulk.md), [mcp-status.md](mcp-status.md),
[troubleshooting.md](troubleshooting.md)) plugs into this overview.

---

## 1. Layer rules

```
┌─────────────────────────────────────────────────────────────────┐
│ Outer / transport adapters │
│ j1.adapters.rest (REST + OpenAPI + SSE) │
│ j1.adapters.webhook (HTTP webhook delivery + CloudEvents) │
│ j1.adapters.<broker> (Kafka / RabbitMQ / SQS — not shipped) │
│ j1.adapters.mcp (MCP — not shipped, see mcp-status.md) │
└────────────────────────────────────┬────────────────────────────┘
 │ depends on
 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Integration boundary (j1.integration.*) │
│ Ports + DTOs + ApplicationFacade (services) │
│ SecurityContext, ApiKeyAuthenticator, JwtAuthenticator │
│ ApplicationEvent, ApplicationEventBus │
│ EventPublisher Protocol + Noop / InMemory / Bus / Composite │
│ CloudEvents 1.0 mapper, signing, subscriptions │
│ AnswerStreamingService, BufferingStreamHandler │
│ BulkExportService, BulkImportService, schemas │
└────────────────────────────────────┬────────────────────────────┘
 │ depends on
 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Application services │
│ DocumentIntakeService, ProcessingService, │
│ HybridQueryEngine, SqliteSearchIndexer, │
│ ProjectActivities, KnowledgeProcessingActivities,... │
└────────────────────────────────────┬────────────────────────────┘
 │ depends on
 ▼
┌─────────────────────────────────────────────────────────────────┐
│ Core / domain │
│ intake, processing, query, search, artifacts, │
│ audit, cost, review, connectors, enrichers, │
│ orchestration (Temporal) │
└─────────────────────────────────────────────────────────────────┘
```

The arrow points one way. Static enforcement:

- [`tests/test_integration_layer.py::test_core_modules_do_not_import_external_layer`](../tests/test_integration_layer.py)
 AST-walks every core module and fails if it imports `j1.integration.*`
 or `j1.adapters.*`.
- [`tests/test_integration_layer.py::test_integration_does_not_import_protocol_adapters`](../tests/test_integration_layer.py)
 asserts `j1.integration` never imports `j1.adapters.*`.
- [`tests/test_external_integration_consistency.py::test_no_outer_layer_imports_in_core_subpackages`](../tests/test_external_integration_consistency.py)
 is a second copy of the guard with an explicit allowlist of core
 packages — catches new packages that forget the rule.

---

## 2. Surfaces at a glance

| Surface | Wire format | Auth | Source of truth |
|---|---|---|---|
| **REST + OpenAPI** | JSON envelope `{requestId, data, meta}` / `{requestId, error}` | API key (`Authorization: Bearer …` or `X-API-Key`) | [rest-api.md](rest-api.md), [security.md](security.md) |
| **SSE streaming** | `text/event-stream` over `POST /answer?stream=true` | Same as `POST /answer` | [rest-api.md § 8](rest-api.md) |
| **Webhooks (HTTP push)** | CloudEvents 1.0 JSON, HMAC-signed `X-KB-Signature` | Receiver-side shared secret | [webhooks.md](webhooks.md) |
| **Queue / event broker** | Broker-native; same `ApplicationEvent` body, same headers | Broker auth (deployment) | [event-integration.md](event-integration.md), [docs/asyncapi/kb-events.asyncapi.yaml](asyncapi/kb-events.asyncapi.yaml) |
| **Bulk import / export** | NDJSON files | Same as REST | [bulk.md](bulk.md) |
| **MCP** | _(not shipped — recipe documented)_ | Same as REST | [mcp-status.md](mcp-status.md) |

---

## 3. Cross-cutting contracts (shared by every surface)

### Identity and tenant scope

`SecurityContext(subject, tenant_id, scopes, auth_type, request_id, metadata)`
flows from each transport's authenticator into the application layer.
Tenant scope is enforced at every read/write — no surface skips it.

### Event types (12)

Defined once as `EVENT_*` constants in
[`j1/integration/events/event.py`](../src/j1/integration/events/event.py)
and frozen as `KB_EVENT_TYPES`. The same names appear in:

- Webhook delivery payloads (CloudEvents `type` field)
- AsyncAPI spec (one `Message` per event type)
- Publisher routing (`EVENT_TYPE_TO_CHANNEL`)
- Test assertions

A single test
([`test_external_integration_consistency.py`](../tests/test_external_integration_consistency.py))
keeps every surface in sync.

### Logical channels (7)

`kb.documents`, `kb.ingestion`, `kb.indexing`, `kb.query`, `kb.answer`,
`kb.citation`, `kb.audit`. Defined in
[`j1/integration/events/channels.py`](../src/j1/integration/events/channels.py).
`channel_for(event_type)` is the single dispatch function — used by
publishers, the AsyncAPI spec, and any future broker adapter.

### Standard envelope (REST + bulk)

Success:

```json
{ "requestId": "9f1c…", "data": {... }, "meta": {} }
```

Error:

```json
{ "requestId": "9f1c…", "error": { "code": "…", "message": "…", "details": {} } }
```

`X-Request-Id` is round-tripped on every response, including SSE
disconnects and 401/403/422 short-circuits.

### CloudEvents extension attributes ↔ message headers

| Carries | CloudEvents extension (webhook) | Publisher header (queue) |
|---|---|---|
| Tenant scope | `kbtenantid` | `tenantId` |
| Correlation | `kbcorrelationid` | `correlationId` (+ `requestId` alias) |
| Authenticated subject | `kbactor` | `actor` |
| Auth method | `kbauthtype` | `authType` |

Different naming because CloudEvents extensions must be lowercase
letters/digits ≤20 chars; publisher headers use camelCase. Same
semantic fields. Verified by
[`test_cloudevents_extension_attrs_align_with_publisher_headers`](../tests/test_external_integration_consistency.py).

### Error-code catalogue

| Code | HTTP | Surface |
|-------------------------|---------|---------|
| `UNAUTHENTICATED` | 401 | REST (security middleware) |
| `INSUFFICIENT_SCOPE` | 403 | REST (`Depends(scope_required(...))`) |
| `INVALID_IDENTIFIER` | 400 | Bad tenant / project / document id |
| `INVALID_ARGUMENT` | 400 | `ValueError` |
| `DOCUMENT_NOT_FOUND` | 404 | `DocumentNotFoundError` |
| `ARTIFACT_NOT_FOUND` | 404 | `ArtifactNotFoundError` |
| `REVIEW_ITEM_NOT_FOUND` | 404 | `ReviewItemNotFoundError` |
| `APPLICATION_ERROR` | 400 | Temporal `ApplicationError` |
| `J1_ERROR` | 400 | Any other `J1Error` subclass |
| `HTTP_<status>` | matches | `HTTPException` raised inside the adapter |
| `ANSWER_GENERATION_FAILED` | n/a (SSE event) | `answer.failed` on streamed answer |
| Bulk import codes | n/a (response body) | `INVALID_JSON`, `SCHEMA_VALIDATION_FAILED`, `PROJECT_MISMATCH`, `DOCUMENT_NOT_FOUND`, `INTEGRITY_MISMATCH` |

A test
([`test_rest_error_codes_are_documented`](../tests/test_external_integration_consistency.py))
asserts every code raised by a REST handler appears in either
[rest-api.md](rest-api.md) or [security.md](security.md).

### Scope catalogue

| Scope | What it grants |
|------------------|----------------|
| `kb:read` | Generic reads (documents, artifacts, citations, sources, reviews, capabilities, exports) |
| `kb:search` | `POST /search` |
| `kb:retrieve` | `POST /retrieve` |
| `kb:answer` | `POST /answer` (incl. SSE streaming) |
| `kb:ingest` | Document upload + ingestion-job start + bulk import |
| `kb:feedback` | `POST /feedback` |
| `kb:admin` | Project provisioning, workflow control, review decisions |
| `kb:audit.read` | Audit + cost reports + feedback exports |
| `kb:delete` | Reserved (no endpoints currently bound) |

Defined once in
[`j1/integration/security/scopes.py`](../src/j1/integration/security/scopes.py)
as `SCOPE_*` constants. A test
([`test_no_inline_scope_literals_in_rest_routes`](../tests/test_external_integration_consistency.py))
asserts every route uses the constants — never a `"kb:..."` string
literal.

---

## 4. Failure isolation guarantee

Webhook / event delivery failures **must never** break a core
operation. Enforced at four layers:

1. **`ApplicationEventBus.publish`** — wraps every subscriber call;
 subscriber exceptions are logged, not propagated.
2. **`WebhookEventSubscriber.handle`** — wraps registry lookup and
 executor submission.
3. **`WebhookDeliveryService.deliver`** — wraps the HTTP call and
 retry loop; transport errors become `WebhookTransportError` and are
 recorded as failed attempts. Returns instead of raising.
4. **`EventPublisher.publish`** — Protocol contract: implementations
 MUST NOT raise. Built-in `Noop` / `InMemory` / `Bus` / `Composite`
 all comply (and the latter three are tested for it).

Asserted by
[`tests/test_webhook_delivery.py::test_bus_publish_does_not_raise_when_webhook_dies`](../tests/test_webhook_delivery.py),
[`tests/test_event_publisher.py::test_bus_publisher_swallows_bus_exceptions`](../tests/test_event_publisher.py),
[`tests/test_event_publisher.py::test_composite_isolates_failing_delegate`](../tests/test_event_publisher.py),
and several others.

---

## 5. Optional capabilities and the safe-default principle

Every external surface is **opt-in** at adapter construction. When
omitted:

| Construction param omitted | Behaviour |
|---|---|
| `authenticator=None` | Anonymous — useful for local dev / tests; production deployments wire an `ApiKeyAuthenticator` |
| `event_bus=None` | REST handlers don't publish events |
| `bulk_export=None` / `bulk_import=None` | Bulk endpoints return 503 |
| `job_starter=None` | `POST /documents/{id}/ingest` returns 503 |
| `workspace=None` | `GET /ingestion-jobs/{id}/events` returns 503 |
| `J1_EVENT_PUBLISHER_TYPE` unset | `noop` — accepts events, ships nothing |
| `J1_AUTH_*` unset | No keys configured (`authenticator` should not be wired) |
| `J1_WEBHOOK_*` unset | No subscriptions configured |

The principle: **misconfiguration silently disables the surface
rather than silently enabling it**. A misconfigured webhook never
sends to the wrong endpoint; a misconfigured publisher never ships to
an unintended broker. `/capabilities` reports what's wired so
operators can verify.

---

## 6. Adding a new transport adapter

The recipe is consistent across REST, webhook, MCP, queue:

1. Create a sibling package under `j1.adapters.<name>`.
2. Map your transport's request → an existing port call on
 `ApplicationFacade` (or a `BulkImportService`, or
 `AnswerStreamingService`, or …).
3. Map the port's return value → your transport's response.
4. Use `SecurityContext` from the inbound auth layer; never invent a
 second one.
5. Use the existing `ApplicationEvent` model for any events you emit.
6. Honour the no-raise contract on any publication / delivery call.

What stays out of the adapter (lives in the integration layer):
business logic, port definitions, DTOs, security primitives, the
event bus.

What stays out of the integration layer (lives in the adapter):
HTTP / WebSocket / broker-client imports, wire formatting, broker
credentials, transport-specific retry policies.

---

## 7. Cross-references

| For | See |
|---|---|
| Endpoint reference, examples | [rest-api.md](rest-api.md) |
| Auth layer + scope catalogue + JWT migration path | [security.md](security.md) |
| Webhook setup, CloudEvents wire format, signature verification | [webhooks.md](webhooks.md) |
| Queue/event broker integration, AsyncAPI, broker-adapter recipe | [event-integration.md](event-integration.md), [asyncapi/kb-events.asyncapi.yaml](asyncapi/kb-events.asyncapi.yaml) |
| NDJSON import/export | [bulk.md](bulk.md) |
| MCP status + scoping recipe | [mcp-status.md](mcp-status.md) |
| Common operational issues | [troubleshooting.md](troubleshooting.md) |
| Layering rules, port catalogue | [architecture.md](architecture.md) |
