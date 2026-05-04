# J1 Environment Variable Reference

Single source-of-truth for every `J1_*` environment variable J1 reads.
Variables are derived from [`/.env.example`](../../.env.example) and the
settings modules under [`src/j1/`](../../src/j1/) (each section links
to the loader). For per-area context (auth specifics, webhook
delivery semantics, etc.) follow the cross-references at the end of
each subsection.

> **Conventions.**
> - All variables use the `J1_` prefix.
> - Empty strings are treated as unset for most variables.
> - Booleans accept `1`, `true`, `yes`, `on` (case-insensitive).
> - JSON-shaped variables (e.g. `J1_TEXT_LLM_LANGCHAIN_CONFIG`) must
>   decode to a JSON **object** — anything else fails fast at startup.
> - `*_FILE` variants point at a JSON file mounted from a secret manager;
>   the inline and `_FILE` forms of the same setting are mutually
>   exclusive.
> - Anything marked **NEEDS VERIFICATION** could not be confirmed from
>   docs+code alone at the time of writing — verify against the
>   relevant settings module before relying on it.

---

## 1. Core runtime

Loader: [`src/j1/config/settings.py`](../../src/j1/config/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_DATA_ROOT` | No | `/data/j1` | Workspace resolver, intake, registries, search index, audit/cost sinks | Absolute filesystem path to the workspace root. All per-tenant / per-project paths derive from this. | **Must be absolute** — `load_settings()` raises `ConfigError` otherwise. In Docker the container path is mapped to a named volume; on the host it's typically a tmpfs / mounted directory. |

---

## 2. Temporal

Loader: [`src/j1/orchestration/temporal/config.py`](../../src/j1/orchestration/temporal/config.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_TEMPORAL_TARGET` | No | `localhost:7233` | Temporal client + worker | `host:port` of the Temporal frontend. | In Docker compose, set to `temporal:7233` — the service-DNS name; never `localhost` from inside a container. |
| `J1_TEMPORAL_NAMESPACE` | No | `default` | Temporal client + worker | Temporal namespace. | The bundled `temporalio/auto-setup` image creates `default` on boot. |
| `J1_TEMPORAL_TASK_QUEUE` | No | `j1-default` | Temporal client + worker | Task queue name shared between the workflow starter and the worker. | Must match across processes — a typo silently means workflows are accepted but never picked up. |
| `J1_TEMPORAL_TLS` | No | `false` | Temporal client | Enable TLS for the Temporal connection. | Boolean (truthy strings: `1`, `true`, `yes`, `on`). |
| `J1_TEMPORAL_API_KEY` | No | _(unset)_ | Temporal client | API key for Temporal Cloud or other authenticated clusters. | **Secret** — load from a secret manager; never commit. |

See also: [`docs/operations/temporal.md`](../operations/temporal.md).

---

## 3. API / REST

The REST adapter does not consume `J1_*` env vars itself — its
behaviour is configured at construction time via `create_rest_api(...)`
parameters. Two variables are read by the bundled dev entrypoint:

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_API_PORT` | No | `8000` | [`deploy/dev/api.py`](../../deploy/dev/api.py); [`deploy/dev/docker-compose.yml`](../../deploy/dev/docker-compose.yml) | TCP port the bundled dev FastAPI process binds to inside the container. | Compose maps it to the same host port. Production deployments typically run their own ASGI host and ignore this. |
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | No | `5` | [`deploy/dev/worker.py`](../../deploy/dev/worker.py) | Maximum number of activities a single worker process runs in parallel. | Tune to taste. Production deployments typically bypass this in favour of explicit `WorkerSpec` config. |

See also: [`docs/rest-api.md`](../rest-api.md).

---

## 4. Security / auth

Loader: [`src/j1/integration/security/settings.py`](../../src/j1/integration/security/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_AUTH_REQUIRED` | No | `false` | Security loader (`SecuritySettings.auth_required`) | Advisory boolean — surfaced on `SecuritySettings.auth_required` for deployment glue to consult. The framework itself does not enforce it: anonymous mode is determined entirely by whether an `authenticator=` is passed to `create_rest_api(...)`. Set this when your composition root needs to gate "did the operator intend to require auth?" before wiring an authenticator. |  |
| `J1_AUTH_API_KEYS` | No | _(unset)_ | API-key authenticator | Inline JSON object keyed by token: `{"<token>":{"subject":...,"tenant_id":...,"scopes":[...]}}`. | **Secret** — never commit. Mutually exclusive with `J1_AUTH_API_KEYS_FILE`. |
| `J1_AUTH_API_KEYS_FILE` | No | _(unset)_ | API-key authenticator | Filesystem path to a JSON file with the same shape as `J1_AUTH_API_KEYS`. Designed for secret-manager mounts. | Mutually exclusive with `J1_AUTH_API_KEYS`. |
| `J1_AUTH_JWT_ENABLED` | No | `false` | JWT authenticator | Boolean — flag to indicate the deployment intends to wire `JwtAuthenticator`. The verifier callable itself is still injected programmatically. |  |
| `J1_AUTH_ANONYMOUS_PATHS` | No | _(unset)_ | Security loader | Comma-separated list of URL paths that bypass authentication (e.g. `/health,/capabilities`). | Useful for liveness probes. |
| `J1_AUTH_DEFAULT_TENANT_ID` | No | _(unset)_ | Security loader | Default tenant assigned to anonymous-mode requests. | Only consulted when no `X-Tenant-Id` header is present and authenticator is anonymous. |

See also: [`docs/security.md`](../security.md).

---

## 5. Text LLM role

Loader: [`src/j1/llm/settings.py`](../../src/j1/llm/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_TEXT_LLM_PROVIDER` | No | `openai_compat` | Text-LLM client factory | Provider type. Allowed values: `openai_compat`, `langchain`. | Unknown values raise `LLMConfigError` at startup. |
| `J1_TEXT_LLM_BASE_URL` | When `openai_compat` | _(unset)_ | OpenAI-compat text client | HTTP base URL of the chat-completions endpoint. | Required for the OpenAI-compat provider; ignored for `langchain`. |
| `J1_TEXT_LLM_API_KEY` | When provider needs it | _(unset)_ | Text LLM client | Bearer token forwarded to the upstream provider. | **Secret** — never commit. |
| `J1_TEXT_LLM_MODEL` | When `openai_compat` | _(unset)_ | OpenAI-compat text client | Model identifier the upstream provider expects. |  |
| `J1_TEXT_LLM_TIMEOUT_SECONDS` | No | `60` | Text LLM client | HTTP timeout per call. | Float; fail-loud on non-numeric. |
| `J1_TEXT_LLM_MAX_RETRIES` | No | `3` | Text LLM client | Retry budget for transient errors. | Integer. |
| `J1_TEXT_LLM_TEMPERATURE` | No | `0.2` | Text LLM client | Decoder temperature passed through to the provider. | Float. |
| `J1_TEXT_LLM_MAX_OUTPUT_TOKENS` | No | `4096` | Text LLM client | Per-call output cap. | Integer. |
| `J1_TEXT_LLM_LANGCHAIN_CONFIG` | When `langchain` | `{}` | LangChain text adapter | JSON object containing the `class` (alias from the catalog or `module:Class`) plus constructor kwargs. | Must decode to an object. **Secret** — kwargs may include API keys. |

See also: [`docs/providers.md`](../providers.md) § 1.

---

## 6. Vision LLM role

Loader: [`src/j1/llm/settings.py`](../../src/j1/llm/settings.py)

Same shape as the text role, with vision-friendly defaults.

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `J1_VISION_LLM_PROVIDER` | No | `openai_compat` | Vision-LLM client factory | `openai_compat` or `langchain`. |
| `J1_VISION_LLM_BASE_URL` | When `openai_compat` | _(unset)_ | OpenAI-compat vision client | HTTP base URL. |
| `J1_VISION_LLM_API_KEY` | When provider needs it | _(unset)_ | Vision LLM client | **Secret.** |
| `J1_VISION_LLM_MODEL` | When `openai_compat` | _(unset)_ | OpenAI-compat vision client | Vision-capable model name. |
| `J1_VISION_LLM_TIMEOUT_SECONDS` | No | `90` | Vision LLM client | Higher default than text — vision calls take longer. |
| `J1_VISION_LLM_MAX_RETRIES` | No | `3` | Vision LLM client | |
| `J1_VISION_LLM_TEMPERATURE` | No | `0.1` | Vision LLM client | Lower default than text; vision tasks favour determinism. |
| `J1_VISION_LLM_MAX_OUTPUT_TOKENS` | No | `4096` | Vision LLM client | |
| `J1_VISION_LLM_LANGCHAIN_CONFIG` | When `langchain` | `{}` | LangChain vision adapter | JSON object; same rules as the text equivalent. **Secret.** |

Required when *any* of `J1_ENRICH_IMAGES` / `J1_ENRICH_DIAGRAMS` /
`J1_ENRICH_SCANNED_PAGES` is true (the bootstrap raises `ConfigError`
otherwise). See also: [`docs/providers.md`](../providers.md) § 1.

---

## 7. Embedding model

Loader: [`src/j1/llm/settings.py`](../../src/j1/llm/settings.py)

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `J1_EMBEDDING_PROVIDER` | No | `openai_compat` | Embedding client factory | `openai_compat` or `langchain`. |
| `J1_EMBEDDING_BASE_URL` | When `openai_compat` | _(unset)_ | OpenAI-compat embedding client | HTTP base URL. |
| `J1_EMBEDDING_API_KEY` | When provider needs it | _(unset)_ | Embedding client | **Secret.** |
| `J1_EMBEDDING_MODEL` | When `openai_compat` | _(unset)_ | OpenAI-compat embedding client | Embedding model identifier. |
| `J1_EMBEDDING_DIM` | No | _(unset)_ | Embedding client | Expected vector dimension. Used to validate provider output and to allocate vector storage. |
| `J1_EMBEDDING_MAX_TOKENS` | No | `8192` | Embedding client | Per-input token cap; longer inputs are chunked. |
| `J1_EMBEDDING_BATCH_SIZE` | No | `32` | Embedding client | Embeddings per HTTP call. |
| `J1_EMBEDDING_TIMEOUT_SECONDS` | No | `60` | Embedding client | HTTP timeout per call. |
| `J1_EMBEDDING_MAX_RETRIES` | No | `3` | Embedding client | |
| `J1_EMBEDDING_LANGCHAIN_CONFIG` | When `langchain` | `{}` | LangChain embedding adapter | JSON object; same rules as the text equivalent. **Secret.** |

Required when the selected compiler / retrieval provider needs
embeddings (RAGAnything always does). See also:
[`docs/providers.md`](../providers.md) § 1.

---

## 8. RAGAnything provider

Loader: [`src/j1/providers/raganything/settings.py`](../../src/j1/providers/raganything/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_RAGANYTHING_MODE` | No | `local` | RAGAnything settings loader | Free-form mode string; consumed by the bridge / a deployment-supplied processor hook. |  |
| `J1_RAGANYTHING_WORKDIR` | No | `./data/raganything` | RAGAnything bridge | Filesystem directory the vendor uses for its own working files. | Created on first use. |
| `J1_RAGANYTHING_STORAGE_DIR` | No | `<workdir>/storage` | RAGAnything bridge | Storage directory the vendor writes graph + KV-store files to. | Inferred from `WORKDIR` if unset. |
| `J1_RAGANYTHING_CACHE_DIR` | No | `<workdir>/cache` | RAGAnything bridge | Cache directory. | Inferred from `WORKDIR` if unset. |
| `J1_RAGANYTHING_COMPILER_PROCESSOR` | No | _(unset)_ | RAGAnything compiler | Override the default Python bridge with `module.path:callable_name`. The class-loader allowlist must accept the module prefix. | Bypasses the default `_bridge.py`. |
| `J1_RAGANYTHING_GRAPH_PROCESSOR` | No | _(unset)_ | RAGAnything graph builder | Override hook for the graph stage (same format). |  |
| `J1_RAGANYTHING_RETRIEVAL_PROCESSOR` | No | _(unset)_ | RAGAnything query provider | Override hook for retrieval (same format). |  |
| `J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS` | No | `.doc,.xls,.ppt,.rtf,.odt,.ods,.odp,.pages,.numbers,.key,.wps` | RAGAnything compiler bridge | Comma-separated list of file extensions (with or without leading dot, case-insensitive) for which the bridge pre-converts to PDF via `soffice --headless --convert-to pdf` before handing the document to raganything. Covers legacy / non-OOXML formats raganything's native parsers can't read. | Set to empty (`J1_RAGANYTHING_PDF_CONVERT_EXTENSIONS=`) to disable conversion entirely. The conversion requires the LibreOffice headless binary (see next row). |
| `J1_RAGANYTHING_LIBREOFFICE_BINARY` | No | `soffice` | RAGAnything compiler bridge | Name or absolute path of the LibreOffice headless binary. Resolved via `$PATH`. Some distros ship `libreoffice` as the user-facing alias. | When the binary isn't found, the bridge raises `ProviderUnavailable` with an actionable message. |
| `J1_RAGANYTHING_LIBREOFFICE_TIMEOUT_SECONDS` | No | `120` | RAGAnything compiler bridge | Per-conversion timeout. LibreOffice can be slow on first launch (font cache rebuild). | Must be > 0; non-numeric values fail at startup. |

See also: [`docs/providers.md`](../providers.md) § 2 (RAGAnything section).

---

## 9. Graphify provider

Loader: [`src/j1/providers/graphify/settings.py`](../../src/j1/providers/graphify/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_GRAPHIFY_ENABLED` | No | `false` | Bootstrap selection check | Boolean — when false, selecting `graphify` as the default graph provider raises `ConfigError`. |  |
| `J1_GRAPHIFY_MODE` | No | `cli` | Graphify bridge | `cli` (subprocess) or `python` (lazy-imported package). |  |
| `J1_GRAPHIFY_COMMAND` | No | `graphify` | Graphify CLI bridge | Binary name (resolved via `PATH`) or absolute path. | Only consulted when `MODE=cli`. |
| `J1_GRAPHIFY_WORKDIR` | No | `./data/graphify` | Graphify CLI bridge | Working directory passed to the binary. | Created on first use. |
| `J1_GRAPHIFY_GRAPH_PROCESSOR` | No | _(unset)_ | Graphify graph builder | Override hook (`module.path:callable_name`). | Bypasses the default bridge entirely. |

See also: [`docs/providers.md`](../providers.md) § 2 (Graphify section).

---

## 10. Provider selection

Loader: [`src/j1/compose/bootstrap.py`](../../src/j1/compose/bootstrap.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_DEFAULT_COMPILER` | No | `raganything` | Bootstrap | Name of the registered compiler used by default. Built-in values: `raganything`, `mock`. | The bundled `.env.example` ships with `mock` so the dev stack runs end-to-end without LLM credentials; flip to `raganything` once an LLM endpoint is configured. Unknown values raise `ConfigError` listing the registered providers. |
| `J1_DEFAULT_GRAPH_PROVIDER` | No | `raganything` | Bootstrap | Default graph builder. Built-in values: `raganything`, `graphify`, `mock`. | Selecting `graphify` requires `J1_GRAPHIFY_ENABLED=true`. The `.env.example` ships with `mock`. |
| `J1_DEFAULT_RETRIEVAL_PROVIDER` | No | `raganything` | Bootstrap | Default retrieval / query provider. Built-in values: `raganything`, `mock`. | The `mock` retriever has an empty corpus — it returns `RetrievalResult(evidences=[])` rather than fabricating answers. |

---

## 11. Enrichment toggles

Loader: [`src/j1/compose/bootstrap.py`](../../src/j1/compose/bootstrap.py)

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `J1_ENRICH_ENABLED` | No | `true` | Bootstrap | Master switch for the enrichment stage. |
| `J1_ENRICH_CONFIDENCE_THRESHOLD` | No | `0.75` | Bootstrap | Confidence cutoff below which findings escalate to review. |
| `J1_ENRICH_IMAGES` | No | `true` | Bootstrap | Whether the image-modality enricher runs. Requires the vision role. |
| `J1_ENRICH_TABLES` | No | `true` | Bootstrap | Whether the table-modality enricher runs. |
| `J1_ENRICH_DIAGRAMS` | No | `true` | Bootstrap | Whether the diagram-modality enricher runs. Requires the vision role. |
| `J1_ENRICH_SCANNED_PAGES` | No | `true` | Bootstrap | Whether the scanned-page enricher runs. Requires the vision role. |

When any vision-requiring modality is enabled and no vision LLM is
configured, `Bootstrap.build()` raises `ConfigError` with an
actionable message naming the missing env vars.

---

## 12. Events / publisher

Loader: [`src/j1/integration/events/publisher_settings.py`](../../src/j1/integration/events/publisher_settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_EVENT_PUBLISHER_TYPE` | No | `noop` | Event-publisher factory | One of `noop`, `memory`, `bus`, `composite` (and broker-specific values for deployment-supplied publishers). | Unknown values raise `LLMConfigError`-style at load. |
| `J1_EVENT_PUBLISHER_PRODUCER` | No | `j1` | Event publisher | Logical producer identifier set on every published envelope's `producer` header. |  |
| `J1_EVENT_PUBLISHER_SCHEMA_VERSION` | No | `1.0` | Event publisher | `schemaVersion` header on every published envelope. | Bump when payload shape changes incompatibly. |
| `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS` | No | `false` | Event publisher | Boolean — include sensitive payload fields in published events. | Off by default — events are designed for downstream consumers that may not be authorised to see raw content. |

See also: [`docs/event-integration.md`](../event-integration.md).

---

## 13. Webhooks

Loader: [`src/j1/integration/events/settings.py`](../../src/j1/integration/events/settings.py)

| Name | Required | Default | Used by | Description | Notes |
|---|---|---|---|---|---|
| `J1_WEBHOOK_ENABLED` | No | `false` | Webhook subscriber | Boolean — gate for the webhook delivery subsystem. |  |
| `J1_WEBHOOK_SUBSCRIPTIONS` | No | _(unset)_ | Subscription registry | Inline JSON list of subscription specs. | Mutually exclusive with `J1_WEBHOOK_SUBSCRIPTIONS_FILE`. **Secret** — subscriptions include shared HMAC keys. |
| `J1_WEBHOOK_SUBSCRIPTIONS_FILE` | No | _(unset)_ | Subscription registry | Filesystem path to a JSON file with the same shape. | Designed for secret-manager mounts. |
| `J1_WEBHOOK_DEFAULT_TIMEOUT_SECONDS` | No | `10.0` | Webhook delivery service | Default per-attempt HTTP timeout for outbound webhook posts. | Per-subscription `timeout_seconds` overrides this default. |
| `J1_WEBHOOK_DEFAULT_MAX_ATTEMPTS` | No | `5` | Webhook delivery service | Default retry-attempt cap before a delivery is marked failed. | Per-subscription `retry_max_attempts` overrides this default. The retry loop also uses fixed defaults from the same module: initial delay `1.0s`, backoff factor `2.0`, max delay `60.0s` (per-subscription override fields: `retry_initial_delay_seconds`, `retry_backoff`, `retry_max_delay_seconds`). |

See also: [`docs/webhooks.md`](../webhooks.md).

---

## 14. Worker runtime

Worker behaviour is configured at construction time via `WorkerSpec`
and the Temporal env vars (§ 2). Two convenience env vars are read
by the bundled dev entrypoint:

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | No | `5` | [`deploy/dev/worker.py`](../../deploy/dev/worker.py) | Caps simultaneous in-flight activities per worker process. |
| _(All `J1_TEMPORAL_*`)_ | _(see § 2)_ | — | Worker | Same Temporal connection vars — workers share them with the API. |

See also: [`docs/operations/temporal.md`](../operations/temporal.md).

---

## 15. Development / testing

The test suite is hermetic — it injects env values via fixtures and
does not require any `J1_*` variables to be set. Common
non-framework variables:

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `PYTHONPATH` | No | _(set by editable install)_ | Pytest | Test runner imports `j1` from `src/`. The pyproject.toml's `[tool.pytest.ini_options].pythonpath = ["src"]` handles this automatically. |
| `PYTEST_*` | No | — | Pytest | Standard pytest env vars; not consumed by the framework. |

---

## 16. Index by use case

| If you want to … | Set at minimum |
|---|---|
| Run unit tests | _(nothing — fixtures inject values)_ |
| Run the dev Docker stack | Copy [`.env.example`](../../.env.example) → `.env` |
| Bring up the REST API standalone | `J1_DATA_ROOT` |
| Start a Temporal worker | `J1_DATA_ROOT`, `J1_TEMPORAL_TARGET`, `J1_TEMPORAL_NAMESPACE`, `J1_TEMPORAL_TASK_QUEUE` |
| Drive a real LLM-backed pipeline | The variables above + `J1_TEXT_LLM_*` + `J1_EMBEDDING_*` (+ `J1_VISION_LLM_*` if visual enrichment is on) |
| Use Graphify instead of the default graph provider | `J1_GRAPHIFY_ENABLED=true` + `J1_DEFAULT_GRAPH_PROVIDER=graphify` (+ `J1_GRAPHIFY_COMMAND` if not on `PATH`) |
| Require API authentication | Either `J1_AUTH_API_KEYS` or `J1_AUTH_API_KEYS_FILE` (and pass an `authenticator=` to `create_rest_api`) |
| Deliver events to webhooks | `J1_WEBHOOK_ENABLED=true` + `J1_WEBHOOK_SUBSCRIPTIONS[_FILE]` + `J1_EVENT_PUBLISHER_TYPE=bus` |

---

## 17. Cross-references

- [`README.md`](../../README.md) — install + first-run quickstart
- [`docs/development/onboarding.md`](../development/onboarding.md) — sequenced "from zero to first query"
- [`docs/architecture.md`](../architecture.md) — what consumes each setting at the architecture level
- [`docs/providers.md`](../providers.md) — provider-specific configuration (RAGAnything, Graphify, LLM roles)
- [`docs/security.md`](../security.md) — authentication / authorization specifics
- [`docs/webhooks.md`](../webhooks.md) — webhook delivery semantics
- [`docs/event-integration.md`](../event-integration.md) — event publisher + AsyncAPI contract
- [`docs/operations/temporal.md`](../operations/temporal.md) — Temporal worker operations
- [`deploy/dev/README.md`](../../deploy/dev/README.md) — local Docker stack walkthrough
