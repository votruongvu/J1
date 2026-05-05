# J1 Architecture

> Companion docs:
> [external-integration-architecture.md](external-integration-architecture.md)
> for the REST/SSE/webhook/queue/bulk surface map;
> [providers.md](providers.md) for the LLM role abstraction +
> optional RAGAnything / Graphify integrations;
> [configuration/environment.md](configuration/environment.md) for
> the canonical `J1_*` env-var reference;
> [operations/temporal.md](operations/temporal.md) for worker
> setup, signals, and recovery;
> [extension/overview.md](extension/overview.md) for the 5-layer
> extension model, the 12 contracts, and the generic workflow shape;
> [extension/contracts.md](extension/contracts.md) for the contracts
> and canonical primitives reference;
> [extension/manifest-and-registry.md](extension/manifest-and-registry.md)
> for the manifest schema + capability registry;
> [extension/conformance-tests.md](extension/conformance-tests.md)
> for the shared adapter test harnesses;
> [extension/add-a-provider.md](extension/add-a-provider.md) for
> plugging in a new compiler / graph / retrieval / LLM provider;
> [extension/domain-module-isolation.md](extension/domain-module-isolation.md)
> for what belongs outside the J1 core;
> [troubleshooting.md](troubleshooting.md) for operational
> issues.

---

## 1. What J1 is

J1 is a reusable Python library for building knowledge-intelligence
systems over heterogeneous documents. It is **not** an application,
not a SaaS, and not bound to any industry. It exposes:

- **Pluggable processing contracts** — every stage (compile, enrich,
  build graph, index, query, complete) is a `Protocol`. Callers
  implement or wrap their own backend.
- **Durable workflow orchestration** on Temporal — long-running
  pipelines survive restarts, support pause/resume/cancel, and pause
  at human-review or budget gates.
- **A complete external-integration surface** — REST + OpenAPI + SSE
  streaming, HMAC-signed webhooks (CloudEvents 1.0), AsyncAPI 3.0
  contract for queue/event delivery, NDJSON bulk import/export, scope-
  based authorisation.

Domain-specific behaviour — taxonomies, prompts, JSON-Schema shapes,
report templates, query routing — lives in **profiles**. The framework
itself ships only the bundled `default` profile, intentionally empty.
A second profile is one directory of YAML/Markdown files and zero
code.

J1 is consumed as `pip install j1`. Application authors wire it into
whatever surface they need (CLI, web service, queue worker, MCP
server, …) — the framework provides every piece except the deployment
glue.

---

## 2. Core principles

These are the seven rules every contributor + reviewer applies. They
are enforced by tests where possible; everywhere else they're a
review checklist.

| # | Principle | Enforcement |
|---|---|---|
| 1 | **Domain-neutral core.** No industry vocabulary in `src/j1/` outside `profiles/`. | Naming sweep + review |
| 2 | **Outward dependency direction.** Outer layers (adapters, integration) depend on inner (services, core); never the reverse. | [`tests/test_integration_layer.py`](../tests/test_integration_layer.py), [`tests/test_external_integration_consistency.py`](../tests/test_external_integration_consistency.py) |
| 3 | **Workflows coordinate, activities act.** `ProjectProcessingWorkflow` decides *what runs in what order*; activities perform the side effects. Workflows must stay deterministic; activities are where I/O happens. | Temporal SDK constraints |
| 4 | **Workflow state stores IDs and metadata, not large content.** `WorkflowStatus` carries `documents_total`, `documents_completed`, `produced_artifact_ids`, `error` — never document bytes, embeddings, or raw artifact bodies. Large content lives on disk in the workspace. | Code review + Temporal payload limits |
| 5 | **External tools are wrapped through connectors.** A specific compiler binary, a graph-builder service, an LLM vendor — each lives behind a connector or `ModelProvider` Protocol. Core never imports vendor SDKs. | `j1.connectors/` package boundary |
| 6 | **Every stage is auditable + cost-tracked.** Compile / enrich / build / index / query each emit an audit event and (where applicable) cost events. Audit + cost are append-only JSONL — no in-place mutation. | `ProcessingService` writes both via injected recorders |
| 7 | **Misconfiguration silently disables a surface, never silently enables it.** Every optional integration (auth, bulk, events, job control, …) is opt-in at construction. When omitted, the matching endpoint returns 503; `/capabilities` reports what's on. | Standard adapter constructor pattern |

---

## 3. Workspace model

Every project gets a deterministic on-disk layout under
`{J1_DATA_ROOT}/tenants/{tenant_id}/projects/{project_id}/`:

```
raw/         Original ingested files ({document_id}{ext})
compiled/    Compiled artifacts ({artifact_id}{ext})
enriched/    Enriched artifacts
graph/       Graph artifacts
search/      SQLite FTS5 database (rebuildable cache)
audit/       events.jsonl + costs.jsonl (append-only)
runtime/     documents.json, artifacts.json, review_items.json,
             feedback.jsonl, webhook_deliveries.jsonl, optional locks
```

Path resolution + traversal protection live in
[`WorkspaceResolver`](../src/j1/workspace/resolver.py). Areas are
defined as a `StrEnum`
([`WorkspaceArea`](../src/j1/workspace/layout.py)) with helpers
`is_durable()` / `is_rebuildable()` so backup tooling can decide what
to snapshot.

`{J1_DATA_ROOT}` defaults to `/data/j1`. Override per-process via the
`J1_DATA_ROOT` environment variable. The path **must be absolute** —
`load_settings()` raises `ConfigError` otherwise.

---

## 4. Document intake

> **Terminology.** Three closely-named concepts — keep them
> distinct:
> - **Intake** is *registering* a raw file into a project: hash,
>   dedup, write to `raw/`, audit. No processing yet.
> - **Compile** is one of the pipeline stages — turning a
>   registered raw document into compiled artifacts via a
>   `KnowledgeCompiler`.
> - **Ingestion job** is the workflow-level concept exposed via
>   the REST surface (`POST /ingestion-jobs`) and the Temporal
>   workflow that drives intake → compile → enrich → graph →
>   index for a project or a single document.

[`DocumentIntakeService`](../src/j1/intake/service.py) registers a
document into a project. Two entry points:

- `register_from_path(ctx, path, *, mime_type=None, actor="system",
  correlation_id=None)` — for files already on disk
- `register_from_stream(ctx, stream, *, original_filename, ...)` — for
  streaming uploads

Behaviour:

1. Stream bytes into a temp file in the project's `raw/` area (same
   filesystem as the final destination — guarantees an atomic
   rename).
2. Compute SHA-256 during streaming.
3. Look up by checksum in the
   [`SourceRegistry`](../src/j1/intake/registry.py). If a matching
   `DocumentRecord` exists: delete the temp file, audit
   `document.duplicate_detected`, raise `DuplicateDocumentError`
   (carries the existing `document_id`).
4. Otherwise: rename temp → `{document_id}{ext}`, write a
   `DocumentRecord` to the registry, audit `document.registered`.

Identifier rules: `tenant_id` and `project_id` must match
`[A-Za-z0-9_-]+`; `original_filename` is required for stream
uploads. The default registry implementation
(`JsonSourceRegistry`) is single-writer JSON; concurrent writers from
multiple processes against the same project are not supported.

---

> **Provider layer + composition root.** Every Protocol below is a
> swappable boundary — the framework selects implementations by
> `kind` string at composition time. The framework currently ships
> two **optional** vendor integrations (RAGAnything as the default
> selection for compiler / graph / retrieval; Graphify as an
> alternative graph provider) plus two LLM client implementations
> (OpenAI-compatible HTTP and LangChain) — none of them is part of
> J1 core identity. To plug in a different vendor / in-house
> implementation, follow the recipe in
> [extension/add-a-provider.md](extension/add-a-provider.md). For
> day-to-day configuration of the bundled providers, see
> [providers.md](providers.md).

## 5. Processing contracts

[`j1.processing.contracts`](../src/j1/processing/contracts.py)
defines the six core protocols. Every backend a deployment plugs in
implements one of them.

| Protocol | Method | Returns |
|---|---|---|
| `KnowledgeCompiler` | `compile(ctx, document_id)` | `ArtifactProcessingResult` |
| `EnrichmentProcessor` | `enrich(ctx, artifact_id)` | `ArtifactProcessingResult` |
| `GraphBuilder` | `build(ctx, artifact_ids)` | `ArtifactProcessingResult` |
| `SearchIndexer` | `index(ctx, artifact_ids)` | `ProcessingResult` |
| `QueryProvider` | `query(ctx, question, *, max_results=None)` | `QueryResult` |
| `ModelProvider` | `complete(ctx, prompt, *, model=None, ...)` | `ModelResponse` |

Each protocol has a `kind: str` attribute used as a dispatch key in
the Temporal activity classes.

[`ArtifactProcessingResult`](../src/j1/processing/results.py) carries
either `drafts` (in-memory) or `artifacts` (already persisted) plus
optional `cost_events` and a `ResultStatus`. The orchestrating
[`ProcessingService`](../src/j1/processing/service.py) materialises
drafts to disk, computes content-hash dedup keys, writes `ArtifactRecord`s
to the registry, and records the audit + cost trail.

`ArtifactDraft` is the framework's representation of "an artifact
that doesn't yet have an id" — it carries `kind`, `content`,
`suggested_extension`, `source_document_ids`, `source_artifact_ids`,
`metadata`, and `review_required`.

---

## 6. Temporal orchestration

J1 uses Temporal as its durable-workflow substrate. Two workflows
ship:

- [`ProjectProcessingWorkflow`](../src/j1/orchestration/workflows/project_processing.py)
  — full pipeline. Validates the project, lists pending documents,
  runs compile per document, optionally enriches / builds graph /
  indexes, supports pause / resume / cancel signals, budget-approval
  gate, and human-review gates after any stage. Internal state machine
  (visible via `get_status` query):

  ```
  RUNNING → (PAUSED | WAITING_FOR_BUDGET_APPROVAL | WAITING_FOR_REVIEW)
         → COMPLETED | CANCELLED | FAILED_RECOVERABLE | FAILED_FINAL
  ```

  Operator-facing `final_status` (on the workflow's return value):
  `COMPLETED | PARTIAL_COMPLETED | FAILED | CANCELLED | TIMED_OUT`.
  When the internal state ends in either FAILED_* the workflow
  **raises** `ApplicationError` so Temporal sees the run as Failed
  in the UI — it does not return a result with a failure-encoded
  status field.

- [`DocumentProcessingWorkflow`](../src/j1/orchestration/workflows/document_processing.py)
  — single-document path: compile → enrich → index. No gates, no
  per-document loop. Useful for callers that drive ingestion one
  document at a time.

**Workflow state discipline:** the workflow object stores integers,
strings, lists of IDs, and small dataclasses
([`WorkflowStatus`](../src/j1/orchestration/workflows/project_processing.py)).
It never holds document bytes, raw extracted text, embeddings, or
artifact bodies — those live on disk and are addressed by
`document_id` / `artifact_id`. This keeps workflow histories small
enough for Temporal's internal payload limits and means a
continue-as-new restart can carry the *summary* state forward without
streaming megabytes.

**Workflow-failure propagation:** when a required step fails, the
workflow raises
[`temporalio.exceptions.ApplicationError`](https://python.temporal.io/temporalio.exceptions.ApplicationError.html)
with a stable `type` (`J1_INGEST_REQUIRED_STEP_FAILED` for business
failures, `J1_INGEST_UNEXPECTED_ERROR` for wrapped exceptions, and
`J1_INGEST_LOOKUP_FAILED` for missing entities). Temporal sees the
workflow as **Failed** in the UI — never as Completed-with-an-error-
string-inside. The error types are stable: dashboards and alerts
can filter on them without parsing message text. Internal
`WorkflowState` (FAILED_FINAL vs. FAILED_RECOVERABLE) is still
recorded for the `get_status` query, but the operator-facing
`FinalStatus` collapses both to `FAILED`.

**FinalStatus and per-step accounting:** every workflow run produces
a [`ProjectProcessingResult`](../src/j1/orchestration/workflows/project_processing.py)
with:

- `final_status` — `COMPLETED | PARTIAL_COMPLETED | FAILED |
  CANCELLED | TIMED_OUT`. The field tests and operators should
  assert on. `COMPLETED` means *every required enabled step succeeded*
  — never just "the workflow exited cleanly".
- `step_results` — one
  [`StepResult`](../src/j1/processing/step_result.py) per stage with
  `status`, `required`, `source` (caller / planner / policy / default
  / config), optional `reason` for skips, and optional
  [`StepError`](../src/j1/processing/step_result.py) for failures.
  Skipped stages always carry a reason — operators don't have to
  read the request payload to answer "why didn't graph run?".
- `state` — the lower-level `WorkflowState` for callers that need
  the business-vs-unexpected distinction.

**Visibility:** the workflow emits
[structured logs](../src/j1/orchestration/workflows/project_processing.py)
via `workflow.logger` at every lifecycle transition (started /
completed / cancelled / failed) with operationally safe context
(`tenant_id`, `project_id`, `compiler_kind`, etc. — never document
content). It also publishes a typed search attribute `J1IngestStage`
that tracks the active stage so the Temporal UI can group / filter
running workflows. When adaptive planning is enabled, `J1IngestMode`
exposes the chosen mode. Search-attribute upserts are best-effort:
deployments that haven't registered the keys with the namespace just
get no signal (no error). Long-running activities (compile,
build_graph) heartbeat at start / finish, governed by `HEARTBEAT_TIMEOUT`
(2 minutes) so a stalled vendor call surfaces as a timeout instead
of consuming the full activity start-to-close budget.

**Signals available on `ProjectProcessingWorkflow`:**

| Signal | Effect |
|---|---|
| `pause` | Sets pause flag. Workflow waits before the next operation. |
| `resume` | Clears the pause flag. |
| `cancel` | Marks for graceful cancellation; finishes current activity, then exits as `CANCELLED`. |
| `approve_budget` / `reject_budget` | Resolves a budget gate. |
| `approve_review` / `reject_review` | Resolves a human-review gate. |

**Query:** `get_status` returns the current `WorkflowStatus`.

**Retry policy:** `DEFAULT_RETRY = RetryPolicySpec(initial=1s,
backoff=2.0, max_interval=60s, max_attempts=5)`, applied to every
activity unless overridden. The non-retryable list classifies every
deterministic-by-nature failure so a typo doesn't loop forever:

- `J1_INGEST_REQUIRED_STEP_FAILED` — required compile / index / etc.
  reported failure
- `ConfigError` / `ValidationError` / `LLMConfigError` — operator-
  reachable bugs
- `DocumentNotFoundError` / `UnknownProcessorError` — caller named
  something that doesn't exist

Provider 5xx, network blips, and other transient classes stay
retryable — see
[`temporal/retries.py`](../src/j1/orchestration/temporal/retries.py)
for the full list.

---

## 7. Activities

Activities are where the framework does I/O. They live under
[`j1.orchestration.activities/`](../src/j1/orchestration/activities/)
and group by lifecycle role:

| Activity class | What it does |
|---|---|
| `ProjectLifecycleActivities` | `validate_project`, `prepare_workspace`, `register_documents`, `finalize_processing` |
| `KnowledgeProcessingActivities` | `run_knowledge_compilation`, `register_compiled_artifacts`, `run_artifact_enrichment`, `prepare_graph_corpus`, `run_graph_build`, `register_graph_artifacts` |
| `ProcessingActivities` | Generic dispatcher: `compile`, `enrich`, `build_graph`, `index`, `query`. Uses the `kind` attribute on each Protocol to look up the right backend. |
| `ProfilingActivities` | `profile_document` — cheap deterministic profile (extension, MIME, size, page count). Used by the adaptive planner; only registered when adaptive planning is enabled. |
| `ProjectActivities` | `validate_context`, `list_pending_documents`, `compute_spend`, `finalize` |
| `SearchActivities` | `build_search_index` |
| `ReviewActivities` | `create_review_items`, `apply_review_decision` |
| `AccountingActivities` | `calculate_cost`, `write_audit` |

Activities are **idempotent by design** — they may be retried by
Temporal. Mutations that need uniqueness (artifact registration,
review-item creation) use content-hash or correlation-key dedup.

All activity inputs/outputs are
[Temporal-serialisable dataclasses](../src/j1/orchestration/activities/payloads.py)
under one module so the wire shape is auditable in one place.

---

## 8. Adaptive ingestion planning

A clean text PDF and a 200-page scanned multimodal PDF should not
cost the same to process. Adaptive ingestion planning splits "what
does this document look like?" from "what should we do about it?"
and runs only the stages a given document actually benefits from.

**Off by default.** When `J1_INGEST_PLANNER_ENABLED=false` (default)
the workflow uses the legacy "kind is None → skip" gate logic and
behaves exactly as before. Enabling the planner is opt-in per
deployment and per-job (`ProjectProcessingRequest.planner_enabled`).

### 8.1 Profile

[`DocumentProfile`](../src/j1/processing/profiling.py) captures cheap
signals about the document — extension, MIME, file size, page count,
and three-state booleans for `has_images` / `has_tables` /
`has_scanned_pages` (`True` / `False` / `None`-for-unknown). The
[`DeterministicDocumentProfiler`](../src/j1/processing/profiling.py)
runs sub-second: stdlib `mimetypes`, `os.stat`, optional `pypdf` for
page counts. It explicitly does NOT invoke MinerU / RAGAnything / any
LLM — profiling must be cheap enough to run on every document. Profile
failures degrade to warnings; only file-not-found raises.

### 8.2 Plan

[`DefaultIngestPlanner`](../src/j1/processing/planning.py) consumes
a profile + an `IngestPolicy` + the available steps and emits an
[`IngestPlan`](../src/j1/processing/planning.py): mode, per-step
decisions (`enabled`, `required`, `source`, `reason`), confidence,
and a coarse `estimated_cost_level`.

**Policies** bias the planner:

| Policy | Bias |
|---|---|
| `auto` | Planner decides from the profile alone |
| `cost_saving` | Prefer skipping; only enable expensive stages when the profile demands it |
| `balanced` | Production default (currently identical to `auto`; distinct constant so deployments can rebind) |
| `high_accuracy` | Conservative — when uncertain, enable more (e.g. graph for unknown profiles) |
| `force_full` | Enable every configured stage |
| `text_only` | Force compile + index only; record warnings for tables / images / scanned pages so the trade-off is auditable |

**Modes** are pure data — descriptive labels mapped to a frozenset of
enabled steps:

`TEXT_ONLY`, `TEXT_WITH_LIGHT_ENRICHMENT`, `TABLE_AWARE`,
`MULTIMODAL_LIGHT`, `MULTIMODAL_FULL`, `GRAPH_AWARE`,
`FULL_DIAGNOSTIC`.

### 8.3 Step source precedence

Caller-supplied `compilerKind` / `enricherKind` / `graphBuilderKind` /
`indexerKind` always win over the planner. The recorded `StepResult`
carries `source=CALLER` so audit logs explain which decisions came
from the operator vs. the planner. Order of precedence (highest
first):

1. **Caller** — explicit kind on the ingest request.
2. **Planner** — adaptive planner's mode-driven decision.
3. **Policy** — global / per-job ingest policy override.
4. **Default** — capability default (no caller, no planner).
5. **Config** — operator-set deployment config (e.g. enrichment
   disabled).

### 8.4 FAST LLM role

The optional [`LLM_ROLE_FAST`](../src/j1/llm/registry.py) role
covers cheap, structured tasks (document classification, mode
selection, light metadata, heading normalisation). It reuses the
existing OpenAI-compat client class — adding the role does NOT
introduce a separate adapter. Typical setups point both
`J1_TEXT_LLM_BASE_URL` and `J1_FAST_LLM_BASE_URL` at the same endpoint
with just a different `J1_FAST_LLM_MODEL`.

The FAST role is OPTIONAL: deterministic planning works without it.
When `J1_FAST_LLM_*` is unconfigured the planner skips its LLM-
fallback path entirely. Consumers must call `try_fast()` rather than
`fast()` so missing config is a no-op rather than a startup failure.

---

## 9. Knowledge compiler connector

External compilers — typically a binary, microservice, or in-process
library — are wrapped via
[`ExternalKnowledgeCompiler`](../src/j1/connectors/compiler/connector.py),
which implements the `KnowledgeCompiler` Protocol.

Two adapters ship for the wrapper:

- `CallableCompilerAdapter` — wraps any Python callable. Useful for
  in-process integration and tests.
- `SubprocessCompilerAdapter` — invokes an external binary with
  template-substituted arguments (`{input}`, `{outdir}`,
  `{document_id}`, `{cache_dir}`).

Communication is **filesystem-based**: input file → temp directory,
output files read back from temp directory, then mapped to artifact
kinds via `output_mapping` in the
[`CompilerConfig`](../src/j1/connectors/compiler/config.py).

The connector itself never invokes an LLM or knows about vendor
APIs — that responsibility belongs to whatever the adapter wraps.

---

## 10. Enrichment pipeline

[`j1.enrichers`](../src/j1/enrichers.py) ships 9 built-in
`_StructuredEnricher` subclasses. Each implements the
`EnrichmentProcessor` Protocol with a defined output `kind` and a JSON
schema:

`DocumentClassifier`, `RequirementExtractor`, `TableExtractor`,
`VisualContentDescriber`, `FormulaExtractor`, `RiskExtractor`,
`ConsistencyChecker`, `SourceMapper`, `ConfidenceAssessor`.

These are **scaffolds** — the `_produce()` method is a stub that
returns empty structured output by default. A deployment plugs in a
`ModelProvider` and overrides `_produce()` (or replaces the enricher
entirely with a custom `EnrichmentProcessor`) to produce real
content. The framework intentionally ships **no** LLM-vendor
integration; production deployments wire that in their own code.

`GENERIC_ENRICHERS` is a tuple of all nine classes for callers that
want to register them all.

---

## 11. Graph builder connector

[`ExternalGraphBuilder`](../src/j1/connectors/graph/connector.py)
wraps an external graph-construction tool the same way the compiler
connector wraps a compiler. Two adapters: `CallableGraphAdapter` and
`SubprocessGraphAdapter`.

The connector loads the project's `Profile` and surfaces the graph
taxonomy (node types, edge types) and review rules to the underlying
adapter. The adapter is responsible for actually building the graph
and writing the output files into the supplied temp directory.
Output kinds are configured via
[`GraphConfig.output_mapping`](../src/j1/connectors/graph/config.py).

The shipped artifact kinds are:

- `graph_json` — the graph itself
- `graph_html` — optional rendered visualisation
- `graph_metadata`, `graph_report`, `graph_cache` — supporting outputs

The framework treats every graph file as opaque content + metadata —
no built-in path-finding or BFS/DFS. The
[`GraphQueryProvider`](../src/j1/query/providers.py) reads
`graph_json` artifacts and extracts edge lists for query-time
traversal (limited; see § Limitations).

---

## 12. Search indexing

[`SqliteSearchIndexer`](../src/j1/search/indexer.py) implements the
`SearchIndexer` Protocol on top of SQLite FTS5. One database file per
project under `<workspace>/search/index.db`.

Behaviour:

- `index(ctx, artifact_ids)` — reads each artifact's content from
  disk and indexes its text into the per-project FTS5 table. Bytes
  beyond `MAX_INDEXED_BYTES` are truncated.
- `search(ctx, query, *, artifact_types=None, max_results=20)` —
  returns ranked `SearchHit`s with BM25 score + the artifact's
  `source_document_id` and `source_location` metadata for citations.
- `build_full_index(ctx)` — convenience helper that lists the
  registry and indexes everything.

The search database is a **rebuildable cache** — `WorkspaceArea.SEARCH`
is in `REBUILDABLE_AREAS`, so backup tooling can skip it. Runtime
checks at indexer construction confirm SQLite was built with FTS5
support, raising `SearchIndexerError` early if not.

---

## 13. Hybrid query engine

[`HybridQueryEngine`](../src/j1/query/engine.py) is the front door
for retrieval. It composes five
[`QueryProvider`](../src/j1/query/providers.py) implementations:

| Provider | Backed by |
|---|---|
| `KnowledgeQueryProvider` | FTS5 search; concatenates top hits |
| `GraphQueryProvider` | Reads `graph_json` artifacts; emits source citations |
| `EvidenceProvider` | FTS5 + source-document verification |
| `ConsistencyProvider` | Reads `enriched.consistency_findings` artifacts |
| `ReportGenerator` | Materialises an answer using the profile's report template |

[`QueryMode`](../src/j1/query/models.py) selects the routing:

- `AUTO` — `QueryIntentClassifier` keyword-matches to a mode
- `KNOWLEDGE_FIRST`, `GRAPH_FIRST`, `EVIDENCE_FIRST`,
  `CONSISTENCY_CHECK`, `REPORT_GENERATION` — explicit override

`AUTO` falls back from `KNOWLEDGE_FIRST` to `GRAPH_FIRST` when the
former returns no sources, and merges the results.

The engine returns
[`QueryResponse`](../src/j1/query/models.py) — `answer`, `mode_used`,
`sources` (list of `SourceReference`), `related_artifacts`,
`graph_paths`, `confidence`, `confidence_level`, `review_required`,
`warnings`, `warning_categories`.

The current intent classifier is keyword-based; report rendering is
naive `{{question}}`/`{{artifacts}}` substitution. Replacing either is
a localised change that doesn't ripple.

---

## 14. Cost control

[`j1.cost`](../src/j1/cost/) splits cost concerns into focused
modules:

- [`breakdown.py`](../src/j1/cost/breakdown.py) — `CostBreakdown` (a
  vendor-neutral `(vendor, model, unit_kind, units, amount)` record)
  + `CostResult`.
- [`recorder.py`](../src/j1/cost/recorder.py) — `CostRecorder`
  Protocol + `DefaultCostRecorder` writing `CostEvent`s.
- [`sink.py`](../src/j1/cost/sink.py) — `CostSink` Protocol +
  `JsonlCostSink` (append-only JSONL under `audit/costs.jsonl`).
- [`aggregator.py`](../src/j1/cost/aggregator.py) — `CostAggregator`
  reads the JSONL log and computes totals by correlation id, document
  id, query id, and `BudgetLevel`.
- [`budget.py`](../src/j1/cost/budget.py) — `BudgetGuard`,
  `BudgetPolicy`, `BudgetCheck`, `BudgetDecision`. Used by the
  workflow's budget gate to decide whether spend exceeds an approved
  ceiling.
- [`router.py`](../src/j1/cost/router.py) — `ModelRouter` maps a
  `TaskCategory` (CLASSIFICATION, SUMMARIZATION, EXTRACTION,
  VISUAL_DESCRIPTION, FORMULA_ANALYSIS, GRAPH_EXTRACTION,
  QUERY_ANSWERING, REPORT_GENERATION) → `ModelSelection`. Provides a
  pluggable abstraction over `ModelProvider`s.
- [`estimator.py`](../src/j1/cost/estimator.py) — pre-flight cost
  estimation helpers.

Cost recording is mandatory — `ProcessingService` records a cost
event for every stage that returns one, even if the amount is zero.
Aggregation is deferred to the `CostAggregator`, which scans the log
on demand.

---

## 15. Human review

[`j1.review`](../src/j1/review/) is the framework's gate for
human-in-the-loop decisions.

- [`ReviewItem`](../src/j1/review/models.py) — the queue entry
  (review_item_id, project, target_kind, target_id, review_status,
  requested_at, optional actor + notes + metadata).
- [`ReviewQueue`](../src/j1/review/queue.py) Protocol +
  `JsonReviewQueue` (per-project JSON file under
  `runtime/review_items.json`).
- [`ReviewActivities.create_review_items`](../src/j1/orchestration/activities/review.py)
  — workflow-side helper that turns
  `enriched.review_findings`-style outputs into queue entries.
- [`ReviewActivities.apply_review_decision`](../src/j1/orchestration/activities/review.py)
  — applies an `approved` / `rejected` decision, updates the queue
  entry, and writes a `review.decision` audit event.
- [`governance.py`](../src/j1/review/governance.py) — `ConfidenceLevel`
  + `WarningCategory` enums + `confidence_level_from_score()` helper.

The workflow's review gates (`GATE_AFTER_COMPILE`, `GATE_AFTER_ENRICH`,
`GATE_AFTER_GRAPH`, `GATE_AFTER_INDEX`) are configured via
`ProjectProcessingRequest.review_after`. When a gate is reached the
workflow enters `WAITING_FOR_REVIEW` and only resumes after the
`approve_review` (or `reject_review`) signal arrives.

---

## 16. Audit logging

[`j1.audit`](../src/j1/audit/) is the framework's append-only audit
trail.

- [`AuditEvent`](../src/j1/audit/events.py) — `event_id`,
  `occurred_at`, `project`, `actor`, `action`, `target_kind`,
  `target_id`, `correlation_id`, `payload` (free-form dict for safe
  metadata).
- [`AuditRecorder`](../src/j1/audit/recorder.py) Protocol +
  `DefaultAuditRecorder`.
- [`AuditSink`](../src/j1/audit/sink.py) Protocol + `JsonlAuditSink`
  (append-only JSONL under `audit/events.jsonl`).

Action names are stable strings — `document.registered`,
`document.duplicate_detected`, `processing.compile.completed`,
`processing.enrich.completed`, `processing.graph.completed`,
`processing.index.completed`, `processing.query.completed`,
`review.decision`, etc. Tests use these as assertion anchors.

The audit log is a **separate concept from the integration-layer
event system** — integration events (`document.uploaded`,
`answer.generated`, …) are application events for external delivery
(webhooks / queues); audit events are a forensic record of what the
framework did internally. Both are durable but they live in different
JSONL files and serve different consumers.

---

## 17. Profile system

A profile is a directory of YAML / JSON / Markdown files that
configures domain-specific behaviour without changing code.

[`ProfileLoader`](../src/j1/profiles/loader.py) finds profiles by
directory name across configurable search paths. The bundled
[`default` profile](../src/j1/profiles/default/) has a deliberately
empty taxonomy + routing — the framework itself ships no industry
vocabulary.

A profile contains:

| File | Purpose |
|---|---|
| `profile.yaml` | Identity + version + descriptive metadata |
| `graph_taxonomy.yaml` | Allowed node types, edge types, and validation rules |
| `query_routing.yaml` | Keyword → mode hints for the intent classifier |
| `review_rules.yaml` | Patterns that elevate findings to the human-review queue |
| `prompts/*.md` | Stage-keyed prompt templates (consumed by `ModelProvider` callers) |
| `schemas/*.json` | JSON Schemas the connectors / enrichers validate against |
| `report_templates/*.md` | Templates for `ReportGenerator` |

Loading: `ProfileLoader().load(profile_id)` returns a
[`Profile`](../src/j1/profiles/model.py) dataclass. Activities and
connectors that need profile-specific behaviour (graph builder,
report generator) accept a `Profile` and read from it.

A second profile is one new directory + zero code. The framework's
own tests use `default`.

---

## 18. Local development

### Prerequisites

- Python 3.11+
- Optional: a Temporal server (only when running workers; not needed
  for unit tests)

### Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Environment

```bash
export J1_DATA_ROOT=/tmp/j1-dev          # absolute path required
# Temporal — only when running a worker:
export J1_TEMPORAL_TARGET=localhost:7233
```

Full env-var reference is in
[`src/j1/config/settings.py`](../src/j1/config/settings.py),
[`src/j1/orchestration/temporal/config.py`](../src/j1/orchestration/temporal/config.py),
and the per-area docs (`security.md`, `webhooks.md`,
`event-integration.md`).

### Run a smoke test

```python
from pathlib import Path
import tempfile
from j1 import (
    DocumentIntakeService, JsonArtifactRegistry, JsonSourceRegistry,
    JsonlAuditSink, ProjectContext, Settings, SqliteSearchIndexer,
    WorkspaceResolver,
)

with tempfile.TemporaryDirectory() as tmp:
    settings = Settings(data_root=Path(tmp).resolve())
    workspace = WorkspaceResolver(settings)
    ctx = ProjectContext(tenant_id="dev", project_id="smoke")
    workspace.ensure(ctx)

    sources = JsonSourceRegistry(workspace)
    artifacts = JsonArtifactRegistry(workspace)
    audit = JsonlAuditSink(workspace)
    intake = DocumentIntakeService(workspace, sources, audit)

    src = Path(tmp) / "test.txt"
    src.write_bytes(b"hello j1")
    record = intake.register_from_path(ctx, src)
    print("registered:", record.document_id)

    indexer = SqliteSearchIndexer(workspace, artifacts, sources)
    print("search hits:", indexer.search(ctx, "hello"))   # [] (no artifacts yet)
```

### Run a Temporal worker

```python
import asyncio
from j1 import (
    DocumentProcessingWorkflow, ProjectProcessingWorkflow,
    WorkerSpec, build_client, build_worker, load_temporal_settings,
    run_worker,
)

async def main():
    temporal = load_temporal_settings()
    client = await build_client(temporal)
    spec = WorkerSpec(
        workflows=[ProjectProcessingWorkflow, DocumentProcessingWorkflow],
        activities=[*processing_activities.all_activities(), ...],
    )
    await run_worker(client, temporal, spec)

asyncio.run(main())
```

### Stand up the REST surface

See [README.md](../README.md) → "Make it talk to the outside world"
and [docs/rest-api.md](rest-api.md) for a complete `create_rest_api`
recipe.

### Lint / type-check

Not configured today (`.gitignore` references `.mypy_cache/` and
`.ruff_cache/` for future use). When you wire them in, add to
`pyproject.toml`:

```toml
[tool.ruff]
src = ["src", "tests"]

[tool.mypy]
files = ["src"]
strict = true
```

---

## 19. Testing strategy

### Composition

```
tests/
├── conftest.py                   Shared fixtures: workspace, ctx,
│                                  registries, recorders, services,
│                                  activity classes
├── test_<core-module>.py         One per src/j1/<package>
├── test_orchestration_*.py       Workflow + activity tests
├── test_rest_*.py                REST adapter (auth, security,
│                                  events, SSE, bulk, base routes)
├── test_security.py              Security primitives
├── test_events.py                ApplicationEvent + bus + cloudevents
│                                  + signing + subscription + settings
├── test_event_publisher.py       Publisher abstraction (noop, memory,
│                                  bus, composite, headers, settings)
├── test_webhook_delivery.py      WebhookDeliveryService end-to-end
├── test_asyncapi.py              AsyncAPI spec ↔ publisher registry
├── test_bulk.py                  Bulk export/import primitives
├── test_external_integration_   Cross-layer consistency guard
│   consistency.py
├── test_integration_layer.py     Dependency-direction + integration
│                                  port behaviours
└── test_e2e_processing_flow.py   The single end-to-end spine
```

### Principles

1. **Hermetic.** Every test uses `tmp_path` for filesystem isolation;
   no external services, no network. The full suite runs in ~4 s.
2. **Real services where cheap.** Tests that exercise
   `ProcessingService` use the real implementation against stub
   `KnowledgeCompiler` / `EnrichmentProcessor` / `GraphBuilder`
   instances — that way the artifact materialization, content-hash
   dedup, and audit + cost recording paths are actually executed.
3. **Mocked Temporal runtime.** Workflow tests patch
   `workflow.execute_activity_method` and `workflow.wait_condition` so
   the workflow's state machine, signals, and gates run in-process
   without a Temporal server.
4. **Cross-layer consistency tests.** Five separate test files assert
   that the contracts across REST + integration + events + bulk +
   AsyncAPI stay aligned. A drift fails the build.
5. **Dependency-direction guards.** Two AST-walking tests assert that
   no core module imports from `j1.integration.*` or `j1.adapters.*`,
   and that `j1.integration` itself never imports from
   `j1.adapters.*`.

### Run

```bash
.venv/bin/pytest                    # full suite (hermetic, runs in seconds)
.venv/bin/pytest tests/test_e2e_processing_flow.py -v
.venv/bin/pytest tests/test_external_integration_consistency.py -v
.venv/bin/pytest --durations=10
```

### Adding a new test

1. Reuse the conftest fixtures (`workspace`, `ctx`, `registry`,
   `artifact_registry`, `audit_recorder`, `cost_recorder`, …) — don't
   re-wire them.
2. If you need more than one J1 service, use
   [`make_test_environment(tmp_path)`](../src/j1/testing.py) — builds
   a fully wired `TestEnvironment` dataclass.
3. Tests for protected REST routes go in `tests/test_rest_security.py`
   if they exercise auth/scope behaviour, or the per-area
   `tests/test_rest_<area>.py` if they exercise routing.
4. New event types must extend the publisher's channel registry
   *and* the AsyncAPI spec — the cross-layer consistency test will
   fail until both happen.
