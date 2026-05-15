# 04. Core Data Model

> Audience: engineers + integrators.
> [Back to README](../README.md). See also
> [01-overall-architecture.md](01-overall-architecture.md),
> [08-multi-kb-model.md](08-multi-kb-model.md).

## The durable nouns

```
Tenant ─┐
        │
        ▼
      Project ─┬─▶ Document ──▶ DocumentVersion (per uploaded bytes)
               │       │
               │       ├──▶ IngestionRun (per processing attempt)
               │       │       └─▶ Artifact (per produced output)
               │       │
               │       └──▶ DocumentSnapshot (per knowledge state)
               │               ├─ state: BUILDING | READY | SUPERSEDED | FAILED
               │               └─ created_by_run_id ──┐
               │                                       ▼
               │                                   IngestionRun
               │
               └─▶ Domain pack (selected per request / per project)
                   ProfileLoader (per request)
                   Knowledge Base (union of attached documents)
```

## Tenant

The isolation boundary. Every store, registry, audit log, and
workspace path is keyed by `tenant_id`. The REST layer requires the
`X-Tenant-Id` header on every request, and cross-tenant access fails
with the same 404 a missing resource would produce — existence is
not probeable.

Practical implication: nothing in the codebase has a "global"
view. All services are constructed with a workspace resolver that
binds to `(tenant, project)` at call time.

## Project

The operator-facing workspace inside a tenant. Holds:

- The document registry (`documents.json` per project).
- The artifact registry (`artifacts.jsonl`).
- The snapshot store (`document_snapshots.jsonl`).
- The run store (`ingestion_runs.jsonl`).
- The audit log (`audit.jsonl`).
- The validation surfaces' per-document stores (imported test cases).
- The RAGAnything workspace (a per-document, per-snapshot subtree).

A project's knowledge base is exactly "every document in this
project whose `knowledge_state == attached` and `active_snapshot_id`
is set". See [08-multi-kb-model.md](08-multi-kb-model.md).

## Document

A `DocumentRecord` represents an uploaded file. The shape lives in
`src/j1/documents/models.py`. Key fields:

| Field | Purpose |
| --- | --- |
| `document_id` | Opaque uuid. The durable handle every other table FKs against. |
| `original_filename` / `stored_filename` | Filenames as uploaded vs as written to the workspace. |
| `mime_type`, `file_size`, `checksum` | Upload metadata. |
| `status` | `ProcessingStatus` of the most recent ingestion outcome. |
| `created_at`, `updated_at` | Timestamps. |
| `knowledge_state` | `attached` / `detached` / `removed`. The operator gate. |
| `active_snapshot_id` | The current visibility key. **Only** field the query layer is allowed to consult. |
| `latest_version_id` | Pointer to the most recent `DocumentVersion`. |
| `removed_at` | Set when `knowledge_state == removed`. |
| `lifecycle_status` | Operational sub-state: `stable` / `removing` / `removed` / `cleanup_failed` / `failed`. |
| `pending_operation`, `pending_operation_run_id`, `pending_operation_started_at` | Per-document mutating-operation lock (re-index / detach / remove). CAS-acquired by the dispatcher. |

There is no `active_run_id` field. Re-introducing one is a bug.

## DocumentVersion

A `DocumentVersion` is one stored version of a document's file
content. Identity is content-based: two uploads with the same
`file_hash` under the same `document_id` resolve to the same
version. This is what makes "re-upload the same file" idempotent.

## IngestionRun

`IngestionRun` (in `src/j1/runs/models.py`) is **one processing
attempt**. It is *not* a source of truth for visibility.

Important fields:

| Field | Purpose |
| --- | --- |
| `run_id` | Opaque uuid. |
| `document_id` | Owning document. |
| `workflow_id`, `workflow_run_id` | Temporal handles. |
| `status` | `RunStatus` (CREATED / RUNNING / SUCCEEDED / FAILED / …). |
| `started_at`, `updated_at`, `completed_at` | Timestamps. |
| `run_type` | `initial` / `reindex` / `resume` / `retry` / `refresh_enrich` / `run_domain_enrichment`. |
| `parent_run_id` | Lineage for reindex / refresh. |
| `document_version_id` | Pointer to the version this run consumed. |
| `display_version` | Operator-facing `DDMMYYYY-NN` label. |
| `target_snapshot_id` | The candidate `DocumentSnapshot` this run is building. **Allocated up-front by the dispatch layer.** |
| `superseded_at` | Set when another run's snapshot promotes past this one. |
| `cleanup_status` | `live` / `superseded` / `removing` / `cleaned` / `cleanup_failed`. |

### Why `IngestionRun` is not the source of truth

Older revisions stored `active_run_id` on the document and treated
"the most recent successful run" as the visibility key. That made
re-ingest partially clobber live state, and it conflated trace
metadata (which attempt ran) with operator decisions (what users
should see). The current model promotes a **snapshot** atomically;
the run is just the trace.

## DocumentSnapshot

The versioned knowledge state. `j1.documents.snapshot` /
`j1.documents.snapshot_service` own the model. Lifecycle:

```
   create_candidate
        │
        ▼
   BUILDING ───── compile + enrich succeed ─────▶ READY
        │                                          │
        │                                          │
        │                                          ▼
        ▼                                       promoted
     FAILED                                  (active for the
                                              document)
                                                    │
                                          another snapshot
                                          promotes past it
                                                    │
                                                    ▼
                                                SUPERSEDED
```

Each snapshot record carries:

- `snapshot_id`, `document_id`, `tenant_id`, `project_id`.
- `created_by_run_id` — the run that built it (for diagnostics).
- `state` (`BUILDING` / `READY` / `SUPERSEDED` / `FAILED`).
- `created_at`, `promoted_at`, `superseded_at`.
- `index_refs` — the indexes that exist for this snapshot.
- `summary` — small per-snapshot blob (parse outcome, warning count).

**Promotion is CAS-guarded** against the document's current
`active_snapshot_id`. If a concurrent reindex promoted first, the
losing CAS fails and the candidate is cleaned up. Without CAS we
would silently overwrite the winner or re-promote onto a removed
document.

### How re-index creates new knowledge

1. A reindex creates a fresh `IngestionRun` with
   `target_snapshot_id` set to a freshly-allocated candidate
   snapshot.
2. The workflow builds the candidate.
3. On terminal success, the runs activity calls
   `DocumentSnapshotService.promote(previous=<doc.active_snapshot_id>)`.
4. The document's `active_snapshot_id` flips to the new value.
5. The previous snapshot's artifacts are stamped
   `search_state=superseded` so retrieval ignores them.

### Stale runs do not pollute active knowledge

- Queries read `document.active_snapshot_id`. A run that failed or
  ran concurrently does not change that field.
- The eligibility resolver refuses to admit any document whose
  `lifecycle_status` is `removing` / `removed` / `failed` /
  `cleanup_failed`.
- Artifacts whose `metadata.snapshot_id` doesn't match the
  document's active snapshot are excluded from retrieval.

## Profile

A profile is the runtime configuration bundle that the API uses
when handling a request. It is loaded via `ProfileLoader` and
typically picked by the calling tenant / project (the dev default
is `DEFAULT_PROFILE_ID`). A profile carries:

- LLM role bindings (which client backs FAST / TEXT / VLM).
- Query engine knob defaults (top-k caps, evidence budget).
- Retrieval policy switches.
- Domain pack selection defaults.

Profiles are runtime-only — they are not stored per-document. The
document's data on disk is independent of the profile that
ingested or queried it.

## Knowledge Base

The Knowledge Base is the *queryable scope*. For a given project,
the KB is computed at query time:

```
KB(project) = { doc for doc in project.documents
                if doc.knowledge_state == 'attached'
                   and doc.active_snapshot_id is not None
                   and doc.lifecycle_status == 'stable' }
```

Attach/detach toggles whether a document participates in the KB.
Remove takes the document out permanently. Re-index swaps the
`active_snapshot_id` atomically — the KB membership doesn't churn.

See [08-multi-kb-model.md](08-multi-kb-model.md) for how the same
project can host multiple knowledge surfaces by profile.

## Artifact

Anything compile or enrichment produces:

- Chunks (the dominant artifact kind).
- Graph JSON.
- Enriched tables / images / metadata.
- Final summary + final ingestion report.

Every artifact carries `metadata.snapshot_id` (and `metadata.run_id`
for trace). The retrieval layer filters by snapshot_id; run_id is
diagnostic only.

## Display-version naming rule

Operator-facing version labels are `DDMMYYYY-NN`: the calendar day
the run started (UTC) plus a one-based ordinal within that day for
that document. `allocate_display_version` in `src/j1/runs/models.py`
owns the rule. The FE renders the label as the version chip on the
run-history table.

This is intentionally human-friendly. UI surfaces should prefer the
display version; system surfaces use `run_id` and `snapshot_id`.
