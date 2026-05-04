# J1 Developer Onboarding

A sequenced "from zero to first working flow" path. Follow each step
before moving to the next. Most steps are command-driven and take
minutes; the optional steps are clearly marked.

If anything fails, the [Troubleshooting pointers](#10-troubleshooting-pointers)
at the bottom map common errors to the relevant docs.

---

## 1. What J1 is

J1 is a reusable, **domain-neutral** Python framework for ingesting,
processing, indexing, and querying document-based knowledge.

It ships:

- A pluggable processing pipeline (intake → compile → enrich → graph
  → index → query) where every stage is a `Protocol`.
- Durable workflow orchestration via Temporal (pause / resume /
  cancel, budget gates, human-review gates).
- An external integration boundary: REST + OpenAPI + SSE + webhooks
  (CloudEvents 1.0) + AsyncAPI 3.0 contract + NDJSON bulk
  import/export.
- Optional vendor integrations (RAGAnything, Graphify, LangChain) —
  isolated behind providers; the core never imports vendor SDKs.

J1 is consumed as a Python library. The repository ships a complete
local Docker development stack but the framework itself is not a
service.

For deeper architectural detail see
[`docs/architecture.md`](../architecture.md).

---

## 2. Recommended reading order

Read in this sequence — each builds on the previous:

1. [`README.md`](../../README.md) — repo identity, quickstart links.
2. **This file** — to get a working environment.
3. [`docs/architecture.md`](../architecture.md) — sections 1–6 only
   (workspace, intake, contracts, Temporal). Skim the rest.
4. [`docs/providers.md`](../providers.md) — when you need to
   configure LLMs / RAGAnything / Graphify.
5. [`docs/configuration/environment.md`](../configuration/environment.md) —
   when you need a specific env var.
6. [`docs/external-integration-architecture.md`](../external-integration-architecture.md)
   — when you start touching the REST / webhook / event surface.
7. Per-area docs as needed: [`security.md`](../security.md),
   [`webhooks.md`](../webhooks.md),
   [`event-integration.md`](../event-integration.md),
   [`bulk.md`](../bulk.md), [`mcp-status.md`](../mcp-status.md).
8. [`docs/extension/add-a-provider.md`](../extension/add-a-provider.md)
   — when you need to plug in a new compiler / graph / retrieval / LLM.

---

## 3. Install dependencies

**Prerequisites.**

- Python 3.11+ (3.13 is the version used in CI / the dev container)
- Git
- Docker (only when running the full Docker stack — § 6)

**Steps.**

```bash
# 1. Clone
git clone <repo-url> j1
cd j1

# 2. Create a virtualenv
python3.11 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate

# 3. Install with dev extras (pulls pytest + httpx + the framework itself, editable)
pip install -e ".[dev]"
```

**Optional vendor integrations.** Install only what your work needs:

```bash
pip install -e ".[raganything]"        # RAGAnything compiler / graph / retrieval
pip install -e ".[graphify]"           # Graphify graph builder (CLI or Python mode)
pip install -e ".[langchain-openai]"   # LangChain text/embedding via OpenAI
pip install -e ".[all-providers]"      # Everything optional, in one go
```

The full extras list is in [`pyproject.toml`](../../pyproject.toml).

---

## 4. Configure environment

The framework reads `J1_*` environment variables. The minimum for
local development:

```bash
export J1_DATA_ROOT=/tmp/j1-dev      # absolute path required
```

The Docker stack is configured via a `.env` file at the repo root:

```bash
cp .env.example .env
```

**Full reference:** [`docs/configuration/environment.md`](../configuration/environment.md).

---

## 5. Run tests

The test suite is hermetic — no external services, no network. It
runs in seconds.

```bash
.venv/bin/pytest             # full suite
.venv/bin/pytest -q          # quiet mode
.venv/bin/pytest -x -v tests/test_intake.py   # one file, verbose, fail-fast
```

If the suite is green, the install worked.

---

## 6. Start the local Docker stack

A complete stack — REST API + Temporal worker + Temporal server +
Temporal Web UI — comes up with a single command:

```bash
docker compose -f deploy/dev/docker-compose.yml up --build
```

Services:

| URL / port | What |
|---|---|
| <http://localhost:8000> | J1 REST API |
| <http://localhost:8080> | Temporal Web UI |
| `localhost:7233` | Temporal gRPC (clients connect here) |

The first run takes a couple of minutes (image build + Temporal
schema init). Subsequent runs are fast.

**Full walkthrough:** [`deploy/dev/README.md`](../../deploy/dev/README.md).

---

## 7. Start API or worker outside Docker (optional)

If you'd rather run the API / worker as plain Python processes
(easier for debugging), start them in two terminals.

**Terminal 1 — API.**

```bash
.venv/bin/python -m deploy.dev.api
# or any ASGI host pointed at the FastAPI app object
```

**Terminal 2 — worker.**

```bash
.venv/bin/python -m deploy.dev.worker
```

Both processes read the same `J1_*` env vars (you'll need
`J1_DATA_ROOT` and `J1_TEMPORAL_TARGET` at minimum). See
[`docs/operations/temporal.md`](../operations/temporal.md) for a
production-shaped worker.

---

## 8. Trigger a sample workflow

With the Docker stack (or both Python processes) running:

```bash
# 1. Create a project (tenant-scoped)
curl -X POST http://localhost:8000/projects \
  -H "X-Tenant-Id: acme" -H "Content-Type: application/json" \
  -d '{"projectId": "alpha"}'

# 2. Upload a sample document
echo "hello from local development" > /tmp/sample.txt
curl -X POST http://localhost:8000/documents \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -F "file=@/tmp/sample.txt"

# 3. Start a project-wide ingestion workflow
curl -X POST http://localhost:8000/ingestion-jobs \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"compilerKind":"stub.compiler","actor":"local-dev","correlationId":"demo-1"}'
```

Each call returns a JSON envelope `{requestId, data, meta}` (see
[`docs/rest-api.md`](../rest-api.md) § 3 for the full envelope
shape).

---

## 9. Verify and inspect

**Workflow status (REST):**

```bash
curl http://localhost:8000/ingestion-jobs/<jobId> \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"
```

**Workflow status (Temporal UI):** Open <http://localhost:8080> —
the workflow execution appears under the `default` namespace with
its activity timeline.

**What's wired (capabilities probe):**

```bash
curl http://localhost:8000/capabilities | jq '.data.capabilities[] | {name, available}'
```

**Workspace contents on disk:**

```bash
# Inside the container:
docker compose -f deploy/dev/docker-compose.yml exec api ls -la /var/lib/j1/tenants/acme/projects/alpha
# Outside (depending on how you mounted J1_DATA_ROOT):
ls -la "$J1_DATA_ROOT/tenants/acme/projects/alpha"
```

The dev stack registers no real `KnowledgeCompiler` /
`EnrichmentProcessor` implementations — `stub.compiler` is
intentionally not registered, so the workflow will reach a `failed`
state after dispatch. **That's expected.** It confirms the plumbing
(API → Temporal → worker → activities) end-to-end without forcing a
specific vendor.

To run the pipeline against real processors:

1. Install vendor extras: `pip install -e ".[raganything]"` (and
   any LLM provider you want).
2. Set the LLM env vars from
   [`docs/configuration/environment.md`](../configuration/environment.md).
3. Fork [`deploy/dev/worker.py`](../../deploy/dev/worker.py) (or
   build your own worker entrypoint) and pass real `compilers=` /
   `enrichers=` / `graph_builders=` maps to `build_worker_spec`.

---

## 10. Troubleshooting pointers

| Symptom | Look at |
|---|---|
| `pytest` fails on import | Verify the editable install: `pip install -e ".[dev]"` |
| `ConfigError: data_root must be absolute` | Set `J1_DATA_ROOT` to an absolute path |
| API returns `503` for an endpoint | The capability isn't wired — see [`docs/troubleshooting.md`](../troubleshooting.md) "503" section |
| `401 UNAUTHENTICATED` | Authenticator is configured; send `Authorization: Bearer` or `X-API-Key` — see [`docs/security.md`](../security.md) |
| Worker can't connect to Temporal | Check `J1_TEMPORAL_TARGET`; from inside Docker use `temporal:7233`, not `localhost:7233` |
| Workflow accepted but nothing runs | Worker isn't on the same task queue — confirm `J1_TEMPORAL_TASK_QUEUE` matches between API and worker |
| `INVALID_IDENTIFIER` 400 | Tenant / project IDs must match `[A-Za-z0-9_-]+` |
| Webhook never fires | Check `J1_WEBHOOK_ENABLED=true` AND `J1_EVENT_PUBLISHER_TYPE=bus`; see [`docs/webhooks.md`](../webhooks.md) |
| Anything else | [`docs/troubleshooting.md`](../troubleshooting.md) (REST + worker + integration issues) |

---

## 11. First contribution checklist

When you're ready to open a PR:

- [ ] Read [`CONTRIBUTING.md`](../../CONTRIBUTING.md) (architecture + naming + testing rules).
- [ ] Run the full suite: `.venv/bin/pytest`.
- [ ] If you added behaviour, add a test that fails without your change.
- [ ] If you added env vars, add them to [`.env.example`](../../.env.example) AND [`docs/configuration/environment.md`](../configuration/environment.md).
- [ ] If you added an external surface (REST endpoint, webhook event type, broker channel, MCP tool), update the relevant per-area doc.
- [ ] No domain-specific names in `src/j1/` core (no industry vocabulary, no phase / training references — see [`docs/extension/domain-module-isolation.md`](../extension/domain-module-isolation.md)).
- [ ] No vendor SDK imports outside `j1.providers/` or `j1.adapters/` or `j1.llm/`.
- [ ] Per-PR: small, single-concern; no drive-by refactors.

---

## 12. Where to go next

| You want to … | Read |
|---|---|
| Understand the core architecture in depth | [`docs/architecture.md`](../architecture.md) |
| Configure LLMs / RAGAnything / Graphify | [`docs/providers.md`](../providers.md) |
| Add a new provider (compiler, graph, retrieval, LLM) | [`docs/extension/add-a-provider.md`](../extension/add-a-provider.md) |
| Understand the integration boundary | [`docs/external-integration-architecture.md`](../external-integration-architecture.md) |
| Operate Temporal | [`docs/operations/temporal.md`](../operations/temporal.md) |
| Build a domain module on top of J1 | [`docs/extension/domain-module-isolation.md`](../extension/domain-module-isolation.md) |
