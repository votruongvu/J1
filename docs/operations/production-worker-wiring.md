# Production worker wiring runbook

This runbook is the deployment-side checklist for wiring the
ingestion enrichment pipeline. The architecture lives in
[`docs/architecture/`](../architecture/); this page is the recipe
for stitching it together when standing up a new worker (staging,
prod, single-tenant deploy, …).

## Current state

- `deploy/dev/_wiring.py` + `deploy/dev/worker.py` are the only
  existing entrypoints. They follow the documented pattern in full.
- **No staging / prod entrypoint exists yet.** Future deployment
  artefacts (helm chart, docker image, terraform module, …) must
  follow the shape below.
- The development worker is the reference implementation. Treat any
  staging/prod variant as a port of `deploy/dev/_wiring.py` with
  deployment-specific secrets + endpoints, not a new design.

## The wiring shape

```
┌──────────────────────────────────────────────────────────────────┐
│ 1. bootstrap_from_env()                                          │
│    → BootstrapResult{ llm_registry, llm_call_limiter,            │
│                       compilers, graph_builders,                 │
│                       enrichment_concurrency_settings }          │
├──────────────────────────────────────────────────────────────────┤
│ 2. Resolve clients from boot.llm_registry                        │
│    text_client     = registry.try_text()                         │
│    vision_client   = registry.try_vision()                       │
│    embedding_cl    = registry.try_embedding()                    │
│    (any may be None — fresh deploys without credentials)         │
├──────────────────────────────────────────────────────────────────┤
│ 3. Wrap text client (optional, explicit)                         │
│    text_adapter = TextLLMClientAdapter(text_client)              │
│    Vision client passes through RAW — adapter is per-run.        │
├──────────────────────────────────────────────────────────────────┤
│ 4. ProcessingActivities(...)                                     │
│      processing=..., sources=..., artifacts=...,                 │
│      enrichment_text_client=text_adapter,                        │
│      enrichment_vision_client=vision_client,    # raw            │
│      enrichment_llm_call_limiter=boot.llm_call_limiter           │
├──────────────────────────────────────────────────────────────────┤
│ 5. Activity-side per run (inside run_enrichment_stage):          │
│      provider = WorkspaceImageBytesProvider(                     │
│          artifact_registry, workspace, ctx, document_id, run_id) │
│      vision_adapter = PerImageVisionAdapter(                     │
│          raw_vision_client,                                      │
│          image_provider=provider,                                │
│          llm_call_limiter=self._enrichment_llm_call_limiter)     │
│      modules = build_legacy_enricher_modules(                    │
│          text_client=enrichment_text_client,                     │
│          vision_client=vision_adapter,                           │
│          llm_call_limiter=self._enrichment_llm_call_limiter)     │
├──────────────────────────────────────────────────────────────────┤
│ 6. Runtime: each per-image vision call is individually limiter-  │
│    bounded (Wave 11B). Text / classification / table modules     │
│    are bounded one-call-per-module-invocation.                   │
└──────────────────────────────────────────────────────────────────┘
```

## Step-by-step

### 1. Bootstrap

Bootstrap reads typed settings from env vars and returns the
`BootstrapResult`:

```python
from j1.compose.bootstrap import bootstrap_from_env

boot = bootstrap_from_env()
# boot.llm_registry           — LLMProviderRegistry
# boot.llm_call_limiter       — LLMCallLimiter | None
# boot.compilers              — Mapping[str, KnowledgeCompiler]
# boot.enrichment             — EnrichmentSettings (modality flags)
# boot.enrichment_concurrency_settings  — EnrichmentConcurrencySettings | None
```

### 2. Resolve LLM clients

The registry exposes `try_*()` accessors that return `None` when
the role isn't configured:

```python
text_client      = boot.llm_registry.try_text()       # TextLLMClient | None
vision_client    = boot.llm_registry.try_vision()     # VisionLLMClient | None
embedding_client = boot.llm_registry.try_embedding()  # EmbeddingClient | None
fast_client      = boot.llm_registry.try_fast()       # TextLLMClient | None
```

A `None` here is **not a failure** — it's the documented
"no LLM credentials configured" path. The matching enrichment
adapters will SKIP at runtime with the operator-readable reason
`"no text LLM client configured"` / `"no vision LLM client
configured"`.

### 3. Wrap the text client (optional, explicit)

The production `TextLLMClient` already structurally matches the
`TextAnalysisClient` Protocol; wrapping is optional but makes the
dependency arrow visible:

```python
from j1.processing.enrichment_clients import TextLLMClientAdapter

enrichment_text_client = (
    TextLLMClientAdapter(text_client) if text_client else None
)
```

The vision client **does not** get wrapped at this stage. The
activity wraps it per-run with the workspace-aware image-bytes
provider (see step 5).

### 4. Construct `ProcessingActivities`

Thread the three enrichment kwargs through:

```python
from j1.orchestration.activities.processing import ProcessingActivities

activity = ProcessingActivities(
    processing=processing_service,
    sources=source_registry,
    artifacts=artifact_registry,
    compilers=dict(boot.compilers),
    enrichers=dict(resolved_enrichers),
    graph_builders=dict(boot.graph_builders),
    indexers={SqliteSearchIndexer.kind: indexer, **(indexers or {})},
    query_providers=dict(boot.retrieval_providers),
    progress_reporter=progress_reporter,
    result_cache=JsonlProcessingResultCache(workspace),
    # ↓ Wave 10.6 + 11A + 11B
    enrichment_text_client=enrichment_text_client,        # adapter (or None)
    enrichment_vision_client=vision_client,                # RAW client
    enrichment_llm_call_limiter=boot.llm_call_limiter,    # shared limiter
)
```

### 5. Activity-side per-run construction

The activity's `run_enrichment_stage` method does the rest. **No
deployment-side work** required here — this is documented for the
audit trail. The activity:

- Builds `WorkspaceImageBytesProvider(artifact_registry, workspace,
  ctx, document_id, run_id)` so the provider resolves
  `compile.image` artifacts for the current run.
- Wraps the raw vision client in `PerImageVisionAdapter(raw,
  image_provider=provider, llm_call_limiter=...)`.
- Calls `build_legacy_enricher_modules(text_client=...,
  vision_client=adapter, llm_call_limiter=...)`.
- Registers the four legacy-compatible adapter modules alongside
  the Wave-6 skeleton modules in `CompositeEnrichmentRunner`.

### 6. Backward-compatible path

If the caller (tests, alternate composition) supplies a
pre-constructed `VisionAnalysisClient` (any object with `.analyze`),
the activity uses it as-is — no re-wrap. This preserves the
Wave-10.6 test composition without forcing every test to construct
a provider.

## Configuration / env settings

| Env var | Default | Purpose |
|---|---|---|
| `J1_ENRICH_IMAGES` | `true` | Modality kill switch — disables `VisualContentDescriber` + the new `ImageEnrichmentModule` when `false` |
| `J1_ENRICH_TABLES` | `true` | Same for tables |
| `J1_ENRICH_DIAGRAMS` | `true` | Same for diagrams |
| `J1_ENRICH_SCANNED_PAGES` | `true` | Same for scanned pages |
| `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` | dev: 4; prod: per deployment | The `LLMCallLimiter` ceiling. 0 = disable limiter entirely |
| `J1_ENRICHMENT_TIMEOUT_SECONDS` | dev: 60 | Per-call timeout the limiter enforces |
| `J1_ENRICHMENT_RETRY_LIMIT` | dev: 1 | Bounded retry count inside the limiter |
| `J1_ENRICHMENT_REQUIRE_SUCCESS` | `false` | Env-default for `require_enrichment_success` (domain pack still wins) |
| `J1_DEFAULT_ENRICHMENT_MODEL_TIER` | none | `fast` / `standard` / `premium` — picks the LLM client variant when multiple are registered |
| `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS` | `true` in dev | Drops the limiter ceiling + extends timeouts for local laptops |
| `J1_TEXT_LLM_*` / `J1_VISION_LLM_*` | per role | LLM provider + model + credentials. See [`docs/providers.md`](../providers.md) |
| `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` | `false` outside dev | Opt-in to `J1IngestStage` / `J1FinalStatus` / retry-count search attributes |

## Concurrency model

Three independent layers:

| Layer | What it bounds | Where it's configured |
|---|---|---|
| **Temporal worker concurrency** | Parallel workflows / activities | Temporal worker config (`max_concurrent_activities`) |
| **Enrichment LLM call concurrency** | Parallel LLM HTTP calls across enrichment modules | `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` → `LLMCallLimiter` |
| **Per-tier semaphores** (DEFERRED) | Per-model-tier concurrency (e.g. premium ≤ N, fast ≤ M) | not implemented; one limiter spans all tiers today |

The current limiter is **shared across text + classification +
table + image** LLM paths. Per-image vision calls bound
individually (one limiter slot per image). Per-model-tier
semaphores are deferred — opening this surface needs careful work
because it interacts with the cost model + the model selector.

## Misconfigured deployments

Every misconfiguration produces an **explicit, operator-readable
skip** — never a silent failure. Concretely:

| Operator misconfiguration | Runtime symptom |
|---|---|
| No `J1_TEXT_LLM_*` env | `text_enrichment`, `classification_enrichment`, `table_enrichment` → SKIPPED with reason `"no text LLM client configured"` on every run |
| No `J1_VISION_LLM_*` env | `image_enrichment` → SKIPPED with reason `"no vision LLM client configured"` |
| `J1_ENRICH_IMAGES=false` | `enriched.visuals` enricher dropped at composite construction; `image_enrichment` still runs but adapter sees no images (provider returns empty) |
| Workspace permissions wrong on `compile.image` files | Provider emits warning `"image artifact X: bytes not loadable (PermissionError)"` on the image module's outcome warnings |
| `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS=0` | Limiter is `None`; modules call clients directly without concurrency bounding |
| Required vision but no vision client | If `require_enrichment_success=True` AND the post-compile plan REQUIRES image_enrichment, run lands at `failed_enrichment_required` |

All of these surface in `final_ingestion_report.enrichment_summary.module_outcomes[].status` + `.reason`.

## Validation

After deploying a new worker:

1. Hit `GET /capabilities` — confirms which provider kinds the API
   knows about.
2. Upload a small PDF → wait for `run.completed` event.
3. Fetch `GET /ingestion-runs/{run_id}/final-ingestion-report`:
   - Verify `final_status == "completed_with_enrichment"` (or
     `"completed_without_enrichment"` if the plan said SKIP).
   - Verify `enrichment_summary.module_outcomes` includes the 7
     module ids (3 skeletons + 4 legacy-compatible adapters).
   - Verify any SKIPPED modules carry a useful reason.
4. If you wired vision: upload a PDF with images → confirm
   `image_summaries[]` is populated + `provenance.source_artifact_id`
   points at a `compile.image` artifact.

## Related pages

- [Ingestion pipeline](../architecture/ingestion-pipeline.md)
- [Enrichment overlay](../architecture/enrichment-overlay.md)
- [Final ingestion report](../architecture/final-ingestion-report.md)
- [Configuration / environment vars](../configuration/environment.md)
- [Providers / LLM registry](../providers.md)
