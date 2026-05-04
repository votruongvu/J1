# Local development with Docker

A minimal Docker Compose stack for running the J1 framework locally.
Brings up a REST API, a Temporal worker, a Temporal server (with
Postgres for its own storage), and the Temporal web UI on a single
laptop.

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
- Free local ports: **8000** (API), **7233** (Temporal gRPC),
  **8080** (Temporal UI)

That's it. No Python install, no Postgres, no Redis, no S3.

---

## 2. Bring it up

```bash
# 1. Copy the env template
cp .env.example .env

# 2. Start everything
docker compose -f deploy/dev/docker-compose.yml up --build
```

The first run takes a couple of minutes (image build + Temporal
schema init). Subsequent runs are fast.

Services that come up:

| Service        | URL / Port                            | What it is |
|----------------|---------------------------------------|------------|
| `api`          | http://localhost:8000                 | J1 REST API (`python -m deploy.dev.api`) |
| `worker`       | (no port — connects to Temporal)      | J1 Temporal worker (`python -m deploy.dev.worker`) |
| `temporal`     | localhost:7233 (gRPC)                 | Temporal server (`temporalio/auto-setup`) |
| `temporal-ui`  | http://localhost:8080                 | Temporal web UI |
| `postgresql`   | (no exposed port; Temporal-internal)  | Temporal's storage — **not** used by J1 |

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
#    Note: `compilerKind` is OPTIONAL — when omitted, the API
#    falls back to the bootstrap's `J1_DEFAULT_COMPILER` selection
#    (which is `mock` per .env.example). Sending an unregistered
#    `compilerKind` value is rejected at the API boundary with 400.
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
`bootstrap_from_env()` and wires the framework's bundled mock
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

The same `worker.py` / `bootstrap_from_env()` path serves both
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
| `J1_TEMPORAL_TARGET` | `temporal:7233` | Docker network DNS — don't use `localhost` from inside the container |
| `J1_TEMPORAL_NAMESPACE` | `default` | The Temporal `auto-setup` image creates this on boot |
| `J1_TEMPORAL_TASK_QUEUE` | `j1-processing` | Generic, stable; both API + worker read this |
| `J1_API_PORT` | `8000` | Port inside the container; compose maps it to the same host port |
| `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` | `5` | Tune for laptop workload |
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
| [`docker-compose.yml`](docker-compose.yml) | Brings up API + worker + Temporal + UI |
| [`api.py`](api.py) | `python -m deploy.dev.api` — FastAPI server entrypoint |
| [`worker.py`](worker.py) | `python -m deploy.dev.worker` — Temporal worker entrypoint |
| [`_wiring.py`](_wiring.py) | Shared `ApplicationFacade` + `WorkerSpec` constructors |
| `__init__.py` | Package marker |

The framework's library code lives in `src/j1/` — none of it
imports anything from this directory. This is a *deployment*; J1
itself stays library-only.
