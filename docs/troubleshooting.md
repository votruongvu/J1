# Troubleshooting

Common operational issues across the external integration surface,
keyed by what the operator sees on the wire or in the logs.

For the architectural map, see
[external-integration-architecture.md](external-integration-architecture.md).

---

## REST

### `401 UNAUTHENTICATED` on every call

The deployment was constructed with an `authenticator=` and the
incoming request has no credential.

```json
{ "error": { "code": "UNAUTHENTICATED", "message": "missing authentication credential" } }
```

Send either header (the framework accepts both):

```http
Authorization: Bearer <token>
```
or
```http
X-API-Key: <token>
```

Check the wire: anything else under `Authorization:` (e.g. `Basic …`)
is rejected and counted as missing — the framework only accepts the
`bearer` scheme. `Authorization: bearer` lower-case works (case-
insensitive). See [security.md § 2.1](security.md).

### `403 INSUFFICIENT_SCOPE` with `details.required_scope`

Authenticated but the token's scope set doesn't include the route's
required scope. The required scope is in the response body under
`error.details.required_scope` — match it against your token in the
[`SCOPE_*` table](security.md#4-scope-catalog). Common confusions:

- `kb:answer` is required for both `POST /answer` AND `POST /answer?stream=true`.
  The streaming branch shares the same dependency.
- `kb:audit.read` (NOT `kb:read`) is required for `GET /cost` and
  `GET /ingestion-jobs/{id}/events`.
- `kb:admin` is required for project provisioning and workflow control.

### `400 HTTP_400` "X-Tenant-Id header required"

You hit a tenant-scoped route without tenant headers. Add both:

```http
X-Tenant-Id: <id>
X-Project-Id: <id>
```

`POST /projects` is the one exception — it only needs `X-Tenant-Id`
because the `projectId` comes from the request body.

### `503` on `POST /documents/{id}/ingest` / `GET /ingestion-jobs/{id}` / `GET /reviews` / etc.

The capability isn't wired. The framework defaults to "missing
capability → 503" so misconfiguration silently disables a surface
rather than silently enabling it. Check `GET /capabilities`:

```bash
curl ... /capabilities | jq '.data.capabilities[] | select(.available == false)'
```

Then revisit the constructor params on `create_rest_api(...)` (e.g.
`job_starter=`, `bulk_export=`, `bulk_import=`, `event_bus=`,
`workspace=`).

### 404 envelope code is `DOCUMENT_NOT_FOUND` / `ARTIFACT_NOT_FOUND` / `REVIEW_ITEM_NOT_FOUND`

The id you supplied doesn't exist in the registry **for the resolved
tenant / project**. Common causes:

- Typo in the path (`/documents/doc-1` ≠ `/documents/Doc-1` —
  identifiers are case-sensitive).
- Wrong `X-Tenant-Id` / `X-Project-Id` (you're looking at the
  document under another tenant).
- The document was registered into a different J1 instance and
  you're querying the wrong one.

### `INVALID_IDENTIFIER` 400

The tenant or project id contains characters outside `[A-Za-z0-9_-]+`.
Identifiers must be non-empty and use only those characters.

---

## SSE streaming

### Stream ends immediately after `event: answer.failed`

The underlying `AnswerService` raised. The wire payload is the safe-
masked envelope:

```json
{ "code": "ANSWER_GENERATION_FAILED", "message": "Answer generation failed.", "retryable": true }
```

The actual exception is logged server-side with the request id —
`grep <request-id> <log-file>` to find it. The wire body is
intentionally generic so secrets / file paths / provider error
messages cannot leak. See [rest-api.md § 8](rest-api.md).

### Stream stops after a few events with no `answer.completed` or `answer.failed`

The client disconnected. The server detects this via
`request.is_disconnected()` between events and stops emitting. The
in-progress `AnswerService.answer()` call already finished
synchronously, so no work was wasted — but if you're behind a proxy
that closes idle connections, set `proxy_read_timeout` (or
equivalent) higher than your longest-expected answer duration.

### Browser `EventSource` doesn't connect

Native `EventSource` only supports `GET`. Use `fetch` with
`ReadableStream` — there's a complete example in
[rest-api.md § 8](rest-api.md).

---

## Webhooks

### Delivery shows `status: "retrying"` then `"failed"` in the JSONL log

Read [`<workspace>/runtime/webhook_deliveries.jsonl`](webhooks.md):

```bash
jq -c 'select(.subscription_id == "billing-sink")' \
  < <workspace>/runtime/webhook_deliveries.jsonl
```

The `error` field on each attempt distinguishes:

- `"http <status>"` — the receiver returned a non-2xx.
- Free text starting with `"timeout"` / `"transport error"` /
  `"dns fail"` — connectivity problem.

Retries follow exponential backoff clamped to `retry_max_delay_seconds`.
If you see `attempt: 5, status: "failed"` the subscription has
exhausted its retries; the framework does **not** re-drive
automatically — feed the rows back into your own retry tooling.

### Receiver verifies `X-KB-Signature` but always fails

Verify against the **raw request body bytes**, not the re-serialised
JSON. Even a single whitespace difference invalidates the HMAC. Use
[`verify_signature(secret, body, signature)`](../src/j1/integration/events/signing.py)
on the consumer side — it does constant-time comparison and the
prefix handling automatically.

### Subscription with `event_types: ["*"]` receives nothing

`*` is a literal string — make sure it's in a JSON array, not bare:

```json
{ "id": "all", "url": "https://...", "event_types": ["*"] }
```

Also confirm the bus actually has the subscriber attached:

```python
bus = ApplicationEventBus(subscribers=[WebhookEventSubscriber(...)])
```

If you only constructed the publisher (`BusEventPublisher(bus)`) but
didn't wire any subscriber, events go nowhere — the publisher just
forwards to the bus.

### Webhook delivery breaking core ingestion

This should be impossible — **report it as a bug**. Webhook failure
isolation is enforced at four layers (see
[external-integration-architecture.md § 4](external-integration-architecture.md))
and asserted by tests. If you see ingestion fail because of a webhook,
something subverted the contract.

---

## Bulk import / export

### `POST /imports/documents.ndjson` returns `succeeded: 0, skippedIdempotent: N`

Idempotent. The supplied checksums all matched existing documents in
the registry. This is the safe round-trip behaviour — a freshly-
exported file always re-imports as 0 / N / 0. To intentionally
re-add, the source-side document must have a different checksum (i.e.
different content).

### Failures with `code: "PROJECT_MISMATCH"`

A row's `tenantId` or `projectId` doesn't match the request's tenant
headers. Critical safety check — this is what stops a bulk upload
from being used to cross tenant boundaries. Either fix the rows or
issue the request with the matching tenant/project headers.

### `POST /imports/metadata.ndjson` failures with `code: "INTEGRITY_MISMATCH"`

The metadata import is a **verifier**, not a writer (see
[bulk.md § 2](bulk.md) note 1). When the supplied fields don't equal
the registry's stored values, the `message` lists the differing field
names. Use this as the final acceptance check after a backup/restore.

### `503` on `GET /exports/*` or `POST /imports/*`

The bulk services weren't passed to `create_rest_api`. Wire:

```python
app = create_rest_api(
    facade,
    bulk_export=BulkExportService(source_registry, artifact_registry, feedback_store),
    bulk_import=BulkImportService(source_registry),
)
```

---

## Event publishing / queue surface

### Events aren't reaching the broker

Run through the chain in order:

1. `J1_EVENT_PUBLISHER_TYPE` — defaults to `noop`. Set to `bus`
   (or your custom adapter type) to enable.
2. The publisher itself — `select_publisher`/your wiring code must
   construct the right one based on settings.
3. Subscribers — for `bus`, did you actually attach a webhook
   subscriber and/or a broker subscriber to the `ApplicationEventBus`?
4. The `event_bus=` param on `create_rest_api` — without it, REST
   handlers don't publish anything.

### `unsupported J1_EVENT_PUBLISHER_TYPE 'kafka'`

The built-in factory only knows `noop` / `memory` / `bus`. Real
broker adapters use their own type strings — they map `kafka` to
their own `KafkaEventPublisher` factory in the deployment's wiring
code, never via `load_event_publisher_settings`. See
[event-integration.md § 7](event-integration.md).

### CloudEvents extension attribute names look weird (`kbtenantid` not `tenantId`)

CloudEvents 1.0 spec: extension attribute names must be lowercase
letters/digits, ≤ 20 chars. The semantic equivalents on the queue
side use camelCase headers (`tenantId`). Same fields, different
naming due to spec constraints. Cross-checked by
[`test_cloudevents_extension_attrs_align_with_publisher_headers`](../tests/test_external_integration_consistency.py).

### AsyncAPI spec validation fails

```bash
npx -y @asyncapi/cli validate docs/asyncapi/kb-events.asyncapi.yaml
```

The framework's own structural test
([`tests/test_asyncapi.py`](../tests/test_asyncapi.py)) also fails
the build if the spec drifts from the publisher's channel/event
registry. Run the cross-layer guard:

```bash
.venv/bin/pytest tests/test_external_integration_consistency.py
```

---

## Configuration

### Env-var changes don't take effect

The framework reads env at construction time, not on every request.
Restart the process after changing:

| Variable group | Read by |
|---|---|
| `J1_AUTH_*` | `load_security_settings` (called at startup) |
| `J1_WEBHOOK_*` | `load_webhook_settings` (called at startup) |
| `J1_EVENT_*` | `load_event_publisher_settings` (called at startup) |
| `J1_TEMPORAL_*` | `load_temporal_settings` (called at worker startup) |
| `J1_DATA_ROOT` | `load_settings` (called at startup) |

### `ConfigError: failed to read api keys file`

`J1_AUTH_API_KEYS_FILE` points at a file the process can't read.
Check:

```bash
ls -l "$J1_AUTH_API_KEYS_FILE"
```

If the path is correct, check permissions (the file should be
readable by the process user). Do not put real keys in
`J1_AUTH_API_KEYS` (the inline form) for production — use the file
form so secrets stay out of the env / process listing.

### `ConfigError: set only one of J1_WEBHOOK_SUBSCRIPTIONS or J1_WEBHOOK_SUBSCRIPTIONS_FILE`

You set both. Pick one — inline JSON for dev, file for production.
The `J1_AUTH_API_KEYS` config has the same exclusivity rule.

---

## Architecture / dependency-direction

### Test failure: `test_no_outer_layer_imports_in_core_subpackages`

A core module imported from `j1.integration.*` or `j1.adapters.*`.
The arrow points one way only — outer layers depend on inner, never
the reverse. Move the import out of core, or pass the dependency in
via constructor.

### Test failure: `test_event_constants_exported_through_top_level`

A new event-type constant was added in
`j1.integration.events.event` but the export wiring through
`j1.integration.__init__` and `j1.__init__` was forgotten. Add it.

### Test failure: `test_no_inline_scope_literals_in_rest_routes`

A REST route used `scope_required("kb:read")` instead of
`scope_required(SCOPE_READ)`. Fix the route to use the constant —
keeps the scope catalogue in one place.

### Test failure: `test_rest_error_codes_are_documented`

A new `error_response(code="...")` was added but the code isn't
mentioned in [rest-api.md](rest-api.md) or [security.md](security.md).
Add a row to the appropriate error-code table.

---

## Tests

### How do I run only the external-integration tests?

```bash
.venv/bin/pytest \
  tests/test_rest_adapter.py \
  tests/test_rest_security.py \
  tests/test_rest_events.py \
  tests/test_rest_sse.py \
  tests/test_rest_bulk.py \
  tests/test_security.py \
  tests/test_events.py \
  tests/test_event_publisher.py \
  tests/test_webhook_delivery.py \
  tests/test_asyncapi.py \
  tests/test_bulk.py \
  tests/test_integration_layer.py \
  tests/test_external_integration_consistency.py
```

### How do I run a single test?

```bash
.venv/bin/pytest tests/test_rest_security.py::test_streaming_requires_kb_answer_scope
```

### How do I see which tests are slow?

```bash
.venv/bin/pytest --durations=10
```

The full suite runs in ~4s on a laptop; nothing should be slow
enough to need optimisation today.
