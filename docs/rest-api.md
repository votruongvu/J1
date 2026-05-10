# J1 REST API Adapter

The REST adapter exposes the J1 knowledge base over HTTP. It lives in
[`src/j1/adapters/rest/`](../src/j1/adapters/rest/) and is a thin translation
layer over the integration ports defined in [`src/j1/integration/ports.py`](../src/j1/integration/ports.py).

The adapter is **optional** — the framework remains library-first. Mount it
into any ASGI host (uvicorn, hypercorn, FastAPI lifespan, etc.) when you want
to publish a JSON HTTP surface.

---

## 1. Construction

```python
from j1 import (
    ApplicationFacade, create_rest_api, WorkspaceResolver, load_settings,
)

facade = ApplicationFacade(
    ingestion=...,            # DocumentIngestionPort   (required)
    retrieval=...,            # RetrievalPort           (required)
    source_lookup=...,        # SourceLookupPort        (required)
    citation_lookup=...,      # CitationLookupPort      (required)
    feedback=...,             # FeedbackPort            (required)
    event_publisher=...,      # EventPublisherPort      (required)
    search=...,               # SearchPort              (optional)
    answer=...,               # AnswerPort              (optional)
    job_status=...,           # JobStatusPort           (optional)
    project_admin=...,        # ProjectAdminPort        (optional)
    job_control=...,          # JobControlPort          (optional)
    cost_summary=...,         # CostSummaryPort         (optional)
    review=...,               # ReviewPort              (optional)
)

app = create_rest_api(
    facade,
    workspace=WorkspaceResolver(load_settings()),  # required for /events
    job_starter=my_job_starter,                    # required for /documents/{id}/ingest
    processing_capabilities=capabilities,          # see § 1.1 below — optional but strongly recommended
    version="1.2.0",
)
```

`create_rest_api` returns a `FastAPI` instance. Optional dependencies degrade
gracefully:

| Missing dependency        | Endpoints affected                              | Behaviour |
|---------------------------|-------------------------------------------------|-----------|
| `facade.search=None`      | `POST /search`, `POST /retrieve`                | `503`     |
| `facade.answer=None`      | `POST /answer`                                  | `503`     |
| `facade.job_status=None`  | `GET /ingestion-jobs/{jobId}`                   | `503`     |
| `facade.project_admin=None` | `POST /projects`                              | `503`     |
| `facade.job_control=None` | `POST /ingestion-jobs[/{id}/{pause,resume,cancel}]` | `503` |
| `facade.cost_summary=None`| `GET /cost`                                     | `503`     |
| `facade.review=None`      | `GET /reviews`, `POST /reviews/{id}/decision`   | `503`     |
| `job_starter=None`        | `POST /documents/{documentId}/ingest`           | `503`     |
| `workspace=None`          | `GET /ingestion-jobs/{jobId}/events`            | `503`     |
| `processing_capabilities=None` | `compilerKind` request field is required + not validated against registered kinds (see § 1.1) | — |

`/capabilities` advertises which endpoints the deployment has wired up.

### 1.1 Processing capabilities (recommended)

`create_rest_api` accepts an optional `processing_capabilities`
argument — a [`ProcessingCapabilities`](../src/j1/integration/dto.py)
DTO describing which processor `kind`s the runtime accepts. When
supplied, the API:

  * **Defaults** an omitted `compilerKind` request field to
    `default_compiler_kind` so simple clients can omit it entirely.
  * **Validates** `compilerKind` / `graphBuilderKind` /
    `enricherKind` / `indexerKind` against the registered set,
    rejecting unknown values with `400 INVALID_ARGUMENT` (instead
    of letting them surface as a workflow `UnknownProcessorError`
    seconds later).

Most deployments that use J1's bootstrap should pass
`capabilities_from_bootstrap(boot)`:

```python
from j1 import bootstrap_from_env, capabilities_from_bootstrap, create_rest_api
from j1.search.indexer import SqliteSearchIndexer

boot = bootstrap_from_env()
capabilities = capabilities_from_bootstrap(
    boot,
    # Bootstrap only manages compiler / graph / retrieval. Pass the
    # other roles your worker wires (typically the SQLite indexer
    # and any custom enrichers) so the API can validate them too.
    indexer_kinds=frozenset({SqliteSearchIndexer.kind}),
    enricher_kinds=frozenset({"my-enricher"}),
)
app = create_rest_api(facade, processing_capabilities=capabilities, ...)
```

When `processing_capabilities` is omitted, the API stays backward-
compatible: clients MUST send `compilerKind`, and any value is
forwarded to the workflow without validation.

---

## 2. Project context

Every endpoint operates inside a `ProjectContext (tenant_id, project_id)`. The
default resolver reads two headers on every request:

| Header           | Required | Notes                               |
|------------------|----------|-------------------------------------|
| `X-Tenant-Id`    | yes      | Alphanumeric / `_` / `-`            |
| `X-Project-Id`   | yes¹     | Same character class as tenant      |
| `X-Request-Id`   | no       | Echoed back; auto-generated if absent |

¹ `POST /projects` is a tenant-scoped operation and only requires
`X-Tenant-Id` — the project being created comes from the request body.

To use a different scoping scheme (JWT claim, path prefix, etc.) pass a
`context_resolver` callable into `create_rest_api`:

```python
def my_resolver(request: Request) -> ProjectContext:
    claims = decode_jwt(request.headers["authorization"])
    return ProjectContext(claims["tenant"], claims["project"])

app = create_rest_api(facade, context_resolver=my_resolver)
```

---

## 3. Standard envelope

### Success

```json
{
  "requestId": "9f1c…",
  "data":      { ...endpoint payload (camelCase)... },
  "meta":      { ...optional adapter-specific extras... }
}
```

### Error

```json
{
  "requestId": "9f1c…",
  "error": {
    "code":    "DOCUMENT_NOT_FOUND",
    "message": "no document with id 'doc-x'",
    "details": { "type": "DocumentNotFoundError" }
  }
}
```

`requestId` is also returned in the `X-Request-Id` response header on every
call.

### Error codes

| Code                  | HTTP    | Source                                      |
|-----------------------|---------|---------------------------------------------|
| `HTTP_4xx`/`HTTP_5xx` | 4xx/5xx | `HTTPException` raised inside the adapter   |
| `INVALID_IDENTIFIER`  | 400     | Bad tenant/project/document identifier      |
| `INVALID_ARGUMENT`    | 400     | `ValueError` (e.g. unknown query mode)      |
| `DOCUMENT_NOT_FOUND`  | 404     | `DocumentNotFoundError`                     |
| `ARTIFACT_NOT_FOUND`  | 404     | `ArtifactNotFoundError`                     |
| `REVIEW_ITEM_NOT_FOUND` | 404   | `ReviewItemNotFoundError`                   |
| `REVIEW_NOT_FOUND`    | 404     | `ingestion_review.ReviewNotFound` — raised by the result-review surface (`/ingestion-runs/{id}/summary`, …); covers missing run AND cross-tenant access (uniform 404 to avoid existence leak) |
| `UPLOAD_TOO_LARGE`    | 413     | `UploadTooLargeError` — multipart upload exceeded the configured `J1_MAX_UPLOAD_BYTES` cap (default 200 MiB). Response `details` carries `sizeBytes` and `maxBytes` so the client can render an actionable message. |
| `UNSUPPORTED_FILE_TYPE` | 415   | `UnsupportedFileTypeError` — uploaded filename's extension isn't in the configured `J1_ALLOWED_UPLOAD_EXTENSIONS` allow-list. Response `details` carries `extension` (the offending suffix) and `allowedExtensions` (the current allow-list). |
| `APPLICATION_ERROR`   | 400     | Temporal `ApplicationError`                 |
| `J1_ERROR`            | 400     | Any other `J1Error` subclass                |
| `RESUME_INCOMPATIBLE` | 412     | `ingestion_review.ResumeIncompatible` — raised by `POST /ingestion-runs/{id}/resume-from-checkpoint` when the prior run's settings hash doesn't match the candidate's. Response `details` carries `diff` (`{field: {"before": x, "after": y}}`) so the client can render exactly which settings drifted and prompt the operator to full-reindex instead. |

---

## 4. Endpoints

All payloads are JSON with **camelCase** keys. All requests scoped via the
context-resolver headers.

### Projects

| Method | Path         | Notes |
|--------|--------------|-------|
| `POST` | `/projects`  | Body: `{projectId, profile?}`. Provisions the workspace under the resolved tenant. Idempotent. |

### Documents

| Method | Path                                  | Notes |
|--------|---------------------------------------|-------|
| `POST` | `/documents`                          | Multipart upload (`file=`, optional `actor`, `correlationId`). Returns `DocumentRecord`; duplicates return existing record with `meta.duplicate=true`. |
| `GET`  | `/documents/{documentId}`             | Returns `DocumentRecord`. |
| `POST` | `/documents/{documentId}/ingest`      | Body: `IngestRequest`. Triggers the per-document `job_starter` callable; returns `{jobId, documentId, status}`. |
| `GET`  | `/documents/{documentId}/status`      | Returns `{documentId, status}`. |

### Ingestion jobs

| Method | Path                                  | Notes |
|--------|---------------------------------------|-------|
| `POST` | `/ingestion-jobs`                     | Body: `ProjectIngestionRequest`. Starts a project-wide `ProjectProcessingWorkflow`. Returns `{jobId, action: "start"}`. |
| `GET`  | `/ingestion-jobs/{jobId}`             | Returns full `JobStatusRecord` (state, current operation, totals, gates, error). |
| `GET`  | `/ingestion-jobs/{jobId}/events`      | Reads `audit/events.jsonl` filtered by `correlationId == jobId`. |
| `POST` | `/ingestion-jobs/{jobId}/pause`       | Sends a `pause` signal to the workflow. |
| `POST` | `/ingestion-jobs/{jobId}/resume`      | Sends a `resume` signal. |
| `POST` | `/ingestion-jobs/{jobId}/cancel`      | Sends a `cancel` signal. |

### Artifacts

| Method | Path                              | Notes |
|--------|-----------------------------------|-------|
| `GET`  | `/artifacts?kind=...`             | Returns the project's `ArtifactRecord` list, optionally filtered by `kind`. Locations are workspace-relative — never absolute. |
| `GET`  | `/artifacts/{artifactId}`         | Returns one `ArtifactRecord`. |

### Search / retrieve / answer

These three endpoints intentionally have **distinct** shapes — do not collapse
them into a generic `/query`.

| Method | Path        | Returns | Use when |
|--------|-------------|---------|----------|
| `POST` | `/search`   | Ranked `SearchHitRecord[]` (`artifactId`, `score`, citation fields). | You want hit metadata for UI lists. |
| `POST` | `/retrieve` | `ContextBlockRecord[]` (`text` body + `citation`). | You're grounding an external LLM. |
| `POST` | `/answer`   | `AnswerRecord` (`answer`, `mode`, `citations[]`, `graphPaths[]`, `confidence`, warnings). | You want J1 to answer directly. |
| `POST` | `/answer?stream=true` | SSE stream (`text/event-stream`) — see [§ 8 Streaming](#8-streaming-answer-output). | You want incremental output. |

`/answer` accepts an explicit `mode` (`AUTO` / `KNOWLEDGE_FIRST` /
`GRAPH_FIRST` / `EVIDENCE_FIRST` / `CONSISTENCY_CHECK` / `REPORT_GENERATION`)
or omits it for auto-routing.

### Citations / sources

| Method | Path                       | Notes |
|--------|----------------------------|-------|
| `GET`  | `/citations/{citationId}`  | `citationId` is the underlying `artifactId`. Returns `CitationDetailRecord`. |
| `GET`  | `/sources/{sourceId}`      | `sourceId` is a `documentId`. Returns full `SourceDetailRecord`. |

### Cost

| Method | Path  | Notes |
|--------|-------|-------|
| `GET`  | `/cost?correlationId=&documentId=&queryId=` | Aggregates spend across the project's cost log. All filters optional. Returns `CostSummaryRecord` with `totalAmount` and `byLevel`. |

### Reviews

| Method | Path                                | Notes |
|--------|-------------------------------------|-------|
| `GET`  | `/reviews?pendingOnly=true`         | Lists items in the human-review queue. |
| `POST` | `/reviews/{reviewId}/decision`      | Body: `{decision, actor, notes?, correlationId?}`. Applies a decision and writes an audit event. |

### Feedback

| Method | Path        | Notes |
|--------|-------------|-------|
| `POST` | `/feedback` | Body: `FeedbackRequest` (`targetKind`, `targetId`, `rating` ∈ {-1, 0, 1}, optional `comment`). Returns `{feedbackId, submittedAt}`. |

### Bulk import / export

NDJSON streams of documents, sources, chunks (artifacts), citations,
metadata, and feedback. See [bulk.md](bulk.md) for full record schemas,
idempotency rules, partial-failure response shape, and the
recommended backup/restore flow.

| Method | Path                                          | Required scope    |
|--------|-----------------------------------------------|-------------------|
| `GET`  | `/exports/{documents\|sources\|chunks\|citations\|metadata}.ndjson` | `kb:read` |
| `GET`  | `/exports/feedback.ndjson`                    | `kb:audit.read`   |
| `POST` | `/imports/{documents\|sources\|metadata}.ndjson` | `kb:ingest`    |

### System

| Method | Path             | Notes |
|--------|------------------|-------|
| `GET`  | `/health`        | `{status: "ok"}` |
| `GET`  | `/version`       | `{version}` from constructor |
| `GET`  | `/capabilities`  | Lists which optional endpoints are wired |

---

## 5. OpenAPI

FastAPI auto-generates OpenAPI 3.1 from the Pydantic schemas. The schema is
served at `/openapi.json` and the interactive docs at `/docs` and `/redoc`.

The grouping is driven by the `tags` list passed in the constructor; tag
descriptions are documented in `app.py`.

---

## 6. Examples

### Upload a document

```bash
curl -X POST http://localhost:8000/documents \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -F "file=@spec.pdf" -F "actor=alice"
```

```json
{
  "requestId": "ab12…",
  "data": {
    "documentId": "doc_01J…",
    "tenantId": "acme",
    "projectId": "alpha",
    "originalFilename": "spec.pdf",
    "checksum": "sha256:…",
    "status": "registered",
    "createdAt": "2026-05-03T08:30:21Z"
  }
}
```

### Generate an answer

```bash
curl -X POST http://localhost:8000/answer \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"question": "What deliverables are due?", "mode": "EVIDENCE_FIRST", "maxResults": 5}'
```

### Submit feedback

```bash
curl -X POST http://localhost:8000/feedback \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"targetKind": "answer", "targetId": "ans_01J…", "rating": 4, "comment": "useful but missed clause 4.2"}'
```

---

## 8. Streaming answer output

`POST /answer?stream=true` returns the same answer in incremental
Server-Sent Events form. **The non-streaming `POST /answer` shape is
unchanged** — clients that don't pass `?stream=true` get the existing
JSON envelope as before.

### Layering

Streaming follows the standard outer-layer rule: the core
[`AnswerService`](../src/j1/integration/services.py) is untouched. A
new transport-neutral
[`AnswerStreamingService`](../src/j1/integration/streaming/service.py)
wraps the existing answer port and emits `AnswerStreamEvent`s through a
handler callback. The REST adapter's
[`sse.py`](../src/j1/adapters/rest/sse.py) is the only place that
knows about `text/event-stream` framing.

### Same security as `POST /answer`

The streaming branch lives on the **same route** as non-streaming, so
every middleware and dependency runs identically:

- The `kb:answer` scope is required.
- Authentication, request validation (Pydantic `AnswerRequest`),
  tenant scoping, and the standard error envelope all apply.
- Errors *before* the stream is opened (401 / 403 / 422) come back as
  the normal JSON envelope.
- Errors *after* the stream is opened are emitted as a masked
  `answer.failed` event (see below) — the underlying exception is
  logged with the request id and never appears on the wire.

### Request

Same body as non-streaming `POST /answer`:

```http
POST /answer?stream=true HTTP/1.1
Authorization: Bearer <api-key>
X-Tenant-Id: acme
X-Project-Id: alpha
Content-Type: application/json

{ "question": "What deliverables are due?", "mode": "AUTO" }
```

### Response

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
X-Request-Id: 9f1c…
```

### Event types

| Event                 | Meaning                                                |
|-----------------------|--------------------------------------------------------|
| `answer.started`      | The stream is open; question + mode in `data`.         |
| `retrieval.started`   | Retrieval has begun.                                   |
| `retrieval.completed` | Retrieval finished; `data.sourceCount` is the citation count. |
| `generation.delta`    | A chunk of answer text in `data.text`. Repeated until the answer is fully sent. |
| `citation.added`      | One per source, `data.artifactId` + `data.sourceDocumentId`. |
| `answer.completed`    | Terminal-success — confidence, warnings, mode used.    |
| `answer.failed`       | Terminal-failure with masked payload (see below).      |

Emission order: `answer.started → retrieval.started → retrieval.completed
→ generation.delta+ → citation.added* → answer.completed`. On failure:
`answer.started → retrieval.started → answer.failed` (no further
events).

### Wire format

Each event is one SSE message:

```
event: <event-name>
data: <json-payload>

```

The JSON payload always carries the standard envelope:

```json
{ "requestId": "9f1c…", "event": "generation.delta", "data": {"text": "partial answer"} }
```

so consumers don't have to split `event:` from `data:` themselves.

### Example stream

```
event: answer.started
data: {"requestId":"9f1c…","event":"answer.started","data":{"question":"What deliverables are due?","mode":"AUTO","actor":"svc-power"}}

event: retrieval.started
data: {"requestId":"9f1c…","event":"retrieval.started","data":{"mode":"AUTO"}}

event: retrieval.completed
data: {"requestId":"9f1c…","event":"retrieval.completed","data":{"sourceCount":3,"modeUsed":"KNOWLEDGE_FIRST","relatedArtifactCount":0}}

event: generation.delta
data: {"requestId":"9f1c…","event":"generation.delta","data":{"text":"Three deliverables are due before"}}

event: generation.delta
data: {"requestId":"9f1c…","event":"generation.delta","data":{"text":"the end of the quarter."}}

event: citation.added
data: {"requestId":"9f1c…","event":"citation.added","data":{"artifactId":"a-1","artifactType":"compiled.text","sourceDocumentId":"doc-7","sourceLocation":null}}

event: answer.completed
data: {"requestId":"9f1c…","event":"answer.completed","data":{"modeUsed":"KNOWLEDGE_FIRST","citationCount":3,"confidence":0.81,"confidenceLevel":"high","reviewRequired":false,"warnings":[],"warningCategories":[]}}
```

### Error masking

If generation raises after the stream is open, exactly one terminal
event is emitted:

```json
{
  "requestId": "9f1c…",
  "event": "answer.failed",
  "data": {
    "code": "ANSWER_GENERATION_FAILED",
    "message": "Answer generation failed.",
    "retryable": true
  }
}
```

The wire body never contains the underlying exception type, message,
file path, or stack — that's logged server-side with the request id
for triage and never crosses the network.

### Client example (fetch + ReadableStream)

Native `EventSource` only supports `GET`, but the framework's auth
contract requires headers, so we use `fetch`:

```javascript
const res = await fetch("/answer?stream=true", {
  method: "POST",
  headers: {
    "Authorization": "Bearer kb_local_dev_001",
    "X-Tenant-Id": "acme",
    "X-Project-Id": "alpha",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ question: "What deliverables are due?" }),
});
if (!res.ok) throw new Error(`HTTP ${res.status}`);

const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const messages = buffer.split("\n\n");
  buffer = messages.pop();  // last item may be incomplete
  for (const block of messages) {
    if (!block.trim()) continue;
    let eventName = null, dataStr = null;
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) eventName = line.slice(7);
      else if (line.startsWith("data: ")) dataStr = line.slice(6);
    }
    const payload = JSON.parse(dataStr);
    handle(eventName, payload);
  }
}
```

### Cancellation / disconnect

The streaming response loop calls `request.is_disconnected()` between
events and stops emitting once the client goes away. **Limitation:**
the underlying `AnswerService.answer()` is currently synchronous and
not cancellable mid-flight, so a disconnect cancels further event
emission but does not abort an in-progress answer call. When a
token-streaming `ModelProvider` is wired in, the streaming service can
emit deltas as they arrive and propagate cancellation into the
provider — the SSE adapter contract stays the same.

---

## 9. Integration notes

- **Layering.** The adapter depends on `j1.integration.*` — never on
  `j1.processing.*` or `j1.orchestration.*` directly. New endpoints must reach
  back through a port.
- **Idempotency.** `POST /documents` is content-hash-deduplicated; it's safe
  to retry. `POST /projects` is idempotent (returns the existing project on a
  repeat call). `POST /documents/{id}/ingest` and `POST /ingestion-jobs` both
  return a fresh `jobId` each call — callers should track job IDs themselves.
- **Auth.** None ships with the adapter. Wrap the returned `FastAPI` instance
  with whatever middleware your deployment requires (OAuth2, mTLS, rate
  limiting), or supply a `context_resolver` that performs auth before mapping
  to a `ProjectContext`.
- **Rate limiting & observability.** Out of scope. Add via standard FastAPI
  middleware (`slowapi`, OpenTelemetry, etc.) on the returned app.
- **Tests.** [`tests/test_rest_adapter.py`](../tests/test_rest_adapter.py)
  covers every endpoint, the standard envelope, request ID echoing, custom
  context resolvers, and validation failures.
