# 05. Developer Onboarding

> Audience: engineers picking up the repository for the first time.
> [Back to README](../README.md).

## Prerequisites

- **Python 3.13** (the codebase uses 3.13 generics + dataclass
  syntax). A venv is strongly recommended.
- **Node 20+** for the frontend.
- **Docker + Docker Compose** for the infrastructure services
  (Postgres, Redis, MinIO, Qdrant, Neo4j, Temporal).
- **An LLM API key** for at least one provider you intend to wire
  (OpenAI, etc.). Without one, the synthesizer falls back to a
  retrieval-only response.

## Repository layout

```
src/j1/
  intake/                   Document registry + upload.
  documents/                Document records, snapshots, lifecycle.
  orchestration/            Temporal workflow + activities.
    workflows/
    activities/
  providers/raganything/    Compile + graph + retrieval black box.
  processing/               Compile assessment, enrichment, result handling.
  domains/                  Domain packs (general + civil_engineering).
  query/                    SmartQueryOrchestrator, eligibility, intent.
  validation/               Manual test query + imported test cases.
    imported_test_cases.py
    service.py
    dtos.py
  search/                   Evidence adapter (Postgres FTS).
  artifacts/                Artifact registry.
  audit/                    Audit recorder + sinks.
  adapters/rest/            FastAPI application.
    app.py
    schemas.py
    security.py
  integration/              Application facade + DTOs.
  config/                   Settings, runtime config, env binding.
  compose.py                Bootstrap from env.
  errors/                   Typed exceptions.

frontend/
  src/pages/                Top-level routes.
    DocumentsPage.tsx
    DocumentDetailPage.tsx
    RunDetailPage.tsx
    run-detail/             RunDetailPage internals.
      results/              The Validation / Manual Query tabs.
  src/lib/api/              client.ts + api-client.ts (REST client).
  src/types/                TS types matching the wire schema.

deploy/dev/
  docker-compose.yml        Postgres / Redis / MinIO / Temporal / API / worker.
  api.py                    `python -m deploy.dev.api`.
  worker.py                 `python -m deploy.dev.worker`.
  _wiring.py                Dependency wiring shared by api + worker.

tests/                      Backend pytest suite.
```

## Key backend modules to read first

| Module | What lives there |
| --- | --- |
| `src/j1/adapters/rest/app.py` | Every REST endpoint. The shape of the API. |
| `src/j1/orchestration/workflows/project_processing.py` | The full ingestion workflow. |
| `src/j1/orchestration/activities/processing.py` | `compile` / `enrich` / `build_graph` / `index` activity bodies. |
| `src/j1/documents/snapshot_service.py` | Snapshot lifecycle + CAS promotion. |
| `src/j1/query/orchestrator.py` | The query pipeline. |
| `src/j1/query/eligibility.py` | The single source of query visibility. |
| `src/j1/validation/service.py` | Manual test query + imported-test-case service. |
| `src/j1/providers/raganything/_bridge.py` | The compile-black-box implementation. |
| `src/j1/config/runtime.py` | Provider config validation. Read when an env var "doesn't take". |
| `deploy/dev/_wiring.py` | How everything is glued together at startup. |

## Key frontend modules

| File | Purpose |
| --- | --- |
| `frontend/src/pages/DocumentsPage.tsx` | Project document list (read-only; lifecycle lives in the detail page). |
| `frontend/src/pages/DocumentDetailPage.tsx` | Per-document detail + lifecycle actions + run history. |
| `frontend/src/pages/RunDetailPage.tsx` | Per-run detail with the Validation / Manual Query Trace tabs. |
| `frontend/src/pages/run-detail/results/ValidationTab.tsx` | Imported test cases section + Manual Test Query console. |
| `frontend/src/pages/run-detail/results/ManualQueryConsole.tsx` | The "ask a question" surface. Exposes `loadQuestion(text)` via ref. |
| `frontend/src/lib/api/api-client.ts` | The REST client. |
| `frontend/src/types/review.ts` | Wire-shape TypeScript types. |

## Running the project

### Full docker stack

```sh
cp .env.example .env
docker compose -f deploy/dev/docker-compose.yml up -d
```

Then:

- API at `http://localhost:8000`.
- Frontend at `http://localhost:8081`.
- Temporal UI at `http://localhost:8233`.
- MinIO Console at `http://localhost:9001` (`j1-dev` / `j1-dev-secret`).

### Hybrid: infra in Docker, Python on host

```sh
docker compose -f deploy/dev/docker-compose.yml up -d \
  postgres redis minio minio-init temporal temporal-init

python3.13 -m venv .venv && source .venv/bin/activate
pip install -e .

# Workspace shared between api + worker.
export J1_DATA_ROOT=$(pwd)/.data

python -m deploy.dev.api &
python -m deploy.dev.worker &

cd frontend && npm install && npm run dev
```

When the API and worker run as separate host processes (or
containers), they MUST share `J1_DATA_ROOT`. If they don't, the
worker won't find the document the API just registered. The dev
Compose stack already shares the `j1_temp` volume at `/tmp/j1`;
set `J1_DATA_ROOT=/tmp/j1` if you customise the wiring.

### Frontend-only

```sh
cd frontend && npm install && npm run dev
```

Set `VITE_API_BASE_URL` if the API isn't on `http://localhost:8000`.

## Tests

### Backend

```sh
# Full sweep.
python -m pytest tests/ -q

# Narrower (e.g. while iterating).
python -m pytest tests/test_imported_test_cases.py -v
python -m pytest tests/test_rest_imported_test_cases.py -v
```

The suite assumes Postgres is *not* required for the unit tests — the
tests run against in-memory / JSONL stores. Integration tests that
need Postgres explicitly skip when the DSN isn't reachable.

### Frontend

```sh
cd frontend
npx tsc -b           # type-check
npx vitest run       # unit tests
```

## Smoke tests

### Ingest a document

```sh
curl -X POST http://localhost:8000/documents/upload \
  -H "X-Tenant-Id: acme" \
  -H "X-Project-Id: alpha" \
  -F "file=@tests/fixtures/sample.pdf"

# Note the returned documentId; then start ingest:
curl -X POST http://localhost:8000/documents/{documentId}/ingest \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"

# Watch progress on the SSE stream or in the Temporal UI.
```

### Manual test query

Once the run reaches `SUCCEEDED`:

```sh
curl -X POST http://localhost:8000/ingestion-runs/{runId}/test-query \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is X?","topK":10,"synthesize":true}'
```

Look at `validationStatus`, `synthesizedAnswer`, and `citations` in
the response. `debug.orchestrator_trace` carries the full trace.

## Important environment variables

The reference is in [docs/07-deployment-and-scaling.md](07-deployment-and-scaling.md).
The ones you change most often:

| Variable | Default | Notes |
| --- | --- | --- |
| `J1_RUNTIME_PROFILE` | `dev` | `prod` makes the runtime validator strict. |
| `J1_DATA_ROOT` | `/data/j1` | Must be shared between API and worker. |
| `J1_METADATA_DSN` | unset | Required when `J1_METADATA_BACKEND=postgres`. |
| `J1_ARTIFACT_*` | unset | Required when `J1_ARTIFACT_BACKEND=s3`. |
| `J1_CACHE_URL` | unset | Required when `J1_CACHE_BACKEND=redis`. |
| `J1_TEMPORAL_HOST` | `temporal:7233` | The Temporal frontend address. |
| `J1_FAST_LLM_PROVIDER` / `J1_TEXT_LLM_PROVIDER` | unset | Which LLM client is registered. |
| `J1_QUERY_ENGINE` | `lightrag_native` | Engine catalogue documented in [03-query-flow.md](03-query-flow.md). |
| `J1_ENABLE_BM25_FALLBACK` | `false` | Whether BM25 may answer when native fails. |
| `J1_VALIDATION_CANDIDATE_TOP_K` | `20` | Candidate pool for manual query. |
| `J1_RAG_NATIVE_QUERY_TIMEOUT_SECONDS` | `30` | Timeout for the LightRAG native call. |

`load_runtime_config` in `src/j1/config/runtime.py` validates the
above when the API or worker starts; if anything required for the
selected backend is missing, the process refuses to boot with a
specific list. Read that error message — it tells you exactly which
key is empty.

## Common workflows

### Adding a REST endpoint

1. Add the route in `src/j1/adapters/rest/app.py`.
2. If it needs a new wire schema, extend `src/j1/adapters/rest/schemas.py`
   (Pydantic `CamelModel` subclass).
3. Translate to / from the service-layer DTO in `src/j1/integration/dto.py`
   or `src/j1/validation/dtos.py` — the REST layer must not import
   from the integration or core directly.
4. Add a typed test under `tests/test_rest_*.py`.

### Adding an activity

1. Define the input + result dataclass in
   `src/j1/orchestration/activities/payloads.py`.
2. Implement the activity body next to its siblings in
   `src/j1/orchestration/activities/`.
3. Register it in the activity-class `all_activities()` list.
4. Dispatch it from the workflow in
   `src/j1/orchestration/workflows/project_processing.py`.
5. Always allocate `target_snapshot_id` up-front and thread it
   through the activity input — activities must not allocate
   snapshots lazily.

### Adding a domain pack

1. Create `src/j1/domains/<name>/__init__.py` + `domain.yaml`.
2. Implement `build_<name>_pack()` returning a `DomainPack`.
3. Register it in `src/j1/domains/registry.py::default_registry()`.
4. Test with a small fixture under `tests/test_<name>_pack.py`.
5. See [10-domain-configuration.md](10-domain-configuration.md) for
   the YAML schema.

### Modifying query behaviour

- Intent vocabulary / retrieval planning: `src/j1/query/orchestrator.py`
  + `src/j1/retrieval/intent_router.py`.
- Eligibility / scope: `src/j1/query/eligibility.py`. **Never**
  re-introduce a run-id-based code path here.
- Synthesizer prompts: `src/j1/query/answer_synthesizer.py`.
- Auxiliary evidence (BM25): `src/j1/search/`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ConfigError: RuntimeConfig validation failed for profile 'dev': missing or invalid: metadata, artifact, cache, evidence` | Backend names set in `.env` but no DSN / endpoint / key. Read the error: each entry maps to a `J1_*` env var. |
| Worker logs `DocumentNotFoundError: document ... not found in <tenant>/<project>` | API and worker disagree on `J1_DATA_ROOT`. Set it to a shared path. |
| `temporalio.exceptions.ApplicationError: J1_INGEST_UNEXPECTED_ERROR: AttributeError: 'NoneType' object has no attribute 'snapshot_id'` | The workflow ran without a snapshot service wired. Check `_build_orchestrator_or_none` and `build_validation_service` in `deploy/dev/_wiring.py`. |
| Manual Query Trace shows "No answer" but native debug works | Sufficiency gate refused the pack. Open the trace; the gate result will tell you which check failed. |
| "Live" badge stuck on a terminal run | Stale browser tab. The current build hides the badge on terminal runs at load — refresh. |
| Frontend type errors after pulling new types | `cd frontend && npx tsc -b`. The types are kept in sync with the wire schema; a backend change to `schemas.py` usually needs a matching type change. |

If a Temporal workflow is stuck, the Temporal UI (port `8233`)
plus the `temporal workflow list / describe / terminate` CLI under
`temporal-admin-tools` are your friends.
