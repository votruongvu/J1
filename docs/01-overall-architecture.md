# 01. Overall Architecture

> Audience: product owners, business stakeholders, and engineers who
> want the shape of the system before reading any code.
> [Back to README](../README.md).

## What J1 does, end-to-end

1. A user (a person or an integrating system) **uploads a document**
   into a project.
2. J1 **registers the document** and starts a Temporal workflow that
   profiles it, decides how to parse it, and runs it through a single
   compile black box that produces structured chunks, embeddings, and
   a knowledge graph.
3. Optional **domain enrichment** adds value on top of that compile
   output — extracting entities, classifying sections, generating
   captions, etc. — using domain packs that ship with the platform.
4. The compile + enrichment output is stamped onto a new
   **`DocumentSnapshot`**, atomically promoted to "active", and the
   prior snapshot is moved aside.
5. A user can then **ask questions** in the same project. J1's query
   orchestrator retrieves evidence from the active snapshot, sends
   it to an LLM, and returns an answer plus citations.

Every step is scoped to `(tenant, project)`. Cross-tenant access is
not just discouraged, it is impossible by construction — the registry
+ workspace + audit trail are all per-tenant.

## Main components and how they cooperate

```
                ┌──────────────────────┐
   Upload  ───▶ │  Document registry   │ ────────┐
   (REST/UI)   │  (per project)        │         │
                └──────────────────────┘         │
                                                 ▼
                                  ┌──────────────────────┐
                                  │ ProjectProcessing    │
                                  │  Workflow (Temporal) │
                                  └──────────────────────┘
                                                 │
                          ┌──────────────────────┼──────────────────────┐
                          ▼                      ▼                      ▼
                ┌───────────────┐   ┌─────────────────────┐  ┌──────────────────┐
                │  Profile +    │   │   RAGAnything       │  │ Post-compile     │
                │  Assessment   │──▶│   Compile (black    │─▶│ Domain Enrichment│
                │               │   │   box: parse, chunk,│  │ (per domain pack)│
                │               │   │   embed, graph)     │  │                  │
                └───────────────┘   └─────────────────────┘  └──────────────────┘
                                                 │                      │
                                                 ▼                      ▼
                                  ┌──────────────────────────────────────────┐
                                  │  Artifacts + Snapshot + Index            │
                                  │  (Postgres FTS, S3 artifacts, graph)     │
                                  └──────────────────────────────────────────┘
                                                 │
                                                 ▼
   Query (REST/UI) ─────────▶ ┌──────────────────────────────────────────┐
                              │  SmartQueryOrchestrator                 │
                              │  (retrieve → gate → synthesise → cite)  │
                              └──────────────────────────────────────────┘
                                                 │
                                                 ▼
                                         Answer + citations
```

### Components

- **REST adapter** (`src/j1/adapters/rest`): the public API. Owns
  upload, document lifecycle, run control, query endpoints. Maps
  pure-Python DTOs to the wire schema.
- **Application facade** (`src/j1/integration`): the seam between
  REST and the core services. Cross-cutting concerns (auth, events,
  retrieval, citation lookup) live here.
- **Document layer** (`src/j1/documents`, `src/j1/intake`): the
  document registry, snapshot store, lifecycle service.
- **Orchestration** (`src/j1/orchestration`): Temporal workflows
  and activities. The `ProjectProcessingWorkflow` is the entry
  point for every ingestion.
- **Compile black box** (`src/j1/providers/raganything`): RAGAnything
  wraps LightRAG for graph + vector and MinerU/PyMuPDF for parsing.
  J1 treats compile as one call that consumes a document and
  produces a snapshot-scoped workspace with chunks, embeddings,
  and a knowledge graph.
- **Post-compile processing** (`src/j1/processing`): assessment plan
  building, enrichment overlay, compile-quality gating.
- **Domain packs** (`src/j1/domains`): per-domain configuration —
  prompt overrides, extraction hints, validation rules. Ships with
  `general` and `civil_engineering`.
- **Query layer** (`src/j1/query`): the `SmartQueryOrchestrator`
  with its intent classifier, multi-route retriever, evidence
  selector, sufficiency gate, and answer-quality gate.
- **Validation surface** (`src/j1/validation`): Manual Test Query
  (the detailed inspection tool) and the auxiliary Imported Test
  Cases helper. There is **no** generated-test-case pipeline.
- **Search / evidence** (`src/j1/search`): the Postgres FTS adapter
  that powers auxiliary evidence retrieval and diagnostics.

## Core concepts (high level)

These are the durable nouns. Detail and field-level shapes are in
[04-core-data-model.md](04-core-data-model.md).

- **Tenant** — the isolation boundary. Two tenants share no data,
  no workflows, no audit rows.
- **Project** — the operator's workspace inside a tenant. The
  knowledge base is per project.
- **Document** — an uploaded file. Carries its lifecycle state
  (`attached` / `detached` / `removed`) and points at its currently
  active snapshot.
- **IngestionRun** — *one processing attempt* on a document. Runs
  are diagnostic; they do not by themselves represent the visible
  knowledge state.
- **DocumentSnapshot** — the versioned knowledge state. Created
  up-front by the dispatch layer, populated by the workflow, and
  atomically promoted once compile + enrichment succeed.
- **Profile** — a runtime configuration bundle (LLM choices, query
  policy, behaviour flags) selected per request or per project.
- **Knowledge Base** — the *queryable* set: every document in a
  project whose `knowledge_state == attached` and whose
  `active_snapshot_id` is set.

## Why this architecture exists

### Ingestion as a workflow, not a request

Parsing real-world PDFs is expensive (OCR, vision LLM calls,
chunking, embedding) and intermittently flaky. We use Temporal so:

- Each stage is a retriable activity.
- A worker crash mid-ingest doesn't lose work.
- The history is queryable (the Temporal UI shows every step).
- Long-running and cheap tasks live in the same workflow without
  blocking the API.

### Compile as a black box

We don't want the J1 core to care whether the parse used MinerU or
a vendor API, or whether the graph came from LightRAG, Neo4j, or
something we replace next quarter. The contract is:

- Compile takes `(ctx, document_id, snapshot_id)` and produces
  artifacts under a snapshot-scoped workspace.
- Anything downstream that needs the artifacts asks via the
  artifact registry — not by reaching into RAGAnything internals.

### Domain enrichment after compile

Compile output is generic. Domain packs interpret it: a civil
engineering pack might flag "design codes" sections, surface
material strengths, or extract risk lists. Putting this *after*
compile keeps compile re-usable; replacing or adding a domain pack
doesn't trigger a re-parse.

### Snapshot, not run-id, as the visibility primitive

In older revisions, queries checked `IngestionRun.run_id` to decide
what was visible. That coupled "the most recent successful attempt"
with "what users should see right now" and made re-ingest
partially-clobber the live state. The current model promotes a
`DocumentSnapshot` atomically: queries always read
`document.active_snapshot_id` and never `IngestionRun.run_id`.

## What this document does *not* cover

- Field-level data shapes — see [04-core-data-model.md](04-core-data-model.md).
- Step-by-step ingestion mechanics — see [02-ingestion-flow.md](02-ingestion-flow.md).
- How a query is planned and executed — see [03-query-flow.md](03-query-flow.md).
- How to run and develop locally — see [05-developer-onboarding.md](05-developer-onboarding.md).
- Production deployment direction — see [07-deployment-and-scaling.md](07-deployment-and-scaling.md).
- Domain pack configuration — see [10-domain-configuration.md](10-domain-configuration.md).
- Known gaps between intent and code — see [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md).
