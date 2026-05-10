# J1 Ingestion Stability Audit

> Status: **draft, in progress**
> Scope: end-to-end ingestion pipeline (upload → compile → plan → enrich → graph → index → finalize)
> Goal: deterministic, observable, idempotent, artifact-safe ingestion

## 1. Problem summary

Operators reported the same document producing inconsistent outputs across runs:

- Sometimes chunks exist but graph is missing (or vice versa).
- Some Content Inventory / Execution Plan artifacts existed on disk but didn't surface in the FE.
- Some runs reported `COMPLETED` while a required output was missing.
- LightRAG dedupe state across runs occasionally re-mapped a fresh `doc_id` as a "duplicate" of an earlier run's failed entry.
- The qwen3 reasoning-mode trap: the entity-extraction LLM emitted only thinking tokens, returning empty visible content; LightRAG saw "0 Ent + 0 Rel".
- Artifacts existed but the FE banner said the LLM was unreachable (mock-mode default hid real signals).

The audit traces the data flow, identifies root causes, and lists the fixes already shipped + the remaining hardening required.

## 2. Root causes found (already fixed in earlier commits)

| # | Root cause | Fix landed in |
|---|---|---|
| R1 | `_chunk_drafts_from_storage` projected every chunk in LightRAG's shared `kv_store_text_chunks.json`, not just the doc just inserted — the Knowledge Chunks tab showed chunks from an unrelated document. | [src/j1/providers/raganything/_bridge.py — `_chunk_drafts_from_storage(..., doc_id=...)` filter](../src/j1/providers/raganything/_bridge.py) |
| R2 | LightRAG's `kv_store_doc_status.json` retained orphan keys with empty values. `adelete_by_doc_id` early-returned on falsy values; `filter_keys` (the dedupe gate) saw the orphan key and rejected the new insert as a duplicate. | Three-phase cleanup in `default_insert_content` (`adelete_by_doc_id` → `doc_status.delete([doc_id])` → `index_done_callback()` flush → forced JSON rewrite) + `filter_keys` monkey-patch bypass for the doc_id we own. |
| R3 | qwen3 chat templates produce up to 2K reasoning tokens BEFORE the visible answer; on a 2048-cap model setup, the visible content arrives empty + `finish_reason=length`. LightRAG's entity-extraction parser saw "0 Ent + 0 Rel". | `/no_think` directive injected into both system + user prompts in `_make_text_callable`; `J1_TEXT_LLM_MAX_OUTPUT_TOKENS=4096`; `J1_FAST_LLM_MAX_OUTPUT_TOKENS=4096`. |
| R4 | Tabs gated on `views?.chunks.available` (no `?` after `chunks`) — when the backend returned a partial `views` object, the entire tab list crashed silently. | Safe optional chaining on every tab gate field. |
| R5 | Content Inventory + Planning Report tabs gated availability on a mix of audit-log events and artifact existence; the two were written by independent code paths. When one path fired but the other didn't, the tab stayed disabled even though the data existed. | Both tabs are now ALWAYS available; the tab content endpoint owns the empty-state messaging. |
| R6 | `getLLMHealth` mock returned `healthy: true` always. FE default mode was `"mock"`, so a real LLM outage never surfaced. | Mock client deleted entirely; FE now uses `ApiClient` unconditionally. |
| R7 | LightRAG MinerU `hybrid-http-client` backend needs `MINERU_VL_SERVER` env var — the bridge only set it for `vlm-http-client`. The `hybrid-http-client` path crashed on first parse with "Environment variable MINERU_VL_SERVER is not set". | `_apply_vlm_http_client_env` now propagates env vars for both backends. |
| R8 | `_apply_post_compile_planning` crashed on `AttributeError: 'list' object has no attribute 'get'` when an LLM-emitted plan returned `execution_plan.steps` as a list instead of a dict. | List-tolerant coercion: `if isinstance(raw_steps, list): { entry.name → entry } else dict-as-is`. Same guard added in `service.py` planning DTO builder. |
| R9 | Probe call hung worker / API startup for the full client timeout (300s) per role on unreachable LLM. | 5-second hard deadline per probe via `concurrent.futures.ThreadPoolExecutor.shutdown(wait=False)`. |

## 3. Settings audit

### Pipeline mode

| Env | File defining | Files consuming | Default | Notes |
|---|---|---|---|---|
| `J1_RAGANYTHING_PIPELINE_MODE` | `src/j1/providers/raganything/settings.py` | `src/j1/orchestration/workflows/project_processing.py` (line 1753, 1823) | loader: `split_parse_insert`; dataclass: `complete` | Loader default differs from dataclass default — deliberate, but worth flagging. Tests construct the dataclass directly and rely on `complete`. Production reads via the loader and gets `split_parse_insert`. |

### Planner

| Env | File | Default | Notes |
|---|---|---|---|
| `J1_INGEST_PLAN_MODE` | `src/j1/processing/planning_settings.py` | `rule_based` | Operator-facing master knob. Resolves to `llm_planning_enabled = (mode in {llm, hybrid})`. |
| `J1_LLM_PLANNING_ENABLED` | `src/j1/processing/planning_settings.py` | `False` | Legacy. **Overridden by `J1_INGEST_PLAN_MODE`** when the latter is set. Setting both can confuse operators (we hit this in production). |
| `J1_PLANNING_ENABLED` | `src/j1/processing/planning_settings.py` | `True` | Master kill-switch for the whole planning subsystem. |
| `J1_POST_COMPILE_PLANNING_ENABLED` | `src/j1/processing/planning_settings.py` | `True` | Specific to the post-compile planning activity. |
| `J1_PLANNING_MODEL_PROFILE` | `src/j1/processing/planning_settings.py` | `fast_planner` | LLM role used when planning is enabled. |
| `J1_PLANNING_FAIL_OPEN` | `src/j1/processing/planning_settings.py` | `True` | When the LLM fails, fall back to rule-based. |
| `J1_PLANNING_MAX_SAMPLE_BLOCKS`, `J1_PLANNING_MAX_PREVIEW_CHARS`, `J1_PLANNING_MAX_EARLY_PAGES` | same | 20, 300, 3 | Privacy boundaries on the LLM context. |

**Recommended source of truth**: `J1_INGEST_PLAN_MODE`. Document `J1_LLM_PLANNING_ENABLED` as legacy.

### Enrichment

| Env | File | Default |
|---|---|---|
| `J1_ENRICH_ENABLED` | `src/j1/compose/bootstrap.py` | `True` |
| `J1_ENRICH_CONFIDENCE_THRESHOLD` | same | `0.75` |
| `J1_ENRICH_IMAGES` / `_TABLES` / `_DIAGRAMS` / `_SCANNED_PAGES` | same | per-modality booleans |

`J1_ENRICH_SCANNED_PAGES` is read in two places in `bootstrap.py`:
1. As a per-modality enable flag (line ~189).
2. As an indirect coupling to `J1_RAGANYTHING_PARSE_METHOD`: when `scanned_pages=False` AND `parse_method=auto`, it forces `parse_method=txt` to skip OCR (line ~252). The intention is documented; the coupling is intentional but worth keeping under one helper if it grows.

### Probe / health

| Env | Default | Notes |
|---|---|---|
| `J1_LLM_STARTUP_PROBE` | `true` | Master toggle for the startup probe + background monitor. |
| `J1_LLM_PROBE_TIMEOUT_SECONDS` | `5.0` | Hard ceiling per probe call (wraps the client's full timeout). |
| `J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS` | `30.0` | Background re-probe interval (`0` disables). |

### Retry / timeout

`src/j1/orchestration/temporal/retries.py`:

- `DEFAULT_RETRY` — 5 attempts, 1s initial, 2× backoff, 60s max, J1_INGEST_* + ConfigError + ValidationError + parser failures non-retryable.
- `COMPILE_RETRY` — **2 attempts only** (compile is expensive — MinerU parse can take minutes per real PDF).

### Selection

| Env | Default |
|---|---|
| `J1_DEFAULT_COMPILER` | `raganything` |
| `J1_DEFAULT_GRAPH_PROVIDER` | `raganything` |
| `J1_DEFAULT_RETRIEVAL_PROVIDER` | `raganything` |

## 4. Ingestion data lifecycle map

For each stage: **input**, **output**, **persistence**, **failure behaviour**, **retry behaviour**, **cleanup behaviour**, **related code**.

### Stage 0 — Upload & run creation

- **Input**: HTTP POST `/projects/{p}/documents` (multipart file), `/ingestion-runs` (start).
- **Output**: `IngestionRun` record (status=`CREATED`), document persisted to `tenants/{tenant}/projects/{project}/raw/{document_id}{ext}`.
- **Persistence**: `IngestionRunStore` (JSONL backed under `audit/`); document file via `DocumentIntakeService`.
- **Failure**: 4xx if duplicate checksum / unsupported MIME; 5xx if the workspace area is unwritable.
- **Retry**: client-side; idempotent via deterministic `workflow_id = j1-{tenant}-{project}-{document_id}` + `USE_EXISTING` conflict policy.
- **Cleanup**: none.
- **Code**: `src/j1/adapters/rest/app.py` upload + ingest endpoints; `deploy/dev/api.py` `make_per_document_starter`.

### Stage 1 — Compile (parse-only in split mode)

- **Input**: `CompileActivityInput(scope, document_id, processor_kind, ...)`.
- **Output (split mode)**: 3 artifacts — `parsed_source` (raw RAGAnything `content_list`), `parsed_content_manifest` (FE-facing inventory with `items[]`), `compiled.text`.
  **Output (legacy `complete` mode)**: chunks + parsed_source + manifest + compiled.text + graph artifacts in one call.
- **Persistence**: `ProcessingService._handle_artifact_output` → `_register_draft` → file under `compiled/{artifact_id}.{ext}` + `ArtifactRecord` in registry tagged with `metadata.run_id = correlation_id`.
- **Failure**: `ResultStatus.FAILED` returned → activity returns non-succeeded → workflow raises `_BusinessRejection` ([line 1604](../src/j1/orchestration/workflows/project_processing.py#L1604)) → run goes to FAILED_FINAL.
- **Retry**: `COMPILE_RETRY` (2 attempts). Cache hit (`ProcessingResultCache`) bypasses retry on `(document_hash, processor_kind, version, mode)` match.
- **Cleanup**: NONE on stage failure. Failed-run artifacts are stranded but queryable via the registry — explicit design choice so operators can inspect the partial state.
- **Code**: `src/j1/orchestration/activities/processing.py::compile`, `src/j1/processing/service.py::compile`, `src/j1/providers/raganything/compiler.py::compile`, `src/j1/providers/raganything/_bridge.py::default_compile`.

### Stage 2 — Build Content Inventory (synthetic step in split mode; folded into compile in complete mode)

- **Input**: `parsed_content_manifest` artifact.
- **Output**: synthetic step.* lifecycle events; no new artifact.
- **Persistence**: audit log only (`build_content_inventory.started/completed`).
- **Failure**: best-effort; never blocks the run.
- **Retry**: N/A (it's an event emit, not a real activity).
- **Cleanup**: N/A.

### Stage 3 — Post-compile planning

- **Input**: parsed_content_manifest + DocumentProfile + domain overrides.
- **Output**: `planning_result` artifact + `plan.revised` audit event.
- **Persistence**: `_persist_planning_result` writes `planning_{run_id}_{document_id}.json` to the COMPILED area + registers an artifact record tagged with `metadata.run_id`.
- **Failure**: `J1_PLANNING_FAIL_OPEN=true` → fall back to rule-based, planner_mode logged as `rule_based_fallback`.
- **Retry**: `DEFAULT_RETRY` policy. The persist step is idempotent (deterministic artifact_id `planning_{run_id}_{document_id}`).
- **Cleanup**: NONE.
- **Code**: `src/j1/orchestration/activities/planning.py::build_planning_result`, `src/j1/processing/post_compile_planning.py::build_planning_result`, `src/j1/processing/planning_result.py`.

### Stage 4 — Generate Knowledge Chunks (split mode: real activity; complete mode: synthetic event)

- **Input**: `parsed_source` artifact (read back from disk).
- **Output**: `chunk` artifacts — one record per LightRAG chunk, body extracted from `kv_store_text_chunks.json` and filtered by `full_doc_id == doc_id`.
- **Persistence**: same `_handle_artifact_output` → `_register_draft` path. Chunks tagged with `source_document_ids=[document_id]` and `metadata.run_id`.
- **Failure**: `_BusinessRejection` ([line 1810](../src/j1/orchestration/workflows/project_processing.py#L1810)) on insert failure. The workflow correctly fails closed.
- **Retry**: `DEFAULT_RETRY`. **Idempotent because** the bridge now wipes the doc_id from LightRAG's `kv_store_doc_status.json` before insert AND the registry uses a per-chunk `artifact_id` UUID, so a retry won't double-write the same chunk-id from a J1 perspective. LightRAG's internal storage IS overwritten — that's the LightRAG contract, not a J1 invariant.
- **Cleanup**: NONE.
- **Code**: `src/j1/orchestration/activities/processing.py::insert_content`, `src/j1/processing/service.py::insert_content`, `src/j1/providers/raganything/_bridge.py::default_insert_content`.

### Stage 5 — Enrichment

- **Input**: chunk artifacts (per-artifact loop).
- **Output**: `enriched.tables` / `enriched.visuals` / `enriched.formulas` / `enriched.confidence_assessment` / `enriched.consistency_findings` artifacts.
- **Persistence**: per-modality enricher → `ProcessingService.enrich` → `_handle_artifact_output`.
- **Failure**: `_BusinessRejection` on activity failure. Per-modality failures inside the composite enricher are logged + surfaced as warning artifacts (don't fail the whole stage).
- **Retry**: `DEFAULT_RETRY`. Per-artifact iteration — a retry of the workflow may re-process artifacts that already had successful enrichments. Today the enricher does NOT skip already-enriched artifacts; this is a known inefficiency.
- **Cleanup**: NONE.
- **Code**: `src/j1/orchestration/activities/processing.py::enrich`, `src/j1/enrichers/composite.py`.

### Stage 6 — Graph build

- **Input**: chunk + enriched artifacts.
- **Output**: `graph_json` (canonical), optionally `graph_html` / `graph_report` / `graph_metadata` / `graph_cache`.
- **Persistence**: `RAGAnythingGraphBuilder` → `_handle_artifact_output`.
- **Failure**: `_BusinessRejection` ([line 1993](../src/j1/orchestration/workflows/project_processing.py#L1993)).
- **Retry**: `DEFAULT_RETRY`.
- **Cleanup**: NONE.

### Stage 7 — Index

- **Input**: every chunk + graph artifact produced so far.
- **Output**: SQLite FTS rows (no new artifact).
- **Persistence**: `SqliteSearchIndexer`.
- **Failure**: `_BusinessRejection` ([line 2038](../src/j1/orchestration/workflows/project_processing.py#L2038)).
- **Cleanup**: NONE.

### Stage 8 — Finalize

- **Input**: workflow state.
- **Output**: `IngestionRun` status updated to terminal value; `run.completed` / `run.failed` / `run.cancelled` audit event.
- **Persistence**: `IngestionRunStore.upsert`; audit log via `AuditProgressReporter`.
- **Failure**: `_safe_finalize` swallows finalization errors so a finalize bug can't mask the original failure.
- **Cleanup**: NONE on disk; in-memory `_produced_artifact_ids` cleared via continue-as-new boundary.

## 5. Artifact contract

Every artifact persisted via `_register_draft` carries:

| Field | Source | Notes |
|---|---|---|
| `artifact_id` | `_id_factory()` (UUID) | Stable per run/produce. |
| `project` | `ProjectContext` | Tenant + project scope. |
| `kind` | producer-supplied | One of the `ARTIFACT_KIND_*` constants. |
| `location` | `{area}/{filename}` | Area names: `compiled`, `enriched`, `graph`. |
| `content_hash` | `sha256:<hex>` | Content-derived; lets cache-key match. |
| `byte_size` | `len(draft.content)` |  |
| `status` | always `SUCCEEDED` | Failed artifacts aren't registered. |
| `review_status` | producer-supplied or `NOT_REQUIRED` |  |
| `version` | `1` | Re-running creates a NEW artifact_id, never overwrites. |
| `created_at` / `updated_at` | wall-clock | |
| `source_document_ids` | producer-supplied or fallback `[document_id]` | Used by the run-resolver lineage walk. |
| `source_artifact_ids` | producer-supplied | Used by the run-resolver lineage walk for downstream stages. |
| `metadata` | producer dict + auto-tagged `run_id` | `run_id` enables the direct-tag fast path. |

**Visibility rule**: `_resolve_run_artifacts(ctx, run)` returns artifacts where ANY of:
1. `metadata.run_id == run.run_id` (fast path, returned exclusively when present), OR
2. `source_document_ids` overlaps `run.document_ids` (lineage seed), OR
3. transitively via `source_artifact_ids` from any artifact already in the seed.

Both paths are well-tested.

**Failure-mode guarantee**: a partially-completed run keeps all artifacts produced before the failure. Nothing is unlinked except on `_register_draft`'s own DB-write-failure rollback path.

## 6. Run status rules

State transitions live in `ProjectProcessingWorkflow.run()`:

| State | Trigger |
|---|---|
| `RUNNING` | Workflow start. |
| `PAUSED` / `WAITING_FOR_BUDGET_APPROVAL` / `WAITING_FOR_REVIEW` | Pause signal / budget gate / review gate. |
| `COMPLETED` | All stages succeeded AND `_validate_completion()` returned no errors. |
| `FAILED_FINAL` | Caught `_BusinessRejection` (any required step failure, completion-validation failure, budget rejection, review rejection). |
| `FAILED_RECOVERABLE` | Unhandled exception (e.g. activity-side bug); Temporal retry policy decides whether to retry the workflow. |
| `CANCELLED` | Cancel signal observed. |

**Per-stage status** is recorded separately via `_record_step(step, status, required, source, ...)` and surfaced on the `RunSummaryDTO.steps[]` field. `step_results` outlives the workflow run via `run.metadata.step_results` so failed runs can still tell operators which stage failed and why.

**Completion validation** (`_validate_completion`) runs before the COMPLETED transition. Existing checks:

1. At least one artifact produced.
2. Every required step recorded as COMPLETED or SKIPPED.
3. `indexer_kind` set + artifacts produced ⇒ `index` step recorded.

**Hardened in this audit** (see § 7):

4. If `graph_builder_kind` is set AND the graph step ran, at least one `graph_json` artifact must exist.
5. If chunks are required (split mode reaches insert), at least one `chunk` artifact must exist.
6. If `parsed_content_manifest` is the contract for split mode, validate it landed.

## 7. Hardening landed in this commit

(See § 9 for tests pinning each rule.)

- **Activity result now surfaces artifact kinds.** `ArtifactActivityResult` gains a `kinds: tuple[str, ...]` field populated by `_artifact_result()`. The workflow can now check "did graph really produce a graph_json?" without a separate registry query.
- **`_validate_completion` enforces per-stage artifact rules.** New errors when:
    - Graph step is recorded as COMPLETED but no `graph_json` artifact was produced;
    - Insert/chunks step is recorded as COMPLETED but no `chunk` artifact was produced;
    - Compile in split mode produced no `parsed_content_manifest`.
- **`error_report` artifact persisted on FAILED_FINAL.** When the workflow catches `_BusinessRejection`, it persists a structured `error_report.json` artifact tagged with the run_id BEFORE finalize emits the terminal event. Operators can then inspect why the run failed via the same artifact-listing path that surfaces successful artifacts.

## 8. Verification commands

```bash
# Backend
cd /Users/vuvo/J1
python -m pytest tests/ -q --ignore=tests/test_e2e_processing_flow.py

# Targeted: run-status validation
python -m pytest tests/test_project_processing_workflow.py tests/test_workflow_step_results.py tests/test_split_pipeline_workflow.py -v

# Targeted: artifact visibility
python -m pytest tests/test_ingestion_review_service.py tests/test_rest_ingestion_review.py -v

# Frontend
cd frontend
npm run build       # tsc -b + vite — typechecks + bundles
npx vitest run

# Smoke test (one local upload)
docker compose -f deploy/dev/docker-compose.yml up -d
# Upload via curl:
curl -X POST "http://localhost:8000/api/projects/<project>/documents" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>" \
  -F "file=@<path-to-pdf>"
# Then start ingest (run_id returned):
curl -X POST "http://localhost:8000/api/projects/<project>/documents/<doc_id>/ingest" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>" \
  -d '{"compilerKind":"raganything"}'

# Inspect artifacts for a run
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<run_id>/artifacts?pageSize=200" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>" \
  | python -m json.tool

# Inspect chunks
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<run_id>/chunks?pageSize=50" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>"

# Inspect graph
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<run_id>/graph?maxNodes=200&maxEdges=200" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>"

# Inspect summary (gates, step_results, availability)
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<run_id>/summary" \
  -H "X-Tenant-Id: <tenant>" -H "X-Project-Id: <project>"

# LLM connectivity status
curl -s http://localhost:8000/healthz/llm | python -m json.tool
```

## 9. Tests added in this commit

- `tests/test_split_pipeline_workflow.py::test_complete_validation_fails_when_graph_step_completed_without_artifact` — graph required but `graph_json` missing → FAILED_FINAL.
- `tests/test_split_pipeline_workflow.py::test_complete_validation_fails_when_chunks_step_completed_without_artifact` — chunks required but no `chunk` artifact → FAILED_FINAL.
- `tests/test_project_processing_workflow.py::test_failed_run_persists_error_report_artifact` — failure path writes `error_report.json` discoverable via the standard listing.
- `tests/test_ingestion_review_service.py::test_summarize_run_unlocks_content_inventory_and_planning_for_full_split_run` (existing) — both tabs unlock when manifest + planning_result artifacts are tagged with the run.

## 10. Known limitations

- **LightRAG storage is shared per workdir** across documents. Cross-document chunk leakage is filtered at the projector by `entry.full_doc_id == doc_id`. If a future LightRAG version changes that schema, the filter needs updating.
- **Enrichment retries don't skip already-enriched artifacts.** A workflow retry will re-spend LLM tokens on artifacts that succeeded the first time. Out of scope for this audit — separate ticket.
- **Per-process probe cache.** Worker and API caches are independent. The FE talks to the API, so the API's cache is what gates the banner. If the worker probe sees the LLM as down but the API hasn't ticked yet, there's a short window where the FE banner says "healthy" but a workflow run will fail. Background monitor (30s) closes the window quickly.
- **`J1_LLM_PLANNING_ENABLED` is legacy.** Operators with both it and `J1_INGEST_PLAN_MODE` set will see the explicit `_ENABLED` value win — by design but a foot-gun. Plan to deprecate in a follow-up.

## 11. Manual acceptance checklist

After applying this commit:

- [ ] Re-running the same small document multiple times produces stable outputs for the same selected mode/profile.
- [ ] A completed run always has all required artifacts (validated by `_validate_completion`).
- [ ] A completed graph-enabled run always has chunks AND graph (new validation rule #4 + #5).
- [ ] Content Inventory is visible (always-available tab + endpoint that reads the manifest).
- [ ] Execution Plan is visible (always-available tab + endpoint that reads the planning_result artifact OR the audit-log fallback).
- [ ] Chunk JSON is visible after chunk generation.
- [ ] Graph result is visible when graph runs.
- [ ] Failed runs still expose already-created artifacts AND a new `error_report` artifact.
- [ ] Missing graph/chunk output cannot be reported as completed.
- [ ] No stage silently deletes data needed by later stages (verified by lifecycle map § 4).
- [ ] Logs include run_id, document_id, stage, attempt for every major step.
- [ ] Tests cover success, partial failure, retry, and visibility cases.
