# J1 architecture (index)

J1 is a reusable Python library for building knowledge-intelligence
systems over heterogeneous documents. It's **not** an application,
not a SaaS, and not bound to any industry. The framework exposes
pluggable processing contracts, durable Temporal workflows, an
external-integration surface (REST + SSE + webhooks + bulk), and a
domain-pack system that keeps industry-specific behaviour out of
core code.

This page is an **index** to the authoritative architecture docs.
For the ingestion pipeline specifically — the most complex
subsystem — see the dedicated docs under `docs/architecture/`.

> The ingestion pipeline was refactored across Waves 1–12. The
> authoritative description now lives in `docs/architecture/`.
> Older docs that pre-date the refactor are marked clearly as
> legacy / compatibility; their content does not describe the
> currently shipping system.

## Where to look

### Ingestion pipeline (authoritative)

| Topic | Doc |
|---|---|
| End-to-end stage walkthrough | [architecture/ingestion-pipeline.md](architecture/ingestion-pipeline.md) |
| Domain profile contracts (`DomainPack`, `DomainPromptPack`, policies) | [architecture/domain-profiles.md](architecture/domain-profiles.md) |
| Post-compile enrichment overlay (modules, runner, limiter) | [architecture/enrichment-overlay.md](architecture/enrichment-overlay.md) |
| `final_ingestion_report` artifact + final-status vocabulary | [architecture/final-ingestion-report.md](architecture/final-ingestion-report.md) |
| Add a new domain profile | [guides/adding-a-domain-profile.md](guides/adding-a-domain-profile.md) |
| Add a new enrichment module | [guides/adding-an-enrichment-module.md](guides/adding-an-enrichment-module.md) |
| Production worker wiring runbook | [operations/production-worker-wiring.md](operations/production-worker-wiring.md) |
| Artifact reference + endpoint contract | [reference/artifacts.md](reference/artifacts.md) |
| UI / operator copy guide | [reference/ui-copy.md](reference/ui-copy.md) |
| Known technical debt | [tech-debt.md](tech-debt.md) |

### Framework cross-cutting concerns

| Topic | Doc |
|---|---|
| Configuration / `J1_*` env-var reference | [configuration/environment.md](configuration/environment.md) |
| REST + OpenAPI + SSE surface | [rest-api.md](rest-api.md) |
| Webhooks (HMAC, CloudEvents) | [webhooks.md](webhooks.md) |
| Event integration / AsyncAPI | [event-integration.md](event-integration.md) |
| External-integration surface map | [external-integration-architecture.md](external-integration-architecture.md) |
| Bulk import/export | [bulk.md](bulk.md) |
| Security primitives | [security.md](security.md) |
| Temporal worker setup / signals | [operations/temporal.md](operations/temporal.md) |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) |
| Provider / LLM-role registry | [providers.md](providers.md) |

### Extension model (adding adapters / providers)

| Topic | Doc |
|---|---|
| 5-layer model + 12 contracts | [extension/overview.md](extension/overview.md) |
| Contracts + canonical primitives | [extension/contracts.md](extension/contracts.md) |
| Manifest + capability registry | [extension/manifest-and-registry.md](extension/manifest-and-registry.md) |
| Conformance test harnesses | [extension/conformance-tests.md](extension/conformance-tests.md) |
| Add a provider | [extension/add-a-provider.md](extension/add-a-provider.md) |
| Domain module isolation rules | [extension/domain-module-isolation.md](extension/domain-module-isolation.md) |

### Development

| Topic | Doc |
|---|---|
| Onboarding | [development/onboarding.md](development/onboarding.md) |

## Core principles

These are the seven rules every contributor + reviewer applies.
They're enforced by tests where possible; everywhere else they're a
review checklist.

| # | Principle | Enforcement |
|---|---|---|
| 1 | **Domain-neutral core.** No industry vocabulary in `src/j1/` outside `domains/`. | Naming sweep + review |
| 2 | **Outward dependency direction.** Outer layers (adapters, integration) depend on inner (services, core); never the reverse. | [`tests/test_integration_layer.py`](../tests/test_integration_layer.py), [`tests/test_external_integration_consistency.py`](../tests/test_external_integration_consistency.py) |
| 3 | **Workflows coordinate, activities act.** Workflow code stays deterministic; activities own all I/O. | Temporal SDK constraints |
| 4 | **Workflow state stores IDs and metadata, not large content.** Large content lives on disk in the workspace; the workflow carries `document_id` / `artifact_id` only. | Code review + Temporal payload limits |
| 5 | **External tools are wrapped through connectors.** Vendor SDKs live in `j1.connectors/`. Core never imports them. | Package boundary |
| 6 | **Every stage is auditable + cost-tracked.** Compile / enrich / build / index / query emit append-only JSONL audit + cost events. | `ProcessingService` writes via injected recorders |
| 7 | **Misconfiguration silently disables a surface, never silently enables it.** Optional integrations are opt-in at construction; missing config → endpoint returns 503 + `/capabilities` reports state. | Standard adapter constructor pattern |

## Cross-cutting subsystems (brief)

The remaining sections sketch the framework's non-ingestion
subsystems. Each has its own depth-doc above; the sketches below
are an at-a-glance reference. **The authoritative description of
the ingestion pipeline + enrichment + final report lives under
`docs/architecture/`** — those four files supersede earlier
single-file descriptions.

### Workspace model

Every project gets a deterministic on-disk layout under
`{J1_DATA_ROOT}/tenants/{tenant_id}/projects/{project_id}/`:

```
raw/ Original ingested files
compiled/ Compiled artifacts (chunks, parsed manifests, compile.image, …)
enriched/ Enriched artifacts ( typed overlays + legacy enricher outputs)
graph/ Graph artifacts
search/ SQLite FTS5 database (rebuildable cache)
audit/ events.jsonl + costs.jsonl (append-only)
runtime/ documents.json, artifacts.json, review_items.json, …
```

Path resolution + traversal protection live in
[`WorkspaceResolver`](../src/j1/workspace/resolver.py). Areas are
defined as a `StrEnum`
([`WorkspaceArea`](../src/j1/workspace/layout.py)). `{J1_DATA_ROOT}`
defaults to `/data/j1` and **must be absolute**.

### Document intake

[`DocumentIntakeService`](../src/j1/intake/service.py) registers a
file into a project. Streams bytes into a temp file in `raw/`,
computes SHA-256, dedups via `SourceRegistry`, audits
`document.registered` or `document.duplicate_detected`. Identifier
rules: `tenant_id` and `project_id` must match `[A-Za-z0-9_-]+`.

### Processing contracts

[`j1.processing.contracts`](../src/j1/processing/contracts.py)
defines six protocols every backend implements:

| Protocol | Method | Returns |
|---|---|---|
| `KnowledgeCompiler` | `compile(ctx, document_id)` | `ArtifactProcessingResult` |
| `EnrichmentProcessor` | `enrich(ctx, artifact_id)` | `ArtifactProcessingResult` |
| `GraphBuilder` | `build(ctx, artifact_ids)` | `ArtifactProcessingResult` |
| `SearchIndexer` | `index(ctx, artifact_ids)` | `ProcessingResult` |
| `QueryProvider` | `query(ctx, question, *, max_results=None)` | `QueryResult` |
| `ModelProvider` | `complete(ctx, prompt, *, model=None, …)` | `ModelResponse` |

Each protocol has a `kind: str` for dispatch. The new typed-overlay
modules sit alongside the legacy `EnrichmentProcessor`
implementations — see
[`architecture/enrichment-overlay.md`](architecture/enrichment-overlay.md).

### Temporal orchestration

J1 uses Temporal as its durable-workflow substrate. The
ingestion-specific workflow ([`ProjectProcessingWorkflow`](../src/j1/orchestration/workflows/project_processing.py))
runs the five-stage pipeline (assessment → compile →
post-compile analysis → enrichment → finalize). For the stage
decomposition + final-status vocabulary see
[`architecture/ingestion-pipeline.md`](architecture/ingestion-pipeline.md)
and [`architecture/final-ingestion-report.md`](architecture/final-ingestion-report.md).

Workflow state discipline: the workflow object stores integers,
strings, lists of IDs, and small dataclasses — never document
bytes, raw extracted text, embeddings, or artifact bodies.

Signals on `ProjectProcessingWorkflow`:

| Signal | Effect |
|---|---|
| `pause` / `resume` | Pause flag toggle |
| `cancel` | Graceful cancellation; finishes current activity then exits as CANCELLED |
| `approve_budget` / `reject_budget` | Resolves a budget gate |
| `approve_review` / `reject_review` | Resolves a human-review gate |
| `trigger_compile` | Releases the two-phase compile gate |

For Temporal infrastructure setup (worker boot, search-attribute
registration, signal CLI) see
[`operations/temporal.md`](operations/temporal.md).

### Cost control + audit

Cost recording is mandatory — `ProcessingService` records a cost
event for every stage that returns one, even when the amount is
zero. Aggregation is on-demand via
[`CostAggregator`](../src/j1/cost/aggregator.py).

Audit events are append-only JSONL under `audit/events.jsonl`. The
audit log is a **separate concept** from the integration-layer
event system (webhooks / queue); audit is forensic, integration
events are application-level.

### Human review

Workflow-side review gates (`GATE_AFTER_COMPILE`,
`GATE_AFTER_ENRICH`, `GATE_AFTER_GRAPH`, `GATE_AFTER_INDEX`) are
configured via `ProjectProcessingRequest.review_after`. The
workflow enters `WAITING_FOR_REVIEW` until an `approve_review` /
`reject_review` signal arrives. See
[`j1.review`](../src/j1/review/) for queue + governance helpers.

### Profile + domain-pack system

Domain-specific behaviour — taxonomies, prompts, JSON-Schema
shapes, report templates, query routing — lives in **profiles**
(legacy) and **domain packs** (current). The framework itself
ships only the bundled `default` profile + the `general` domain
pack. Domain packs are the authoritative customisation surface for
the ingestion pipeline; see
[`architecture/domain-profiles.md`](architecture/domain-profiles.md)
and the guide for [adding a domain profile](guides/adding-a-domain-profile.md).

### Hybrid query engine

[`HybridQueryEngine`](../src/j1/query/engine.py) composes five
`QueryProvider` implementations (knowledge / graph / evidence /
consistency / report) and selects via
[`QueryMode`](../src/j1/query/models.py). The intent classifier is
keyword-based; replacing either provider is a localised change.

### Search indexing

[`SqliteSearchIndexer`](../src/j1/search/indexer.py) implements
`SearchIndexer` on top of SQLite FTS5. One database file per
project under `search/index.db` — a rebuildable cache, so backup
tooling can skip it.

## Testing

Hermetic, no network, `tmp_path` for filesystem isolation. The
full backend suite runs in ~25 s. See `tests/conftest.py` for
shared fixtures and [`development/onboarding.md`](development/onboarding.md)
for the contributor workflow.

## Legacy / compatibility notes

Four docs that described removed concepts (`DefaultIngestPlanner`
/ `IngestPlan`, "split mode" / "complete mode", pre-compile
graph/index gating) were deleted entirely during the documentation
cleanup. The replacement mapping lives in
[`migration/deprecated-docs.md`](migration/deprecated-docs.md).

Two docs retain CURRENT operational content but mention legacy
concepts inside banner-stamped sections:

| Retained doc | Note |
|---|---|
| [`ingestion-operations.md`](ingestion-operations.md) | Run-lifecycle operational mechanics (resume / rebuild-index / full-reindex / delete / batch upload) — current |
| [`ingestion-progress.md`](ingestion-progress.md) | SSE / progress event surface — current; legacy `IngestPlan` references replaced by current `initial_execution_plan` + `post_compile_enrichment_plan` mentions |

### Retired concepts

These framework concepts existed in the pre-refactor pipeline and
have been removed from the currently shipping system. They appear
in legacy docs but are NOT present in current code:

- **`IngestPlanner` / `DefaultIngestPlanner` / `IngestPlan` /
 `IngestPolicy`** — the pre-compile adaptive planner. Replaced by
 the cheap pre-compile `InitialExecutionPlan` +
 post-compile `PostCompileEnrichPlan`. The new pipeline
 makes enrichment decisions AFTER compile evidence is visible,
 not before.
- **Split mode / complete mode** — the legacy RAGAnything bridge
 configuration. The current compile stage uses `process_document`
 as a single black-box call; the `split_mode` distinction is gone
 from runtime code (verified by AST tests).
- **Pre-compile graph / index gating** — the legacy planner decided
 graph + index inclusion before compile output existed. The new
 pipeline gates these via the post-compile assessor's typed
 `PostCompileEnrichPlan`, with compile evidence in hand.
- **`final_summary` as the primary aggregate** — `final_summary`
 remains as a backward-compatible artifact for older consumers;
 the **preferred** aggregate is `final_ingestion_report`.

If you find code or a doc that re-introduces any of the above,
the architecture has drifted — file an issue. Tests in
`test__vocabulary.py`, `test__pipeline_hardening.py`,
and `test__docs_and_cleanup.py` enforce that the active
surface stays clean.
