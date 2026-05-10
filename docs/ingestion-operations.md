# J1 Ingestion Operations Guide

> Companion to [`ingestion-stability-audit.md`](./ingestion-stability-audit.md).
> Audience: operators + the next implementation iteration.

This document specifies the operational model for J1 ingestion runs:
**resume**, **rebuild index**, **full re-index**, **delete**, and **multi-upload
batch** behaviour. Some endpoints are documented BEFORE they are
implemented so the contract doesn't drift.

| Status legend | Meaning |
|---|---|
| ✓ implemented | Live in this commit; tests in `tests/`. |
| ⏳ designed | Contract specified here; impl deferred to a follow-up iteration. |
| 🚧 partial | Some pieces live, others deferred. See per-action notes. |

---

## 1. Stage-oriented Temporal design

J1 owns the ingestion plan; adapters execute capabilities. Every backbone stage is its own Temporal Activity. A stage is `succeeded` only when:

1. The activity returned without raising.
2. Required outputs were persisted via `_register_draft` (atomic SQLite write).
3. The output is readable through the same `_resolve_run_artifacts` path the FE uses.
4. Stage-level validation (where applicable) passes.
5. The `StepResult` is recorded and visible via `get_status` query AND persisted to `IngestionRunStore.metadata.step_results`.

**Activities, in order:**

```
validate_context
list_pending_documents (bulk-job mode only)
profile_document (when planner_enabled=true)
build_planning_result (post-compile)
compile (mandatory backbone)
insert_content (split mode only — pure chunk generation)
enrich (per-artifact, when plan enables)
build_graph (when plan enables)
index (when indexer_kind set)
finalize
```

**Stage status machine** (`StepStatus` in `src/j1/processing/status.py`):

```
                    [recorded by workflow]
                            │
   ┌───────┬────────┬───────┴───────┬───────────┐
PENDING  RUNNING  COMPLETED      SKIPPED      FAILED
                            │
                            ▼
              [_validate_completion checks]
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
       (all valid)                   (rules violated)
            │                               │
       COMPLETED                      FAILED_FINAL
```

## 2. Stage checkpoint contract

Every stage records its outcome via `_record_step` ([`src/j1/orchestration/workflows/project_processing.py`](../src/j1/orchestration/workflows/project_processing.py)):

```python
self._record_step(
    step="graph",
    status=StepStatus.COMPLETED,
    required=True,
    source=StepSource.CALLER,           # or PLANNER / DEFAULT / POLICY
    artifact_count=len(graph_result.artifact_ids),
    metadata={"document_id": document_id},
)
```

The `step_results` list is:
- Returned in `ProjectProcessingResult.step_results`.
- Surfaced on `RunSummaryDTO.steps[]` via the REST API.
- Persisted to the `IngestionRun.metadata.step_results` JSON column at finalize, so failed runs still expose the per-stage history through `GET /ingestion-runs/{id}/summary`.

**Output validation rule** added in this commit (see audit § 7):

- Graph step COMPLETED ⇒ `graph_json` artifact MUST exist.
- `generate_knowledge_chunks` step COMPLETED (non-synthetic) ⇒ `chunk` artifact MUST exist.
- `index` step recorded when indexer_kind set + artifacts produced.

These rules fire in `_validate_completion()` before the COMPLETED transition. Violations cause `_BusinessRejection` → FAILED_FINAL.

## 3. Resume from last successful checkpoint  ⏳ designed

### Use case

The run failed at stage X. Operator fixed the underlying issue (LLM endpoint, model config, vector store outage). They want to continue from the last validated stage instead of re-uploading and paying the full re-parse cost.

### Endpoint

```
POST /ingestion-runs/{runId}/resume-from-checkpoint
```

**Request body** (optional):
```json
{
  "fromStage": "build_graph",       // optional override; defaults to first non-succeeded stage
  "actor": "ops@example.com"
}
```

**Response** (201):
```json
{
  "data": {
    "originalRunId": "<runId>",
    "resumeRunId": "<new-run-id>",
    "workflowId": "j1-{tenant}-{project}-{document_id}-resume-{timestamp}",
    "skippedStages": ["compile", "insert_content", "build_planning_result"],
    "fromStage": "build_graph"
  },
  "requestId": "..."
}
```

### Behaviour contract

1. Look up the original run's `IngestionRun` record. Reject with 404 if not found.
2. Reject with 409 if the original run is currently `RUNNING` (the workflow is still alive — the operator should `cancel` first or wait).
3. Snapshot the original run's `step_results` and `produced_artifact_ids` for the resume.
4. Run a **compatibility check**:
   - Selected `compiler_kind`, `enricher_kind`, `graph_builder_kind`, `indexer_kind` unchanged.
   - `IngestPlan.profile`, `mode`, `policy` unchanged.
   - Embedding model unchanged (compare `J1_EMBEDDING_MODEL`).
   - Chunking strategy version unchanged (placeholder field on `parsed_content_manifest`).
   - Compatibility failure ⇒ 409 + actionable message pointing at `full-reindex`.
5. Compute the resume point:
   - Default: first stage with status != `COMPLETED` AND != `SKIPPED`.
   - Override: `fromStage` from request body if supplied AND it's >= the default.
6. Start a NEW workflow with `workflow_id = j1-{tenant}-{project}-{document_id}-resume-{timestamp}`.
7. Pass the original `produced_artifact_ids` via `ProjectProcessingRequest.produced_artifact_ids` (already supported for continue-as-new). The workflow skips stages whose `step_results` came back COMPLETED.
8. Tag every new artifact with `metadata.resume_of=<originalRunId>` AND `metadata.attempt=2` (or higher).
9. Emit `resume.started` audit event on the new run, `resume.completed` on terminal.

### Restrictions

- Resume cannot reach back further than the FIRST FAILED stage. If the operator wants to redo earlier stages, that's `full-reindex`.
- A second resume attempt on the same original run creates `attempt=3`, etc. — no limit currently.

### Tests required

- `test_resume_creates_new_workflow_referencing_original`
- `test_resume_skips_already_completed_stages`
- `test_resume_rejects_when_compatibility_changed`
- `test_resume_rejects_when_original_still_running`

---

## 4. Rebuild index only  ⏳ designed

### Use case

Chunks already exist + are valid; only the retrieval index is stale (vector store cleared, embedding model upgrade, index corruption).

### Endpoint

```
POST /ingestion-runs/{runId}/rebuild-index
```

**Request body** (optional):
```json
{
  "actor": "ops@example.com"
}
```

### Behaviour contract

1. Reject with 404 if run not found.
2. Reject with 409 if run is currently RUNNING.
3. Reject with 409 if no `chunk` artifact exists for the run (nothing to index).
4. Start a workflow with `request.completed_operations` populated such that ONLY the `index` activity runs.
5. The `index` activity reads chunk artifacts directly from the registry (existing path).
6. New `retrieval_index_result` artifact is registered with `metadata.rebuild_of=<runId>` AND `metadata.original_chunks_count=N`.
7. New `validation_report` + `final_summary` artifacts replace the prior ones.
8. The original run's other artifacts (compile, plan, chunks, graph) are PRESERVED.
9. Emit `index.rebuilt` audit event.

### Tests required

- `test_rebuild_index_uses_existing_chunks`
- `test_rebuild_index_rejects_when_no_chunks`
- `test_rebuild_index_preserves_other_artifacts`

---

## 5. Full re-index  ⏳ designed

### Use case

Operator wants to re-process the document from the original file (chunking strategy changed, parser upgraded, profile changed materially, suspected bad data).

### Endpoint

```
POST /ingestion-runs/{runId}/full-reindex
```

**Request body**:
```json
{
  "compilerKind": "raganything",       // optional overrides
  "enricherKind": "...",
  "graphBuilderKind": "...",
  "indexerKind": "...",
  "purgePrevious": false,              // if true, soft-delete the previous run on success
  "actor": "ops@example.com"
}
```

### Behaviour contract

1. Look up the original `IngestionRun` + its `original_document` artifact (or fall back to the source document file in the `raw/` workspace area).
2. Start a NEW ingestion run (new `run_id`, new `workflow_id` derived as `j1-{tenant}-{project}-{document_id}-reindex-{timestamp}`).
3. The new run executes the FULL backbone — parse, plan, chunks, enrich, graph, index — exactly as a fresh upload.
4. Per-result-cache: bypass the `(document_hash, processor_kind, version, mode)` cache so we don't immediately cache-hit the prior run. Implementation: pass a `cache_bypass=True` flag through `CompileActivityInput`.
5. Tag every artifact with `metadata.reindex_of=<originalRunId>`.
6. The original run STAYS visible. UI shows the relationship via `IngestionRun.metadata.reindex_of`.
7. If `purgePrevious=True` AND the new run reaches COMPLETED, soft-delete the original run (see § 6).

### Tests required

- `test_full_reindex_creates_new_run_from_original_document`
- `test_full_reindex_bypasses_processing_cache`
- `test_full_reindex_purgePrevious_soft_deletes_original_on_success`

---

## 6. Delete ingest  ⏳ designed

### Use case

Operator wants to remove a document (and all its derived data) from the active knowledge base.

### Endpoint

```
DELETE /ingestion-runs/{runId}
```

**Query params**:
- `mode=soft` (default) — tombstone everything; reversible.
- `mode=hard` — physically delete; irreversible. Requires elevated scope (`kb:admin`).

### Behaviour contract

**Soft delete:**
1. Reject with 409 if run is currently RUNNING.
2. Mark `IngestionRun.status = "deleted"`. Keep the record + audit trail.
3. Mark every `ArtifactRecord` for this run with `metadata.deleted_at=<iso>` AND `metadata.deleted_by=<actor>`.
4. Update `_resolve_run_artifacts` to exclude `metadata.deleted_at` records UNLESS the caller passes `?includeDeleted=true`.
5. Notify the indexer to delete this run's chunk records from the FTS index.
6. Notify the graph store (LightRAG) to call `adelete_by_doc_id(doc_id)` for the affected document_id.
7. Persist a `deletion_report.json` artifact: what was tombstoned, what was deleted from external stores, any partial failures.
8. Emit `delete.started` and `delete.completed` audit events. On partial failure, `delete.failed`.
9. Idempotent — calling DELETE twice produces a no-op + the same response shape.

**Hard delete:**
1. Steps 1–6 from soft delete (with the addition of physically `unlink()`-ing artifact files from disk).
2. Remove the `ArtifactRecord` rows from the SQLite registry.
3. Remove the `IngestionRun` JSONL record (or rewrite the JSONL with the row dropped).
4. Persist `deletion_report.json` to a separate audit-log path (NOT under the deleted run's artifact area, since the run is gone).
5. Same audit events.

**Scope enforcement**: deletion is scoped by `(tenant_id, project_id, run_id)`. A delete in one project cannot affect another project even if they share a document checksum.

### Tests required

- `test_soft_delete_marks_artifacts_and_excludes_from_resolver`
- `test_soft_delete_removes_chunks_from_fts_index`
- `test_soft_delete_calls_lightrag_adelete_by_doc_id`
- `test_hard_delete_unlinks_artifact_files_and_drops_records`
- `test_delete_is_scoped_to_tenant_project`
- `test_delete_is_idempotent`
- `test_delete_rejects_running_run`
- `test_delete_persists_deletion_report`

---

## 7. Multi-upload batch  ⏳ designed

### Use case

Operator uploads up to 5 files at once. UI shows per-file progress. Backend processes them sequentially.

### Endpoint

```
POST /ingestion-batches
```

**Request body** (multipart):
- One or more `file` parts.
- Optional JSON metadata block: `{"compilerKind": "...", "enricherKind": "...", ...}`.

### Constraints

- **`J1_INGESTION_BATCH_MAX_FILES`** — default `5`, env-overridable.
- **`J1_INGESTION_BATCH_CONCURRENCY`** — default `1` (sequential). MUST stay at 1 for the initial release.

### Behaviour contract

1. Validate file count against `J1_INGESTION_BATCH_MAX_FILES`. Reject with 400 if exceeded.
2. Generate a `batch_run_id` (UUID).
3. For each file: register the document via `DocumentIntakeService.register_*`. Each gets its own `document_id` + `run_id`.
4. Persist a `BatchRunRecord` to `audit/batches.jsonl`: `(batch_run_id, tenant_id, project_id, run_ids[], file_count, status="created", started_at)`.
5. Start ONE parent workflow `BatchProcessingWorkflow` with `batch_run_id` + `child_run_ids`.
6. The parent workflow iterates `child_run_ids` SEQUENTIALLY, starting each child workflow via `execute_child_workflow` and waiting for it to terminate before starting the next.
7. After each child completes, update the `BatchRunRecord` with that child's terminal status.
8. Batch terminal status:
    - `completed`: all children COMPLETED.
    - `completed_with_warnings`: all children COMPLETED, ≥1 had warning_count > 0.
    - `partially_failed`: mix of COMPLETED + FAILED.
    - `failed`: all children FAILED OR batch setup failed.

### Read endpoints

```
GET /ingestion-batches/{batchId}
GET /ingestion-batches/{batchId}/runs
```

`/ingestion-batches/{batchId}` returns:
```json
{
  "data": {
    "batchRunId": "...",
    "status": "running",
    "startedAt": "...",
    "completedAt": null,
    "fileCount": 5,
    "completedCount": 2,
    "failedCount": 0,
    "currentRunId": "<the one currently running>",
    "runs": [
      {"runId": "r1", "documentId": "d1", "filename": "a.pdf", "status": "completed"},
      {"runId": "r2", "documentId": "d2", "filename": "b.pdf", "status": "completed"},
      {"runId": "r3", "documentId": "d3", "filename": "c.pdf", "status": "running", "currentStage": "build_graph"},
      {"runId": "r4", "documentId": "d4", "filename": "d.pdf", "status": "pending"},
      {"runId": "r5", "documentId": "d5", "filename": "e.pdf", "status": "pending"}
    ]
  }
}
```

### Per-file actions

The standard run-detail actions (resume / rebuild / full-reindex / delete) work on each child run individually. Batch-level actions (resume entire batch, etc.) are deferred to a later iteration.

### Tests required

- `test_batch_upload_creates_batch_record_and_per_file_runs`
- `test_batch_upload_rejects_more_than_max_files`
- `test_batch_workflow_processes_children_sequentially_under_concurrency_one`
- `test_batch_status_completed_when_all_children_completed`
- `test_batch_status_partially_failed_when_mixed`
- `test_batch_failed_child_does_not_erase_completed_child_outputs`

---

## 8. Logs + events for one-run debugging

Every major log line and audit event includes:
- `run_id`
- `batch_run_id` (when applicable)
- `document_id`
- `tenant_id`, `project_id`, `workspace_id` (where applicable)
- `stage`, `attempt`
- `mode`, `profile`
- `artifact_type` (when an artifact write/read event)
- `storage_key` (when an artifact write/read event)

Audit-event taxonomy (`src/j1/runs/reporter.py`):
- `run.created`, `run.completed`, `run.completed_with_warnings`, `run.partially_failed`, `run.failed`, `run.cancelled`
- `stage.started`, `stage.completed`, `stage.failed`, `stage.skipped`
- `step.started`, `step.completed`, `step.failed`, `step.skipped`, `step.progress`, `step.warning`
- `artifact.created` (planned)
- `plan.generated`, `plan.revised`, `plan.confirmed`
- `assessment.started`, `assessment.completed`
- `validation.started`, `validation.completed`, `validation.failed` (planned)

To debug a single run by id:

```bash
# All audit events for the run
grep '"correlation_id": "<runId>"' /var/lib/j1/tenants/<tenant>/projects/<project>/audit/audit.jsonl

# All worker log lines for the run (correlation_id propagated through the workflow)
docker compose -f deploy/dev/docker-compose.yml logs worker | grep '<runId>'

# All API log lines
docker compose -f deploy/dev/docker-compose.yml logs api | grep '<runId>'

# Run summary (status, step_results, available_views)
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<runId>/summary"

# All artifacts for the run
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<runId>/artifacts?pageSize=200"
```

## 9. Manual verification commands

See [`ingestion-stability-audit.md`](./ingestion-stability-audit.md) § 8 for the complete list. Quick reference:

```bash
# Backend tests
python -m pytest tests/ -q --ignore=tests/test_e2e_processing_flow.py

# Targeted: stage validation + error_report
python -m pytest tests/test_project_processing_workflow.py tests/test_split_pipeline_workflow.py -v

# FE tests + build
cd frontend && npm run build && npx vitest run

# Smoke test (single doc, chunks_only)
docker compose -f deploy/dev/docker-compose.yml up -d
# upload a small text file via the FE; observe Knowledge Chunks tab populates.

# Smoke test (graph-required)
# upload with graphBuilderKind=raganything via REST; observe Knowledge Graph tab populates.

# Inspect a failed run's error_report
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<runId>/artifacts?kind=error_report"
```

## 10. Known limitations + remaining risks

- **Resume / rebuild-index / full-reindex / delete / batch endpoints are designed but not yet implemented.** Operators currently work around resume by re-uploading the original file (cache-hit gives equivalent behaviour for stages whose inputs are unchanged).
- **Stage-as-Activity, not stage-as-Child-Workflow.** Promoting stages to child workflows would buy stronger compensation semantics but isn't required for visibility/validation today. Tracked for a future iteration.
- **`J1_INGESTION_BATCH_CONCURRENCY` is documented as `1` for the initial release.** When the batch model lands, increasing concurrency requires re-validating the LightRAG dedupe behaviour under parallel inserts.
- **Hard delete is irreversible**; soft delete is the safer default and what the FE will surface first.
- **Audit-log paths are JSONL append-only.** Hard-deletes for compliance scenarios require a separate compaction tool; not in scope here.
