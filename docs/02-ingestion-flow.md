# 02. Ingestion Flow

> Audience: engineers + technical product owners.
> [Back to README](../README.md). See also
> [01-overall-architecture.md](01-overall-architecture.md),
> [04-core-data-model.md](04-core-data-model.md).

## Overview

Every ingestion follows the same shape:

1. **Upload** a file under `(tenant, project)` and get back a
   `document_id`.
2. The REST layer **allocates a candidate snapshot** and creates
   an `IngestionRun`, then signals the workflow.
3. The `ProjectProcessingWorkflow` runs in Temporal:
   profile → assess → compile → enrich → graph → index → finalize.
4. On terminal success, the snapshot is **promoted** to be the
   document's active visibility key. The previous snapshot is
   superseded (artifacts kept for diagnostics; queries stop using
   them).

> Split-mode parsing has been removed from the architecture. Do
> not document or implement it. Compile is a single call with one
> shape per document.

## Step-by-step

### 1. Upload and document registration

- `POST /documents/{id}/ingest` (or the batch upload endpoint)
  accepts a file plus the tenant/project headers and writes the
  bytes into the workspace under the project's `raw/` area.
- The intake registry (`JsonSourceRegistry`) appends a
  `DocumentRecord` with `status=PENDING`, `knowledge_state=attached`,
  `active_snapshot_id=None`. A `DocumentVersion` is allocated for
  the uploaded bytes.
- The REST handler creates the matching `IngestionRun` with a
  newly-allocated `target_snapshot_id` and starts the Temporal
  workflow with `ProjectProcessingRequest.target_snapshot_id` set.

### 2. Profile + assess

Inside `_process_document`:

- A cheap deterministic profile runs (pypdf-based, no LLM). It
  records page count, has-images, has-tables, scanned-pages,
  text-extractable ratio.
- When `planner_enabled` is true, an `InitialExecutionPlan` and an
  `AssessmentPlan` are built. The assessment plan picks the parse
  method, declares required vs optional capabilities (text /
  tables / images / etc.), and is carried as a plain dict so the
  Temporal data converter handles it without dragging the
  assessment module into the activity payload.
- `assessment_failure_policy` decides whether assessment failure
  fails the run (`fail_closed`) or falls back to `settings.parse_method`
  (`fail_open`, the default).

### 3. Compile (RAGAnything as the black box)

Compile is the most expensive activity. It is a single black box
call:

- Input: `CompileActivityInput(scope, document_id, processor_kind,
  correlation_id, assessment_plan_payload, target_snapshot_id)`.
- Output: a `ArtifactActivityResult` listing the compile artifacts
  the activity registered.

What happens inside is RAGAnything's business. In the current
implementation it routes through `j1.providers.raganything._bridge`:

- Parses the document (MinerU by default).
- Splits into chunks.
- Embeds them.
- Builds the per-document LightRAG workspace at
  `{workdir}/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}/`.
- Produces chunk + graph artifacts.

The compile retry loop (`compile_retry_*` knobs on the workflow
request) lets the workflow re-run compile with a different mode if
the first pass produces a degenerate result.

`CompileResult` is normalised through `j1.processing.compile_result`
so the downstream gate evaluates the same shape regardless of which
processor produced it.

### 4. Post-compile enrichment plan + enrichment

- `assess_post_compile_enrich` builds a `PostCompileEnrichPlan`
  from compile evidence + the active domain pack's
  `enrichment_policy` + `extraction_hints`.
- For each task the plan recommends (text enrichment, metadata
  extraction, table interpretation, image captioning, validation,
  classification), the workflow dispatches an `enrich` activity.
- Enrichment results land as new artifacts (`enriched.*` kinds)
  with `metadata.snapshot_id` stamped onto each.
- A best-effort fast-LLM consult can refine the enrich plan before
  dispatch (`is_consult_warranted` decides).

### 5. Graph build + index

- `build_graph` runs once compile + enrichment have produced
  artifacts the graph builder cares about. The graph LLM workspace
  is snapshot-scoped, so two reindex attempts for the same document
  cannot interfere.
- `index` runs the search adapter (`PostgresFtsEvidenceAdapter`)
  against the document's artifacts. Each evidence row is written
  with both `document_id` and `snapshot_id`.

### 6. Snapshot promotion (atomic)

When the workflow reaches terminal success (`succeeded` or
`succeeded_with_warnings`), the runs activity:

1. Reads the document's current `active_snapshot_id`.
2. Marks the candidate snapshot READY.
3. Calls `DocumentSnapshotService.promote` with the prior id as the
   CAS expected value.
4. On success, writes the new id to `document.active_snapshot_id`.
5. Stamps the prior snapshot's artifacts as
   `search_state=superseded` so retrieval stops surfacing them.

If the CAS fails (another reindex won the slot, or the document was
removed mid-run) the candidate snapshot stays in BUILDING/READY
state, gets cleaned up, and the document's active state is left
untouched.

### 7. Audit + final report

- A `final_summary` artifact captures the at-a-glance run outcome.
- A `final_ingestion_report` artifact captures the structured
  per-stage results.
- `j1.runs.report_terminal` writes an audit event with the
  failure code (if any) and the per-step skip/fail summary.

## Re-indexing a document

Re-index is the document-level entry point — not the run-level one.
The run-level "rebuild index" / "continue from compiled result"
controls have been removed from the UI; the canonical action is
`POST /documents/{id}/reindex`.

A reindex:

1. Creates a fresh `IngestionRun` with `run_type="reindex"`,
   `parent_run_id=<previous active run>`,
   `target_snapshot_id=<freshly-allocated candidate>`.
2. Runs the full workflow against the new snapshot.
3. On success, atomically promotes the new snapshot. The previous
   snapshot is moved to SUPERSEDED.
4. On failure, the previous snapshot stays active. Queries are
   unaffected.

There is also `POST /documents/{id}/refresh-enrich`. It reuses the
previous active run's compile output and only re-runs enrichment +
graph + index. Same snapshot promotion contract.

## Detach / remove

- **Detach** (`POST /documents/{id}/detach`) flips
  `knowledge_state` to `detached`. The document and its snapshots
  stay on disk; the query layer's eligibility resolver immediately
  refuses to include the document. Reversible via attach.
- **Remove** (`POST /documents/{id}/remove`) is gate-first +
  cleanup. It flips `lifecycle_status` to `removing` and clears
  `active_snapshot_id` before any destructive work, then runs
  synchronous hard cleanup. The eligibility resolver refuses any
  lifecycle state outside `stable`, so a concurrent query cannot
  leak a removing document.

## Resume / retry principles

Temporal already handles per-activity retries with the policies
configured per activity (`COMPILE_RETRY`, `DEFAULT_RETRY`). On top
of that:

- The compile path persists a `resume_snapshot` on the run's
  metadata when a stage completes. The workflow can resume from
  the last successful stage by reading that snapshot.
- A REST-driven "resume" creates a new run with
  `resume_from_run_id` set and a short list of `resume_completed_steps`.
  The new run gets its own `target_snapshot_id`; the new snapshot
  must promote atomically just like a fresh ingest.
- The full restart fallback is "re-process" (a normal reindex).

## Source-of-truth rules (current)

These are non-negotiable invariants. Code that breaks them is
considered a bug.

1. `document.active_snapshot_id` is the **only** key the query +
   retrieval layers may consult for visibility.
2. `IngestionRun.run_id` is a trace identifier. The retrieval path
   must not gate on it.
3. The artifact registry stamps every artifact with
   `metadata.snapshot_id`. Artifacts without one are unusable for
   retrieval.
4. Snapshot promotion is CAS-guarded against the document's prior
   active id. A failed CAS means "do not promote, run orphan
   cleanup".
5. Cross-document scope leaks are detected by the orchestrator's
   scope check and surface as `scope_error` per question on the
   Imported Test Cases summary.

## What was removed (and stays removed)

Listed here so reviewers know not to re-introduce them:

- **Split-mode parsing** (`split_parse_insert`). Compile is one call.
- **`active_run_id` as a visibility key on `DocumentRecord`**. The
  field is gone. Reset + re-ingest is the only migration path.
- **Generated test-case pipeline**. Validation is manual query +
  imported CSV only.
- **`get_or_create_for_run` as the canonical activity-layer snapshot
  lookup**. The bulk-job workflow allocates a per-document snapshot
  via the `allocate_target_snapshot` activity and threads
  `target_snapshot_id` end-to-end. The activity-layer lookup is
  `require_existing_target_snapshot`.
