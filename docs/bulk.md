# J1 Bulk Import / Export (NDJSON)

Bulk import and export are external-facing adapters built on top of the
existing per-record registries. The KB core's
[`SourceRegistry`](../src/j1/intake/registry.py),
[`ArtifactRegistry`](../src/j1/artifacts/registry.py), and
[`FeedbackStore`](../src/j1/integration/feedback.py) are reused
unchanged — the bulk layer only adds projection (export) and
validated dispatch (import).

---

## 1. Layering

```
┌────────────────────────────────────────────────────────────┐
│  HTTP                                                      │
│    GET  /exports/<file>.ndjson   (StreamingResponse)       │
│    POST /imports/<file>.ndjson   (envelope w/ counts)      │
└──────────────────────────┬─────────────────────────────────┘
                           │ same auth, scopes, tenant scope
                           ▼
┌────────────────────────────────────────────────────────────┐
│  j1.integration.bulk (transport-neutral)                   │
│    BulkExportService.export_*(ctx) → Iterator[bytes]       │
│    BulkImportService.import_*(ctx, lines) → BulkImportResult│
│    Pydantic schemas: DocumentExportRecord, etc.            │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│  Existing core                                             │
│    SourceRegistry, ArtifactRegistry, FeedbackStore         │
└────────────────────────────────────────────────────────────┘
```

The bulk layer never imports FastAPI. The REST adapter is the only
place that wraps the byte iterator into a `StreamingResponse` and
parses the request body as NDJSON lines.

---

## 2. Supported files

| File                  | Maps to                                          | Export | Import |
|-----------------------|--------------------------------------------------|:------:|:------:|
| `documents.ndjson`    | `DocumentRecord` from `SourceRegistry`           |   ✅   |   ✅   |
| `sources.ndjson`      | Alias of `documents.ndjson` (sources == documents) |  ✅   |   ✅   |
| `chunks.ndjson`       | `ArtifactRecord` from `ArtifactRegistry`         |   ✅   |   ❌   |
| `citations.ndjson`    | Derived from artifact lineage                    |   ✅   |   ❌   |
| `metadata.ndjson`     | Denormalised projection of `DocumentRecord`      |   ✅   |   ✅¹ |
| `feedback.ndjson`     | `FeedbackRecord` from `FeedbackStore`            |   ✅   |   ❌   |

¹ `POST /imports/metadata.ndjson` is a **round-trip integrity check**,
not a writer. Each row must reference an existing document and its
declared fields must equal the registry's stored values; mismatches
are reported as `INTEGRITY_MISMATCH` failures. Useful for validating a
backup before promoting it.

Imports for `chunks`, `citations`, and `feedback` are intentionally
omitted: chunks/citations are produced by the pipeline (not authored)
and feedback is append-only audit data.

---

## 3. Endpoints

All endpoints are tenant + project scoped via the standard
`X-Tenant-Id` / `X-Project-Id` headers (or your custom
`context_resolver`). Authentication is the same as every other
endpoint — see [security.md](security.md).

### Export (returns `application/x-ndjson`)

| Method | Path                            | Required scope    |
|--------|---------------------------------|-------------------|
| `GET`  | `/exports/documents.ndjson`     | `kb:read`         |
| `GET`  | `/exports/sources.ndjson`       | `kb:read`         |
| `GET`  | `/exports/chunks.ndjson`        | `kb:read`         |
| `GET`  | `/exports/citations.ndjson`     | `kb:read`         |
| `GET`  | `/exports/metadata.ndjson`      | `kb:read`         |
| `GET`  | `/exports/feedback.ndjson`      | `kb:audit.read`   |

Responses set `Content-Disposition: attachment; filename="..."` so
browser-downloaded files land with the right name.

### Import (returns the standard `{requestId, data}` envelope)

| Method | Path                          | Required scope | Body |
|--------|-------------------------------|----------------|------|
| `POST` | `/imports/documents.ndjson`   | `kb:ingest`    | NDJSON of `DocumentExportRecord` rows |
| `POST` | `/imports/sources.ndjson`     | `kb:ingest`    | NDJSON of `SourceExportRecord` rows (same shape as documents) |
| `POST` | `/imports/metadata.ndjson`    | `kb:ingest`    | NDJSON of `MetadataExportRecord` rows (verify-only, no writes) |

---

## 4. Record shapes

### `DocumentExportRecord` / `SourceExportRecord`

```json
{
  "documentId":       "doc-01J…",
  "tenantId":         "acme",
  "projectId":        "alpha",
  "originalFilename": "spec.pdf",
  "storedFilename":   "doc-01J….pdf",
  "mimeType":         "application/pdf",
  "fileSize":         52431,
  "checksum":         "sha256:9f1c…",
  "status":           "pending",
  "createdAt":        "2026-05-03T08:30:21+00:00"
}
```

### `ArtifactExportRecord` (`chunks.ndjson`)

```json
{
  "artifactId":         "art-01J…",
  "tenantId":           "acme",
  "projectId":          "alpha",
  "kind":               "compiled.text",
  "location":           "compiled/art-01J….txt",
  "contentHash":        "sha256:…",
  "byteSize":           4096,
  "status":             "succeeded",
  "reviewStatus":       "not_required",
  "version":            1,
  "createdAt":          "2026-05-03T08:30:21+00:00",
  "updatedAt":          "2026-05-03T08:30:25+00:00",
  "sourceDocumentIds":  ["doc-01J…"],
  "sourceArtifactIds":  [],
  "metadata":           { "sourceLocation": "page 4" }
}
```

### `CitationExportRecord` (`citations.ndjson`)

One row per `(artifact, source_document_id)` pair:

```json
{
  "artifactId":       "art-01J…",
  "artifactType":     "compiled.text",
  "sourceDocumentId": "doc-01J…",
  "sourceLocation":   "page 4"
}
```

### `MetadataExportRecord` (`metadata.ndjson`)

Analytics-friendly denormalised projection of document fields:

```json
{
  "documentId":       "doc-01J…",
  "tenantId":         "acme",
  "projectId":        "alpha",
  "originalFilename": "spec.pdf",
  "mimeType":         "application/pdf",
  "fileSize":         52431,
  "checksum":         "sha256:9f1c…",
  "status":           "pending",
  "createdAt":        "2026-05-03T08:30:21+00:00"
}
```

### `FeedbackExportRecord` (`feedback.ndjson`)

```json
{
  "feedbackId":      "fb-01J…",
  "tenantId":        "acme",
  "projectId":       "alpha",
  "targetKind":      "answer",
  "targetId":        "ans-01J…",
  "submittedAt":     "2026-05-03T08:32:00+00:00",
  "rating":          1,
  "comment":         "useful",
  "actor":           "alice@example.com",
  "correlationId":   "9f1c…",
  "metadata":        {}
}
```

---

## 5. Idempotency

| File                | Idempotency key       | Behaviour |
|---------------------|-----------------------|-----------|
| `documents.ndjson`  | `checksum`            | Existing checksum → row counted in `skippedIdempotent`, not as a failure. |
| `sources.ndjson`    | `checksum` (alias)    | Same as documents. |
| `metadata.ndjson`   | `documentId`          | Verify-only. No writes ever happen. |

This means a freshly-exported file can always be re-imported into the
same project with `succeeded: 0` and all rows in
`skippedIdempotent` — the safest possible round-trip.

There is **no overwrite mode** in this revision. To intentionally
replace a document, delete it via the appropriate single-record
endpoint first, then re-import.

---

## 6. Validation + partial-failure report

Imports never abort midway. Each line is validated; success and
failure are aggregated into one response:

```json
{
  "requestId": "9f1c…",
  "data": {
    "succeeded":         3,
    "skippedIdempotent": 1,
    "failures": [
      {
        "lineNumber": 5,
        "recordId":   "doc-bad",
        "code":       "SCHEMA_VALIDATION_FAILED",
        "message":    "originalFilename: field required"
      },
      {
        "lineNumber": 8,
        "recordId":   null,
        "code":       "INVALID_JSON",
        "message":    "Expecting value: line 1 column 1 (char 0)"
      }
    ],
    "total": 5
  },
  "meta": {}
}
```

Failure codes:

| Code                       | Meaning |
|----------------------------|---------|
| `INVALID_JSON`             | Line wasn't parseable JSON. `recordId` is `null`. |
| `SCHEMA_VALIDATION_FAILED` | Pydantic rejected the row. |
| `PROJECT_MISMATCH`         | `tenantId`/`projectId` in the row doesn't match the request scope (a critical safety check — bulk uploads can't be used to cross tenant boundaries). |
| `DOCUMENT_NOT_FOUND`       | (metadata only) Row references an unknown `documentId`. |
| `INTEGRITY_MISMATCH`       | (metadata only) Stored values don't match the supplied projection. The `message` lists the differing field names. |

Line numbers are 1-based to match how operators read NDJSON files.

---

## 7. Examples

### Export to a file

```bash
curl -sf \
  -H "Authorization: Bearer $KB_TOKEN" \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  http://localhost:8000/exports/documents.ndjson \
  > documents.ndjson
```

### Restore into another instance

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $KB_TOKEN" \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @documents.ndjson \
  http://other.kb.local/imports/documents.ndjson
```

The response is the standard envelope with `succeeded`,
`skippedIdempotent`, `failures`, and `total`.

### Verify a backup

```bash
curl -sf -X POST \
  -H "Authorization: Bearer $KB_TOKEN" \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  --data-binary @metadata.ndjson \
  http://localhost:8000/imports/metadata.ndjson \
  | jq '.data.failures'
```

A clean response (`[]`) confirms every document mentioned in the
metadata file is in the registry with the same fields.

---

## 8. Recommended backup / restore flow

1. **Snapshot.** Stop ingestion (or accept the snapshot may be
   one-write-behind), then export every file you need:

   ```bash
   for f in documents sources chunks citations metadata feedback; do
     curl ... /exports/$f.ndjson > $f.ndjson
   done
   ```

2. **Verify.** Round-trip the metadata file against the same instance:

   ```bash
   curl -X POST ... /imports/metadata.ndjson < metadata.ndjson | jq '.data.failures'
   ```

   `failures: []` means the snapshot is internally consistent.

3. **Restore.** On the target instance, import documents first
   (chunks/citations are derived and will be re-built by the pipeline):

   ```bash
   curl -X POST ... /imports/documents.ndjson < documents.ndjson
   ```

4. **Re-run processing.** Trigger the project ingestion job
   (`POST /ingestion-jobs`) — chunks, citations, and the search index
   are rebuilt from the imported documents.

---

## 9. Wiring example

```python
from j1 import (
    ApplicationFacade, BulkExportService, BulkImportService,
    create_rest_api,
)

bulk_export = BulkExportService(
    sources=source_registry,
    artifacts=artifact_registry,
    feedback=feedback_store,
)
bulk_import = BulkImportService(sources=source_registry)

app = create_rest_api(
    facade,
    bulk_export=bulk_export,
    bulk_import=bulk_import,
)
```

When `bulk_export=None` or `bulk_import=None`, the corresponding
endpoints return `503` and `/capabilities` advertises them as
unavailable.

---

## 10. Limitations

- **Synchronous endpoints.** Today's bulk import/export runs inline on
  the request thread. The framework's existing per-project
  `JsonSourceRegistry` is single-writer and adequate for this scale,
  but if you need to import millions of rows: the same
  `BulkImportService` can be called from a background worker (e.g. a
  Temporal activity) — the integration layer makes no transport
  assumptions.
- **No overwrite mode.** Re-importing an existing document is always a
  no-op skip. Replacing requires deleting the existing record via
  single-record APIs first.
- **No CSV / Markdown formats.** NDJSON is the only transport. CSV
  metadata + Markdown summaries are easy follow-ups: write a thin
  formatter in `j1.adapters.rest` (or a future CLI) that consumes the
  same `BulkExportService` byte iterator. The integration layer doesn't
  need to change.
