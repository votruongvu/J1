# J1 Ingestion Operations Guide

> Companion to [`ingestion-stability-audit.md`](./ingestion-stability-audit.md).
> Audience: operators + the next implementation iteration.

This document specifies the operational model for J1 ingestion runs:
**resume**, **rebuild index**, **full re-index**, **delete**, and **multi-upload
batch** behaviour. Some endpoints are documented BEFORE they are
implemented so the contract doesn't drift.

| Status legend | Meaning |
|---|---|
| ✓ implemented | Live; tests in `tests/`. |
| 🚧 partial | Some pieces live, others deferred. See per-action notes. |
| ⏳ designed | Contract specified here; impl deferred to a follow-up iteration. |

**Action implementation status (current):**

| Action | Status |
|---|---|
| Cancel running ingest | ✓ |
| Soft-delete ingest | ✓ |
| Full re-index | ✓ |
| Multi-upload batch + status view | ✓ |
| Resume from last checkpoint | ✓ (skips enrich + graph; compile + chunks always re-run) |
| Rebuild index only | ✓ (re-runs index activity against carry-forward chunks) |
| Hard delete (purge) | ✓ (two-step ritual: soft-delete then purge; cascades to validation; audit log preserved) |
| Parent-workflow batch sequencing | ⏳ (workaround: set `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1`) |

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

## 3. Resume from last successful checkpoint  ✓

### Use case

The run failed at stage X. Operator fixed the underlying issue (LLM endpoint, model config, vector store outage). They want to retry without paying the LLM-cost stages that already succeeded.

### Endpoint

```
POST /ingestion-runs/{runId}/resume-from-checkpoint
```

No request body — the endpoint inherits processor selections from the deployment's `processing_capabilities` and validates compatibility against the prior run's snapshot.

**Response** (200):
```json
{
  "data": {
    "originalRunId": "<runId>",
    "resumeRunId": "<new-run-id>",
    "workflowId": "j1-{tenant}-{project}-{document_id}-resume-{new-run-id}",
    "documentId": "<document_id>",
    "status": "created",
    "resumedSteps": ["enrich"],
    "carryForwardArtifactCount": 5
  }
}
```

### What gets skipped

Only the LLM-cost stages that COMPLETED in the prior run, intersected with `RESUMABLE_STAGES` (currently `{"enrich", "graph"}`). Compile + chunk-generation always re-run because their outputs are the structural backbone every downstream stage reads. The carry-forward artifact IDs are seeded on the new workflow's `_produced_artifact_ids` so downstream stages (index, validation_report) see the full artifact set the prior run produced.

### Compatibility check

Settings hash is SHA256 over `RESUME_SETTINGS_FIELDS` ([`src/j1/runs/resume.py`](../src/j1/runs/resume.py)):

- `compiler_kind`, `enricher_kind`, `graph_builder_kind`, `indexer_kind`
- `planner_enabled`, `policy`, `pipeline_mode`
- `domain_override`, `workspace_default_domain`
- `failure_policy`

If any field drifted between the prior snapshot and the candidate request, the endpoint returns **412** with a structured `details.diff` (`{field: {"before": x, "after": y}}`) so the FE can render exactly what changed and prompt the operator to full-reindex instead.

### Snapshot persistence

The workflow's `_emit_run_terminal` builds a `resume_snapshot` dict at every SUCCEEDED / SUCCEEDED_WITH_WARNINGS / FAILED / TIMED_OUT transition and persists it to `IngestionRun.metadata["resume_snapshot"]` via `RunsActivities._persist_run_terminal`. Cancelled runs do not snapshot — operator-initiated cancellation isn't a useful resume point.

### Error responses

| Status | When |
|---|---|
| 404 | Original run / document not found |
| 409 | Original run still RUNNING / PAUSED / CANCELLING / ASSESSING — cancel first |
| 412 | No `resume_snapshot` (legacy run, cancelled run) — full-reindex instead |
| 412 | `RESUME_INCOMPATIBLE` with `details.diff` — settings drifted |

### Tests

- `tests/test_runs_resume.py` — settings hash, diff, snapshot construction
- `tests/test_ingestion_review_service.py::test_resume_*` — service-layer validation
- `tests/test_project_processing_workflow.py::test_resume_skips_enrich_and_graph_when_listed_in_resume_context` — workflow short-circuit
- `tests/test_rest_resume_from_checkpoint.py` — end-to-end REST contract

### Deferred (follow-up iterations)

- Skip compile / chunk-generation: requires re-attaching the prior `parsed_source` artifact to the new run, plus careful handling of the chunk store. Today these always re-run, which is acceptable because they're cheap relative to enrich + graph.
- `fromStage` request override (let operator force re-run of a specific stage).

---

## 4. Rebuild index only  ✓

### Use case

Chunks already exist + are valid; only the retrieval index is stale (vector store cleared, embedding model upgrade, index corruption).

### Endpoint

```
POST /ingestion-runs/{runId}/rebuild-index
```

No request body — the endpoint inherits the indexer kind from the prior run's snapshot (falling back to the deployment default if the snapshot's indexer is no longer registered).

**Response** (200):
```json
{
  "data": {
    "originalRunId": "<runId>",
    "rebuildRunId": "<new-run-id>",
    "workflowId": "j1-{tenant}-{project}-{document_id}-rebuild-{new-run-id}",
    "documentId": "<document_id>",
    "status": "created",
    "carryForwardChunkCount": 12,
    "indexerKind": "sqlite_search"
  }
}
```

### Behaviour

1. Validate the prior run is terminal + non-deleted + has a `resume_snapshot`.
2. Filter `produced_artifact_ids` to the `chunk` kind only (other kinds aren't index inputs and could trip per-stage rules in the new run).
3. Reject with 412 if no chunks exist (nothing to index → `full-reindex` instead).
4. Allocate a new `run_id`, persist an `IngestionRun` with `metadata.rebuild_of=<originalRunId>` + the chunk-id list.
5. Dispatch a workflow with `rebuild_index_only=True` + the chunk ids on `resume_artifact_ids`. The workflow's main loop short-circuits past the documents loop, emits SKIPPED step records for compile / chunks / enrich / graph (with `reason="rebuild index only — chunks reused from {prior}"`), and runs ONLY the index activity.
6. The original run's artifacts are PRESERVED — the new run produces a fresh `retrieval_index_result` (and `validation_report` / `final_summary` per the standard terminal path) but doesn't touch upstream artifacts.
7. The standard terminal snapshot is captured on the new run, so it itself is resumable / re-rebuildable.

### Workflow ID

`j1-{tenant}-{project}-{document_id}-rebuild-{new_run_id}` — distinct from `-reindex-` and `-resume-` so operators can tell them apart in the Temporal UI. Prevents `USE_EXISTING` collision with the original.

### Error responses

| Status | When |
|---|---|
| 404 | Original run / document not found |
| 409 | Original run still RUNNING / PAUSED / CANCELLING / ASSESSING — cancel first |
| 412 | No `resume_snapshot` (legacy run, cancelled run) — full-reindex instead |
| 412 | Snapshot has no chunk artifacts — full-reindex instead |
| 412 | No `indexer_kind` available (prior snapshot null + no default registered) |

### Tests

- `tests/test_ingestion_review_service.py::test_rebuild_index_only_*` — service-layer validation
- `tests/test_project_processing_workflow.py::test_rebuild_index_only_*` — workflow short-circuit + indexer-required guard
- `tests/test_rest_resume_from_checkpoint.py::test_rebuild_index_endpoint_*` — end-to-end REST contract

---

## 5. Full re-index  ✓ implemented

Endpoint: `POST /ingestion-runs/{runId}/full-reindex` ([src/j1/adapters/rest/app.py](../src/j1/adapters/rest/app.py)).

**Behaviour landed:**

1. Looks up the original run + its `document_id`. 404 if not found, 409 if still active, 400 if no `document_id`.
2. Allocates a fresh `run_id` + persists a new `IngestionRun` with `metadata.reindex_of=<originalRunId>` (and inherited `policy` / `mode` / `document_name`).
3. Calls the existing per-document starter with `body.reindex_of=<runId>`. The starter constructs `workflow_id = j1-{tenant}-{project}-{document_id}-reindex-{run_id}` so the new attempt doesn't collide with the original under USE_EXISTING.
4. Returns `{originalRunId, reindexRunId, workflowId, documentId, status: "created"}`.
5. The original run is preserved unchanged. Operators delete it explicitly via DELETE when ready.

**Cache note**: the processing-result cache is keyed by `(document_hash, processor_kind, version, mode)`. A re-index of the same document with the same processor will cache-hit and produce identical artifacts under the new run_id. For an explicit cache-bypass (e.g., LLM output changed), wipe the LightRAG workdir or wait for the cache TTL — a `cache_bypass=True` flag is a follow-up.

### Tests
- Unit-tested via the REST adapter test (success + 404 + 409 + 400-no-document_id paths exercised).


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

## 6. Delete ingest  ✓ implemented (soft mode)

Endpoint: `DELETE /ingestion-runs/{runId}` ([src/j1/adapters/rest/app.py](../src/j1/adapters/rest/app.py)).
Service method: [`IngestionResultReviewService.delete_run`](../src/j1/ingestion_review/service.py).

**Behaviour landed (soft):**

1. 409 if the run is RUNNING / PAUSED / CANCELLING / ASSESSING.
2. 404 if the run doesn't exist.
3. Tombstones the `IngestionRun` record: status → `RunStatus.DELETED`, `metadata.deleted_at`, `metadata.deleted_by`.
4. Tombstones every `ArtifactRecord` belonging to the run via the new `ArtifactRegistry.update_metadata()` method: sets `metadata.deleted_at` + `metadata.deleted_by`.
5. The `_resolve_run_artifacts` resolver now excludes records carrying `metadata.deleted_at`, so subsequent FE reads return zero artifacts for the deleted run.
6. **Idempotent** — calling DELETE twice returns `wasAlreadyDeleted=true` on the second call with `tombstonedArtifactCount=0`.
7. Returns `{runId, status: "deleted", tombstonedArtifactCount, wasAlreadyDeleted, deletedAt}`.

### Soft-delete tests
- `tests/test_ingestion_review_service.py::test_delete_run_tombstones_run_and_artifacts`
- `tests/test_ingestion_review_service.py::test_delete_run_is_idempotent`
- `tests/test_ingestion_review_service.py::test_delete_run_rejects_active_run`
- `tests/test_ingestion_review_service.py::test_delete_run_404s_for_unknown_run`

### Hard delete (purge)  ✓

Endpoint: `POST /ingestion-runs/{runId}/purge` ([src/j1/adapters/rest/app.py](../src/j1/adapters/rest/app.py)).
Service method: [`IngestionResultReviewService.purge_run`](../src/j1/ingestion_review/service.py).

**Two-step ritual.** By default the endpoint refuses to operate on a run that hasn't been soft-deleted first (HTTP 409 — `RunNotTerminal`). Operators do `DELETE /ingestion-runs/{id}` (soft), confirm the run is gone from the FE list, then `POST /ingestion-runs/{id}/purge` (hard). Reduces blast radius of accidental clicks. Pass `?force=true` to bypass the gate for admin tooling.

**Behaviour:**

1. 404 if the run doesn't exist.
2. 409 if the run is in an active state (RUNNING / PAUSED / CANCELLING / ASSESSING).
3. 409 (`RunNotTerminal`) if the run isn't soft-deleted and `?force=true` wasn't passed.
4. Resolve every artifact tagged with this run_id — including soft-deleted records — via `_resolve_run_artifacts(include_deleted=True)`.
5. For each artifact: `Path.unlink(missing_ok=True)` the file, then remove the registry record via `ArtifactRegistry.delete_by_artifact_id`. File-first ordering means a crash mid-purge leaves orphaned files on disk that can be re-resolved by the registry record (still pointing at them); the reverse leaves orphaned files with no pointer.
6. Rewrite the `ingestion_runs.jsonl` minus every snapshot for this run_id (atomic via tmp-file + rename in `JsonlIngestionRunStore.purge`).
7. **Cascade** to validation: the REST orchestration calls `IngestionValidationService.purge_for_run`, which rewrites `validation_sets.jsonl` and `validation_runs.jsonl` minus snapshots referencing this run_id.
8. **Audit log untouched** — events stay on disk for compliance. The run record being gone is orthogonal to event history.
9. Returns `{runId, artifactsPurged, filesDeleted, filesMissing, snapshotsRemoved, validationSetsRemoved, validationRunsRemoved, purgedAt}`.

**Scope:** purge is `(tenant_id, project_id, run_id)`-scoped. A purge in one project cannot touch another project's data even if they share artifact ids (which they shouldn't, since ids are project-namespaced).

**What's deliberately NOT cascaded:**
- **Audit log** — compliance / debugging value outweighs storage. The events.jsonl entries with `target_id=<run_id>` stay forever.
- **Batch records** — a parent batch's `run_ids` array keeps the purged child id. The FE renders missing children as "purged" (it gets a 404 from `getRun`); the historical batch composition is preserved.

### Hard-delete tests
- `tests/test_ingestion_review_service.py::test_purge_run_physically_removes_artifacts_and_run_record`
- `tests/test_ingestion_review_service.py::test_purge_run_requires_soft_delete_first_by_default`
- `tests/test_ingestion_review_service.py::test_purge_run_force_bypasses_soft_delete_gate`
- `tests/test_ingestion_review_service.py::test_purge_run_rejects_active_run`
- `tests/test_ingestion_review_service.py::test_purge_run_404s_for_unknown_run`
- `tests/test_ingestion_review_service.py::test_purge_run_idempotent_on_second_call`

---

## 7. Multi-upload batch  ✓ implemented

Endpoints:
- `POST /ingestion-batches` — register N files, kick off N child workflows.
- `GET /ingestion-batches/{batchId}` — aggregate view.

Store: [`src/j1/runs/batch_store.py`](../src/j1/runs/batch_store.py) — `JsonlBatchRunStore` parallel of `JsonlIngestionRunStore`. `BatchRun` carries the ordered child `run_ids`. Aggregate status is **derived at read-time** from child statuses via `derive_batch_status()`, never persisted, so we never have to write a synchronised view of N rows.

**Constraints:**
- `J1_INGESTION_BATCH_MAX_FILES` — env-overridable, default `5`. Rejected with HTTP 400 when exceeded.
- Sequencing is whatever the worker's task-queue concurrency permits. With the dev default `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=5`, child workflows may execute in parallel at the activity layer. The future `J1_INGESTION_BATCH_CONCURRENCY=1` knob (parent-workflow sequencing via `execute_child_workflow`) is documented but not yet implemented — operators who need strict sequential execution should set `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=1` until the parent-workflow sequencer lands.

**Behaviour landed:**

1. Validate file count against `J1_INGESTION_BATCH_MAX_FILES`.
2. Generate `batch_run_id` (UUID).
3. For each file: register the document via the existing intake service (handles checksum dedupe), allocate a child `run_id`, persist `IngestionRun` with `metadata.batch_run_id`, start a per-document workflow via the existing starter.
4. Persist `BatchRun(batch_run_id, run_ids, file_count, started_at, actor)` to `audit/batch_runs.jsonl`.
5. Return `{batchRunId, fileCount, runIds, status: "running", startedAt}`.

**Read endpoint** returns:
```json
{
  "batchRunId": "...",
  "status": "running" | "completed" | "completed_with_warnings" | "partially_failed" | "failed" | "deleted",
  "startedAt": "...",
  "fileCount": 5,
  "completedCount": 2,
  "failedCount": 0,
  "currentRunId": "<the first non-terminal child>",
  "runs": [
    {"runId": "r1", "documentId": "d1", "filename": "a.pdf", "status": "succeeded", "currentStage": null, "currentStep": null, "progressPercent": 100},
    ...
  ]
}
```

**Status-derivation rules** (`derive_batch_status`):
- any child active → `running`
- all `succeeded` → `completed`
- all `succeeded` + ≥1 `succeeded_with_warnings` → `completed_with_warnings`
- mix of succeeded + failed → `partially_failed`
- all `failed` / `cancelled` → `failed`
- all `deleted` → `deleted`

### Tests
- `tests/test_batch_run_store.py` — store persistence + read-back + idempotent overwrite.
- `tests/test_batch_run_store.py::test_derive_batch_status_*` — every aggregate-status rule pinned.



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
