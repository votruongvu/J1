# Local development with Docker

A minimal Docker Compose stack for running the J1 framework locally.
Brings up a REST API, a Temporal worker, the J1 Execution Console
SPA, a Temporal server (with Postgres for its own storage), and the
Temporal web UI on a single laptop.

> **First time?** Read [`docs/development/onboarding.md`](../../docs/development/onboarding.md)
> first — it walks the full path from install through first
> workflow trigger and points back here when you reach the Docker
> step. For the canonical list of `J1_*` environment variables see
> [`docs/configuration/environment.md`](../../docs/configuration/environment.md).
> For Temporal-specific operations (signals, recovery, scaling
> workers) see [`docs/operations/temporal.md`](../../docs/operations/temporal.md).

> **About Postgres.** It's here **only as Temporal's storage backend**
> — Temporal's official `auto-setup` image only supports
> mysql8 / postgres12 / postgres12_pgx / cassandra. The J1 framework
> itself does **not** use a relational database; J1 state lives in
> flat-file JSON registries + per-project SQLite FTS5 under
> `J1_DATA_ROOT`. The Postgres container's life is fully internal to
> Temporal.

This stack is **for development only** — not a production deployment.
See [docs/architecture.md](../../docs/architecture.md) for the full
architecture, and the per-area docs (security, webhooks,
event-integration, …) for production hardening.

---

## 1. Prerequisites

- Docker (20.10+)
- Docker Compose (v2 — invoked as `docker compose`, not
 `docker-compose`)
- Free local ports: **8000** (API), **8081** (Frontend),
 **7233** (Temporal gRPC), **8080** (Temporal UI)

That's it. No Python install, no Postgres, no Redis, no S3, no
Node toolchain on the host (the frontend image builds its own).

---

## 2. Bring it up

```bash
# 1. Copy the env template
cp.env.example.env

# 2. Start everything
docker compose -f deploy/dev/docker-compose.yml up --build
```

The first run takes 3–5 minutes (image build + Temporal schema init).
Subsequent runs are fast — the build is layered to keep the heavy
work cached:

| Layer | Re-runs when… | First-build cost | Cached cost |
|---|---|---|---|
| `apt-get install …` (LibreOffice, OpenCV libs) | Dockerfile system-deps change | ~60s | ~5s |
| `pip install -e.[all-providers]` (torch, transformers, mineru, langchain-*) | `pyproject.toml` changes | ~3min | ~30–60s |
| `COPY src/`, `COPY deploy/` | Source edits | ~1s | ~1s |

Both heavy layers use **BuildKit cache mounts** (`--mount=type=cache`)
so even when their layer is invalidated by a `pyproject.toml` edit,
the wheel + apt-archive caches survive and rebuilds drop to ~30–60s
instead of re-downloading ~2 GB of dependencies.

A `.dockerignore` at the repo root excludes `.venv/`,
`frontend/node_modules/`, `.git/`, `__pycache__/`, and `tests/` from
the build context — without it, `docker compose build` ships ~2 GB
of dead bytes through Docker Desktop's FUSE bridge to the Linux VM
on every build.

### If your build is still slow

Common causes and fixes:

1. **Build context is huge** — run
 `du -sh.venv frontend/node_modules.git` and confirm
 `.dockerignore` actually excludes them. Then re-check Docker
 Desktop's "Resources → File sharing" → only `/Users/<you>` should
 be shared, not whole-disk.
2. **BuildKit isn't enabled** — Docker Desktop ≥4.0 enables it by
 default; older setups need `DOCKER_BUILDKIT=1` in the env. Check
 for the `# syntax=docker/dockerfile:1.6` directive in build
 output: if BuildKit is off you'll see a parsing warning.
3. **Cache was wiped** — `docker builder prune` or `docker system
 prune -a` clears the BuildKit cache. Next build will re-download.
4. **Apple Silicon emulation** — verify your image platform matches
 the host: `docker version` should show your arch (arm64 on M-series).
 Building an `amd64` image on M-series triggers QEMU emulation,
 which is **5–10× slower**. Add `platform: linux/arm64` to the
 service if the base image supports it (`python:3.13-slim` does).

### Editing Python code

`src/` and `deploy/` are bind-mounted into both `api` and `worker`,
so Python edits land without a rebuild — just restart the affected
container:

```bash
# After editing anything under src/ or deploy/:
docker compose -f deploy/dev/docker-compose.yml restart worker
docker compose -f deploy/dev/docker-compose.yml restart api
```

There's no auto-reload (Temporal workflow code is replay-sensitive,
so reloading half-edited modules into a running worker can corrupt
in-flight workflows). A rebuild is only needed when you change
`pyproject.toml`, the `Dockerfile`, or another build-time input.

> **MinerU runs as an HTTP client only.** J1 forces
> `J1_RAGANYTHING_BACKEND=vlm-http-client` (or `hybrid-http-client`)
> at startup and **rejects** the local-model backends (`pipeline` /
> `vlm-auto-engine` / `hybrid-auto-engine`) — see
> [src/j1/providers/raganything/settings.py](../../src/j1/providers/raganything/settings.py).
> The worker image does NOT ship MinerU model weights, no
> HuggingFace cache mount exists, and `J1_RAGANYTHING_VLM_HTTP_SERVER_URL`
> (or the project-wide `J1_VISION_LLM_BASE_URL` fallback) is required
> at startup — bootstrap fails with a clear `ConfigError` if neither
> is set.
>
> The endpoint must serve a **MinerU-trained layout VLM** —
> `opendatalab/MinerU2.5-Pro-2604-1.2B` is the canonical choice. A
> generic chat VLM (Gemma, plain LLaVA, etc.) replies with prose and
> MinerU drops every page with `Layout output does not match expected
> format`. LM Studio's GGUF catalog does not currently carry a MinerU
> VLM, so the typical local-dev split is LM Studio on `:1234` for the
> chat / vision / embedding roles and vLLM (or sglang) on `:1235` with
> MinerU2.5-Pro for parsing. See the LLM-role table further down for
> the full model-per-role contract.
>
> Operators who genuinely need MinerU's local-model code path must
> fork the deployment, remove the guard in `_validate_backend`, and
> wire their own model-cache mount. The default deployment treats
> "downloads multi-GB models inside the container" as a startup
> error — same posture J1 takes for every other model role.

Services that come up:

| Service | URL / Port | What it is |
|----------------|---------------------------------------|------------|
| `api` | http://localhost:8000 | J1 REST API (`python -m deploy.dev.api`) |
| `worker` | (no port — connects to Temporal) | J1 Temporal worker (`python -m deploy.dev.worker`) |
| `frontend` | http://localhost:8081 | J1 Execution Console SPA (nginx + Vite-built bundle, proxies `/api/*` to the api service) |
| `temporal` | localhost:7233 (gRPC) | Temporal server (`temporalio/auto-setup`) |
| `temporal-init`| (one-shot — exits after first run) | Registers `J1IngestStage` / `J1IngestMode` search attributes; `api` and `worker` block on this completing |
| `temporal-ui` | http://localhost:8080 | Temporal web UI |
| `postgresql` | (no exposed port; Temporal-internal) | Temporal's storage — **not** used by J1 |

### Editing frontend code

The frontend bundle is **baked into the image at build time** (no
bind mount), so source edits under `frontend/src/` need a rebuild:

```bash
docker compose -f deploy/dev/docker-compose.yml up --build frontend
```

For HMR / fast iteration, run Vite directly on the host instead and
keep the container stack for the backend services:

```bash
cd frontend
npm install # first time only
npm run dev # http://localhost:5173 with HMR
```

The host-side dev server hits the api container on its published
port (`http://localhost:8000`); the SPA's "Authorize" modal lets you
override the API base URL per-browser at runtime.

---

## 3. What's intentionally NOT in this stack (and why)

The framework is library-only and the local stack mirrors that minimalism. These services were considered and deliberately omitted:

| Service | Status |
|---|---|
| **PostgreSQL** | Present **only as Temporal's storage**. J1 itself doesn't use it — J1 state stays on disk in JSON + SQLite. Could be omitted by switching Temporal to MySQL or Cassandra; Postgres is the lightest of the supported drivers. |
| **Redis** | Omitted. No caching layer, no session store, no distributed-lock requirement. Temporal handles workflow state + retries; webhooks have their own `WebhookDeliveryStore`. |
| **MinIO / S3** | Omitted. Workspace state lives on disk under `J1_DATA_ROOT`, mounted as the named Docker volume `j1_workspace`. The codebase doesn't use object storage. |
| **Production-grade Temporal** (Cassandra backing, Elasticsearch for advanced search) | Omitted. The `auto-setup` image with Postgres is overkill-light — fine for laptop use. Production deployments swap the storage driver and add ES per Temporal's own deployment guide. |

If a deployment needs any of these, add them in a separate
`deploy/<env>/docker-compose.yml` (e.g. `deploy/staging/`) — keeping
the dev stack minimal is by design.

### Volume strategy (Apple Silicon performance notes)

Docker Desktop on macOS routes bind-mount I/O through gRPC FUSE,
which is **~10× slower** than I/O against a named Docker volume
(stored inside the Linux VM's overlay disk) or against `tmpfs`. The
worker is wired so heavy-write paths NEVER land on a bind mount:

| Path | Backing | Why |
|---|---|---|
| `j1_workspace` named volume → `/var/lib/j1` | Linux VM overlay disk | All persistent state (audit JSONL, run records, artifact registry, FTS index, RAGAnything workdir, MinerU per-doc outputs). |
| `tmpfs` → `/tmp` (1 GiB cap) | RAM | Python `tempfile`, MinerU intermediate scratch, soffice's `j1-soffice-*` mkdtemps. RAM-backed for hottest-path I/O. |
| `j1_postgres` named volume | Linux VM overlay disk | Temporal's storage. |
| `../../src` / `../../deploy` bind mounts → `/app/src`, `/app/deploy` | macOS via gRPC FUSE | Source code only — read-mostly, the bind cost is acceptable. |

**The default `J1_RAGANYTHING_WORKDIR=/var/lib/j1/raganything`** puts
LightRAG's `kv_store_*.json` files and MinerU's per-document outputs
inside the named volume. State persists across `docker compose up
--build`, and writes are fast.

Recommended settings to keep parse times low on Apple Silicon:

- Run **LM Studio natively on macOS** (not in Docker) and reach it
 via `http://host.docker.internal:1234`. The VLM/text-LLM workload
 benefits from native Metal acceleration; running it inside the
 Linux VM forfeits GPU offload.
- Use **`J1_RAGANYTHING_BACKEND=pipeline`** for simple text-layer
 documents (the default fast path); only use
 `vlm-http-client` / `hybrid-http-client` when MinerU's vision
 passes are required (scanned PDFs, complex diagrams).
- Keep **`MAX_ASYNC=1`** when LM Studio is a single inference
 process — concurrent requests just queue at the same model.
- Ingest one or two documents through the dev stack, then check
 `time` — anything over a minute per page strongly indicates the
 vision LLM is the bottleneck, not Docker volumes.

### Inspecting / cleaning volumes

```bash
# List all volumes the dev stack creates
docker volume ls | grep '^local *j1_'

# Inspect a specific volume (mountpoint, size, driver)
docker volume inspect j1_workspace

# Wipe everything (volumes + their contents)
docker compose -f deploy/dev/docker-compose.yml down -v
```

`down -v` is destructive: every project's audit JSONL, every
ingested document, every artifact, and the entire RAGAnything
workdir all go with the named volumes. Reserve for "from-scratch"
reproducers.

### Benchmarking MinerU: Docker vs native

If you suspect Docker is the bottleneck, compare like-for-like:

```bash
# Inside Docker (uses tmpfs /tmp + named-volume workspace)
time docker compose -f deploy/dev/docker-compose.yml exec worker \
 mineru -p /var/lib/j1/tenants/<tenant>/projects/<project>/raw/<doc>.pdf \
 -o /tmp/j1-mineru/benchmark-output \
 -b vlm-http-client \
 -u http://host.docker.internal:1234

# Native macOS (requires `pip install raganything` host-side)
time mineru -p./test.pdf \
 -o./benchmark-output \
 -b vlm-http-client \
 -u http://127.0.0.1:1234
```

Compare the wall-clock numbers. Docker should be within ~5–10 % of
native for compute-bound work; if Docker is dramatically slower,
the gap is almost certainly LM Studio reachability latency, not
filesystem.

### Temporal search attributes (automated)

The workflow upserts two custom search attributes — `J1IngestStage`
and `J1IngestMode` — so operators can filter the Temporal UI's
workflow list by stage / mode. Temporal **rejects upserts for
attributes that aren't registered with the cluster**, so the dev
stack ships a one-shot `temporal-init` service that registers both
on first boot. `api` and `worker` use `depends_on: condition:
service_completed_successfully` against `temporal-init`, so the
order is `temporal → temporal-init → api / worker`. This means
`J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` defaults to `true` in the
dev stack — no manual `tctl` step required.

If you point the framework at a Temporal cluster you manage yourself
(staging / prod), register the attributes there first:

```bash
temporal operator search-attribute create \
 --namespace default --name J1IngestStage --type Keyword
temporal operator search-attribute create \
 --namespace default --name J1IngestMode --type Keyword
```

…or leave `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=false` until you do.

---

## 4. Verify

### API health

```bash
curl http://localhost:8000/health
```

Expected:

```json
{ "requestId": "...", "data": { "status": "ok" }, "meta": {} }
```

### What's wired

```bash
curl http://localhost:8000/capabilities | jq '.data.capabilities[] | {name, available}'
```

### Temporal UI

Open <http://localhost:8080> in a browser. The default namespace is
`default`. Until a workflow is triggered the UI shows an empty
workflows list — that's expected.

---

## 5. Trigger a sample workflow

```bash
# 1. Create a project
curl -X POST http://localhost:8000/projects \
 -H "X-Tenant-Id: acme" \
 -H "Content-Type: application/json" \
 -d '{"projectId": "alpha"}'

# 2. Upload a sample document
echo "hello from local development" > /tmp/sample.txt
curl -X POST http://localhost:8000/documents \
 -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
 -F "file=@/tmp/sample.txt"

# 3. Start a project-wide ingestion workflow
# Note: `compilerKind` is OPTIONAL — when omitted, the API
# falls back to the bootstrap's `J1_DEFAULT_COMPILER` selection
# (which is `mock` per.env.example). Sending an unregistered
# `compilerKind` value is rejected at the API boundary with 400.
curl -X POST http://localhost:8000/ingestion-jobs \
 -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
 -H "Content-Type: application/json" \
 -d '{
 "actor": "local-dev",
 "correlationId": "demo-1"
 }'
```

Response:

```json
{ "requestId": "...", "data": { "jobId": "...", "action": "start" }, "meta": {} }
```

### What you should see

1. **Temporal UI** (http://localhost:8080) — a new workflow execution
 under the `default` namespace, showing the workflow lifecycle.
2. **Worker logs** (`docker compose -f deploy/dev/docker-compose.yml logs -f worker`)
 — activity dispatch lines.
3. **API response** — the `jobId` printed above is the Temporal
 workflow id; you can query its status:

 ```bash
 curl http://localhost:8000/ingestion-jobs/<jobId> \
 -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"
 ```

With the bundled `.env.example` defaults, the dev worker runs
`bootstrap_from_env` and wires the framework's bundled mock
adapters under `kind="mock"` for compiler / graph / retrieval. The
`POST /ingestion-jobs` call above produces a deterministic
end-to-end success: API → Temporal → worker → activities → mock
compiler → mock graph builder → SQLite indexer → workspace
artifacts. Inspect the resulting `compiled.text` artifact with
`GET /artifacts/{artifactId}`.

To run real processing instead of mocks:

1. The bundled image ([`Dockerfile`](Dockerfile)) already installs
 `j1[raganything]` so the vendor adapter is present out of the
 box. (If you trimmed that extra to slim the image, install it
 into the running container or rebuild after restoring the
 `[dev,raganything]` extras spec.)
2. In `.env`, set `J1_DEFAULT_COMPILER=raganything` (and the same
 for `_GRAPH_PROVIDER` / `_RETRIEVAL_PROVIDER`).
3. Configure LLM credentials: `J1_TEXT_LLM_*` and `J1_EMBEDDING_*`
 (and `J1_VISION_LLM_*` if visual enrichment is on).
4. Restart the stack:

 ```bash
 docker compose -f deploy/dev/docker-compose.yml down
 docker compose -f deploy/dev/docker-compose.yml up --build
 ```

> **Important — LLM roles are independent and have different model
> requirements.** Pointing every role at the same chat model is the
> single most common cause of "ingestion ran but the Chunks/Graph tab
> is empty" symptoms: a chat model can't do layout extraction, can't
> produce stable embeddings, and silently no-ops the things that
> actually need it.
>
> | Env var prefix | Used by | Acceptable model |
> |---|---|---|
> | `J1_RAGANYTHING_VLM_HTTP_*` | MinerU PDF parsing (when `J1_RAGANYTHING_BACKEND=vlm-http-client`) | **Only** a MinerU-trained layout VLM (`opendatalab/MinerU2.5-Pro-2604-1.2B`). Generic chat VLMs (Gemma, generic LLaVA) emit prose; MinerU drops every page and reports `Parsing failed: No content was extracted`. |
> | `J1_TEXT_LLM_*` | LightRAG entity/relation extraction, J1 enrichers, retrieval answers | Any decent general chat model — Qwen2.5-7B-Instruct, Llama-3.1-8B, Gemma3-12B, etc. |
> | `J1_VISION_LLM_*` | J1 enrichers describing images/diagrams MinerU already extracted (separate from MinerU itself) | Any general vision-chat model — Qwen2-VL, Llama-3.2-Vision, etc. Re-using your text model's endpoint is fine if it has vision. |
> | `J1_EMBEDDING_*` | LightRAG vector indexing, retrieval | A real embedding model — `nomic-embed-text-v1.5`, `bge-large-en-v1.5`, `text-embedding-3-small`, etc. **Not** a chat LLM. Pointing this at a chat model returns hidden-state-shaped vectors of unpredictable dimension and LightRAG aborts at first vector upsert with `Embedding dimension mismatch`. The dim in `J1_EMBEDDING_DIM` must match exactly what the model returns. |
> | `J1_FAST_LLM_*` | Adaptive ingestion planner (structured output) | Any decent chat model; usually the same as `J1_TEXT_LLM_*`. |
>
> Practical local-dev split: LM Studio on `:1234` with a chat + vision +
> embedding model, vLLM (or sglang) on `:1235` with MinerU2.5-Pro for
> the parser only. If running two model servers is overhead you don't
> want, set `J1_RAGANYTHING_BACKEND=pipeline` instead — MinerU's
> traditional CV detectors handle parsing without any VLM.

The same `worker.py` / `bootstrap_from_env` path serves both
modes — no fork required for the common case. The image carries
the optional adapter so a deployment can flip the env var and
restart without a rebuild.

For deeply custom deployments (custom enricher maps, hand-injected
processor maps, providers other than mock / raganything), forking
[`worker.py`](worker.py) and passing your own maps to
[`build_worker_spec`](_wiring.py) is still the recommended path.

---

## 6. Stop / reset

```bash
# Stop services (volumes preserved)
docker compose -f deploy/dev/docker-compose.yml down

# Stop + delete the workspace volume (factory reset)
docker compose -f deploy/dev/docker-compose.yml down -v
```

`-v` removes the named volume `j1_workspace` — every project, every
artifact, every search index, every audit log goes with it. Useful
when starting from scratch; **do not run on shared environments**.

---

## 7. Configuration

Every variable lives in [`.env.example`](../../.env.example). Highlights:

| Var | Default | Notes |
|---|---|---|
| `J1_DATA_ROOT` | `/var/lib/j1` | Inside the container; mapped to the `j1_workspace` volume |
| `J1_RAGANYTHING_WORKDIR` | `/var/lib/j1/raganything` | Lives inside the workspace volume (fast Linux-VM disk on macOS, persists across `docker compose up --build`). Override only to point at a host bind mount for offline inspection. |
| `J1_KEEP_FAILED_INGEST_ARTIFACTS` | unset | When truthy, suppresses cleanup of MinerU's per-document `outputs/` dir on the SUCCESS path. Failed compiles always preserve their output dir regardless. |
| `J1_TEMPORAL_TARGET` | `temporal:7233` | Docker network DNS — don't use `localhost` from inside the container |
| `J1_TEMPORAL_NAMESPACE` | `default` | The Temporal `auto-setup` image creates this on boot |
| `J1_TEMPORAL_TASK_QUEUE` | `j1-processing` | Generic, stable; both API + worker read this |
| `J1_API_PORT` | `8000` | Port inside the container; compose maps it to the same host port |
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | `5` | Tune for laptop workload |
| `J1_FRONTEND_PORT` | `8081` | Host port the SPA is published on |
| `J1_FRONTEND_API_BASE_URL` | `/api` | Baked into the SPA bundle at build time. Defaults to a relative path so the browser stays single-origin via nginx's proxy block |
| `J1_AUTH_API_KEYS` / `J1_AUTH_API_KEYS_FILE` | unset | Anonymous mode by default; set either to require auth |
| `J1_WEBHOOK_SUBSCRIPTIONS` / `J1_WEBHOOK_SUBSCRIPTIONS_FILE` | unset | No webhook delivery by default |
| `J1_EVENT_PUBLISHER_TYPE` | `noop` | Set to `bus` to fan events into the in-process `ApplicationEventBus` |

The single-page environment-variable reference (every `J1_*` var,
grouped by section, with defaults and required-by-when notes) is at
[docs/configuration/environment.md](../../docs/configuration/environment.md).
Per-area context lives in:

- [docs/security.md](../../docs/security.md) — auth specifics
- [docs/webhooks.md](../../docs/webhooks.md) — webhook delivery
- [docs/event-integration.md](../../docs/event-integration.md) — event publisher / AsyncAPI
- [docs/providers.md](../../docs/providers.md) — RAGAnything / Graphify / LLM roles
- [docs/operations/temporal.md](../../docs/operations/temporal.md) — Temporal worker operations

---

## 8. Forking this for production

1. **Switch Temporal off `auto-setup`.** Use `temporalio/server` with
 a real Cassandra / Postgres + Elasticsearch backing.
2. **Mount `J1_DATA_ROOT` on shared durable storage** (NFS / EFS /
 Azure Files) — the JSON registries are single-writer, so multiple
 API replicas writing to the same project are not supported.
3. **Wire authentication.** Set `J1_AUTH_API_KEYS_FILE` to a path
 mounted from your secret manager. See [docs/security.md](../../docs/security.md).
4. **Plug in real processors.** Fork [`worker.py`](worker.py) and
 register your own `KnowledgeCompiler` / `EnrichmentProcessor` /
 `GraphBuilder` / `ModelProvider` implementations.
5. **Deployment platform.** This compose file is laptop-grade. For
 Kubernetes, the same image (`Dockerfile`) works as a base — split
 the API and worker into separate Deployments / StatefulSets and
 run the worker with N replicas to scale activity throughput.

---

## 9. Files in this directory

| File | Purpose |
|---|---|
| [`Dockerfile`](Dockerfile) | Single image — runs both API and worker |
| [`docker-compose.yml`](docker-compose.yml) | Brings up API + worker + frontend + Temporal + UI |
| [`api.py`](api.py) | `python -m deploy.dev.api` — FastAPI server entrypoint |
| [`worker.py`](worker.py) | `python -m deploy.dev.worker` — Temporal worker entrypoint |
| [`_wiring.py`](_wiring.py) | Shared `ApplicationFacade` + `WorkerSpec` constructors |
| `__init__.py` | Package marker |
| [`../../frontend/Dockerfile`](../../frontend/Dockerfile) | Multi-stage build for the SPA (Vite build → nginx static serve) |
| [`../../frontend/nginx.conf`](../../frontend/nginx.conf) | Static + `/api/*` reverse proxy used by the frontend container |

The framework's library code lives in `src/j1/` — none of it
imports anything from this directory. This is a *deployment*; J1
itself stays library-only.
