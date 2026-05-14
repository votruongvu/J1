# 08. Multi-KB Model

> Audience: integrators + tech leads designing multi-customer or
> multi-domain deployments.
> [Back to README](../README.md). See also
> [04-core-data-model.md](04-core-data-model.md),
> [03-query-flow.md](03-query-flow.md).

## What a Knowledge Base is in J1

A **Knowledge Base (KB)** is the *queryable scope* a particular
question runs against. In J1 today, the canonical KB is computed
per `(tenant, project)` at query time:

```
KB(tenant, project) =
    { doc for doc in project.documents
      if doc.knowledge_state == 'attached'
         and doc.active_snapshot_id is not None
         and doc.lifecycle_status == 'stable' }
```

A KB is not a persisted object — there is no `knowledge_bases`
table. It is derived from the document registry's state. That's
what makes attach/detach instant: flipping `knowledge_state`
immediately changes KB composition.

## How KB scope is built

A query's KB scope is the intersection of:

| Dimension | Where it comes from |
| --- | --- |
| **Tenant** | `X-Tenant-Id` header. Hard isolation boundary. |
| **Project** | `X-Project-Id` header. Inside the tenant. |
| **Profile** | Loaded by `ProfileLoader` per request — drives LLM bindings, engine knobs, domain-pack defaults. |
| **Document membership** | `knowledge_state == 'attached'` AND `lifecycle_status == 'stable'`. |
| **Snapshot visibility** | `document.active_snapshot_id`. The retrieval path filters every chunk by `metadata.snapshot_id == document.active_snapshot_id`. |

The eligibility resolver (`j1.query.eligibility`) is the single
gate that resolves a `QueryScope` into a `(snapshot_ids,
document_ids)` set. Every retrieval path goes through it. There
is no second route.

## How multiple KBs can exist without pollution

Multi-KB in J1 means **multiple projects**, each with its own KB.
There is no cross-project query. The eligibility resolver hard-stops
at `(tenant, project)`.

Per-project isolation is enforced at every layer:

- The REST adapter rejects cross-tenant access with a uniform 404
  (existence is not probeable).
- The workspace resolver builds tenant/project-scoped paths;
  storage is partitioned by directory.
- The Postgres FTS index has `(tenant_id, project_id)` columns in
  every row; queries filter by them before applying any other
  predicate.
- The Temporal workflows take `ProjectScope.from_context(ctx)` as
  the dispatch boundary; there is no "process all projects"
  workflow.

A tenant can host many projects. The KB is per-project; there is
no "merge two projects' KBs" surface.

## Attach / detach / remove — effects on KB composition

- **Attach** (`POST /documents/{id}/attach`). Flips
  `knowledge_state` to `attached`. Document re-enters the KB on
  the *next* query. Already-running queries are not interrupted.
- **Detach** (`POST /documents/{id}/detach`). Flips
  `knowledge_state` to `detached`. Document leaves the KB
  immediately — the eligibility resolver refuses to admit it.
- **Remove** (`POST /documents/{id}/remove`). Gate-first then
  cleanup: `lifecycle_status` flips to `removing`,
  `active_snapshot_id` is cleared *before* destructive cleanup
  starts. The eligibility resolver disqualifies any document
  whose lifecycle is not `stable`, so a concurrent query never
  sees a partially-removed document.

Re-indexing or refresh-enriching does *not* change KB
composition — the document stays in the KB throughout. Atomic
snapshot promotion swaps which artifacts the KB exposes.

## Active snapshots define what is searchable

Within a KB:

- A document contributes exactly **one** snapshot's worth of
  artifacts to retrieval: its `active_snapshot_id`.
- Previous snapshots stay on disk for audit but are marked
  SUPERSEDED. Their artifacts carry `search_state=superseded` and
  are filtered out at retrieval time.
- A document whose ingestion has never reached a successful
  snapshot has `active_snapshot_id = None` and is invisible to
  retrieval even if `knowledge_state == attached`.

## How profiles affect KB behaviour

Profiles are runtime configuration bundles loaded per request. A
profile can change:

- **Which LLM clients** drive synthesis and intent classification
  (FAST / TEXT / VLM roles).
- **Query engine defaults** — top-k, evidence budget, fallback
  policy.
- **Domain pack selection defaults** — the workspace-wide default
  domain if no per-document pack matched at ingest time.

Profiles do **not** change KB membership. Two profiles querying
the same project see the same document set; what they differ on is
*how* the question is answered.

## How multi-KB affects ingestion

Ingestion is always per-document, per-project. The cross-cutting
concern is:

- The compile black box's workspace path is keyed by
  `(tenant, project, document, snapshot)`. Two projects, even in
  the same tenant, never share a LightRAG workdir.
- Domain pack selection happens per ingestion (the pack is
  detected from document content + signals). A project can host
  documents from different domains without manual intervention.
- The audit log is per-project; ingestion audit events do not
  bleed across projects.

## How multi-KB affects query

- Tenant + project headers are mandatory on every query.
- The orchestrator never combines results from multiple projects.
  If a user needs "search across projects A and B", the
  integration layer needs to issue two queries and combine the
  responses outside J1.
- A profile can override engine choice per query, but cannot
  expand the scope past the project.

## Risks

### Stale data leakage

The snapshot-centered model is designed to prevent this, but
implementation drift could re-introduce it. Watch for:

- Direct reads of artifact metadata that bypass the eligibility
  resolver.
- Run-id-based fallback paths re-emerging anywhere in
  `j1.query.eligibility` (the legacy `run_ids` companion set was
  deliberately removed).
- LightRAG workspace directories that survive past a snapshot's
  cleanup window — they don't leak via retrieval (eligibility
  filters by snapshot id), but they do consume disk.

### Cross-scope leakage

The single layer of defence-in-depth is the eligibility resolver.
If a new retrieval path is added that calls the orchestrator
without scope resolution, it can bypass tenant/project isolation.
Mitigation:

- Every retrieval path must go through
  `SmartQueryOrchestrator.run(OrchestratorRequest)`.
- `OrchestratorRequest.scope` is required.
- `RunScope` in the gated path returns empty — only `unchecked`
  callers (the validation diagnostic surface) can run a raw
  `RunScope`.

### Run-id misuse

`IngestionRun.run_id` is a trace identifier. The retrieval +
visibility layers must never gate on it. Re-introducing
`active_run_id` on `DocumentRecord` would be an immediate revert.

### Profile-as-KB confusion

Profiles are *not* KBs. A "profile change" does not move
documents between KBs. If a future feature needs to expose a KB
selector (e.g. "this user only sees the engineering documents"),
the right design is to either:

- Split into multiple projects (the canonical answer today), or
- Add a queryable `kb_id` attribute on documents and filter the
  eligibility resolver by it.

Mixing profile and KB will produce surprising visibility behaviour.

## Future direction

- A first-class `KnowledgeBase` object that documents can be
  attached to independently of a project would let one project
  host multiple curated KBs (e.g. "internal", "customer-facing").
  Not implemented yet.
- Cross-project answering via a federation layer is a possibility,
  but only outside J1's core (each project query stays scoped;
  the federation aggregates).
- Per-KB profile defaults would let "the engineering KB" pick a
  different LLM than "the legal KB" without changing project
  membership.
