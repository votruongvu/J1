# J1

J1 is a document-centric knowledge platform. You upload documents
into a project, the platform parses + enriches them, and you can
then ask questions that are answered with citations drawn from
those documents.

The system is multi-tenant and multi-project. Every document, every
processing run, and every query is scoped to a `(tenant, project)`
pair. Inside that scope, J1 turns raw files into a snapshot-versioned
**knowledge base** that the query path can search against.

## What J1 is trying to solve

Most "RAG on top of an LLM" stacks struggle once real teams use
them. Files change. Documents are re-ingested. Old chunks bleed
into new answers. There's no clean way to roll back, no clean way
to share a tenant boundary, and "what was the index actually like
when this answer was produced" is unanswerable.

J1 is built around three product decisions that fix that:

1. **Snapshot-versioned knowledge.** Every ingestion produces a
   versioned `DocumentSnapshot`. Promotion is atomic. The previous
   snapshot stays on disk for diagnostics but stops driving the
   answer. Re-index never partially-clobbers the live state.
2. **Document is the unit of management.** Operators detach, remove,
   and re-index *documents* — not runs. A run is a processing
   attempt; the document and its active snapshot are the durable
   handles.
3. **Compile is a single black box.** Parsing + chunking + embedding
   + graph generation goes through RAGAnything, which owns its own
   workspace per `(tenant, project, document, snapshot)`. Everything
   the LLM eventually sees is grounded in artifacts that compile
   produced.

## Key capabilities

- Multi-tenant, multi-project document management with knowledge-state
  lifecycle (attach / detach / remove).
- Ingestion through Temporal workflows: profile → assess →
  RAGAnything compile → snapshot promotion. Basic queryability is
  reached at promotion; Domain Enrichment is an optional manual
  post-compile action (see
  [docs/ingestion-flow.md](docs/ingestion-flow.md)).
- Snapshot-centered visibility: queries and answers only see the
  document's currently-promoted snapshot.
- Hybrid retrieval with RAGAnything (LightRAG-native graph +
  vector) as the answering engine; Postgres FTS available as
  auxiliary evidence / diagnostic surface.
- LLM-synthesised answers with grounded citations.
- Domain packs that customise enrichment and query planning (a
  generic pack plus a worked Civil Engineering example).
- Compact validation surface: Manual Test Query for one-off
  inspection plus an imported-CSV test-case section for quick
  confidence checks.
- REST + event-driven integration boundaries.

## High-level project structure

```
src/j1/                     # Python core
  intake/                   # Document registry + upload
  documents/                # Document records, snapshots, lifecycle
  orchestration/            # Temporal workflow + activities
  providers/raganything/    # Compile + graph + retrieval black box
  processing/               # Compile assessment, enrichment, results
  domains/                  # Domain packs (general + civil_engineering)
  query/                    # Orchestrator, eligibility, retrieval
  validation/               # Manual test query + imported test cases
  search/                   # Evidence adapter (Postgres FTS)
  artifacts/                # Artifact registry
  audit/                    # Audit trail
  adapters/rest/            # FastAPI application
  integration/              # Application facade + DTO boundary
frontend/                   # React UI (Vite + TypeScript)
deploy/dev/                 # Docker Compose + dev wiring + workers
tests/                      # Backend tests
docs/                       # Architecture documentation (start here)
```

## Run with Docker

The dev Docker Compose stack stands up Postgres, Redis, MinIO,
Qdrant, Neo4j, Temporal, the J1 API, the worker, and the frontend.
A one-shot init container creates the MinIO bucket.

```sh
# 1. Copy the example env and edit secrets you care about.
cp .env.example .env

# 2. Bring everything up.
docker compose -f deploy/dev/docker-compose.yml up -d

# 3. Open the frontend.
open http://localhost:8081
```

The API is reachable at `http://localhost:8000`, the Temporal UI at
`http://localhost:8233`, and MinIO Console at `http://localhost:9001`.

## Run in local Dev Mode

If you'd rather run Python on the host (faster code iteration,
fewer container layers to fight):

```sh
# 1. Stand up the infrastructure services only.
docker compose -f deploy/dev/docker-compose.yml up -d \
  postgres redis minio minio-init temporal temporal-init

# 2. Activate a 3.13 venv and install the package in editable mode.
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Point your local Python at the shared services + run.
export J1_DATA_ROOT=$(pwd)/.data
python -m deploy.dev.api &
python -m deploy.dev.worker &

# 4. Frontend.
cd frontend && npm install && npm run dev
```

See [docs/05-developer-onboarding.md](docs/05-developer-onboarding.md)
for a longer walkthrough.

## Required environment summary

The full reference is in
[docs/05-developer-onboarding.md](docs/05-developer-onboarding.md)
and [docs/07-deployment-and-scaling.md](docs/07-deployment-and-scaling.md);
at minimum you need:

| Variable | Purpose |
| --- | --- |
| `J1_RUNTIME_PROFILE` | `dev` (default) or `prod`. Controls validator strictness. |
| `J1_DATA_ROOT` | Workspace root. Set to a shared path when API + worker run as separate processes. |
| `J1_METADATA_DSN` | Postgres connection for app metadata + FTS. |
| `J1_ARTIFACT_ENDPOINT` / `_BUCKET` / `_ACCESS_KEY` / `_SECRET_KEY` | S3-compatible artifact storage (MinIO in dev). |
| `J1_CACHE_URL` | Redis URL. |
| `J1_TEMPORAL_HOST` | Temporal frontend address. |
| `J1_FAST_LLM_PROVIDER` / `J1_TEXT_LLM_PROVIDER` | Which LLM client is registered for the FAST and TEXT roles. |
| `OPENAI_API_KEY` etc. | Whatever the chosen LLM provider needs. |

## Documentation

Read in roughly this order. Each doc is standalone, but they assume
you've at least skimmed `01-overall-architecture.md`.

**Canonical architecture docs**

- [docs/01-overall-architecture.md](docs/01-overall-architecture.md) — Business-friendly system overview.
- [docs/ingestion-flow.md](docs/ingestion-flow.md) — Canonical end-to-end ingestion: upload → assess → compile → promote → optional Domain Enrichment.
- [docs/unified-memory-contract.md](docs/unified-memory-contract.md) — Logical projection the query layer reads through.
- [docs/03-query-flow.md](docs/03-query-flow.md) — Manual query → orchestrator → answer + citations.
- [docs/04-core-data-model.md](docs/04-core-data-model.md) — Tenant / Project / Document / Snapshot / Profile / Knowledge Base.

**Operations and integration**

- [docs/05-developer-onboarding.md](docs/05-developer-onboarding.md) — Run, test, extend the codebase.
- [docs/06-risks-and-known-limitations.md](docs/06-risks-and-known-limitations.md) — Where intent and code don't yet line up.
- [docs/07-deployment-and-scaling.md](docs/07-deployment-and-scaling.md) — Production direction.
- [docs/08-multi-kb-model.md](docs/08-multi-kb-model.md) — How the per-project knowledge bases compose.
- [docs/09-external-integration-model.md](docs/09-external-integration-model.md) — REST + event integration with outside systems.
- [docs/10-domain-configuration.md](docs/10-domain-configuration.md) — Domain packs (general + civil engineering).
- [docs/11-ingestion-execution-profiles.md](docs/11-ingestion-execution-profiles.md) — Execution profiles (`minimum_queryable` / `standard` / `advanced`).
- [docs/12-retrieval-intelligence-roadmap.md](docs/12-retrieval-intelligence-roadmap.md) — Staged levels of retrieval intelligence: alias broadening (current default) → LLM rewrite → graph expansion → answer grading.

The older `docs/02-ingestion-flow.md` is **superseded** by
[docs/ingestion-flow.md](docs/ingestion-flow.md); the file remains
as a stub so legacy links resolve.

## License

See `LICENSE`.
