# J1

A reusable, domain-neutral framework for ingesting, processing, and
querying document-based knowledge.

J1 is a Python library — not an application. It exposes pluggable
processing contracts, durable workflow orchestration on Temporal, and
a complete external-integration surface (REST + OpenAPI + SSE +
webhooks + AsyncAPI + bulk import/export). Domain-specific behaviour
lives in **profiles**; the framework itself ships no industry vocabulary.

```
documents → intake → compile → enrich → graph → index → query → answer
 ▲ (every stage is pluggable; profiles configure prompts/schemas)
 │
 audit + cost + review (recorded for every stage)
```

---

## Quick links

| Topic | Doc |
|---|---|
| **Start here — developer onboarding** (zero → first workflow) | [docs/development/onboarding.md](docs/development/onboarding.md) |
| **Full architecture** | [docs/architecture.md](docs/architecture.md) |
| External integration map (REST + webhook + queue + bulk) | [docs/external-integration-architecture.md](docs/external-integration-architecture.md) |
| REST API + SSE streaming | [docs/rest-api.md](docs/rest-api.md) |
| Auth + scopes | [docs/security.md](docs/security.md) |
| Webhooks (CloudEvents 1.0) | [docs/webhooks.md](docs/webhooks.md) |
| Queue / event broker (AsyncAPI 3.0) | [docs/event-integration.md](docs/event-integration.md) |
| Bulk import / export (NDJSON) | [docs/bulk.md](docs/bulk.md) |
| MCP status | [docs/mcp-status.md](docs/mcp-status.md) |
| **Provider layer + composition root** (LLM roles, optional RAGAnything / Graphify) | [docs/providers.md](docs/providers.md) |
| Environment-variable reference (every `J1_*` var) | [docs/configuration/environment.md](docs/configuration/environment.md) |
| **Integrating a new project** (sources, providers, domain policies, evaluators — end-to-end) | [docs/integration-guide.md](docs/integration-guide.md) |
| **Extension model overview** (5-layer map, 12 contracts, generic workflow shape) | [docs/extension/overview.md](docs/extension/overview.md) |
| Adapter contracts and canonical primitives | [docs/extension/contracts.md](docs/extension/contracts.md) |
| Adapter manifests and the capability registry | [docs/extension/manifest-and-registry.md](docs/extension/manifest-and-registry.md) |
| Adapter conformance tests | [docs/extension/conformance-tests.md](docs/extension/conformance-tests.md) |
| Adding a provider (compiler / graph / retrieval / LLM) | [docs/extension/add-a-provider.md](docs/extension/add-a-provider.md) |
| Domain-module isolation (building on top of J1) | [docs/extension/domain-module-isolation.md](docs/extension/domain-module-isolation.md) |
| Temporal operations | [docs/operations/temporal.md](docs/operations/temporal.md) |
| Operational issues | [docs/troubleshooting.md](docs/troubleshooting.md) |
| **Local Docker stack** (API + worker + Temporal + UI) | [deploy/dev/README.md](deploy/dev/README.md) |
| Contributor guide | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Install

Python 3.11+ required.

```bash
python3.11 -m venv.venv
source.venv/bin/activate
pip install -e ".[dev]"
```

A running Temporal server is needed only when running workers; unit
tests don't require one.

---

## Run the tests

```bash.venv/bin/pytest # full suite (~4s).venv/bin/pytest -q # quiet mode.venv/bin/pytest --durations=10
```

The full suite is hermetic: every test uses `tmp_path`-style
filesystem isolation, no external services, no network. CI just runs
`pytest`.

---

## Run the whole stack locally with Docker

A self-contained Docker Compose stack — API + worker + Temporal +
Temporal UI — is in [deploy/dev/](deploy/dev/):

```bash
cp.env.example.env
docker compose -f deploy/dev/docker-compose.yml up --build
```

- API: <http://localhost:8000>
- Temporal UI: <http://localhost:8080>

Trigger a sample workflow:

```bash
curl -X POST http://localhost:8000/projects \
 -H "X-Tenant-Id: acme" -H "Content-Type: application/json" \
 -d '{"projectId": "alpha"}'
```

See [deploy/dev/README.md](deploy/dev/README.md) for the full
walkthrough, including which services were intentionally omitted
(Postgres / Redis / S3) and why.

---

## Run an end-to-end smoke

The single E2E test
([tests/test_e2e_processing_flow.py](tests/test_e2e_processing_flow.py))
walks the entire processing pipeline locally — create project,
register documents, drive the workflow state machine, run real
compile/enrich/graph/index/query through `ProcessingService` against
mock processors, exercise the review gate, verify audit + cost logs.
~50 ms.

```bash.venv/bin/pytest tests/test_e2e_processing_flow.py -v
```

---

## What's where

```
src/j1/
├── intake/ Document registration + dedup
├── processing/ Protocols + ProcessingService + ArtifactDraft
├── enrichers.py Built-in structured enricher scaffolds
├── connectors/ External-tool wrappers (compiler, graph)
├── search/ SQLite FTS5 indexer
├── query/ HybridQueryEngine + intent classifier
├── artifacts/ Per-project artifact registry
├── audit/ Append-only audit log (JSONL)
├── cost/ Cost recording + aggregation + budget gates + model router
├── review/ Human-review queue + governance helpers
├── workspace/ Per-project filesystem layout
├── profiles/ Domain configuration (prompts, schemas, taxonomy, …)
├── orchestration/ Temporal workflows + activities
├── integration/ Ports, DTOs, ApplicationFacade, security, events, bulk
└── adapters/ Outer transport adapters (REST + webhook)
```

The dependency arrow points one way only — outer layers depend on
inner. Statically enforced by tests in
[`tests/test_integration_layer.py`](tests/test_integration_layer.py)
and
[`tests/test_external_integration_consistency.py`](tests/test_external_integration_consistency.py).

---

## Make it talk to the outside world

Stand up a REST server with whatever subset of capabilities you need
wired in:

```python
from j1 import ApplicationFacade, create_rest_api
import uvicorn

facade = ApplicationFacade(...) # see docs/architecture.md § Integration
app = create_rest_api(
 facade,
 authenticator=..., # API-key / JWT — optional
 bulk_export=..., bulk_import=..., # optional
 event_bus=..., # webhooks + queue — optional
 workspace=..., # required for /events lookup
 job_starter=..., # required for /documents/{id}/ingest
)

uvicorn.run(app, host="0.0.0.0", port=8000)
```

Every optional integration gracefully returns 503 when not wired —
misconfiguration silently disables a surface, never silently enables
it. Hit `/capabilities` to see what's on.

---

## License & status

License is currently **unspecified** — no LICENSE file ships with
the repository. Treat redistribution and external use as undecided
until the maintainers publish licensing intent.

The framework's public surface is stable for the modules listed in
[`src/j1/__init__.py`](src/j1/__init__.py). Items flagged "deferred"
or "limitation" in the per-area docs are intentional gaps — see
those docs for the rationale and recipe.

Provider integrations such as RAGAnything and Graphify are
**optional vendor adapters** packaged behind the framework's
provider boundary, not part of the J1 core identity. The same is
true of the LLM clients (OpenAI-compatible HTTP, LangChain) — they
implement role-specific protocols and can be swapped without
touching core code. See [docs/providers.md](docs/providers.md) and
[docs/extension/add-a-provider.md](docs/extension/add-a-provider.md).
