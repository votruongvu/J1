# 09. External Integration Model

> Audience: integrators building on top of J1's REST + event
> surfaces.
> [Back to README](../README.md). See also
> [03-query-flow.md](03-query-flow.md),
> [04-core-data-model.md](04-core-data-model.md).

## Integration model

J1 is a backend service. Integrations connect to it via:

1. **REST API** — synchronous request/response. Document
   lifecycle, ingestion control, query.
2. **Server-sent events (SSE)** — streaming run progress.
   `GET /ingestion-runs/{id}/events/stream`.
3. **Event publisher** (Protocol seam, optional). Outbound webhook
   or message-queue notifications when a domain event fires.

There is no Kafka, NATS, or gRPC surface today. The codebase has
the `EventPublisher` Protocol (`src/j1/integration/events`) — wire
your transport against it.

## Authentication and tenant isolation

- Every REST call carries `X-Tenant-Id` and `X-Project-Id` headers.
- Auth is configurable via the security adapter in
  `src/j1/adapters/rest/security.py`. The default dev wiring runs
  anonymously; production deployments set `J1_AUTH_API_KEYS` or
  plug in a custom authenticator (OAuth, mTLS, vendor IAM).
- A request without the tenant header gets a uniform 400. A
  request with the wrong tenant for a resource gets a uniform 404
  — existence is not probeable across tenants.
- A request with an unknown / inactive tenant gets the same 404.

The integration layer should treat `(tenant_id, project_id,
document_id)` as the public identity of a piece of knowledge.
Snapshot ids and run ids are server-allocated; integrators receive
them in responses but should not synthesise their own.

## Upload / ingestion integration

### Single document upload + ingest

```
POST /documents/upload
  multipart: file=<bytes>
  → 201 { documentId, filename, checksum, … }

POST /documents/{documentId}/ingest
  body: { compilerKind, enricherKind?, graphBuilderKind?, indexerKind? }
  → 201 { documentId, ingestionRunId, workflowId, runType }
```

The `compilerKind` is the processor key registered in the
deployment's processing capabilities (typically `"raganything"`).
Other kinds (`enricherKind`, `graphBuilderKind`, `indexerKind`) are
optional — when unset, the workflow picks the default registered
for each stage.

### Batch upload

```
POST /ingestion-batches
  multipart: files[]=<bytes>...
  → 201 { batchRunId, childRunIds: [...] }
```

The endpoint spawns a `BatchOrchestrationWorkflow` parent that
dispatches one per-document child workflow per file. Operators see
the batch in the Temporal UI as a parent with N children.

### Reindex / refresh-enrich

```
POST /documents/{documentId}/reindex
  → 201 { documentId, reindexRunId, parentRunId, workflowId }

POST /documents/{documentId}/refresh-enrich
  → 201 { documentId, refreshRunId, parentRunId, reusedCompileFromRunId, workflowId }
```

Both are document-scoped. Reindex re-runs the whole workflow
against a fresh snapshot. Refresh-enrich reuses the previous
active run's compile output and only re-runs enrichment + graph
+ index. Both promote a new snapshot atomically on success and
leave the previous one in place on failure.

### Lifecycle

```
POST /documents/{documentId}/attach
POST /documents/{documentId}/detach
POST /documents/{documentId}/remove
```

All return the updated document record. Attach is idempotent;
detach is reversible; remove is gate-first + synchronous hard
cleanup.

## Query integration

### Public answer endpoint

```
POST /answer
  body: { question, scope?, mode?, topK?, citationRequired?, synthesize? }
  → 200 { answer, citations[], retrievedChunks[], evidenceFlags, llm }
```

This is the integration surface for products that just want
"ask a question, get an answer with citations". Internally it
delegates to the SmartQueryOrchestrator.

### Manual test query (interactive surface)

```
POST /ingestion-runs/{runId}/test-query
  body: { question, topK, mode, citationRequired, synthesize, validationScope, includeRaw }
  → 200 { requestId, runId, question, answer, synthesizedAnswer,
          retrievedChunks[], citations[], checks[],
          validationStatus, evidenceFlags, llm, debug }
```

The same orchestrator. The `runId` here is the *target run* the
test should be scoped to — the validation surface uses it; an
integrating system that just wants to query the active KB should
use `/answer` instead.

### Trace surface

```
POST /dev/query-trace
  body: { question, run_id?, document_id? }
  → 200 { final_status, answer, message, trace }
```

Returns the full orchestrator trace. Use for diagnostic
integrations.

## Event-based integration

The `EventPublisher` Protocol in `src/j1/integration/events`
defines a transport-agnostic event sink. Domain events the
publisher emits include:

- `document.uploaded`
- `document.ingestion.started`
- `document.ingestion.completed`
- `document.ingestion.failed`
- `document.attached` / `document.detached` / `document.removed`
- `ingestion.run.completed`
- `query.answered`

A deployment wires one or more publishers at startup:

- Webhook publisher → POSTs JSON to a per-tenant endpoint.
- Message-queue publisher → publishes to Kafka / SQS / Pub/Sub.
- Audit-log only (the default) → events land in the audit JSONL
  and nothing else.

Wiring is per-project; see `src/j1/integration/events/publisher.py`.

## Input / output contracts

The wire shape lives in `src/j1/adapters/rest/schemas.py`. Every
request and response uses Pydantic `CamelModel` so the public
contract is `camelCase` while the Python code stays `snake_case`.

Stability rules the integration layer relies on:

- **Additive changes only on stable endpoints.** Adding a field to
  a response is fine; renaming or removing one is a breaking
  change.
- **Required vs optional**. Required fields are typed without
  `| None`. Optional fields are explicit defaults.
- **Camel-case wire, snake-case service.** The REST adapter
  translates; no service-layer code references `camelCase`.

## Error handling principles

- HTTP status codes map to the typed exceptions thrown by the
  service layer:
  - `400` — Pydantic validation failed, or a client-supplied
    parameter is invalid.
  - `403` — auth failed.
  - `404` — resource missing OR cross-tenant attempt (uniform).
  - `409` — state-conflict (e.g. reindex while previous run is
    still running).
  - `412` — precondition failed (e.g. resume-from-checkpoint with
    no checkpoint).
  - `422` — semantic validation failed (e.g. unknown tester
    verdict). Pydantic blocks most of these earlier; the service
    guards for stand-alone callers.
  - `503` — a required collaborator isn't wired in this
    deployment. The error message says which one.
- The body for errors is the standard envelope:
  `{ requestId, error: { code, message } }`.
- For long-running ingestions, watch the SSE event stream rather
  than polling — backpressure is built in.

## Boundary: what J1 owns vs the integrator

| J1 owns | The integrator owns |
| --- | --- |
| Document storage, parsing, chunking, embedding | The source files and the decision of *what* to upload. |
| Snapshot lifecycle and visibility gating | Tenant + project mapping (who owns what). |
| Audit log of every state transition | Long-term archive of audit events outside J1. |
| Query orchestration + answer synthesis | UI / chat surface that displays answers to end users. |
| Per-tenant LLM client wiring at the platform level | Per-customer LLM cost budgeting (no hard caps in J1). |

J1 is **not** an auth provider, a billing system, or a user
directory. Integrators bring those.

## Future direction

- **WebSocket / SSE for answers.** Today `/answer` is synchronous;
  the wire shape is ready for streaming. Streamed answers are a
  high-value, near-term integration win.
- **Strongly-typed event schema.** The audit log is JSONL; an
  AsyncAPI specification + per-event Pydantic models would let
  integrators code-generate clients.
- **Stable client SDKs.** Today integrators target the REST
  surface directly. Generated Python + TypeScript SDKs against
  the FastAPI OpenAPI spec are planned.
- **Push-based document import.** A "register an existing
  document from an S3 URL" endpoint would let upstream pipelines
  push rather than POST. The internals support it; the surface
  is not wired yet.
