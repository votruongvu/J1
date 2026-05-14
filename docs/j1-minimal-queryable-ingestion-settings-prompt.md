# Claude Prompt: Minimal Queryable Ingestion Mode with Env Hard Overrides

## Objective

Please add / review runtime settings so J1 can run ingestion in the most minimal mode possible while still allowing the document to be queried.

We need a **Minimal Queryable Mode** for debugging ingestion latency. In this mode, ingestion should do the minimum required work to make a document usable for query. Expensive enrichment, validation, rich graph augmentation, and diagnostic extras should be disabled or deferred.

## Current Problem

Current documented ingestion flow:

```text
profile → assess → compile → enrich → graph → index → finalize
```

Important context:

- Compile uses RAGAnything as a black box.
- Inside compile, RAGAnything/MinerU may already parse the document, split chunks, embed content, build the per-document LightRAG workspace, and produce chunk + graph artifacts.
- Therefore, post-compile graph/index/enrichment stages may be adding significant latency or duplicating work.
- Re-index must still start from the raw uploaded file and must not reuse old run data.
- Do not reintroduce split mode.
- The document must still become queryable after successful minimal ingestion.

## Important Control Rule

The **Assessment Plan may recommend capabilities/tasks**, but **environment settings must be the final hard override**.

If an env setting disables a capability, J1 must skip that capability even if the Assessment Plan marks it as required.

Example:

```text
Assessment Plan says image enrichment is required.
J1_IMAGE_ENRICHMENT_ENABLED=false.
Result: image enrichment must be skipped.
```

The run should continue if the skipped capability is not required for basic queryability.

Do not silently ignore the mismatch between Assessment Plan and env override. Record a clear warning in run metadata, final summary, and/or performance trace, for example:

```text
image_enrichment_skipped_by_env_override
```

## Implementation Direction

Please inspect existing settings first.

If a setting already exists:

- Reuse it.
- Make sure it is wired correctly.
- Make sure it fully gates the target stage.
- If it exists but defaults to expensive behavior, change the dev/default config to the minimal setting.

Only add a new setting when there is no existing clean switch.

## Required Result

Create or confirm a minimal ingestion configuration that can be enabled from `.env` / `.env.example`.

## Suggested Settings to Review or Add

### 1. `J1_INGESTION_MODE`

Values:

- `full`: current complete pipeline.
- `minimal_queryable`: minimum work needed to query.

Rules:

- Default for local/dev should be `minimal_queryable`.
- Production can keep `full` if needed.
- In `minimal_queryable`, all optional expensive stages should default to disabled unless explicitly enabled.

### 2. `J1_ENRICHMENT_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Skip post-compile enrichment planning.
- Skip all enrichment activities even if Assessment Plan recommends them.
- This includes text enrichment, metadata extraction, table interpretation, image captioning, validation, and classification unless they have separate more specific toggles.

If there is already an enrichment toggle, reuse it and make sure it fully gates the enrichment stage.

### 3. `J1_FAST_LLM_CONSULT_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Do not run the fast LLM consult for enrichment planning.
- If enrichment is disabled, this must also be skipped automatically.

### 4. `J1_IMAGE_ENRICHMENT_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Do not run image captioning / vision LLM enrichment even if Assessment Plan says images are important.
- Keep RAGAnything/MinerU compile behavior unchanged unless compile truly supports safe image-processing toggles.
- If skipped due to env override, record a warning.

### 5. `J1_TABLE_ENRICHMENT_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Do not run LLM table interpretation after compile even if Assessment Plan says tables are important.
- Do not block basic queryability on table enrichment.
- If skipped due to env override, record a warning.

### 6. `J1_CLASSIFICATION_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Skip document classification if it is not required for query.
- If Assessment Plan recommends classification, skip and record a warning.

### 7. `J1_VALIDATION_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Skip imported/manual validation execution during ingestion.
- Validation should be a user-triggered action, not part of the blocking ingestion path.

### 8. `J1_POST_COMPILE_GRAPH_BUILD_ENABLED`

Default in minimal mode: `false` unless proven required for query.

This is a hard override.

Please investigate:

- Whether RAGAnything compile already creates the LightRAG graph/workspace required for query.
- Whether J1's separate post-compile `build_graph` stage duplicates RAGAnything output.

When false:

- Do not run J1's separate post-compile `build_graph` in minimal mode if compile already produces the queryable graph/workspace.
- If Assessment Plan or domain config recommends graph augmentation but env is false, skip it and record a warning.
- If this stage is required only for diagnostics or enriched artifacts, skip it in minimal mode.

### 9. `J1_POSTGRES_FTS_INDEX_ENABLED`

Default in minimal mode: only enable if query currently requires it.

This is a hard override.

Please investigate:

- Whether query uses LightRAG native retrieval, Postgres FTS, or both.
- Whether Postgres FTS is required for citations/evidence or only used for diagnostics/validation.

Rules:

- If Postgres FTS is only used for evidence/debug/validation, allow it to be disabled in minimal mode.
- If it is required for query citations, keep a minimal version that indexes only compile chunks, not enriched artifacts.

### 10. `J1_COMPILE_RETRY_ENABLED`

Default in minimal mode: `false` or `true` with max attempts = `1`.

This is a hard override.

Rules:

- Avoid rerunning full MinerU/RAGAnything compile unless there is a clear transient failure.
- Do not retry just because quality is imperfect.
- If existing retry knobs exist, ensure minimal mode sets them to no retry or one attempt.

### 11. `J1_FINAL_DETAILED_REPORT_ENABLED`

Default in minimal mode: `false`.

This is a hard override.

When false:

- Keep only a minimal final summary/status.
- Do not generate heavy final reports if they require scanning all artifacts or extra processing.

### 12. `J1_INGEST_PERFORMANCE_TRACE_ENABLED`

Default:

- `true` in dev.
- `false` or configurable in production.

Add stage timing logs for:

- profile duration
- assess duration
- compile duration
- enrichment duration / skipped
- graph build duration / skipped
- index duration / skipped
- finalize duration
- total ingestion duration

Include counts where available:

- page count
- chunk count
- artifact count
- image count
- table count
- LLM call count
- vision LLM call count
- embedding count
- compile retry count
- index row count
- env override skip count

## Env Override Semantics

Implement a central helper if possible, for example:

```text
RuntimeCapabilityPolicy
```

It should combine:

- ingestion mode
- env settings
- Assessment Plan recommendation
- domain policy recommendation
- queryability requirement

Final decision order:

### 1. Hard env override

If env says `false`, skip.

No downstream stage may re-enable it.

Record a warning if Assessment Plan or domain policy requested it.

### 2. Queryability requirement

If the capability is truly required for the document to become queryable, it may remain enabled.

This must be explicitly documented.

Do not mark enrichment, classification, validation, image captioning, or rich report generation as queryability-required unless proven.

### 3. Assessment Plan / domain recommendation

Assessment Plan and domain policy may enable optional capabilities only when env allows them.

### 4. Default by ingestion mode

- `full`: enable normal complete behavior.
- `minimal_queryable`: disable optional expensive behavior.

## Minimal Mode Behavior

When `J1_INGESTION_MODE=minimal_queryable`:

### Required

- upload/register document
- allocate target snapshot
- deterministic profile if cheap
- assessment only if cheap and no LLM
- RAGAnything compile
- register compile artifacts with `snapshot_id`
- create only the minimum index/visibility required for query
- promote snapshot on success
- write minimal final summary

### Skipped by Default

- post-compile enrichment plan
- all enrichment activities
- fast LLM consult
- image captioning
- table interpretation
- classification
- validation
- generated/imported test execution
- rich post-compile graph augmentation if not required for query
- detailed final ingestion report if expensive
- non-essential diagnostic artifact generation
- compile retry beyond one attempt unless explicitly enabled

## Required Warning Behavior

When env disables something that Assessment Plan recommended, record structured warnings such as:

```text
enrichment_skipped_by_env_override
image_enrichment_skipped_by_env_override
table_enrichment_skipped_by_env_override
classification_skipped_by_env_override
validation_skipped_by_env_override
post_compile_graph_skipped_by_env_override
postgres_fts_index_skipped_by_env_override
fast_llm_consult_skipped_by_env_override
```

Each warning should include:

- capability name
- env setting name
- env value
- assessment/domain recommendation
- reason
- whether queryability is affected

## `.env.example` Section

Add or update this section in `.env.example`:

```env
# -----------------------------------------------------------------------------
# Ingestion Runtime / Minimal Queryable Mode
# -----------------------------------------------------------------------------
# full = run complete ingestion with enrichment, graph/index extras, reports.
# minimal_queryable = run only what is required to make the document queryable.
J1_INGESTION_MODE=minimal_queryable

# Hard override. If false, skip enrichment even if Assessment Plan recommends it.
J1_ENRICHMENT_ENABLED=false

# Hard override. If false, skip fast LLM consult even if enrichment planning wants it.
J1_FAST_LLM_CONSULT_ENABLED=false

# Hard override. If false, skip image/vision enrichment after compile.
J1_IMAGE_ENRICHMENT_ENABLED=false

# Hard override. If false, skip LLM table interpretation after compile.
J1_TABLE_ENRICHMENT_ENABLED=false

# Hard override. If false, skip document classification during ingestion.
J1_CLASSIFICATION_ENABLED=false

# Hard override. If false, validation must be manually triggered outside ingestion.
J1_VALIDATION_ENABLED=false

# Hard override. If false, skip extra post-compile graph build unless proven required for query.
J1_POST_COMPILE_GRAPH_BUILD_ENABLED=false

# Hard override. Keep true only if query/citation requires Postgres FTS.
J1_POSTGRES_FTS_INDEX_ENABLED=true

# Hard override. Avoid expensive full compile retries during latency debugging.
J1_COMPILE_RETRY_ENABLED=false
J1_COMPILE_RETRY_MAX_ATTEMPTS=1

# Hard override. Keep final output lightweight.
J1_FINAL_DETAILED_REPORT_ENABLED=false

# Enable stage timing and skip-reason logs while debugging slow ingestion.
J1_INGEST_PERFORMANCE_TRACE_ENABLED=true
```

## Acceptance Criteria

1. With minimal mode enabled, a small PDF can be uploaded, ingested, promoted, and queried.
2. No enrichment activity is dispatched when `J1_ENRICHMENT_ENABLED=false`, even if Assessment Plan recommends enrichment.
3. No fast LLM consult is called when `J1_FAST_LLM_CONSULT_ENABLED=false`.
4. No vision/image captioning LLM is called when `J1_IMAGE_ENRICHMENT_ENABLED=false`.
5. No table interpretation LLM is called when `J1_TABLE_ENRICHMENT_ENABLED=false`.
6. No validation/test-case execution runs during ingestion when `J1_VALIDATION_ENABLED=false`.
7. Compile runs only once when compile retry is disabled or max attempts is 1.
8. Logs clearly show which stages were skipped because of minimal mode and which were skipped specifically because of env override.
9. Final run status clearly says the document is queryable but enrichment/optional capabilities were skipped by runtime configuration.
10. `.env.example` documents every setting clearly, grouped under an `Ingestion Runtime / Minimal Mode` section.
11. Remove or avoid dead legacy switches. Do not add duplicate settings if an existing one already controls the same behavior.

## Please Return

After implementation, return a concise report containing:

- list of existing settings reused
- list of new settings added
- exact default values for dev
- code paths changed
- proof from logs/tests that minimal mode produces a queryable document
- proof that env `false` overrides Assessment Plan required/recommended capabilities
- remaining stages that still consume significant time

## Core Rule to Preserve

Assessment Plan is advisory. Runtime env settings are authoritative.

If env says `false`, the capability must not run.
