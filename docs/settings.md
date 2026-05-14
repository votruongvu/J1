# Settings reference

> [Back to README](../README.md). See also
> [05-developer-onboarding.md](05-developer-onboarding.md),
> [07-deployment-and-scaling.md](07-deployment-and-scaling.md).

Every J1 setting is exposed as an environment variable with the
`J1_` prefix and a default defined in code. Settings are grouped by
feature area below. The reference is exhaustive; small per-feature
modules under `src/j1/**/settings.py` carry the defaults.

## How settings are loaded

1. **Process env.** The dev compose stack reads `.env` at the
   repository root; the local-dev workflow expects you to source
   `.env` into the shell before launching `python -m deploy.dev.api`
   or `python -m deploy.dev.worker`.
2. **Typed loaders.** Each feature area has a small Python loader
   (e.g. `j1.config.runtime.load_runtime_config`,
   `j1.llm.settings.load_llm_settings`,
   `j1.providers.raganything.settings.load_raganything_settings`).
   Loaders return a frozen `dataclass` and raise `ConfigError` on
   misconfiguration *at startup* rather than mid-pipeline.
3. **`load_runtime_config().validate()`** runs at process start.
   In `J1_RUNTIME_PROFILE=prod` mode it refuses dev fallbacks
   (sqlite-local, local-fs artifacts, in-memory cache).

## Where defaults live

| Group | Module |
| --- | --- |
| Runtime profile, providers (metadata / artifact / cache / vector / graph / evidence / rag), concurrency, benchmark, cleanup | `src/j1/config/runtime.py` |
| Data root + workspace resolver | `src/j1/config/settings.py` |
| LLM client roles (FAST / TEXT / VISION / EMBEDDING) | `src/j1/llm/settings.py` |
| RAGAnything compile + VLM HTTP | `src/j1/providers/raganything/settings.py` |
| Graphify optional graph provider | `src/j1/providers/graphify/settings.py` |
| Enrichment | `src/j1/processing/enrichment_settings.py` |
| Enrich assessment | `src/j1/processing/enrich_assessment_settings.py` |
| Planning report | `src/j1/processing/planning_settings.py` |
| Domain detection | `src/j1/domains/models.py` |
| REST security | `src/j1/integration/security/settings.py` |
| Event publisher | `src/j1/integration/events/settings.py` |
| Temporal | `src/j1/orchestration/temporal/config.py` |
| Compile retry | `src/j1/processing/compile_retry.py` |

## `.env` and `.env.example` maintenance

- `.env.example` is the canonical reference of every public,
  configurable setting. It must be kept in lockstep with the
  loader defaults.
- `.env` is your local copy. Never commit it.
- The "Required" column below flags settings that have no usable
  default in `prod` mode. Everything else has a working default.
- When you add a new `J1_*` env var:
  1. Define it on the typed settings dataclass with a default.
  2. Wire the loader to read it.
  3. Add it to `.env.example` with a short comment.
  4. Add a row to the right table in this doc.

---

## Setting groups

The remainder of this document is reference. Skim the headings;
search for the exact name when you need it.

### App / runtime

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_RUNTIME_PROFILE` | `dev` | no | `dev` / `prod` | `runtime.py` | Validation strictness. `prod` rejects dev fallbacks. | Set `prod` for any deployment with real users. |
| `J1_DATA_ROOT` | `/data/j1` | yes (shared) | absolute path | workspace resolver | Workspace root for documents, snapshots, audit, registries. **Must be shared between API and worker.** | When customising the deployment. The dev stack sets `/tmp/j1`. |
| `J1_API_PORT` | `8000` | no | int | `deploy/dev/api.py` | Port the API listens on. | When changing the host port mapping. |
| `J1_API_VERSION` | `0.0.1-dev` | no | str | OpenAPI doc | The version string surfaced in the API spec. | On release. |
| `J1_FRONTEND_API_BASE_URL` | `/api` | no | URL or path | frontend build | API base URL baked into the SPA. `/api` keeps single-origin behind nginx. | When the FE is served from a different origin. |
| `J1_FRONTEND_PORT` | `8081` | no | int | docker compose | Port the SPA is served on. | When changing the host port mapping. |

### Postgres (metadata + evidence FTS)

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_METADATA_BACKEND` | `postgres` | no | `postgres` / `sqlite_local` | runtime config | Backend selection. `sqlite_local` is dev-only. | Production = `postgres`. |
| `J1_METADATA_DSN` | unset | yes when backend=postgres | URL | runtime config | Postgres connection string. | Always set in dev + prod. |
| `J1_METADATA_SCHEMA` | `j1` | no | identifier | runtime config | Schema for J1's tables inside the DB. | When sharing a DB. |
| `J1_EVIDENCE_BACKEND` | `postgres_fts` | no | `postgres_fts` | runtime config | The only supported backend. | Don't. |
| `J1_EVIDENCE_DSN` | falls back to `J1_METADATA_DSN` | no | URL | runtime config | Separate FTS DB. | When FTS outgrows the app DB. |
| `J1_POSTGRES_USER` / `J1_POSTGRES_PASSWORD` / `J1_POSTGRES_PORT` | `j1` / `j1` / `5432` | no | str / str / int | docker compose | Dev Postgres container credentials. | When the dev DSN changes. |

### Cache (Redis)

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_CACHE_BACKEND` | `redis` | no | `redis` / `memory` | runtime config | Cache backend. `memory` is dev-only. | Production = `redis`. |
| `J1_CACHE_URL` | unset | yes when backend=redis | URL | runtime config | Redis URL. | Always set in dev + prod. |
| `J1_REDIS_PORT` | `6379` | no | int | docker compose | Dev container port. | Rare. |

### Artifact store (S3 / MinIO)

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_ARTIFACT_BACKEND` | `s3` | no | `s3` / `local_fs` | runtime config | Backend. `local_fs` is dev-only. | Production = `s3`. |
| `J1_ARTIFACT_ENDPOINT` | unset | yes when backend=s3 | URL | runtime config | S3-compatible endpoint. | Always set. |
| `J1_ARTIFACT_REGION` | unset | yes when backend=s3 | str | runtime config | AWS-style region label. | Always set. |
| `J1_ARTIFACT_BUCKET` | unset | yes when backend=s3 | str | runtime config | Bucket name. | Always set. |
| `J1_ARTIFACT_ACCESS_KEY` / `J1_ARTIFACT_SECRET_KEY` | unset | yes when backend=s3 | str | runtime config | Credentials. **Secrets — never commit.** | Always set; mount via secret store in prod. |
| `J1_ARTIFACT_USE_TLS` | `false` | no | bool | runtime config | Whether the endpoint uses TLS. | True in prod (AWS / GCS / Cloudflare). |
| `J1_ARTIFACT_LOCAL_ROOT` | unset | yes when backend=local_fs | absolute path | runtime config | Filesystem fallback root. | Dev only. |
| `J1_MINIO_PORT` / `J1_MINIO_CONSOLE_PORT` | `9000` / `9001` | no | int | docker compose | Dev container ports. | Rare. |

### Vector + graph stores (reserved)

The current answer path uses RAGAnything + LightRAG, so direct
vector / graph adapters are not wired yet. The settings exist for
forward compatibility.

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_VECTOR_BACKEND` | `embedded_lightrag` | no | `embedded_lightrag` / `qdrant` | runtime config | `qdrant` raises until the adapter ships. |
| `J1_GRAPH_BACKEND` | `embedded_lightrag` | no | `embedded_lightrag` / `neo4j` | runtime config | `neo4j` raises until the adapter ships. |
| `J1_VECTOR_URL` / `J1_VECTOR_API_KEY` / `J1_VECTOR_COLLECTION_PREFIX` | unset / unset / `j1` | no | URL / str / str | runtime config | Reserved. |
| `J1_GRAPH_URL` / `J1_GRAPH_USER` / `J1_GRAPH_PASSWORD` / `J1_GRAPH_DATABASE` | unset / unset / unset / `neo4j` | no | URL / str / str / str | runtime config | Reserved. |
| `J1_QDRANT_PORT` / `J1_QDRANT_GRPC_PORT` | `6333` / `6334` | no | int / int | docker compose | Dev container ports. |
| `J1_NEO4J_HTTP_PORT` / `J1_NEO4J_BOLT_PORT` | `7474` / `7687` | no | int / int | docker compose | Dev container ports. |

### Temporal

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_TEMPORAL_TARGET` | `temporal:7233` | yes | host:port | temporal client | Temporal frontend address. |
| `J1_TEMPORAL_NAMESPACE` | `default` | no | str | temporal client | Namespace. |
| `J1_TEMPORAL_TASK_QUEUE` | `j1-processing` | no | str | worker | Queue worker registers against. |
| `J1_TEMPORAL_TLS` | `false` | no | bool | temporal client | Use TLS to talk to Temporal. |
| `J1_TEMPORAL_API_KEY` | unset | no | str | temporal client | API key for Temporal Cloud. Secret. |
| `J1_TEMPORAL_UI_PORT` | `8233` | no | int | docker compose | Dev UI port. |
| `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` | `false` | no | bool | workflow | Opt-in to per-run search-attribute upserts. Requires registering the attributes via the Temporal CLI first. |

### Ingestion / workspace limits

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_MAX_UPLOAD_BYTES` | `52428800` (50 MB) | no | int | API | Hard cap per upload. | Raise for large PDFs. |
| `J1_ALLOWED_UPLOAD_EXTENSIONS` | unset (= accept all) | no | comma list | API | Optional extension allow-list. | Lock down in prod. |
| `J1_INGESTION_BATCH_MAX_FILES` | `5` | no | int | API | Max files per `POST /ingestion-batches`. | Raise after profiling. |
| `J1_KEEP_FAILED_INGEST_ARTIFACTS` | unset (= false) | no | bool | RAGAnything bridge | When truthy, keep MinerU per-doc `outputs/` on success. | For parser debugging. |

### Pre-compile assessment

| Setting | Default | Required | Valid values | Used by | Description | When to change |
| --- | --- | --- | --- | --- | --- | --- |
| `J1_ASSESSMENT_ENABLED` | `true` | no | bool | dev/api wiring → workflow request | Run the profile + AssessmentPlan stage before compile. | Disable to fall back to `J1_RAGANYTHING_PARSE_METHOD`. |
| `J1_ASSESSMENT_FAILURE_POLICY` | `fail_open` | no | `fail_open` / `fail_closed` | workflow | Whether assessment failure aborts the run. | `fail_closed` when you'd rather fail than fall back to a generic parse. |

### Compile (RAGAnything black box)

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_DEFAULT_COMPILER` | `raganything` | no | processor key | API | Default compiler processor for new ingestions. |
| `J1_RAGANYTHING_WORKDIR` | `./data/raganything` | yes (shared if multi-worker) | path | RAGAnything bridge | LightRAG workspace root. |
| `J1_RAGANYTHING_STORAGE_DIR` | falls back to workdir | no | path | RAGAnything bridge | Override for the LightRAG storage subtree. |
| `J1_RAGANYTHING_CACHE_DIR` | `<workdir>/cache` | no | path | RAGAnything bridge | Override for the per-document cache. |
| `J1_RAGANYTHING_PARSE_METHOD` | `auto` | no | `auto` / `txt` / `ocr` | MinerU | `--method` value. |
| `J1_RAGANYTHING_BACKEND` | `vlm-http-client` | no | MinerU backends | MinerU | `--backend` value. Local-model backends rejected at startup. |
| `J1_RAGANYTHING_SUPPORTS_IMAGE` / `_TABLE` / `_EQUATION` | `true` × 3 | no | bool | AssessmentPlan mapper | Advisory capability advertisements. |
| `J1_RAGANYTHING_ALLOWED_PARSE_METHODS` | unset (= all) | no | comma list | AssessmentPlan mapper | Narrow the assessment plan's choice. |
| `J1_RAGANYTHING_COMPILER_PROCESSOR` / `_GRAPH_PROCESSOR` / `_RETRIEVAL_PROCESSOR` | unset | no | importable | bridge | Optional callable overrides. |
| `J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS` | `.docx,.doc,.pptx,.ppt,.xlsx,.xls` | no | comma list | bridge | Extensions LibreOffice pre-converts to PDF. Empty = disabled. |
| `J1_RAGANYTHING_LIBREOFFICE_BINARY` | `soffice` | no | str | bridge | LibreOffice headless binary. |
| `J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS` | `120` | no | float | bridge | Per-conversion timeout. |
| `J1_RAGANYTHING_VLM_HTTP_SERVER_URL` | falls back to `J1_VISION_LLM_BASE_URL` | yes if no fallback | URL | MinerU | VLM endpoint URL. |
| `J1_RAGANYTHING_VLM_HTTP_API_KEY` | falls back to `J1_VISION_LLM_API_KEY` | no | str | MinerU | VLM API key. Secret. |
| `J1_RAGANYTHING_VLM_HTTP_MODEL_NAME` | falls back to `J1_VISION_LLM_MODEL` | no | str | MinerU | VLM model name. |
| `J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY` | `1` | no | int | MinerU | Cap on parallel VLM requests. |
| `J1_COMPILE_RETRY_ENABLED` | `true` | no | bool | retry settings | Compile-quality retry loop. |
| `J1_COMPILE_MAX_ATTEMPTS` | `2` | no | int | retry settings | Max compile attempts. |
| `J1_COMPILE_RETRY_MIN_TEXT_CHARS` | `200` | no | int | retry settings | Healthy-parse text floor. |
| `J1_COMPILE_RETRY_MIN_CHUNKS` | `1` | no | int | retry settings | Healthy-parse chunk floor. |

### Domain enrichment

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_ENRICHMENT_ENABLED` | `true` | no | bool | enrichment service | Master switch. |
| `J1_ENRICHMENT_REQUIRE_SUCCESS` | `false` | no | bool | enrichment service | Fail the run on any enrich failure. |
| `J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS` | `4` | no | int | enrichment service | Per-run task concurrency. |
| `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` | `4` | no | int | enrichment service | Per-run LLM concurrency. |
| `J1_ENRICHMENT_RETRY_LIMIT` | `2` | no | int | enrichment service | Per-task retry. |
| `J1_ENRICHMENT_TIMEOUT_SECONDS` | `180` | no | int | enrichment service | Per-task timeout. |
| `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS` | `false` | no | bool | enrichment service | Halves concurrency in dev. |
| `J1_ENRICH_ENABLED` / `_IMAGES` / `_TABLES` / `_DIAGRAMS` / `_SCANNED_PAGES` | `true` × 5 | no | bool | enrichment service | Per-capability switches. |
| `J1_ENRICH_CONFIDENCE_THRESHOLD` | `0.6` | no | float | enrichment service | Confidence floor for surfacing enrichment hits. |
| `J1_ENRICH_FAST_LLM_ENABLED` | `true` | no | bool | enrichment service | Allow fast-LLM consults during enrichment. |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_ENABLED` | `true` | no | bool | enrich-assessment | Whether the assessment may consult the FAST LLM. |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_PROVIDER` / `_MODEL` / `_TIMEOUT_SECONDS` | `openai_compat` / `gpt-4o-mini` / `30` | no | str / str / int | enrich-assessment | Fast LLM for assessment consults. |
| `J1_DEFAULT_ENRICHMENT_MODEL_TIER` | `text` | no | `text` / `fast` | enrichment service | Which registered role enrichers use. |
| `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` | `0.35` | no | float | domain registry | Detection score floor; below this the workspace default domain wins. |

### Planning report

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_LLM_PLANNING_ENABLED` | `false` | no | bool | planning settings | Enable LLM-assisted Planning Report augmentation. |
| `J1_PLANNING_MODEL_PROFILE` | `fast_planner` | no | str | planning settings | Named registered LLM role. |
| `J1_PLANNING_MAX_SAMPLE_BLOCKS` | `20` | no | int | planning settings | Privacy boundary: max content blocks sent to the planner LLM. |
| `J1_PLANNING_MAX_PREVIEW_CHARS` | `300` | no | int | planning settings | Privacy boundary: max characters per block. |

### Query / retrieval

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_DEFAULT_GRAPH_PROVIDER` | `raganything` | no | provider key | bootstrap | Default graph provider. |
| `J1_DEFAULT_RETRIEVAL_PROVIDER` | `raganything` | no | provider key | bootstrap | Default retrieval provider. |
| `J1_VALIDATION_CANDIDATE_TOP_K` | `20` | no | int | orchestrator | Candidate pool before reranking. |
| `J1_RAG_NATIVE_QUERY_TIMEOUT_SECONDS` | `30` | no | float | orchestrator | LightRAG aquery timeout. |
| `J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS` | `128` | no | int | evidence resolver | Per-query body cache size. |

### LLM clients

Each role takes the same shape (`PROVIDER` / `BASE_URL` /
`API_KEY` / `MODEL` / `TIMEOUT_SECONDS` / `MAX_RETRIES` /
`TEMPERATURE` / `MAX_OUTPUT_TOKENS` / `CONTEXT_WINDOW_TOKENS` /
`SAFETY_MARGIN_TOKENS` / `LANGCHAIN_CONFIG`).

| Role | Prefix | Default model | Purpose |
| --- | --- | --- | --- |
| FAST | `J1_FAST_LLM_*` | `gpt-4o-mini` | Intent classification, plan refinement, enricher consults. |
| TEXT | `J1_TEXT_LLM_*` | `gpt-4o` | The main answer synthesizer. |
| VISION | `J1_VISION_LLM_*` | `gpt-4o` | Vision enrichers + VLM HTTP fallback. |
| EMBEDDING | `J1_EMBEDDING_*` | `text-embedding-3-small` | Vector search embeddings. |

Health monitor (background probe):

| Setting | Default | Description |
| --- | --- | --- |
| `J1_LLM_STARTUP_PROBE` | `true` | Probe registered clients at startup. |
| `J1_LLM_PROBE_TIMEOUT_SECONDS` | `5` | Per-probe timeout. |
| `J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS` | `30` | Periodic re-probe interval. |

### Worker concurrency

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | `8` | no | int | worker | Per-process Temporal activity concurrency. |
| `J1_RAG_MAX_CONCURRENT_DOCUMENTS` | `4` | no | int | RAGAnything bridge | Cap on documents in flight through LightRAG simultaneously. |

### Security / auth

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_AUTH_API_KEYS` | unset | no | `key:scopes,key2:scopes` | REST security | Inline API keys with scopes. **Secret.** |
| `J1_AUTH_API_KEYS_FILE` | unset | no | absolute path | REST security | File-backed alternative for secret managers. |
| `J1_ALLOW_ANONYMOUS_ADMIN` | `false` | no | bool | REST app | Suppress the "auth disabled" startup warning when running anonymously. |

### Webhooks

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_WEBHOOK_ENABLED` | `false` | no | bool | webhook adapter | Master switch. |
| `J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS` | `10` | no | int | webhook adapter | Per-call timeout. |
| `J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS` | `3` | no | int | webhook adapter | Per-call retry cap. |
| `J1_WEBHOOK_SUBSCRIPTIONS` / `_SUBSCRIPTIONS_FILE` | unset | no | JSON list / path | webhook adapter | Subscription definitions. |

### Event publisher

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_EVENT_PUBLISHER_TYPE` | `noop` | no | `noop` / `bus` | event publisher | `noop` writes audit-log only; `bus` fans through the in-process bus. |
| `J1_EVENT_PUBLISHER_PRODUCER` | `j1` | no | str | event publisher | `producer` field stamped on every event. |
| `J1_EVENT_PUBLISHER_SCHEMA_VERSION` | `1` | no | int | event publisher | Schema version stamped on every event. |
| `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS` | `false` | no | bool | event publisher | Whether full payloads ride alongside events. |

### Observability / cleanup

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_BENCHMARK_STAGE_TIMING` | `false` | no | bool | runtime config | Per-stage timing telemetry. |
| `J1_BENCHMARK_INGESTION` | `false` | no | bool | runtime config | Per-document benchmark output. |
| `J1_BENCHMARK_OUTPUT_PATH` | `/var/lib/j1/benchmarks` | no | path | runtime config | Benchmark JSONL location. |
| `J1_CLEANUP_HARD_DELETE` | `false` | no | bool | cleanup service | Hard-delete instead of tombstone. |
| `J1_CLEANUP_RETENTION_DAYS` | unset | no | int | cleanup service | Retention window for periodic prune. |

### Ingestion performance trace

Dedicated developer/operator debugging surface for investigating slow
or stuck ingestion. See [docs/ingestion-tracing.md](ingestion-tracing.md)
for the full event schema and verification recipe. Disabled by default;
turn on only while debugging.

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_INGEST_TRACE_ENABLED` | `false` | no | bool | ingest trace logger | Master switch. When false, the trace helper is a no-op and no file is created. |
| `J1_INGEST_TRACE_LEVEL` | `INFO` | no | `INFO` / `DEBUG` | ingest trace logger | INFO records stage-level timing; DEBUG may include additional safe metadata summaries. |
| `J1_INGEST_TRACE_SLOW_STAGE_MS` | `30000` | no | int > 0 | ingest trace logger | Threshold (ms) above which a stage is flagged `slow=true` and emits one `ingest.trace.slow_stage` warning on the normal logger. |
| `J1_INGEST_TRACE_OUTPUT` | `logs/ingest_trace.jsonl` | no | path | ingest trace logger | JSONL output file. Parent directory is created automatically. |

### Optional: Graphify graph provider

| Setting | Default | Required | Valid values | Used by | Description |
| --- | --- | --- | --- | --- | --- |
| `J1_GRAPHIFY_ENABLED` | `false` | no | bool | bootstrap | Selects Graphify instead of RAGAnything as the graph provider. |
| `J1_GRAPHIFY_MODE` | `cli` | no | `cli` / `python` | Graphify bridge | Adapter mode. |
| `J1_GRAPHIFY_COMMAND` | `graphify` | no | str | Graphify bridge | CLI binary path. |
| `J1_GRAPHIFY_WORKDIR` | `/var/lib/j1/graphify` | no | path | Graphify bridge | Workspace root. |
| `J1_GRAPHIFY_GRAPH_PROCESSOR` | unset | no | importable | Graphify bridge | Optional override callable. |

---

## Recommendations

### Dev mode

- Keep `J1_RUNTIME_PROFILE=dev`.
- Point everything at the dev Docker stack's services (`postgres`,
  `redis`, `minio`, `temporal` hostnames).
- Use the `replace-me` placeholder LLM keys until you wire a real
  provider; the FE will surface the "LLM unreachable" banner.

### Docker stack

- The dev compose stack reads `.env` at the repo root. Variables
  prefixed `J1_POSTGRES_*` / `J1_MINIO_*` / etc. configure the
  containers themselves; the app-facing keys (`J1_METADATA_DSN`,
  `J1_ARTIFACT_ENDPOINT`, …) point at the container hostnames.
- Set `J1_DATA_ROOT=/tmp/j1` so the API and worker share the
  `j1_temp` named volume.

### Production / staging

- `J1_RUNTIME_PROFILE=prod`.
- Every Required-yes value populated.
- LLM keys mounted via secret store (use `_FILE` variants where
  available, e.g. `J1_AUTH_API_KEYS_FILE`).
- TLS on for every external endpoint (`J1_ARTIFACT_USE_TLS=true`,
  `J1_TEMPORAL_TLS=true`).
- `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true` only after
  registering the attributes against the namespace.
- Shared filesystem (or vendor backend) under
  `J1_RAGANYTHING_WORKDIR` when running multi-worker.

### Secret handling

- Never commit a populated `.env`.
- `_API_KEY` and `_SECRET_KEY` and `_PASSWORD` values are secrets;
  use a secret store in production.
- The `_FILE` variants (`J1_AUTH_API_KEYS_FILE`,
  `J1_WEBHOOK_SUBSCRIPTIONS_FILE`) accept absolute paths so secrets
  can be mounted via Kubernetes secrets / Docker secrets / Vault.

### Adding a new setting

1. Define the field on the typed dataclass with a default value
   close to the field.
2. Add the loader read with a clear error on invalid input
   (`raise ConfigError(...)`).
3. Add the key to the canonical `.env.example` reference (below).
4. Add a row to the matching table in this document.
5. If the setting is required in `prod`, extend
   `RuntimeConfig.validate()` (or the matching feature loader)
   so misconfiguration fails fast at startup.

---

## Canonical `.env.example`

Copy the block below into `.env.example` at the repo root and into
your local `.env`. This is the single source of truth for public
configuration. Anything that drifts from it is a bug.

```dotenv
# =====================================================================
#  J1 environment configuration
# =====================================================================
# Copy this file to `.env` and edit the values you need.
# Every key is grouped by feature area. Keys not listed here fall back
# to the code defaults in `src/j1/config/runtime.py` and the per-feature
# settings modules. See `docs/settings.md` for the full reference.

# ---------------------------------------------------------------------
#  App / runtime
# ---------------------------------------------------------------------
J1_RUNTIME_PROFILE=dev
J1_DATA_ROOT=/tmp/j1

# ---------------------------------------------------------------------
#  API / backend
# ---------------------------------------------------------------------
J1_API_PORT=8000
J1_API_VERSION=0.0.1-dev
J1_FRONTEND_API_BASE_URL=/api
J1_FRONTEND_PORT=8081

# ---------------------------------------------------------------------
#  Postgres (metadata + evidence FTS)
# ---------------------------------------------------------------------
J1_METADATA_BACKEND=postgres
J1_METADATA_DSN=postgresql://j1:j1@postgres:5432/j1
J1_METADATA_SCHEMA=j1
J1_EVIDENCE_BACKEND=postgres_fts
#J1_EVIDENCE_DSN=
J1_POSTGRES_USER=j1
J1_POSTGRES_PASSWORD=j1
J1_POSTGRES_PORT=5432

# ---------------------------------------------------------------------
#  Redis cache
# ---------------------------------------------------------------------
J1_CACHE_BACKEND=redis
J1_CACHE_URL=redis://redis:6379/0
J1_REDIS_PORT=6379

# ---------------------------------------------------------------------
#  Artifact store (S3 / MinIO)
# ---------------------------------------------------------------------
J1_ARTIFACT_BACKEND=s3
J1_ARTIFACT_ENDPOINT=http://minio:9000
J1_ARTIFACT_REGION=us-east-1
J1_ARTIFACT_BUCKET=j1-artifacts
J1_ARTIFACT_ACCESS_KEY=j1-dev
J1_ARTIFACT_SECRET_KEY=j1-dev-secret
J1_ARTIFACT_USE_TLS=false
#J1_ARTIFACT_LOCAL_ROOT=
J1_MINIO_PORT=9000
J1_MINIO_CONSOLE_PORT=9001

# ---------------------------------------------------------------------
#  Vector + graph stores (reserved)
# ---------------------------------------------------------------------
J1_VECTOR_BACKEND=embedded_lightrag
J1_GRAPH_BACKEND=embedded_lightrag
J1_QDRANT_PORT=6333
J1_QDRANT_GRPC_PORT=6334
J1_NEO4J_HTTP_PORT=7474
J1_NEO4J_BOLT_PORT=7687

# ---------------------------------------------------------------------
#  Temporal
# ---------------------------------------------------------------------
J1_TEMPORAL_TARGET=temporal:7233
J1_TEMPORAL_NAMESPACE=default
J1_TEMPORAL_TASK_QUEUE=j1-processing
J1_TEMPORAL_TLS=false
#J1_TEMPORAL_API_KEY=
J1_TEMPORAL_UI_PORT=8233
J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=false

# ---------------------------------------------------------------------
#  Ingestion / workspace limits
# ---------------------------------------------------------------------
J1_MAX_UPLOAD_BYTES=52428800
J1_ALLOWED_UPLOAD_EXTENSIONS=.pdf,.docx,.xlsx,.pptx,.md,.txt
J1_INGESTION_BATCH_MAX_FILES=5
#J1_KEEP_FAILED_INGEST_ARTIFACTS=false

# ---------------------------------------------------------------------
#  Pre-compile assessment
# ---------------------------------------------------------------------
J1_ASSESSMENT_ENABLED=true
J1_ASSESSMENT_FAILURE_POLICY=fail_open

# ---------------------------------------------------------------------
#  Compile (RAGAnything)
# ---------------------------------------------------------------------
J1_DEFAULT_COMPILER=raganything
J1_RAGANYTHING_WORKDIR=/var/lib/j1/raganything
#J1_RAGANYTHING_STORAGE_DIR=
#J1_RAGANYTHING_CACHE_DIR=
J1_RAGANYTHING_PARSE_METHOD=auto
J1_RAGANYTHING_BACKEND=vlm-http-client
J1_RAGANYTHING_SUPPORTS_IMAGE=true
J1_RAGANYTHING_SUPPORTS_TABLE=true
J1_RAGANYTHING_SUPPORTS_EQUATION=true
J1_RAGANYTHING_ALLOWED_PARSE_METHODS=
#J1_RAGANYTHING_COMPILER_PROCESSOR=
#J1_RAGANYTHING_GRAPH_PROCESSOR=
#J1_RAGANYTHING_RETRIEVAL_PROCESSOR=
J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS=.docx,.doc,.pptx,.ppt,.xlsx,.xls
J1_RAGANYTHING_LIBREOFFICE_BINARY=soffice
J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS=120
#J1_RAGANYTHING_VLM_HTTP_SERVER_URL=
#J1_RAGANYTHING_VLM_HTTP_API_KEY=
#J1_RAGANYTHING_VLM_HTTP_MODEL_NAME=
J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY=1
J1_COMPILE_RETRY_ENABLED=true
J1_COMPILE_MAX_ATTEMPTS=2
J1_COMPILE_RETRY_MIN_TEXT_CHARS=200
J1_COMPILE_RETRY_MIN_CHUNKS=1

# ---------------------------------------------------------------------
#  Domain enrichment (post-compile)
# ---------------------------------------------------------------------
J1_ENRICHMENT_ENABLED=true
J1_ENRICHMENT_REQUIRE_SUCCESS=false
J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS=4
J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS=4
J1_ENRICHMENT_RETRY_LIMIT=2
J1_ENRICHMENT_TIMEOUT_SECONDS=180
J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS=false
J1_ENRICH_ENABLED=true
J1_ENRICH_IMAGES=true
J1_ENRICH_TABLES=true
J1_ENRICH_DIAGRAMS=true
J1_ENRICH_SCANNED_PAGES=true
J1_ENRICH_CONFIDENCE_THRESHOLD=0.6
J1_ENRICH_FAST_LLM_ENABLED=true
J1_ENRICH_ASSESSMENT_FAST_LLM_ENABLED=true
J1_ENRICH_ASSESSMENT_FAST_LLM_PROVIDER=openai_compat
J1_ENRICH_ASSESSMENT_FAST_LLM_MODEL=gpt-4o-mini
J1_ENRICH_ASSESSMENT_FAST_LLM_TIMEOUT_SECONDS=30
J1_DEFAULT_ENRICHMENT_MODEL_TIER=text
J1_DOMAIN_DETECTION_MIN_CONFIDENCE=0.35

# ---------------------------------------------------------------------
#  Planning report (post-compile audit projection)
# ---------------------------------------------------------------------
J1_LLM_PLANNING_ENABLED=false
J1_PLANNING_MODEL_PROFILE=fast_planner
J1_PLANNING_MAX_SAMPLE_BLOCKS=20
J1_PLANNING_MAX_PREVIEW_CHARS=300

# ---------------------------------------------------------------------
#  Query / retrieval
# ---------------------------------------------------------------------
J1_DEFAULT_GRAPH_PROVIDER=raganything
J1_DEFAULT_RETRIEVAL_PROVIDER=raganything
J1_VALIDATION_CANDIDATE_TOP_K=20
J1_RAG_NATIVE_QUERY_TIMEOUT_SECONDS=30
J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS=128

# ---------------------------------------------------------------------
#  LLM clients
# ---------------------------------------------------------------------
J1_FAST_LLM_PROVIDER=openai_compat
J1_FAST_LLM_BASE_URL=https://api.openai.com/v1
J1_FAST_LLM_API_KEY=replace-me
J1_FAST_LLM_MODEL=gpt-4o-mini
J1_FAST_LLM_TIMEOUT_SECONDS=30
J1_FAST_LLM_MAX_RETRIES=2
J1_FAST_LLM_TEMPERATURE=0.0
J1_FAST_LLM_MAX_OUTPUT_TOKENS=1024
J1_FAST_LLM_CONTEXT_WINDOW_TOKENS=128000
J1_FAST_LLM_SAFETY_MARGIN_TOKENS=1024
#J1_FAST_LLM_LANGCHAIN_CONFIG=

J1_TEXT_LLM_PROVIDER=openai_compat
J1_TEXT_LLM_BASE_URL=https://api.openai.com/v1
J1_TEXT_LLM_API_KEY=replace-me
J1_TEXT_LLM_MODEL=gpt-4o
J1_TEXT_LLM_TIMEOUT_SECONDS=60
J1_TEXT_LLM_MAX_RETRIES=2
J1_TEXT_LLM_TEMPERATURE=0.1
J1_TEXT_LLM_MAX_OUTPUT_TOKENS=4096
J1_TEXT_LLM_CONTEXT_WINDOW_TOKENS=128000
J1_TEXT_LLM_SAFETY_MARGIN_TOKENS=2048
#J1_TEXT_LLM_LANGCHAIN_CONFIG=

J1_VISION_LLM_PROVIDER=openai_compat
J1_VISION_LLM_BASE_URL=https://api.openai.com/v1
J1_VISION_LLM_API_KEY=replace-me
J1_VISION_LLM_MODEL=gpt-4o
J1_VISION_LLM_TIMEOUT_SECONDS=60
J1_VISION_LLM_MAX_RETRIES=2
J1_VISION_LLM_TEMPERATURE=0.0
J1_VISION_LLM_MAX_OUTPUT_TOKENS=2048
J1_VISION_LLM_CONTEXT_WINDOW_TOKENS=128000
J1_VISION_LLM_SAFETY_MARGIN_TOKENS=2048
#J1_VISION_LLM_LANGCHAIN_CONFIG=

J1_EMBEDDING_PROVIDER=openai_compat
J1_EMBEDDING_BASE_URL=https://api.openai.com/v1
J1_EMBEDDING_API_KEY=replace-me
J1_EMBEDDING_MODEL=text-embedding-3-small
J1_EMBEDDING_DIM=1536
J1_EMBEDDING_BATCH_SIZE=32
J1_EMBEDDING_MAX_RETRIES=2
J1_EMBEDDING_MAX_TOKENS=8192
J1_EMBEDDING_TIMEOUT_SECONDS=30
#J1_EMBEDDING_LANGCHAIN_CONFIG=

J1_LLM_STARTUP_PROBE=true
J1_LLM_PROBE_TIMEOUT_SECONDS=5
J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS=30

# ---------------------------------------------------------------------
#  Worker concurrency
# ---------------------------------------------------------------------
J1_WORKER_MAX_CONCURRENT_ACTIVITIES=8
J1_RAG_MAX_CONCURRENT_DOCUMENTS=4

# ---------------------------------------------------------------------
#  Security / auth
# ---------------------------------------------------------------------
#J1_AUTH_API_KEYS=
#J1_AUTH_API_KEYS_FILE=
J1_ALLOW_ANONYMOUS_ADMIN=false

# ---------------------------------------------------------------------
#  Webhooks
# ---------------------------------------------------------------------
J1_WEBHOOK_ENABLED=false
J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS=10
J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS=3
#J1_WEBHOOK_SUBSCRIPTIONS=
#J1_WEBHOOK_SUBSCRIPTIONS_FILE=

# ---------------------------------------------------------------------
#  Event publisher
# ---------------------------------------------------------------------
J1_EVENT_PUBLISHER_TYPE=noop
J1_EVENT_PUBLISHER_PRODUCER=j1
J1_EVENT_PUBLISHER_SCHEMA_VERSION=1
J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS=false

# ---------------------------------------------------------------------
#  Observability / cleanup
# ---------------------------------------------------------------------
J1_BENCHMARK_STAGE_TIMING=false
J1_BENCHMARK_INGESTION=false
J1_BENCHMARK_OUTPUT_PATH=/var/lib/j1/benchmarks
J1_CLEANUP_HARD_DELETE=false
#J1_CLEANUP_RETENTION_DAYS=

# ---------------------------------------------------------------------
#  Ingestion performance trace
#    Developer/operator debugging only. Disabled by default. See
#    docs/ingestion-tracing.md for the event schema and recipes.
# ---------------------------------------------------------------------
J1_INGEST_TRACE_ENABLED=false
J1_INGEST_TRACE_LEVEL=INFO
J1_INGEST_TRACE_SLOW_STAGE_MS=30000
J1_INGEST_TRACE_OUTPUT=logs/ingest_trace.jsonl

# ---------------------------------------------------------------------
#  Optional: Graphify graph provider (alternative to RAGAnything)
# ---------------------------------------------------------------------
J1_GRAPHIFY_ENABLED=false
J1_GRAPHIFY_MODE=cli
J1_GRAPHIFY_COMMAND=graphify
J1_GRAPHIFY_WORKDIR=/var/lib/j1/graphify
#J1_GRAPHIFY_GRAPH_PROCESSOR=
```
