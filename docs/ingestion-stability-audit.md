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

## 11. Expected ingestion operating model

J1 owns planning + validation. Adapters execute selected capabilities. A run is `COMPLETED` only when validation against the `IngestPlan` passes.

**Backbone stages (every run):**

`create_run → persist_original_document → resolve_config → document_profile → initial_execution_plan → parse_or_compile → content_inventory → refined_execution_plan? → generate_chunks → persist_or_index_chunks → artifact_manifest → final_validation → final_summary`

**Conditional capabilities (gated by plan):**
- `graph` — runs only when `IngestPlan.steps[graph].enabled == True`. Today: gated by caller-supplied `graph_builder_kind` (forces enabled with `source=CALLER`) OR planner decision (with `source=PLANNER`). The post-compile overlay must NOT silently flip a CALLER-enabled graph back to skipped — `_apply_post_compile_planning` honors this.
- `enrich` — same model.
- `vision_enrich` — folded inside the composite enricher; per-modality flags from `EnrichmentSettings`.

**Stage-success rule**: a stage is `succeeded` only when (a) the activity returned successfully, (b) required outputs were persisted, (c) outputs are readable, (d) per-stage validation passes, (e) the stage record is durably saved.

The current workflow ([`src/j1/orchestration/workflows/project_processing.py`](../src/j1/orchestration/workflows/project_processing.py)) already enforces (a)–(c) via `_handle_artifact_output` + raise-on-failure semantics, and partially enforces (d) via `_validate_completion`. (e) is implicit through `_record_step` which is read by `RunSummaryDTO.steps[]`.

## 12. Gap analysis (plan-first model vs. current state)

| Required behaviour | Current state | Gap | Fix landed this commit |
|---|---|---|---|
| J1 owns the IngestPlan | ✓ — `DefaultIngestPlanner` + `_apply_post_compile_planning` | None | — |
| Graph not implicit in parse | ✓ — split-mode parses without inserting; complete-mode keeps everything in one call but is documented as legacy | `complete` mode is honest about combined behaviour via `parse_boundary="legacy_combined"` metadata | — |
| Enrich gated by plan | ✓ via `_stage_enabled(plan, "enrich", ...)` | None | — |
| Each stage validates output | ✓ for compile/insert/enrich/graph/index — fail-closed via `_BusinessRejection`. + per-stage rules now in `_validate_completion` | None remaining | Per-stage required-output rules (graph→`graph_json`, chunks→`chunk`) |
| `validation_report` artifact persisted | ✗ existed only as in-memory `validation_errors` | Add a durable artifact summarising what `_validate_completion` saw + decided | **YES (this commit)** |
| `final_summary` artifact persisted | ✗ summary only existed in-memory + audit log | Add a `final_summary.json` artifact at terminal state (success or failure) | **YES (this commit)** |
| `error_report` artifact persisted on failure | ✗ → ✓ | — | Landed in previous commit (see § 7) |
| Failed runs expose partial artifacts | ✓ — verified in lifecycle map § 4 | None | — |
| Resume from last checkpoint | ✗ no resume action; users have to re-upload | Big surface; documented in operations doc, deferred for impl iteration | Doc only |
| Rebuild index only | ✗ no endpoint | Same — operations doc | Doc only |
| Full re-index (new attempt) | Partial — re-uploading same checksum cache-hits and re-uses | Operations doc explains the contract; impl follow-up | Doc only |
| Delete ingest (soft / hard) | ✗ no endpoint | Operations doc explains the contract; impl follow-up | Doc only |
| Multi-upload batch | ✗ each upload is independent; no batch_run_id | Operations doc explains the contract; impl follow-up | Doc only |
| Sequential concurrency=1 | The dev worker already has `J1_WORKER_MAX_CONCURRENT_ACTIVITIES=5` (default) but the workflow processes documents inside one workflow sequentially. Multi-upload concurrency is N/A until batch model lands. | Doc the current behaviour; flag `J1_INGESTION_BATCH_CONCURRENCY` as the future knob | Doc only |

## 13. Execution plan contract

The `IngestPlan` ([`src/j1/processing/planning.py`](../src/j1/processing/planning.py)) is the source of truth.

```python
@dataclass(frozen=True)
class IngestPlan:
    document_id: str
    mode: IngestMode          # CHUNKS_ONLY | TEXT_ONLY | MULTIMODAL_LIGHT | MULTIMODAL_FULL
    policy: IngestPolicy      # AUTO | FAST | PREMIUM
    steps: tuple[PlannedStep, ...]    # one entry per backbone+conditional stage
    confidence: float
    estimated_cost_level: str
    profile: DocumentProfile | None
    requires_vision: bool = False
    requires_premium_llm: bool = False
    warnings: tuple[str, ...] = ()
```

Each `PlannedStep` carries:
- `name` (`compile` / `enrich` / `graph` / `index`)
- `enabled` (bool)
- `required` (bool — failure causes run failure)
- `decision` (`RUN` / `SKIP`)
- `source` (`CALLER` / `PLANNER` / `DEFAULT` / `POLICY`)
- `reason`
- `risk_level`, `estimated_cost_tier`, `llm_class`, `expected_engine`, `expected_provider`
- `metadata` (free-form per-step extras)

**Caller-wins rule**: when the upload supplied `graph_builder_kind` / `enricher_kind`, the corresponding step is created with `source=CALLER, enabled=True, required=True`. The post-compile overlay can never flip these back to `enabled=False`.

**Skip-with-reason invariant**: every disabled step carries a non-empty `reason`. The FE's Execution Plan tab renders this verbatim.

**Plan revision audit**: the post-compile planning activity emits `plan.revised` audit events with a `diff` describing which steps' enabled flags changed.

## 14. Stage optionality policy

| Stage | Class | Required-policy |
|---|---|---|
| `create_run` / `persist_original_document` / `resolve_config` | Mandatory backbone | Always `required=True`; failure = `failed`. |
| `document_profile` | Mandatory backbone | Light-mode allowed; produces `DocumentProfile` from extension/MIME/size/early-page heuristics. Failure = `failed`. |
| `compile` / `parse_or_compile` | Mandatory backbone | `required=True`. Failure = `failed`. May be marked `synthetic` when RAGAnything's `process_document_complete` collapses parse+chunk into one call (legacy `complete` mode). |
| `content_inventory` | Mandatory backbone, may be synthetic | Today: synthetic step wrapping the `parsed_content_manifest` artifact compile produced. Marked via `metadata.synthetic=True, metadata.synthesised_from="compile"`. |
| `execution_plan` | Mandatory backbone | Implicit until split mode landed; now has its own `planning_result` artifact + `plan.generated`/`plan.revised` audit events. |
| `generate_knowledge_chunks` | Mandatory backbone | Real activity in split mode (`insert_content`); synthetic step in complete mode. New rule: COMPLETED requires a `chunk` artifact. |
| `persist_or_index_chunks` | Mandatory backbone (when indexer wired) | `index` step. New rule (already in `_validate_completion`): `indexer_kind` set + artifacts produced ⇒ `index` step must have run. |
| `artifact_manifest` | Mandatory backbone | The `parsed_content_manifest` artifact serves this role today. |
| `final_validation` | Mandatory backbone | Now persists a `validation_report` artifact (this commit). |
| `final_summary` | Mandatory backbone | Now persists a `final_summary` artifact at terminal state (this commit). |
| `enrich` | Quality enhancement | Skipped explicitly when text-only profile + no enricher_kind. Required only when caller-supplied. |
| `graph` | Conditional capability | Skipped unless caller supplied `graph_builder_kind` or planner enabled it. New rule: COMPLETED requires `graph_json` artifact. |
| `vision_enrich` | Quality enhancement | Folded into composite enricher; per-modality flag. |

## 15. Graph / enrich / vision decision rules

The deterministic planner's decision matrix lives in [`src/j1/processing/planning.py::DefaultIngestPlanner`](../src/j1/processing/planning.py). Highlights:

**Enrichment**: enabled when ANY of:
- `parse_quality_score < 0.7`
- `text_sufficiency_score < 0.7`
- `image_count > 0` AND `J1_ENRICH_IMAGES=true`
- `table_count > 0` AND `J1_ENRICH_TABLES=true`
- domain pack requires it (e.g. `civil_engineering`)
- caller supplied `enricher_kind`

Skipped when text-only profile + no caller override + no domain trigger.

**Graph**: enabled when ANY of:
- caller supplied `graph_builder_kind`
- mode is `MULTIMODAL_FULL` AND document has structural signals (`text_block_count > 8` or `entity_count > 0`)
- profile is `civil_engineering` / `software_development` (relationship-heavy)
- post-compile observed signals lift the doc into "graph candidate" tier

Skipped when `mode=CHUNKS_ONLY`, single-page doc, no relationship signals.

**Vision**: tied to enrichment + per-modality flags. Triggered when image/diagram blocks present in manifest AND `J1_ENRICH_IMAGES` (or per-modality equivalent) is true.

The planner's full decision-tree is unit-tested in `tests/test_planning.py` + `tests/test_planning_activity.py` + `tests/test_domain_planning_integration.py`.

## 16. RAGAnything adapter boundary

The bridge ([`src/j1/providers/raganything/_bridge.py`](../src/j1/providers/raganything/_bridge.py)) wraps the vendor library. J1 core never imports `raganything` directly — only through this bridge.

**Capability methods** the bridge exposes to J1:

| Method | What | Used by |
|---|---|---|
| `default_compile(request)` | Top-level compile dispatch — routes to `default_parse_source` (split mode) or `_default_compile_complete` (legacy). | `RAGAnythingCompiler.compile` |
| `default_parse_source(request)` | Pure parse — calls `RAGAnything.parse_document()`, persists `parsed_source` + `parsed_content_manifest` + `compiled.text` drafts. NO chunk/graph side effects. | Split mode only |
| `default_insert_content(request, content_list, doc_id, source_filename)` | Pure chunk generation — calls `RAGAnything.insert_content_list()`, persists `chunk` drafts (filtered by `full_doc_id`). | Split mode only |
| `_default_compile_complete(request)` | Legacy single-shot — calls `RAGAnything.process_document_complete()`. Produces parse+chunk+graph artifacts together. Marked with `metadata.parse_boundary="legacy_combined"` so operators know the boundaries are synthetic. | Legacy `complete` mode |
| Graph builder + retrieval providers | Separate `RAGAnythingGraphBuilder` / `RAGAnythingRetrieval` classes — instantiated only when `J1_DEFAULT_GRAPH_PROVIDER=raganything` / `J1_DEFAULT_RETRIEVAL_PROVIDER=raganything`. | `bootstrap.py` selection |

**No implicit side effects**: Split mode is the recommended (and currently default-via-loader) configuration. In split mode, `parse_document` does NOT touch LightRAG storage — chunks + graph are produced ONLY when the workflow's `insert_content` activity fires after the planner approves the chunks step. Graph is produced ONLY when the workflow runs the `build_graph` activity, which only fires when `IngestPlan` enables it.

In complete mode, `process_document_complete` DOES combine parse + chunk + graph internally. The bridge marks this honestly via metadata so the FE can render the synthetic boundary correctly. **Operators running graph-required workflows should use split mode**; complete mode is for legacy compatibility.

## 17. Temporal stage / checkpoint design (current + future)

**Current state** ([`src/j1/orchestration/workflows/project_processing.py`](../src/j1/orchestration/workflows/project_processing.py)):
- One workflow per document (per per-document upload) OR per project bulk job.
- Each backbone stage is its own Temporal Activity — `compile`, `insert_content`, `build_planning_result`, `enrich`, `build_graph`, `index`, `finalize`.
- `_record_step` writes a per-stage `StepResult` into the workflow's in-memory state, surfaced via `get_status` query AND persisted to `IngestionRunStore.metadata.step_results` so failed runs can still report which stage failed.
- Continue-as-new boundary at configurable `continue_as_new_after_documents` / `history_event_threshold` for bulk jobs.
- Activity retries follow `DEFAULT_RETRY` (5 attempts) or `COMPILE_RETRY` (2 attempts) — non-retryable error types from `_NON_RETRYABLE_ERROR_TYPES` short-circuit retries.

**Stage-checkpoint guarantee**:
- Each activity persists its output via `_handle_artifact_output` BEFORE returning success. Artifact registration is a single SQLite-backed write — atomic from the workflow's perspective.
- Activity-level retry can re-run a stage; idempotency is at the artifact-id level (each retry creates a new artifact_id, never overwrites). LightRAG's internal storage IS overwritten — the bridge clears the doc_id from `kv_store_doc_status.json` BEFORE insert specifically to make retries safe.

**What's NOT yet a child workflow**: each stage is a single Activity, not a Child Workflow. Promoting them to child workflows would buy stronger compensation semantics (and per-stage Temporal queries) but isn't required for the current visibility / validation guarantees. Tracked as a follow-up.

**Resume model** (deferred to next iteration; documented in [`docs/ingestion-operations.md`](./ingestion-operations.md)):
- New workflow attempt linked via `original_run_id` metadata.
- Loads the original run's `step_results` and `_produced_artifact_ids`.
- Skips stages whose outputs are still valid under the current plan.
- Rejects compatibility-incompatible resumes (model/profile/embedding changed) with an explicit error pointing the operator at full re-index.

## 18. Operational actions (UI/API surface)

See [`docs/ingestion-operations.md`](./ingestion-operations.md) for the full API + behaviour spec. Summary:

| Action | Endpoint | Status |
|---|---|---|
| Cancel running ingest | `POST /ingestion-runs/{id}/cancel` | ✓ implemented (signal to running workflow) |
| **Soft-delete ingest** | `DELETE /ingestion-runs/{id}` | **✓ implemented** (run + artifacts tombstoned; resolver excludes; idempotent) |
| **Full re-index** | `POST /ingestion-runs/{id}/full-reindex` | **✓ implemented** (new run + new workflow_id `…-reindex-{run_id}`; tags `metadata.reindex_of`) |
| **Multi-upload batch** | `POST /ingestion-batches` | **✓ implemented** (`J1_INGESTION_BATCH_MAX_FILES=5` default; per-doc workflows via existing starter; status derived at read-time) |
| **Get batch detail** | `GET /ingestion-batches/{id}` | **✓ implemented** (per-file run rows + aggregate status) |
| Resume from last checkpoint | `POST /ingestion-runs/{id}/resume-from-checkpoint` | ⏳ designed, not yet implemented (needs compatibility checker for model/profile drift) |
| Rebuild index only | `POST /ingestion-runs/{id}/rebuild-index` | ⏳ designed, not yet implemented |

Each deferred endpoint has a stable contract documented in `ingestion-operations.md` so the next implementation iteration doesn't need to re-discover the design.

## 19. Settings + storage governance

- **Source of truth**: `J1_INGEST_PLAN_MODE` for planner; `J1_RAGANYTHING_PIPELINE_MODE` for compile path; `J1_DEFAULT_*` for adapter selection.
- **Legacy / deprecated**: `J1_LLM_PLANNING_ENABLED` (overridden by `J1_INGEST_PLAN_MODE`).
- **Multi-upload concurrency knob (planned)**: `J1_INGESTION_BATCH_CONCURRENCY` — default `1` (sequential). Not yet wired since the batch model itself is deferred.
- **Cleanup boundary**: only `_register_draft`'s DB-write rollback unlinks files. No stage cleans up data on failure. Deletion (when implemented) is the only API-driven cleanup path.

## 20. Manual acceptance checklist

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
