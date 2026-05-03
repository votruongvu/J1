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

`/capabilities` advertises which endpoints the deployment has wired up.

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
| `APPLICATION_ERROR`   | 400     | Temporal `ApplicationError`                 |
| `J1_ERROR`            | 400     | Any other `J1Error` subclass                |

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

## 7. Integration notes

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
