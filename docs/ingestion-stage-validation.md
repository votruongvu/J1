# Stage Output Validation / Quality Gates

Status: **partial — 4 of 13 stages wired** (compile, generate_chunks, enrich, graph + final-validation aggregator). The remaining 9 stages (`persist_original_document`, `resolve_config`, `document_profile`, `parse_or_compile`, `content_inventory`, `execution_plan`, `persist_or_index_chunks`, `artifact_manifest`, `final_summary`) follow the same contract documented here and are queued for follow-up sessions.

## Core rule

> Never mark a stage `succeeded` just because a function returned successfully.

A stage is `succeeded` only when:

1. Stage execution completed.
2. Required output exists.
3. Output is persisted.
4. Output can be **read back** (file exists on disk + non-zero bytes + parses).
5. Output passes quality checks.
6. Output has correct tenant/project/workspace/run/document scope.
7. Output links to the correct upstream input.
8. `stage_validation_report` artifact is saved.
9. Workflow's `_record_step(COMPLETED)` runs only after the gate passes.

## Contract

### `StageValidationResult` ([src/j1/processing/stage_validation.py](../src/j1/processing/stage_validation.py))

```python
StageValidationResult(
  stage_name: str,            # one of STAGE_COMPILE / GENERATE_CHUNKS / ENRICH / GRAPH
  run_id: str,
  document_id: str | None,
  tenant_id: str,
  project_id: str,
  workspace_id: str | None,
  attempt: int,
  validation_status: str,     # passed | warning | failed
  checks: list[StageValidationCheck],
  errors: list[str],
  warnings: list[str],
  output_refs: list[str],     # artifact ids the stage produced
  artifact_refs: list[str],   # artifact ids the validator read back
  validator_version: str,
)
```

`passed()` returns True for both `passed` and `warning` — only `failed` blocks the COMPLETED transition. Warnings are surfaced for operator attention but don't fail the stage.

### `StageValidationCheck`

```python
StageValidationCheck(
  name: str,                  # snake_case rule id, e.g. chunk_count_positive
  status: str,                # passed | warning | failed
  message: str | None,        # one-line operational explanation
)
```

### `aggregate_status(checks)` rules

- any `failed` → `failed`
- else any `warning` → `warning`
- else `passed`

## Safe stage write order

The workflow MUST execute every durable stage in this order:

```
1. Execute stage activity
2. (activity persists output + writes artifact)
3. Workflow invokes validate_stage activity
4. validate_stage:
   a. reads back each artifact's bytes
   b. runs the stage's validator
   c. persists stage_validation_report artifact
   d. returns StageValidationActivityResult
5. If result.passed → workflow records COMPLETED + add stage to _validated_stages
6. If not result.passed → workflow records FAILED + raises _BusinessRejection
7. Continue to next stage only when 5. ran
```

The persist-validation-report-AS-an-artifact step (4c) is non-negotiable: the report's existence is what `_validate_completion` later checks. Failing to persist the report demotes the stage to `failed` even if every check passed.

## Per-stage required outputs and quality checks

### compile

**Required:**
- ≥1 artifact registered.
- At least one of `parsed_source` / `parsed_content_manifest` / `chunk` kinds present (canonical kinds — without one, downstream stages have no input).
- Each artifact's file readable + non-zero bytes.
- Scope matches: tenant, project, run_id (via `metadata.run_id`), document_id (via `source_document_ids`).

**Warnings:**
- No `parsed_content_manifest` present → Content Inventory tab will be unavailable.

**Failures:**
- Zero artifacts produced.
- No canonical kinds present.
- Any artifact unreadable / zero-bytes / wrong scope.

### generate_knowledge_chunks (split mode)

**Required:**
- ≥1 chunk artifact registered.
- Each chunk artifact readable, parses as JSON or NDJSON.
- Total chunk count > 0.
- Chunk ids unique across the run (auto-synthesised as `{artifact_id}#{index}` when entries omit explicit ids).
- Scope matches.

**Warnings:**
- Some chunks have empty body/content/text (vs all empty).
- One chunk holds >90% of total body bytes (chunking misconfigured).

**Failures:**
- Zero chunk artifacts.
- Any chunk artifact unreadable / parse error / parses to empty list.
- Every chunk has empty body/content/text.
- Duplicate chunk ids.

### enrich

**When `enrich_required=True`:**
- ≥1 `enriched.*` artifact present.
- Each enriched artifact readable + scope-correct.
- Each enriched artifact has non-empty `source_artifact_ids` (links to upstream chunks).

**When `enrich_required=False`:**
- No enriched artifacts present (warning if any leaked through).

**Failures (when required):**
- Zero enriched artifacts.
- Orphaned enriched artifact (no upstream link).

### graph

**When `graph_required=True`:**
- 1 `graph_json` artifact present.
- Artifact readable, parses as JSON object with `nodes` + `edges` arrays.
- Node count > 0.
- Every edge's `source` and `target` reference existing node ids.
- If artifact carries `source_artifact_ids`, every id matches one of the run's chunk artifact ids (graph grounded in the run).

**When `graph_required=False`:**
- No graph_json artifacts present (warning if any leaked through).

**Failures (when required):**
- No graph_json artifact.
- Empty nodes list.
- Dangling edges (referencing missing nodes).
- Graph references chunks that don't belong to this run.

### final_validation (aggregator)

Run inside `_validate_completion` at the COMPLETED transition. Rules:

1. `at_least_one_artifact_produced` — `_produced_artifact_ids` non-empty.
2. `required_steps_completed_or_skipped` — every required step ended COMPLETED or SKIPPED.
3. `indexer_kind_set_implies_index_step_ran` — if `indexer_kind` was passed AND artifacts exist, an `index` step must be recorded.
4. `graph_step_completed_implies_graph_json_artifact` — graph step COMPLETED requires graph_json artifact.
5. `chunks_step_completed_implies_chunk_artifact` — chunks step COMPLETED (non-synthetic) requires chunk artifact.
6. **`every_durable_completed_stage_has_validation_report`** — every COMPLETED durable stage (compile / generate_knowledge_chunks / enrich / graph) MUST have an entry in `_validated_stages`. Synthetic stages and runs without `correlation_id` are exempt.

Any rule failure raises `_BusinessRejection` → workflow terminates `FAILED_FINAL`. The error list is also persisted in the run-level `validation_report` artifact.

## Failure behaviour

When a stage's validation fails:

1. Workflow records `_record_step(step=..., status=FAILED)` with the validator's error messages in `reason` + `error.message`.
2. Workflow raises `_BusinessRejection`. Temporal sees the workflow as **Failed**, not Completed.
3. The `_BusinessRejection` handler:
   - persists `error_report` artifact (failure detail)
   - persists `final_summary` artifact (final_status="failed")
   - persists `validation_report` artifact (the run-level aggregate)
   - emits `j1.runs.report_terminal` activity
4. Per-stage `stage_validation_report` artifacts from earlier stages STAY visible — operators inspect them to triage which checks failed.
5. Downstream dependent stages do NOT run (the `raise` short-circuits the workflow).
6. The run record's `status` flips to `FAILED` via the standard terminal-transition path.

## Manual inspection

Per-stage validation reports live alongside other artifacts. To inspect:

```bash
# List every stage_validation_report for one run
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<runId>/artifacts?kind=stage_validation_report" \
  | jq '.data.items'

# Read one report
curl -s "http://localhost:8000/api/projects/<project>/ingestion-runs/<runId>/artifacts/<artifactId>/content" \
  | jq '.'

# Filename pattern: stage_validation_{stage}_{runId}_{attempt}.json
```

The report payload carries the full `StageValidationResult.to_payload()` shape — every check's name + status + message, the full errors/warnings lists, and the artifact_refs the validator inspected.

For audit-log queries by stage validation outcome:

```bash
# All runs whose graph validation failed
grep '"action": "j1.processing.validate_stage"' \
     audit/events.jsonl \
  | jq 'select(.payload.stage_name == "graph" and .payload.validation_status == "failed")'
```

## Acceptance criteria

| Criterion | Status |
|---|---|
| Every durable stage has explicit validation | ✅ for compile / generate_chunks / enrich / graph |
| Stage success requires persisted, readable, validated output | ✅ workflow gate enforces; aggregator double-checks |
| Missing/empty/invalid/unreadable output prevents success | ✅ via per-stage validators |
| Chunks cannot succeed with zero chunks | ✅ `chunk_count_positive` check |
| Graph cannot succeed without valid graph when `graph_required=true` | ✅ `graph_node_count_positive` + `graph_edges_reference_nodes` |
| Content Inventory cannot succeed when empty/unreadable | 🟡 surfaces as compile warning today; dedicated stage validator queued |
| Execution Plan cannot succeed when contradictory/incomplete | ⏳ queued — planning has its own validation today; doesn't yet emit `stage_validation_report` |
| Final validation aggregates stage validation reports | ✅ `every_durable_completed_stage_has_validation_report` rule |
| Completed status is impossible if required validation failed | ✅ workflow raises `_BusinessRejection` before COMPLETED transition |
| Validation reports are persisted and inspectable | ✅ `stage_validation_report` artifact kind |

## Tests

- [tests/test_stage_validation.py](../tests/test_stage_validation.py) — 27 tests covering the contract + every per-stage validator (zero outputs, unreadable, scope mismatch, duplicate ids, empty bodies, dangling edges, ungrounded graph, happy paths, skipped stages).
- [tests/test_project_processing_workflow.py](../tests/test_project_processing_workflow.py) — `test_stage_validation_failure_blocks_completed_status`, `test_aggregator_blocks_succeeded_when_durable_stage_skips_validation`.

## Adding a new stage validator

1. Add the per-stage check function to [`stage_validators.py`](../src/j1/processing/stage_validators.py). Take the artifacts the stage produced + a `read_back` callable + scope context; return `list[StageValidationCheck]`.
2. Wire it into the `validate_stage` activity's stage dispatch ([`activities/processing.py`](../src/j1/orchestration/activities/processing.py)).
3. Add the workflow gate at the stage's success site: `await self._validate_stage_output(...)` → branch on `result.passed` → record COMPLETED or FAILED.
4. Add the stage name to `_DURABLE_STAGES` in `_validate_completion` so the aggregator catches a missing report.
5. Add unit tests in `tests/test_stage_validation.py` covering the new validator's failure modes + happy path.
6. Document the new stage's required outputs / quality checks / failure modes in this file.
