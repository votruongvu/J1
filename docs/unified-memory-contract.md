# Unified Memory Contract

> The logical read model the query layer consumes. Physical storage
> stays split; this document defines the projection that composes it.
>
> [Back to README](../README.md). See also
> [ingestion-flow.md](ingestion-flow.md),
> [03-query-flow.md](03-query-flow.md),
> [04-core-data-model.md](04-core-data-model.md).

## Why this exists

The query / retrieval / answer-synthesis layers must not reach into
unrelated records to assemble "what is currently queryable for this
scope." A query that asks for "this document's active knowledge"
should not re-derive the active snapshot by joining the run store,
the snapshot store, the artifact registry, and the lifecycle flags
itself.

This contract defines a single logical projection — the
`UnifiedMemoryView` — that callers consume. The physical storage
remains split (DocumentRecord, IngestionRun, snapshot store,
artifact registry); only the **read shape** is unified.

## UnifiedMemoryView

The minimum shape every scope variant returns. Fields are documented
in business terms — concrete Python field names land when the Phase 2
resolver is implemented.

| Field | Source | Notes |
| --- | --- | --- |
| `project_id` | request context | Required. |
| `document_id` | request scope | `None` for project-wide views. |
| `active_snapshot_id` | `DocumentRecord.active_snapshot_id` | The visibility key. `None` until first successful promote. |
| `active_run_id` | run whose `target_snapshot_id == active_snapshot_id` | The producing run for the active snapshot. |
| `run_status` | `IngestionRun.status` | For the producing run. |
| `compile_status` | summary stamped onto the final run report | One of `succeeded` / `failed` / `not_started`. |
| `queryable_status` | computed (see §"Queryability rules") | Explicit, explainable. |
| `queryable_reason` | computed | Operator-readable copy when `queryable=false`. |
| `compile_artifact_refs` | artifact registry filtered by `(document_id, snapshot_id)` | Artifact ids only — no raw bytes. |
| `graph_or_index_ref` | adapter-supplied reference | Opaque handle; never a raw filesystem path on the wire. |
| `domain_id` | run metadata / project workspace default | Which domain pack drove enrichment, if any. |
| `enrichment_status` | last `run_type=run_domain_enrichment` run for the snapshot | `none` / `succeeded` / `failed` / `stale`. |
| `enrichment_artifact_refs` | artifacts whose `kind` starts with `enriched.` for the active snapshot | Optional augmentation. |
| `plan_warnings` | `final_ingestion_report.warnings` | Surface as-is in the UI. |
| `unsupported_controls` | assessment-plan warnings | Honest disclosure of controls RAGAnything cannot honour. |
| `created_at` / `updated_at` | snapshot store timestamps | Where available. |

Project-wide variants (`resolve_project_active_memory`) return a
collection of these views, one per attached document. The
collection-level result also carries an aggregate `queryable_status`
("queryable" when at least one document is queryable; "empty" when
the project has no attached documents; otherwise "not_queryable").

## Resolver entry points

A single resolver owns the projection. Suggested names — final API
lands with the Phase 2 implementation:

```text
UnifiedMemoryResolver
  resolve_project_active_memory(project_id)
    → ProjectActiveMemoryView (collection of document views + aggregate)
  resolve_document_active_memory(document_id)
    → DocumentActiveMemoryView
  resolve_run_memory(run_id)
    → RunMemoryView (explicit run scope; for audit / diagnostics)
```

Callers MUST go through this resolver. They MUST NOT:

- Re-derive the active snapshot by sorting runs by `started_at`.
- Read `IngestionRun.metadata` to decide visibility.
- Treat "the most recent succeeded run" as the visibility key.
- Mix run-level and snapshot-level scopes in the same query.

## Queryability rules

Queryable status is explicit and explainable. The vocabulary below
is the **canonical shipped contract** — the strings on the wire and
the values of ``j1.memory.QueryableStatus``. The REST surface emits
them verbatim in the ``details.queryableStatus`` payload on a
``MEMORY_NOT_QUERYABLE`` 409.

| Status | Meaning |
| --- | --- |
| `not_started` | Document exists, no run has produced a snapshot yet. |
| `compile_failed` | The producing run's compile failed; the previous active (if any) is still queryable. |
| `missing_artifacts` | Active snapshot is set but its compile artifacts cannot be resolved — usually a cleanup race. Treated as not-queryable; re-index. |
| `not_attached` | Document is detached or has a non-`stable` lifecycle. Attach it to bring it back into scope. |
| `removed` | Document was removed from the knowledge base. Re-upload to restore. |
| `queryable` | Active snapshot is promoted and compile artifacts resolve. Basic-only knowledge is queryable. |
| `enrichment_available` | Same as `queryable`, plus a successful enrichment artifact set is attached. |
| `enrichment_failed` | `queryable` is still true; the last enrichment attempt failed but the active snapshot is unchanged. |
| `run_unknown` | ``resolve_run_memory`` only — the requested run id is not in the run store. |

A view is **queryable** iff its status is one of ``queryable`` /
``enrichment_available`` / ``enrichment_failed``. Everything else
is treated as not-queryable by callers.

Concrete rules every implementation must follow:

1. **Compile is the floor.** A document is queryable as soon as
   compile succeeds, the active snapshot is promoted, and the
   compile artifacts are durable. Domain Enrichment success is
   **never** part of this check unless a domain policy explicitly
   sets `require_enrichment_success=true`.
2. **Enrichment failure does not regress queryability.** A failed
   enrichment run does not promote and leaves the previous active
   snapshot — and therefore the queryable status — untouched.
3. **Missing artifacts override status.** If
   `(document_id, snapshot_id)` has no compile artifacts in the
   registry, the view reports `not_queryable` even when the
   snapshot store says `READY`. The DB optimistic state never wins
   over the actual artifact set.
4. **Old runs do not pollute active query by default.** A query
   scoped to "project active" or "document active" only resolves
   the producing run's artifacts. A run that succeeded but did
   not promote (CAS conflict, superseded mid-flight) is invisible
   to active query.
5. **Explicit run scope is allowed for audit.** A caller may ask
   for `resolve_run_memory(run_id)`. The resolver returns a view
   only if the run still has valid compile output; otherwise the
   view reports `not_queryable` with a reason such as
   `run_artifacts_cleaned_up`.
6. **Deleted runs and removed documents leave no traces.** When a
   run is cleaned up or a document is removed, every store —
   artifact registry, evidence index, LightRAG workspace,
   enrichment artifacts — must reflect the deletion. The Unified
   Memory View MUST NOT carry refs to artifacts that no longer
   exist physically.

## Scope vocabulary

Suggested `MemoryScope` shapes:

```text
MemoryScope.project_active
  Project-wide active query. Resolves every attached document with
  a promoted snapshot.

MemoryScope.document_active(document_id)
  Single-document active query. Resolves the document's active
  snapshot.

MemoryScope.run_explicit(run_id)
  Audit / diagnostic scope. Resolves the named run only.
```

Mapping to the existing `QueryScope` types (Phase 2 may rename, but
the semantics are stable):

| Existing `QueryScope` | Maps to |
| --- | --- |
| `WorkspaceScope` | `project_active` |
| `ActiveScope(document_id)` | `document_active(document_id)` |
| `RunScope(run_id)` | `run_explicit(run_id)` |

## Enrichment is optional augmentation

Enrichment metadata surfaces on the view as additive fields. The
contract:

- A `UnifiedMemoryView` with `enrichment_status=none` is still
  queryable when `queryable_status=queryable`.
- The query orchestrator MUST NOT use enrichment artifacts as a
  substitute for compile output. Citations cannot point at an
  enrichment artifact alone; they bind to chunk / graph / table
  artifacts produced by compile.
- The query orchestrator MAY consult enrichment metadata for
  optional augmentation: query expansion (aliases / domain terms),
  rerank hints, diagnostic copy. Augmentation must be inspectable
  in the trace and disable-able per request.
- A future entity-alias provider (Phase 4) reads aliases from
  domain pack config first, then from enrichment artifacts. The
  resolver exposes both via the augmentation provider interface;
  it does not pre-merge them.

## Old run rules

By default, an old (non-active) run does **not** participate in
active query.

- Active query scopes never resolve a non-active run's artifacts.
- A run-scoped query may resolve a specific run for audit, but the
  surface that exposes it must make the explicit-run nature
  visible (e.g. the run-detail Manual Test Query console). It
  cannot silently switch from "answer using current knowledge" to
  "answer using this old run".
- Old run data is fair game for diagnostics, run-history rendering,
  and audit comparison — but never for the public `/answer` route
  or for the project-active / document-active query paths.

## Query augmentation seam

The Unified Memory contract feeds the query layer's augmentation
seam. The shape is documented here because it's where retrieval
code learns "what does the active scope's domain pack say about
this query?" without reading pack internals.

Three exports in `j1.memory`:

- **`DomainQueryAugmentationProvider`** — protocol. Implementations
  receive `(DocumentMemoryView, query)` and return
  `AugmentationHints` (terms, alias pairs, recommended retrieval
  expansions, source tag).
- **`DomainPackAugmentationProvider`** — domain-pack-backed default.
  Reads `DomainPack.extraction_hints` (terminology, retrieval
  hints, `entity_aliases`). Returns `(short, long)` alias pairs +
  query-aware expansion forms.
- **`compute_query_expansion(query, hints, max_terms=8)`** —
  deterministic capped helper. Returns a tuple with the **original
  query at index 0** (never evicted by the cap) plus the
  `max_terms - 1` highest-precedence expansion forms. The
  module-level ceiling `MAX_QUERY_EXPANSION_TERMS` silently clamps
  oversized callers — a runaway pack cannot fan retrieval into
  hundreds of terms.

Feature flag: `J1_DOMAIN_QUERY_AUGMENTATION_ENABLED` (default
`true`). When `false`, the provider returns the empty
`source="disabled"` shape so callers branch uniformly without
re-reading the env.

The augmentation is **advisory**. Synthesis MUST consume the
original query. Retrieval MAY consume the expansion list. Neither
is required to produce a correct answer — when the provider
returns empty hints, retrieval falls back to the original query.

## What the resolver does NOT do

- **It does not load chunk bytes.** It returns artifact refs; the
  retriever loads bodies as needed via the chunk projector.
- **It does not enforce auth.** Tenant / project scoping happens
  in the REST adapter and the eligibility resolver. The Unified
  Memory Resolver assumes a valid `(tenant, project)` context.
- **It does not run queries.** It only resolves "what knowledge is
  eligible for this scope." Retrieval, evidence selection,
  synthesis, and grounding live in the existing
  `SmartQueryOrchestrator` pipeline (see
  [03-query-flow.md](03-query-flow.md)).
- **It does not mutate state.** Reads only. Promotions, deletions,
  and snapshot CAS happen elsewhere.

## Migration from the current code shape

The current code already implements most of the building blocks:

- `j1.query.eligibility.resolve_query_snapshots` returns eligible
  `(document_id, snapshot_id)` pairs given a `QueryScope`.
- `j1.documents.snapshot_service` owns snapshot state and promotion.
- The artifact registry owns artifact lookup keyed by
  `metadata.snapshot_id`.
- The run store owns lifecycle / status / metadata for runs.

The Phase 2 deliverable is a single facade that composes these into
the `UnifiedMemoryView` shape and replaces every duplicate "find
the active snapshot for this scope" implementation. The downstream
call sites (manual test query, imported test cases, public
`/answer`, dev query trace) keep their behaviour; they just stop
reaching past the facade.
