# J1 Configuration Audit — 2026-05-11

Scope: every `J1_*` environment variable plus a small number of non-prefixed
vendor variables (`MAX_ASYNC`, `MAX_GLEANING`, `EMBEDDING_TIMEOUT`,
`LLM_TIMEOUT`) referenced by the J1 stack.

Method:
- Extracted name sets from `.env.example`, local `.env` (names only),
  Python source under `src/j1/` + `deploy/`, frontend under
  `frontend/src/`, and `deploy/dev/docker-compose.yml`.
- Cross-referenced declaration sites against runtime read sites (not just
  declarations).
- Verified every cleanup target by tracing actual code references, not
  grep alone.

Counts (post-trace):
- `.env.example`: 116 distinct `J1_*` names (including commented-out
  examples) + 2 vendor names (`MAX_ASYNC`, `MAX_GLEANING`).
- Local `.env`: 88 active `J1_*` names + `EMBEDDING_TIMEOUT`,
  `LLM_TIMEOUT`, `MAX_ASYNC`, `MAX_GLEANING`.
- Python source: 144 `J1_*` string literals (of which 4 are Temporal
  failure-type names, not env vars: `J1_ERROR`,
  `J1_INGEST_LOOKUP_FAILED`, `J1_INGEST_REQUIRED_STEP_FAILED`,
  `J1_INGEST_UNEXPECTED_ERROR`).

## Section 1 — Variable inventory

Columns: name | group | declared (`E`=`.env.example`, `L`=local `.env`,
`D`=docker-compose, `C`=Python code) | runtime read site | default |
status.

### Workspace + API service

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_DATA_ROOT` | Workspace | E,L,D,C | `deploy/dev/_wiring.py` | `/var/lib/j1` | KEEP |
| `J1_API_PORT` | API | E,L,D,C | `deploy/dev/api.py` | `8000` | KEEP |
| `J1_API_VERSION` | API | (commented in E),C | `src/j1/adapters/rest/app.py` | `0.0.1-dev` | KEEP |
| `J1_MAX_UPLOAD_BYTES` | API | (commented in E),L,C | `src/j1/adapters/rest/intake.py` | `209715200` | KEEP |
| `J1_ALLOWED_UPLOAD_EXTENSIONS` | API | (commented in E),C | `src/j1/adapters/rest/intake.py` | (sensible list) | KEEP |
| `J1_KEEP_FAILED_INGEST_ARTIFACTS` | API | (commented in E),C | `src/j1/adapters/rest/intake.py` | `false` | KEEP |
| `J1_ALLOW_ANONYMOUS_ADMIN` | API | (commented in E),C | `src/j1/integration/security/settings.py` | unset | KEEP |
| `J1_FRONTEND_PORT` | Frontend | E,D | docker-compose only | `8081` | KEEP (docker-only) |
| `J1_FRONTEND_API_BASE_URL` | Frontend | (commented in E),D | docker-compose `VITE_API_BASE_URL` baked into bundle | `/api` | KEEP (docker-only) |

### Temporal

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_TEMPORAL_TARGET` | Temporal | E,L,D,C | `src/j1/orchestration/temporal/config.py` | `localhost:7233` | KEEP |
| `J1_TEMPORAL_NAMESPACE` | Temporal | E,L,D,C | same | `default` | KEEP |
| `J1_TEMPORAL_TASK_QUEUE` | Temporal | E,L,D,C | same | `j1-processing` | KEEP |
| `J1_TEMPORAL_TLS` | Temporal | (commented in E),C | same | `false` | KEEP |
| `J1_TEMPORAL_API_KEY` | Temporal | (commented in E),C | same | unset | KEEP (secret) |
| `J1_TEMPORAL_UI_PORT` | Temporal | (commented in E),D | docker-compose only | `8080` | KEEP (docker-only) |
| `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` | Temporal | E,L,D-comment | **no Python read site** | `true` (declared) | **WIRING GAP** — see Section 2 |
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | Temporal worker | E,L,D,C | `deploy/dev/worker.py` | `5` | KEEP |

### Auth, webhooks, events

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_AUTH_API_KEYS` | Auth | (commented in E),C | `src/j1/integration/security/settings.py` | unset | KEEP (secret) |
| `J1_AUTH_API_KEYS_FILE` | Auth | (commented in E),C | same | unset | KEEP |
| `J1_AUTH_REQUIRED` | Auth | C only | same | `false` | ADD to E |
| `J1_AUTH_JWT_ENABLED` | Auth | C only | same | `false` | ADD to E |
| `J1_AUTH_DEFAULT_TENANT_ID` | Auth | C only | same | unset | ADD to E |
| `J1_AUTH_ANONYMOUS_PATHS` | Auth | C only | same | (sensible defaults) | ADD to E |
| `J1_WEBHOOK_ENABLED` | Webhooks | C only | `src/j1/integration/events/settings.py` | `false` | ADD to E |
| `J1_WEBHOOK_SUBSCRIPTIONS` | Webhooks | C only | same | unset | ADD to E |
| `J1_WEBHOOK_SUBSCRIPTIONS_FILE` | Webhooks | (commented in E),C | same | unset | KEEP |
| `J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS` | Webhooks | C only | same | `3` | ADD to E (commented) |
| `J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS` | Webhooks | C only | same | `15` | ADD to E (commented) |
| `J1_EVENT_PUBLISHER_TYPE` | Events | (commented in E),C | `src/j1/integration/events/settings.py` | `noop` | KEEP |
| `J1_EVENT_PUBLISHER_PRODUCER` | Events | C only | same | (derived) | KEEP (advanced) |
| `J1_EVENT_PUBLISHER_SCHEMA_VERSION` | Events | C only | same | (constant) | KEEP (advanced) |
| `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS` | Events | C only | same | `false` | KEEP (security) |

### Processing pipeline selection

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_DEFAULT_COMPILER` | Pipeline | E,L,C | `src/j1/compose/bootstrap.py` | `mock` | KEEP |
| `J1_DEFAULT_GRAPH_PROVIDER` | Pipeline | E,L,C | same | `mock` | KEEP |
| `J1_DEFAULT_RETRIEVAL_PROVIDER` | Pipeline | E,L,C | same | `mock` | KEEP |

### Enrichment

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_ENRICH_ENABLED` | Enrich | E,L,C | `src/j1/compose/bootstrap.py` | `false` | KEEP |
| `J1_ENRICH_CONFIDENCE_THRESHOLD` | Enrich | E,L,C | same | `0.75` | KEEP |
| `J1_ENRICH_IMAGES` | Enrich | E,L,C | same | `true` | KEEP |
| `J1_ENRICH_TABLES` | Enrich | E,L,C | same | `true` | KEEP |
| `J1_ENRICH_DIAGRAMS` | Enrich | E,L,C | same | `true` | KEEP |
| `J1_ENRICH_SCANNED_PAGES` | Enrich | E,L,C | same | `true` | KEEP |
| `J1_ENRICHMENT_ENABLED` | Enrich | C only | enrichment module loader | `true` | **DUPE** with `J1_ENRICH_ENABLED` — see Section 2 |
| `J1_ENRICHMENT_REQUIRE_SUCCESS` | Enrich | C only | same | `false` | ADD to E (commented) |
| `J1_ENRICHMENT_TIMEOUT_SECONDS` | Enrich | C only | same | (constant) | ADD to E (commented) |
| `J1_ENRICHMENT_RETRY_LIMIT` | Enrich | C only | same | (constant) | ADD to E (commented) |
| `J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS` | Enrich | C only | same | (constant) | ADD to E (commented) |
| `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` | Enrich/LLM | C only | LLMCallLimiter wiring | (constant) | **ADD to E** — this is the dev-LLM-cost lever called out in the audit prompt |
| `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS` | Enrich | C only | same | `false` | KEEP (advanced) |
| `J1_DEFAULT_ENRICHMENT_MODEL_TIER` | Enrich | C only | enrichment model routing | `fast` | KEEP (advanced) |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_ENABLED` | Enrich | C only | post-compile assessor | `false` | ADD to E (commented) |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_PROVIDER` | Enrich | C only | same | (inherits FAST) | ADD to E (commented) |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_MODEL` | Enrich | C only | same | (inherits FAST) | ADD to E (commented) |
| `J1_ENRICH_ASSESSMENT_FAST_LLM_TIMEOUT_SECONDS` | Enrich | C only | same | `10` | ADD to E (commented) |

### Compile retry (post-RAGAnything)

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_COMPILE_RETRY_ENABLED` | Compile | C only | `src/j1/processing/compile_retry.py` | `true` | ADD to E (commented) |
| `J1_COMPILE_MAX_ATTEMPTS` | Compile | C only | same | `2` | ADD to E (commented) |
| `J1_COMPILE_RETRY_MIN_CHUNKS` | Compile | C only | same | (constant) | KEEP (advanced) |
| `J1_COMPILE_RETRY_MIN_TEXT_CHARS` | Compile | C only | same | (constant) | KEEP (advanced) |
| `J1_ASSESSMENT_FAILURE_POLICY` | Compile | C only | `src/j1/processing/assessment.py` | `fail_closed` | ADD to E (commented) |

### Planning vocabulary

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_INGEST_PLANNER_ENABLED` | Planning | E,L,C | `deploy/dev/api.py` | `true` | KEEP |
| `J1_INGEST_DEFAULT_POLICY` | Planning | E,L | docker-compose only (passed to API) | `auto` | KEEP |
| `J1_INGEST_PLAN_MODE` | Planning | (commented in E),L,C | `src/j1/processing/planning_settings.py` | `rule_based` | KEEP (operator-facing) |
| `J1_PLANNING_ENABLED` | Planning | (commented in E),L,C | same | `true` | KEEP |
| `J1_LLM_PLANNING_ENABLED` | Planning | (commented in E),L,C | same | `false` | **DEPRECATED alias** — overridden by `J1_INGEST_PLAN_MODE`; keep temporarily |
| `J1_POST_COMPILE_PLANNING_ENABLED` | Planning | C only | same | `true` | ADD to E (commented) |
| `J1_PLANNING_MODEL_PROFILE` | Planning | (commented in E),L,C | same | `fast_planner` | KEEP |
| `J1_PLANNING_MAX_SAMPLE_BLOCKS` | Planning | (commented in E),L,C | same | `20` | KEEP |
| `J1_PLANNING_MAX_PREVIEW_CHARS` | Planning | (commented in E),L,C | same | `300` | KEEP |
| `J1_PLANNING_MAX_EARLY_PAGES` | Planning | C only | same | (constant) | KEEP (advanced) |
| `J1_PLANNING_FAIL_OPEN` | Planning | (commented in E),L,C | same | `true` | KEEP |
| `J1_PLANNING_TRACE_ENABLED` | Planning | C only | same | `false` | ADD to E (commented) |
| `J1_PLANNING_TRACE_BODY` | Planning | C only | same | `false` | ADD to E (commented) |

### Domain packs

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_DOMAIN_PACKS_ENABLED` | Domain | (commented in E),L,C | `src/j1/processing/planning_settings.py` | `true` | KEEP |
| `J1_DEFAULT_DOMAIN` | Domain | (commented in E),L,C | same | `general` | KEEP |
| `J1_DOMAIN_DETECTION_ENABLED` | Domain | (commented in E),L,C | same | `true` | KEEP |
| `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` | Domain | (commented in E),L,C | same | `0.65` | KEEP |
| `J1_ALLOWED_DOMAIN_OVERRIDES` | Domain | (commented in E),L,C | same | (any) | KEEP — rewrite example to drop civil_engineering specificity |
| `J1_WORKSPACE_DEFAULT_DOMAIN` | Domain | (commented in E),L,C | same | `general` | KEEP |

### LLM roles — text / vision / embedding / fast

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_LLM_STARTUP_PROBE` | LLM probe | (commented in E),L,C | `src/j1/llm/settings.py` | `true` | KEEP |
| `J1_LLM_PROBE_TIMEOUT_SECONDS` | LLM probe | (commented in E),L,C | same | `5` | KEEP |
| `J1_LLM_HEALTH_MONITOR_INTERVAL_SECONDS` | LLM probe | (commented in E),L,C | same | `30` | KEEP |
| `J1_TEXT_LLM_PROVIDER` | LLM Text | E,L,C | same | `openai_compat` | KEEP |
| `J1_TEXT_LLM_BASE_URL` | LLM Text | E,L,C | same | unset | KEEP |
| `J1_TEXT_LLM_API_KEY` | LLM Text | E,L,C | same | unset | KEEP (secret) |
| `J1_TEXT_LLM_MODEL` | LLM Text | E,L,C | same | unset | KEEP |
| `J1_TEXT_LLM_TIMEOUT_SECONDS` | LLM Text | E,L,C | same | `60` | KEEP |
| `J1_TEXT_LLM_MAX_RETRIES` | LLM Text | E,L,C | same | `3` | KEEP |
| `J1_TEXT_LLM_TEMPERATURE` | LLM Text | E,L,C | same | `0.2` | KEEP |
| `J1_TEXT_LLM_MAX_OUTPUT_TOKENS` | LLM Text | E,L,C | same | `4096` | KEEP |
| `J1_TEXT_LLM_CONTEXT_WINDOW_TOKENS` | LLM Text | (commented in E),L,C | same | unset | KEEP |
| `J1_TEXT_LLM_SAFETY_MARGIN_TOKENS` | LLM Text | (commented in E),L,C | same | `256` | KEEP |
| `J1_TEXT_LLM_LANGCHAIN_CONFIG` | LLM Text | (commented in E),C | same | unset | KEEP |
| `J1_VISION_LLM_*` | LLM Vision | same shape as Text | same | same | KEEP |
| `J1_EMBEDDING_*` | LLM Embedding | E,L,C | same | (defaults vary per field) | KEEP — note `J1_EMBEDDING_DIM=1024`, `J1_EMBEDDING_MAX_TOKENS=8192`, `J1_EMBEDDING_BATCH_SIZE=32` |
| `J1_FAST_LLM_*` | LLM Fast | E,L,C | same | (cheap-role defaults) | KEEP |

### LightRAG vendor variables (NOT J1-prefixed)

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `MAX_ASYNC` | LightRAG | E,L | read directly by `lightrag` package | `1` (J1's recommended) | KEEP |
| `MAX_GLEANING` | LightRAG | E,L | same | `0` (J1's recommended) | KEEP |
| `EMBEDDING_TIMEOUT` | LightRAG | (commented in E),L | same | (vendor `30`) | KEEP (commented) |
| `LLM_TIMEOUT` | LightRAG | (commented in E),L | same | (vendor `60`) | KEEP (commented) |
| `MINERU_VL_MAX_CONCURRENCY` | MinerU | propagated by code, not env-read | derived from `J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY` | `1` | INTERNAL |

### RAGAnything provider

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_RAGANYTHING_MODE` | RAGAnything | E,L,C | `src/j1/providers/raganything/settings.py` | `local` | KEEP |
| `J1_RAGANYTHING_WORKDIR` | RAGAnything | E,L,C | same | `./data/raganything` | KEEP |
| `J1_RAGANYTHING_STORAGE_DIR` | RAGAnything | (commented in E),C | same | (derived from WORKDIR) | KEEP |
| `J1_RAGANYTHING_CACHE_DIR` | RAGAnything | (commented in E),C | same | (derived) | KEEP |
| `J1_RAGANYTHING_PIPELINE_MODE` | RAGAnything | (commented in E),L,**no C** | **none** | (none) | **REMOVE — DEAD split-mode artifact (see Section 2.A)** |
| `J1_RAGANYTHING_PARSE_METHOD` | RAGAnything | (commented in E),L,C | `src/j1/providers/raganything/settings.py` | `auto` | KEEP |
| `J1_RAGANYTHING_ALLOWED_PARSE_METHODS` | RAGAnything | C only | same | (`auto`,`txt`,`ocr`) | KEEP (advanced) |
| `J1_RAGANYTHING_BACKEND` | RAGAnything | (commented in E),L,C | same | `vlm-http-client` | KEEP |
| `J1_RAGANYTHING_VLM_HTTP_SERVER_URL` | RAGAnything | (commented in E),L,C | same | falls back to `J1_VISION_LLM_BASE_URL` | KEEP |
| `J1_RAGANYTHING_VLM_HTTP_API_KEY` | RAGAnything | (commented in E),L,C | same | unset | KEEP (secret) |
| `J1_RAGANYTHING_VLM_HTTP_MODEL_NAME` | RAGAnything | (commented in E),L,C | same | unset | KEEP |
| `J1_RAGANYTHING_VLM_HTTP_MAX_CONCURRENCY` | RAGAnything | (commented in E),L,C | same | `1` | KEEP |
| `J1_RAGANYTHING_COMPILER_PROCESSOR` | RAGAnything | (commented in E),C | same | (default class) | KEEP |
| `J1_RAGANYTHING_GRAPH_PROCESSOR` | RAGAnything | (commented in E),C | same | unset (graph optional) | KEEP |
| `J1_RAGANYTHING_RETRIEVAL_PROCESSOR` | RAGAnything | (commented in E),C | same | unset | KEEP |
| `J1_RAGANYTHING_SUPPORTS_TABLE` | RAGAnything | C only | same | `true` | INTERNAL — remove from public surface |
| `J1_RAGANYTHING_SUPPORTS_IMAGE` | RAGAnything | C only | same | `true` | INTERNAL |
| `J1_RAGANYTHING_SUPPORTS_EQUATION` | RAGAnything | C only | same | `true` | INTERNAL |
| `J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS` | RAGAnything | (commented in E),C | same | (sensible default) | KEEP |
| `J1_RAGANYTHING_LIBREOFFICE_BINARY` | RAGAnything | (commented in E),C | same | `soffice` | KEEP |
| `J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS` | RAGAnything | (commented in E),C | same | `120` | KEEP |

### Graphify provider (optional)

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_GRAPHIFY_ENABLED` | Graphify | E,L,C | `src/j1/providers/graphify/settings.py` | `false` | KEEP |
| `J1_GRAPHIFY_MODE` | Graphify | E,L,C | same | `cli` | KEEP |
| `J1_GRAPHIFY_COMMAND` | Graphify | E,L,C | same | `graphify` | KEEP |
| `J1_GRAPHIFY_WORKDIR` | Graphify | E,L,C | same | `./data/graphify` | KEEP |
| `J1_GRAPHIFY_GRAPH_PROCESSOR` | Graphify | (commented in E),C | same | unset | KEEP |

### Other

| Name | Group | Decl | Read site | Default | Status |
| --- | --- | --- | --- | --- | --- |
| `J1_INGESTION_BATCH_MAX_FILES` | Ingestion | C only | `src/j1/adapters/rest/intake.py` | (constant) | ADD to E (commented) |

## Section 2 — Cleanup targets verified

### 2.A Split-mode / pipeline-mode — CONFIRMED DEAD

- `J1_RAGANYTHING_PIPELINE_MODE` appears in `.env.example` lines
  181-202 (commented example) and in local `.env` (commented set).
  **Zero Python read sites.** The `process_document_complete` string
  is referenced 14 times in `src/j1/providers/raganything/` but those
  are the **active** RAGAnything API; there is no longer a code branch
  for `split_parse_insert` vs `complete`.
- `.env.example` describes `complete` as "legacy single-shot" and
  `split_parse_insert` as RECOMMENDED — **both characterisations are
  wrong post-cleanup.** `process_document_complete` is the only
  supported call shape; `split_parse_insert` no longer exists.

**Action**: remove the 22-line `J1_RAGANYTHING_PIPELINE_MODE` block
from `.env.example`. Drop the line from local `.env` (safe — it's
been ignored for months). No code change required.

### 2.B Legacy planning vocabulary

The planning module deliberately maintains a deprecation alias:
- `J1_INGEST_PLAN_MODE` (operator-facing, `rule_based|llm|hybrid`)
  is the canonical knob.
- `J1_LLM_PLANNING_ENABLED` is the legacy boolean; the resolver in
  `src/j1/processing/planning_settings.py:176-184` documents that
  `J1_INGEST_PLAN_MODE` overrides it.

**Recommendation**: keep both for one more release. Add an explicit
DEPRECATED tag to the `J1_LLM_PLANNING_ENABLED` comment in
`.env.example`, drop it from the "recommended" examples (it's
already commented out, no behaviour change). A runtime deprecation
warning could be emitted on set, but emitting from a workflow
context is awkward; defer.

`J1_PLANNING_ENABLED` and `J1_POST_COMPILE_PLANNING_ENABLED` are
separate concepts (master switch + stage gate). Keep both.

### 2.C Civil-engineering naming in generic settings — present, cosmetic

Two references in `.env.example`:
- Line 494: "e.g. Civil Engineering recognises BOQ / inspection
  reports / drawings and tunes planning accordingly". This is an
  example of HOW a domain pack works.
- Line 508: example value `J1_ALLOWED_DOMAIN_OVERRIDES=general,civil_engineering`.

Code is not domain-coupled: `src/j1/domains/` ships a generic
DomainPack registry; civil engineering is one shipped example.

**Action**: rephrase the comment to make the example domain-agnostic
("e.g. a finance domain might recognise invoices, contracts, and
financial statements"). Drop civil_engineering from the example
value of `J1_ALLOWED_DOMAIN_OVERRIDES` — leave it as just
`general`. The pack itself still ships, but the public-facing config
is now domain-neutral.

### 2.D OpenKB references — none found

No `openkb`, `OPENKB`, `OpenKB` string anywhere under `src/j1/`,
`deploy/`, `frontend/src/`, `.env.example`, or `.env`. Clean.

### 2.E `_build_plan` / `_apply_post_compile_planning` — none in env layer

No env var gates those methods. Memory says they are deferred (not
built). Their absence in the runtime is policy, not config. No
.env action needed.

### 2.F Phase-based naming in env comments

Found:
- `.env.example` line 398: `# ---- FAST LLM role (Phase B, optional) ----`

**Action**: rename to `# ---- FAST LLM role (cheap / fast role,
optional) ----`. Pure comment change.

### 2.G Duplicate provider settings

- `J1_TEXT_LLM_BASE_URL` vs `J1_RAGANYTHING_VLM_HTTP_SERVER_URL`:
  not duplicates. Text LLM is a separate role; VLM endpoint is a
  MinerU-specific layout-extraction VLM (different protocol). The
  `.env.example` documents the fallback chain
  (VLM → `J1_VISION_LLM_BASE_URL`). Keep both, the comment is
  already correct.

- `J1_ENRICH_*` (bootstrap-side modality switches) vs
  `J1_ENRICHMENT_*` (enrichment module loader settings): both
  live. Different code paths read them. `J1_ENRICH_ENABLED` is the
  bootstrap master switch; `J1_ENRICHMENT_ENABLED` toggles the
  enrichment-module ensemble. **Potential confusion.** Recommend
  documenting the relationship in `.env.example` but not renaming
  (renames touch >30 code sites and `.env` files in the wild).

### 2.H Negative flags — none found

All boolean env vars are positive (`*_ENABLED=true`, etc.). Clean.

### 2.I Wiring gap — `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED`

`.env.example` line 530 sets `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true`
and the comments at lines 514-529 describe it as the deployment-wide
opt-in. **But the variable is never read anywhere in Python code.**
The `ProjectProcessingWorkflow.run` request type has a
`search_attributes_enabled: bool = False` field that the workflow
respects, but no deploy entrypoint reads the env var and supplies
it. Only `tests/` set it to True via direct request construction.

**Impact**: setting `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true`
today does nothing. The dev compose stack's `temporal-init` service
registers the attributes correctly, but the workflow never upserts
them because `request.search_attributes_enabled` stays False.

**Recommendation**: document the gap in CONFIG_AUDIT.md (this
section) and add a follow-up tag in `.env.example`. Wiring the env
var to `_wiring.py` / the request builder is a 5-10 line code
change but counts as a behaviour change (would start emitting search
attributes from the dev stack), so it's outside this non-destructive
audit's scope.

### 2.J Civil-engineering in domain audit

The domain pack itself (`src/j1/domains/civil_engineering/`) is
shipped intentionally as a working domain example. No action — keep.

## Section 3 — Variables in `.env.example` but never referenced in source

After filtering false positives (TEXT/VISION/EMBEDDING/FAST regex
prefix collisions), the truly never-read entries are:

| Name | Verdict |
| --- | --- |
| `J1_RAGANYTHING_PIPELINE_MODE` | **REMOVE** — dead split-mode artifact (Section 2.A) |
| `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` | Keep + add wiring-gap warning (Section 2.I). Not dead per se — wired into `request` type, just not from env. |

Everything else cross-references cleanly.

## Section 4 — Variables in source code but missing from `.env.example`

37 names. Categorised:

| Category | Names | Action |
| --- | --- | --- |
| Auth (advanced) | `J1_AUTH_REQUIRED`, `J1_AUTH_JWT_ENABLED`, `J1_AUTH_DEFAULT_TENANT_ID`, `J1_AUTH_ANONYMOUS_PATHS` | ADD as commented examples |
| Webhooks (advanced) | `J1_WEBHOOK_ENABLED`, `J1_WEBHOOK_SUBSCRIPTIONS`, `J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS`, `J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS` | ADD as commented examples |
| Events (advanced) | `J1_EVENT_PUBLISHER_PRODUCER`, `J1_EVENT_PUBLISHER_SCHEMA_VERSION`, `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS` | KEEP UNDOCUMENTED — internal advanced tuning |
| Compile retry | `J1_COMPILE_RETRY_ENABLED`, `J1_COMPILE_MAX_ATTEMPTS`, `J1_COMPILE_RETRY_MIN_CHUNKS`, `J1_COMPILE_RETRY_MIN_TEXT_CHARS`, `J1_ASSESSMENT_FAILURE_POLICY` | ADD as commented examples |
| Enrichment (advanced) | `J1_ENRICHMENT_ENABLED`, `J1_ENRICHMENT_REQUIRE_SUCCESS`, `J1_ENRICHMENT_TIMEOUT_SECONDS`, `J1_ENRICHMENT_RETRY_LIMIT`, `J1_ENRICHMENT_MAX_CONCURRENT_ENRICHMENT_TASKS`, `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS`, `J1_ENRICHMENT_DEV_MODE_CONSERVATIVE_LIMITS`, `J1_DEFAULT_ENRICHMENT_MODEL_TIER`, `J1_ENRICH_ASSESSMENT_FAST_LLM_*` (4) | ADD as commented examples; specifically call out `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS` as the dev-LLM-cost lever |
| Planning (advanced) | `J1_PLANNING_MAX_EARLY_PAGES`, `J1_PLANNING_TRACE_ENABLED`, `J1_PLANNING_TRACE_BODY`, `J1_POST_COMPILE_PLANNING_ENABLED` | ADD as commented examples |
| RAGAnything internal | `J1_RAGANYTHING_ALLOWED_PARSE_METHODS`, `J1_RAGANYTHING_SUPPORTS_*` (3) | KEEP UNDOCUMENTED — internal capability flags |
| Ingestion | `J1_INGESTION_BATCH_MAX_FILES` | ADD as commented example |

## Section 5 — Variables set in local `.env` but missing from `.env.example`

After Section 4 / "false positive" filtering: **none**. Every name
in local `.env` matches either a documented entry or a still-commented
example in `.env.example`. (The previous grep diff listed 30 names
but all are commented-out examples in `.env.example`, not omissions.)

## Section 6 — Proposed non-destructive cleanup (this PR)

1. **Delete `J1_RAGANYTHING_PIPELINE_MODE` block** from
   `.env.example` (lines 181-202 inclusive, plus the immediately
   preceding linkage). Confirmed dead.
2. **Add a "DEPRECATED" tag** to the `J1_LLM_PLANNING_ENABLED`
   commented line in `.env.example`, pointing to
   `J1_INGEST_PLAN_MODE` as the replacement.
3. **Rephrase `# ---- FAST LLM role (Phase B, optional) ----`** to
   drop the "Phase B" reference. Pure comment.
4. **Rephrase the civil-engineering example** in the Domain pack
   section to be domain-agnostic; drop `civil_engineering` from the
   `J1_ALLOWED_DOMAIN_OVERRIDES` example value.
5. **Drop the "legacy single-shot `process_document_complete`"
   characterisation** — `process_document_complete` is the active
   API; describe it neutrally if at all.
6. **Add a wiring-gap note** to the
   `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` block. Operators
   currently expect the variable to work; documenting that it isn't
   wired prevents surprise.
7. **Add commented examples** for the most operator-relevant
   undocumented variables (auth, webhooks, compile retry,
   enrichment LLM concurrency cap, planning trace).

**Not in this PR** (deferred — destructive or behavioural):
- Renaming `J1_ENRICH_*` ↔ `J1_ENRICHMENT_*` to remove duplication.
- Wiring `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` to the deploy
  entrypoint (behaviour change).
- Removing `J1_LLM_PLANNING_ENABLED` and the deprecation-alias
  branch in `planning_settings.py`.
- Emitting runtime deprecation warnings.
- Frontend / docker-compose comment cleanup.

## Section 7 — Risks

- Removing the `PIPELINE_MODE` block from local `.env` files in the
  wild is a no-op (nobody reads the value). No risk.
- The wording changes are pure comments. No risk.
- The Domain Packs example reword does not change behaviour — the
  shipped civil_engineering pack still works.
- The undocumented-vars additions are all `#`-commented so they
  cannot accidentally change defaults.

## Section 8 — Tests to add

- An existence regression test: every variable name actually read by
  `src/j1/` Python code should either appear in `.env.example`
  (potentially commented) OR be on a documented exclusion list
  (internal-only). Counter-test: every variable NAMED in
  `.env.example` should be referenced somewhere in source code or
  on a documented docker-only/frontend-only list.

- A "no retired-config-vocabulary" regression test akin to the
  existing planning-vocabulary regression — pin that
  `pipeline_mode`, `split_parse_insert`, `phase b`, `civil
  engineering recognises` (as a phrase) do NOT appear in
  `.env.example`.

Both tests are non-trivial fixtures but small additions to the
existing `tests/test_docs_and_cleanup.py` patterns. Out of scope
for this PR; flagged for follow-up.

## Section 9 — Future cleanup (separate PR)

- Group everything under the prescribed prefix convention
  (`J1_TEMPORAL_*`, `J1_INGEST_*`, `J1_LLM_*`, …) — most of the
  surface already matches; the exceptions are
  `J1_ENRICH_*`/`J1_ENRICHMENT_*` duplication.
- Wire `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` through
  `_wiring.py` to `ProjectProcessingRequest`.
- Move `J1_RAGANYTHING_SUPPORTS_{TABLE,IMAGE,EQUATION}` from env to
  capability detection inside the adapter (env override stays but
  default comes from probing).
- Drop `J1_LLM_PLANNING_ENABLED` and its deprecation alias once a
  release has passed.
