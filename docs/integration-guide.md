# J1 Integration Guide

A practical, end-to-end guide for engineering teams plugging a new
project into J1: new sources, new compilers, new retrievers, new
LLM providers, new evaluators, new domain policies. The goal is to
do all of that **without modifying the J1 core engine.**

This is the document you read when you have a project to land and
want to know where to put your code. Companion docs:

- [`docs/extension/overview.md`](extension/overview.md) — the 5-layer
 extension model + 12 contracts at a glance.
- [`docs/extension/contracts.md`](extension/contracts.md) — per-contract
 reference.
- [`docs/extension/manifest-and-registry.md`](extension/manifest-and-registry.md)
 — manifest schema + registry API.
- [`docs/extension/conformance-tests.md`](extension/conformance-tests.md)
 — test harnesses for adapters.
- [`docs/extension/add-a-provider.md`](extension/add-a-provider.md) —
 recipe for one specific provider type.
- [`docs/extension/domain-module-isolation.md`](extension/domain-module-isolation.md)
 — what stays outside core.

---

## 1. Overview

### Integration philosophy

J1 is a **stable, domain-neutral core** with a **deliberately small
extension boundary**. Everything project-specific — what data you
ingest, which LLM you call, how you rank evidence, what counts as
"good enough" — lives outside the core, behind a fixed set of
contracts.

The four rules:

1. **Stable core.** `src/j1/` (excluding `j1.profiles/` and
 `j1.extension/mocks.py`) does not change to accommodate a
 project. If your project requires a core change, that's a sign
 the change should be *generic and reusable*, not a one-off.
2. **Extensible boundary.** Project-specific behaviour is added by
 implementing one or more of the 12 contracts in
 [`j1.extension.contracts`](../src/j1/extension/contracts.py) (or
 the legacy core protocols in
 [`j1.processing.contracts`](../src/j1/processing/contracts.py)).
3. **Project-specific behaviour outside core.** Vertical taxonomies,
 custom prompts, domain ontologies, customer-named identifiers —
 all live in your own package or in a J1 *profile* directory.
4. **Wiring through contracts and registry.** The composition root
 binds your concrete adapters to the framework via the
 [`CapabilityRegistry`](../src/j1/extension/registry.py). The
 workflow code dispatches by `kind` / `role` strings; it never
 imports a concrete provider class.

If you find yourself editing `src/j1/processing/`, `src/j1/intake/`,
`src/j1/orchestration/`, `src/j1/integration/`, or `src/j1/adapters/`
to make your project work, **stop** and re-read this guide — you're
crossing a boundary.

---

## 2. Integration boundaries

| Layer | Where it lives | Owns | Does NOT own |
|---|---|---|---|
| **J1 core** | `src/j1/` (excluding `profiles/`, `extension/mocks.py`) | Pipeline shape, persistence, audit/cost, security primitives, workspace, contracts, registry | Anything project-, vendor-, or industry-specific |
| **Outer transport adapters** | `src/j1/adapters/<name>/` | REST / webhook / future MCP / future broker — wire-format ↔ port mapping | Business logic, LLM calls, persistence decisions |
| **Vendor adapters / connectors / providers** | `src/j1/providers/<vendor>/`, `src/j1/llm/<vendor>.py`, OR your own package | Implementations of one or more extension contracts; wraps a vendor SDK | Domain rules, cross-cutting orchestration |
| **Domain modules** | Your own Python package (e.g. `acme_domain`) | Vertical taxonomy, prompts, report templates, vertical enrichers, `DomainPolicy` impl, project-named identifiers | Anything reusable across deployments |
| **Profiles** | `src/j1/profiles/<name>/` (bundled `default` is empty) OR your domain package's `profiles/` | YAML / JSON / Markdown configuration: graph taxonomy, query routing, review rules, prompt templates, schemas, report templates | Python code |
| **Workflow definitions** | The composition root + `WorkerSpec` you build at startup | Which adapters are wired to which roles, what gates fire, retry policy overrides | Adapter implementations themselves |
| **Application layer** | Your own `main.py` / ASGI app / worker entrypoint | Wiring auth, building the `ApplicationFacade`, mounting `create_rest_api(...)`, starting the Temporal worker | The framework's contract definitions |
| **Infrastructure layer** | Your deployment (Docker / K8s / cloud) | Secrets resolution, network, storage, Temporal cluster, observability backends | What the application actually does with those resources |

**Rule of thumb.** Read the row that matches the file you want to
change. If your concern doesn't fit the "Owns" column for that row,
move it.

---

## 3. Supported integration types

The 12 extension contracts plus what each is for. Method signatures
are abbreviated; for the canonical Python signatures see
[`src/j1/extension/contracts.py`](../src/j1/extension/contracts.py)
and the [contracts reference doc](extension/contracts.md).

### 3.1 `SourceConnector`

| Aspect | Detail |
|---|---|
| Responsibility | Fetch document bytes + metadata from an external system (HTTP, S3, gdrive, on-prem fileshare, …). |
| Should NOT do | Persist into J1 (the framework calls `DocumentIntakeService` for that). Compile, enrich, or interpret content. Cache state outside the project workspace. |
| Inputs | `ProjectContext`, optional `query: dict` (free-form filter). |
| Outputs | `list[SourceMetadata]` from `list`; `Source(content=bytes, metadata=SourceMetadata)` from `fetch`. |
| Testing | `assert_source_connector_conformance(connector, ctx)` + per-connector tests for credential / retry / pagination behaviour. |

### 3.2 `CompilerAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Turn a registered raw document (`document_id`) into one or more `ArtifactDraft`s the framework persists into the `compiled/` workspace area. |
| Should NOT do | Build graphs, run retrieval, or call any other contract; chain implicitly into enrichment; mutate the source file. |
| Inputs | `ProjectContext`, `document_id: str`. |
| Outputs | `ArtifactProcessingResult` (`status`, `drafts`, `metadata`). |
| Testing | `assert_compiler_adapter_conformance(adapter, ctx, document_id)` + tests for empty input, vendor-missing → `ProviderUnavailable`, real vendor boundary invoked when present. |

### 3.3 `EnrichmentAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Extract structured fields from one compiled artifact (e.g. titles, dates, classifications). |
| Should NOT do | Re-fetch the source. Modify the artifact's stored content. Make decisions about whether to surface — that's `DomainPolicy.requires_review`. |
| Inputs | `ProjectContext`, `artifact_id: str`. |
| Outputs | `ArtifactProcessingResult` whose drafts have descriptive `kind` strings (e.g. `enriched.fields`). |
| Testing | `assert_enrichment_adapter_conformance(...)` + per-enricher tests with representative artifacts. |

### 3.4 `GraphAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Build a knowledge graph from a set of artifact ids. Output one `graph_json` artifact (or several shards). |
| Should NOT do | Persist into a graph database that lives outside the workspace (use a connector for that, or own the storage explicitly). |
| Inputs | `ProjectContext`, `artifact_ids: list[str]`. |
| Outputs | `ArtifactProcessingResult`. May surface `nodes` / `edges` counts in `metadata`. |
| Testing | `assert_graph_adapter_conformance(...)` + tests for empty input (must succeed without raising). |

### 3.5 `RetrievalAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Return `Evidence` items for a question. |
| Should NOT do | Generate a final answer (that's `OutputFormatter`). Apply re-ranking (that's `RerankerAdapter`). Apply redaction (that's `DomainPolicy.redact`). |
| Inputs | `ProjectContext`, `question: str`, optional `max_results`, optional `filters: dict`. |
| Outputs | `RetrievalResult(status, evidences=[Evidence(content, score, citations=[Citation(...)])])`. |
| Testing | `assert_retrieval_adapter_conformance(...)` — verifies citation typing and tolerates empty queries. |

### 3.6 `RerankerAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Re-order or filter `Evidence` items returned by a retriever. **Pure** — never mutate inputs. |
| Should NOT do | Drop citations from evidence items. Call retrieval again. |
| Inputs | `ProjectContext`, `question: str`, `evidences: list[Evidence]`, optional `max_results`. |
| Outputs | New (possibly shorter, reordered) `list[Evidence]`. |
| Testing | `assert_reranker_adapter_conformance(...)` — explicitly checks input non-mutation. |

### 3.7 `LLMProviderAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Generic single-shot text generation. Returns a plain dict with `text` (and optional `model`, `metadata`). |
| Should NOT do | Hold conversation state across calls. Stream by default (the contract is single-shot; streaming is a separate concern wired through `j1.integration.streaming`). |
| Inputs | `ProjectContext`, `prompt: str`, optional `system`, `max_tokens`, `metadata`. |
| Outputs | `dict[str, Any]` with `text: str` at minimum. |
| Testing | `assert_llm_provider_adapter_conformance(...)` + tests for empty prompt + secret-leakage scan. |

### 3.8 `EmbeddingProviderAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Embed a list of strings into equal-length float vectors. |
| Should NOT do | Re-batch differently than the caller's input order (vectors must come back in input order). |
| Inputs | `ProjectContext`, `texts: list[str]`. |
| Outputs | `list[list[float]]` of length `len(texts)`, each inner list of length `dimension`. |
| Testing | `assert_embedding_provider_adapter_conformance(...)` — checks ordering, length, empty-input. |

### 3.9 `VisionProviderAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Describe / answer about an image given bytes (and an optional prompt). |
| Should NOT do | OCR-and-then-translate as a chain; if you need that, chain in your enricher. |
| Inputs | `ProjectContext`, `image_bytes: bytes`, optional `prompt`, `metadata`. |
| Outputs | `dict[str, Any]` with `text: str` at minimum. |
| Testing | `assert_vision_provider_adapter_conformance(...)`. |

### 3.10 `OutputFormatter`

| Aspect | Detail |
|---|---|
| Responsibility | Render a final structured output from `(question, evidences, citations)`. |
| Should NOT do | Run additional retrieval. Modify evidence (use `RerankerAdapter` or `DomainPolicy.redact` upstream). Call an LLM unless the formatter type explicitly needs one. |
| Inputs | `ProjectContext`, `question: str`, `evidences: list[Evidence]`, optional `citations: list[Citation]`, `metadata`. |
| Outputs | `dict[str, Any]` — schema is the formatter's choice (one shape per consumer). |
| Testing | `assert_output_formatter_conformance(...)` + tests for the formatter's specific output schema. |

### 3.11 `EvaluationAdapter`

| Aspect | Detail |
|---|---|
| Responsibility | Score / validate a `RetrievalResult` (or final formatted output). Used for workflow gating, offline evals, continuous quality monitoring. |
| Should NOT do | Mutate inputs. Be non-deterministic without flagging it in `metadata`. |
| Inputs | `ProjectContext`, `question: str`, `evidences: list[Evidence]`, optional `expected: dict`, `metadata`. |
| Outputs | `EvaluationResult(status, score, passed, findings, metadata)`. |
| Testing | `assert_evaluation_adapter_conformance(...)` — explicitly checks **determinism** (same input → same output). |

### 3.12 `DomainPolicy`

| Aspect | Detail |
|---|---|
| Responsibility | Pluggable, side-effect-free decision hooks: indexing filter, review escalation, output redaction. The single seam where domain rules influence framework behaviour. |
| Should NOT do | Modify core data structures. Hold state across calls. Talk to external systems. |
| Inputs | Various — see signatures below. |
| Outputs | `bool` (decisions) or `list[Evidence]` (redaction). |
| Testing | Custom unit tests per policy decision. No `assert_*_conformance` harness ships for `DomainPolicy` because the decision logic is intrinsically domain-specific; the *shape* is checked via the runtime-checkable Protocol. |

```python
class DomainPolicy(Protocol):
 kind: str
 def should_index(self, ctx, artifact_id, metadata=None) -> bool:...
 def requires_review(self, ctx, target_kind, target_id, metadata=None) -> bool:...
 def redact(self, ctx, evidences: list[Evidence]) -> list[Evidence]:...
```

---

## 4. Standard integration flow

Step-by-step, in the order you usually do them. Each step is a
gate: don't move on until the previous one is real (not a stub
spec).

### Step 1 — Define the use case

Write three sentences that answer:

- **What question** does the system answer for the user?
- **From what sources** must the answer be derived?
- **What's the success criterion** (a single number — recall@k,
 pass-rate, manual approval, …)?

Without these, every later step has a free parameter.

### Step 2 — Identify sources

For each external data source, list:

- Authentication mechanism (API key, OAuth, mTLS, IAM role).
- Listing model (full crawl vs incremental cursor vs webhook push).
- Rate limits and quota.
- Document content types (PDF / DOCX / HTML / JSON / image / mixed).
- Whether the source is mutable (can a document change after first
 fetch? if yes, your connector must surface that via
 `SourceMetadata.checksum`).

### Step 3 — Create a `SourceConnector`

Implement `j1.extension.contracts.SourceConnector` for each
discrete source. One source per connector keeps testing tractable.
Examples of outer-layer integration:

- `acme_domain.connectors.HttpConnector` — generic HTTP / REST source.
- `acme_domain.connectors.S3Connector` — bucket-scoped fetch with
 ETag-as-checksum.

> **Example (outer-layer integration).** A connector for an internal
> document repository would live in
> `acme_domain/connectors/internal_repo.py`, implement
> `SourceConnector`, and ship its own `AdapterManifest`. The J1
> core does not change.

### Step 4 — Map data into J1 primitives

Map your source's records into the canonical primitives:

| Your record | J1 primitive |
|---|---|
| One file / blob | `Source(content=bytes, metadata=SourceMetadata(uri, content_type, checksum, …))` |
| One semantic record (after compilation) | `ArtifactDraft(kind="compiled.text", content=bytes, …)` |
| One sub-record / paragraph (after chunking) | `Chunk(chunk_id, document_id, content, position, …)` |
| One retrievable unit | `Evidence(content, score, citations=[Citation(document_id, locator)])` |

Anything that doesn't fit cleanly belongs in the `metadata` dict on
the relevant primitive — never as a new field added to the canonical
type.

### Step 5 — Select a compiler

Choose between:

- An **existing bundled compiler**: e.g. `RAGAnythingCompiler` for
 the default RAG pipeline (see [`docs/providers.md`](providers.md) §2).
- A **vendor-supplied compiler** wrapped behind your own
 `CompilerAdapter`.
- A **bespoke compiler** in your own package implementing the
 Protocol from scratch.

Selection at runtime is via `J1_DEFAULT_COMPILER=<kind>` or the
`compilers={kind: instance}` map you pass to `ProcessingActivities`
when building the worker.

### Step 6 — Configure enrichment

Decide whether enrichment runs at all (`J1_ENRICH_ENABLED=true|false`)
and which modalities are on (`J1_ENRICH_IMAGES`, `J1_ENRICH_TABLES`,
`J1_ENRICH_DIAGRAMS`, `J1_ENRICH_SCANNED_PAGES`). Any
modality requiring vision needs the vision LLM role configured —
see [`docs/configuration/environment.md`](configuration/environment.md) §6.

If you have domain-specific enrichers (e.g. an extractor for a
specific structured form), implement `EnrichmentAdapter` (or the
legacy `EnrichmentProcessor`) and register it under a unique `kind`.

### Step 7 — Configure retrieval

Pick a retrieval strategy:

- **Default RAG** via the bundled `RAGAnythingQueryProvider` — needs
 text + embedding LLM roles.
- **Hybrid retrieval** by composing multiple `RetrievalAdapter`s in
 your own driver and registering them under different roles
 (`primary-retrieve`, `fallback-retrieve`).
- **Custom retrieval** behind your own `RetrievalAdapter`
 implementation.

Optionally chain a `RerankerAdapter` for score smoothing /
filtering. Order is: retrieve → optional rerank → format.

### Step 8 — Add a domain policy

If your project needs ANY of:

- Filtering which artifacts get indexed.
- Escalating specific findings to human review.
- Redacting / masking content before output formatting.

… implement `DomainPolicy` in your domain module and register it.
Without a `DomainPolicy`, the framework defaults are: index
everything, no automatic escalation, no redaction.

### Step 9 — Add an output formatter

Pick the output schema your consumers expect (chat shape, API
contract, CSV row, …) and implement `OutputFormatter`. The same
underlying `(question, evidences, citations)` triple can drive
multiple formatters in parallel — register one per consumer
under different `kind`s.

### Step 10 — Add an evaluator

If quality gates are part of the workflow (auto-pass vs send-for-review,
auto-promote vs hold), implement `EvaluationAdapter`. If you only
need offline batch evaluation, the same adapter can be invoked from
your own evaluation harness without being on the critical path.

### Step 11 — Register capabilities

For each adapter, define an `AdapterManifest` and register it with
the [`CapabilityRegistry`](../src/j1/extension/registry.py) at
composition time:

```python
from j1.extension import CapabilityRegistry

registry = CapabilityRegistry
registry.register(MyConnector.MANIFEST, MyConnector(...), role="primary-source")
registry.register(MyCompiler.MANIFEST, MyCompiler(...), role="primary-compile")
registry.register(MyRetrieval.MANIFEST, MyRetrieval(...), role="primary-retrieve")
registry.register(MyOutputFormatter.MANIFEST, MyOutputFormatter,
 role="primary-format")
```

Use **roles** to wire workflow steps without coupling to a specific
adapter name. Use **capabilities** in the manifest to advertise
optional features your driver can branch on.

### Step 12 — Configure the workflow

J1's bundled Temporal workflows already dispatch by `kind` (see
§8 below). For a typical project you wire:

- Processor maps (`compilers={…}`, `enrichers={…}`,
 `graph_builders={…}`, `indexers={…}`, `query_providers={…}`)
 passed into `ProcessingActivities` / `KnowledgeProcessingActivities`
 in your worker entrypoint.

#### API ↔ worker capability alignment

Pass a [`ProcessingCapabilities`](../src/j1/integration/dto.py)
to `create_rest_api(processing_capabilities=...)` so the API can:

- **Default** an omitted `compilerKind` request field to the
 bootstrap's `J1_DEFAULT_COMPILER` selection.
- **Reject** unknown `compilerKind` / `graphBuilderKind` /
 `enricherKind` / `indexerKind` values at the API boundary
 (`400 INVALID_ARGUMENT`) instead of letting them surface as a
 workflow `UnknownProcessorError` seconds later.

The typical wiring uses `capabilities_from_bootstrap(boot)`:

```python
from j1 import bootstrap_from_env, capabilities_from_bootstrap, create_rest_api
from j1.search.indexer import SqliteSearchIndexer

boot = bootstrap_from_env
capabilities = capabilities_from_bootstrap(
 boot,
 indexer_kinds=frozenset({SqliteSearchIndexer.kind}),
 enricher_kinds=frozenset({"my-enricher"}), # whatever your worker wires
)
app = create_rest_api(facade, processing_capabilities=capabilities,...)
```

Without this wiring the API stays backwards-compatible: clients
MUST send `compilerKind` and any value is forwarded to the
workflow without validation.
- The `J1_DEFAULT_COMPILER` / `J1_DEFAULT_GRAPH_PROVIDER` /
 `J1_DEFAULT_RETRIEVAL_PROVIDER` env vars to select which
 registered processor is the default.

For richer workflows (custom orchestration over the extension
contracts), build your own driver — see
[`tests/extension/test_e2e_mock_workflow.py`](../tests/extension/test_e2e_mock_workflow.py)
for an end-to-end example that drives every contract through the
registry without Temporal.

### Step 13 — Run tests

Before every PR:

- **Unit tests** for each adapter you wrote.
- **Conformance tests** using the harnesses in
 [`j1.extension.conformance`](../src/j1/extension/conformance.py).
 (See §9 below + [`docs/extension/conformance-tests.md`](extension/conformance-tests.md).)
- **End-to-end integration test** against a small real-world fixture
 set if your deployment can support it.

The full J1 suite must remain green: `.venv/bin/pytest`.

### Step 14 — Deploy

Production checklist:

- All required `J1_*` env vars set
 (see [`docs/configuration/environment.md`](configuration/environment.md)).
- Secrets resolved through your secret manager — not committed to
 any file.
- Workspace volume (`J1_DATA_ROOT`) on durable storage.
- Temporal cluster sized for your workflow concurrency
 (see [`docs/operations/temporal.md`](operations/temporal.md)).
- Authentication wired (`authenticator=` on `create_rest_api(...)`)
 unless you intentionally run anonymously
 (see [`docs/security.md`](security.md)).
- Capability snapshot logged at startup
 (`registry.snapshot` or `/capabilities` endpoint).

---

## 5. Adapter manifest

Every adapter ships with an `AdapterManifest`. The manifest is the
contract a deployment commits to.

### Required fields

| Field | Type | Rules |
|---|---|---|
| `name` | `str` | Lowercase ASCII + digits + `.` `-` `_`. **Vendors MUST namespace** (e.g. `acme.compiler`, never bare `compiler`). |
| `type` | `str` | One of `KNOWN_ADAPTER_TYPES` (`source-connector`, `compiler`, `enrichment`, `graph`, `retrieval`, `reranker`, `llm`, `embedding`, `vision`, `output-formatter`, `evaluation`, `domain-policy`) or `unknown:<your-name>`. |
| `version` | `str` | `MAJOR[.MINOR[.PATCH]][-prerelease]` (e.g. `1.0.0`, `2.1.0-rc.1`). |

### Optional fields

| Field | Type | Purpose |
|---|---|---|
| `capabilities` | `tuple[str,...]` | Free-form labels the registry indexes (e.g. `streaming`, `multilingual`, `batch`). |
| `supported_input_types` | `tuple[str,...]` | Content types your adapter accepts (`text/plain`, `application/pdf`, …). |
| `output_types` | `tuple[str,...]` | Content / artifact-kind types your adapter produces. |
| `required_config_keys` | `tuple[str,...]` | Names of plaintext config the adapter expects. |
| `optional_config_keys` | `tuple[str,...]` | Names of optional config. MUST NOT overlap `required_config_keys`. |
| `required_secret_keys` | `tuple[str,...]` | **Names** (never values) of secrets the adapter expects. The deployment resolves them. |
| `health_check` | `bool` | `True` if the adapter implements a `health_check(ctx) -> dict` method the orchestrator can call. |
| `description` | `str` | Human-readable summary. |
| `metadata` | `dict[str, Any]` | Free-form bag for vendor extensions. **Must not contain secret values** — the manifest validator rejects obvious secret prefixes. |

### Timeout / retry expectations

The manifest does NOT carry timeouts or retry budgets — those are
per-deployment / per-workflow concerns set at the orchestration
layer (Temporal activity timeouts, HTTP-client timeouts, etc.).
Document your adapter's expected behaviour in `description` and in
the per-adapter README:

- "10s default; respect `client_timeout` if injected"
- "retries safe / idempotent / not-idempotent"
- "expects `connection_pool` to be reused across calls"

If your adapter is expected to honour a deployment-supplied
`timeout: float` or `max_attempts: int` parameter, document them as
`optional_config_keys`.

### Generic YAML example

```yaml
# Example only — outer-layer integration. Replace `acme.*` with your
# vendor / domain namespace. Substitute concrete values for the
# capabilities / config keys your adapter actually uses.
name: acme.retrieval
type: retrieval
version: 1.0.0
capabilities:
 - hybrid
 - filtered
 - streaming
supported_input_types:
 - text/plain
 - application/json
output_types:
 - application/json
required_config_keys:
 - base_url
 - index_name
optional_config_keys:
 - max_concurrency
 - request_timeout_seconds
required_secret_keys:
 - ACME_API_KEY
health_check: true
description: |
 Hybrid (vector + keyword) retrieval against an Acme index.
 Returns Evidence items with citations back to source document ids.
metadata:
 vendor: acme
 homepage: https://example.invalid/acme-retrieval
```

Loadable via `AdapterManifest.from_dict(yaml.safe_load(text))`.

---

## 6. Capability registry

The [`CapabilityRegistry`](../src/j1/extension/registry.py) is a
small in-memory index. It is populated at composition time and
read at workflow time.

### Registration

```python
from j1.extension import CapabilityRegistry

registry = CapabilityRegistry
registry.register(adapter.MANIFEST, adapter, role="primary-retrieve")
```

- `(type, name)` is the primary key. Duplicate registration raises
 `RegistryError`.
- `role` is optional; when set, the entry is indexed under the role
 for role-based lookup.
- The registry refuses adapters whose `kind` attribute disagrees
 with their manifest's `name` — this catches the common
 copy-paste-and-forget-to-rename mistake.

### Lookup

```python
# Exact:
entry = registry.require("retrieval", "acme.retrieval")
adapter = entry.adapter

# By role (workflow steps use this):
adapter = registry.find_by_role("primary-retrieve")[0].adapter

# By capability:
streaming_capable = registry.find_by_capability("streaming")
```

### Selecting capabilities in workflow config

A workflow should select adapters by **role** (or capability),
never by concrete class name:

```python
# Good — selection from config / env, dispatched via registry:
def run_workflow(registry, ctx, question: str):
 retriever = registry.require("retrieval",
 os.environ["J1_DEFAULT_RETRIEVAL_PROVIDER"]).adapter
 formatter = registry.find_by_role("primary-format")[0].adapter
 evidences = retriever.retrieve(ctx, question).evidences
 return formatter.format(ctx, question, evidences)

# Bad — concrete-class import in workflow code:
from acme_domain.retrieval import AcmeRetrieval # DON'T do this in workflow code
def run_workflow(ctx, question):
 return AcmeRetrieval.retrieve(ctx, question)
```

The composition root knows what's wired. Workflow code knows what
*roles* it needs.

### Diagnostics

`registry.snapshot` returns a JSON-friendly list of every
registered entry — useful for `/capabilities` endpoints and startup
log lines.

---

## 7. Domain policy guidance

Project-specific rules belong in `DomainPolicy`. The framework
calls the policy's three hooks from neutral seams; the policy is
the only place where business rules influence behaviour.

### 7.1 Decision: choosing retrieval strategy

Your `DomainPolicy` does not call retrievers directly. Instead, it
*advises* which retrieval `role` the driver should pick — your
driver reads the advice and resolves the role from the registry.

> **Example (outer-layer integration, neutral).** A policy registers
> a method `recommended_retrieval_role(ctx, query_metadata)` that
> returns either `"primary-retrieve"` (default), `"fallback-retrieve"`
> (when the query metadata indicates a sensitive collection), or
> `"strict-retrieve"` (when explicit precision is needed). The
> driver consults the policy, then resolves the role from the
> registry. The framework workflow code is unchanged.

### 7.2 Decision: fallback behavior

If primary retrieval returns no evidence, your driver should fall
back deterministically. The policy decides whether a fallback is
allowed at all (some workloads must fail closed).

```python
# Pseudocode driver — outer-layer.
result = retriever.retrieve(ctx, question)
if not result.evidences and policy.allow_retrieval_fallback(ctx):
 fallback = registry.find_by_role("fallback-retrieve")[0].adapter
 result = fallback.retrieve(ctx, question)
```

### 7.3 Decision: selecting output format

Multiple `OutputFormatter`s registered under different roles
(`primary-format`, `legacy-format`, `audit-format`). The policy
chooses which to use for the current request, e.g. based on the
caller's `SecurityContext` or a request flag.

### 7.4 Validating evidence requirements

Before generation, the policy verifies that the evidence set meets
the project's bar:

```python
class AcmePolicy:
 def evidence_meets_threshold(self, evidences: list[Evidence]) -> bool:
 # Outer-layer rule: at least 2 evidence items, each with score >= 0.4,
 # at least one with a citation.
 if len(evidences) < 2:
 return False
 if not any(e.citations for e in evidences):
 return False
 return all(e.score >= 0.4 for e in evidences)
```

The driver consults the policy and either proceeds, falls back, or
short-circuits to "insufficient evidence" — none of this logic
touches the J1 core.

### 7.5 Deciding whether generation is allowed

Some classes of query must never trigger LLM generation (regulated
content, denied users). The policy decides; the driver enforces:

```python
if not policy.allow_generation(ctx, question):
 return formatter.format(ctx, question, evidences=[]) # echo-only
```

These hooks are the entire policy surface for a normal project. If
you find yourself wanting a hook the framework doesn't expose,
that's a signal to either:

- Add a method to your own `DomainPolicy` Protocol subclass and
 call it from your own driver (no J1 change), OR
- Open an issue against J1 to propose a *generic* hook addition to
 the contract (only if every conceivable deployment would benefit).

---

## 8. Workflow integration

J1 currently uses **Temporal** as its durable workflow substrate.
Temporal exists in the repo today; this section is descriptive,
not aspirational.

### Workflows that ship

- [`ProjectProcessingWorkflow`](../src/j1/orchestration/workflows/project_processing.py)
 — full project pipeline with budget + review gates, signals
 (`pause`, `resume`, `cancel`, `approve_budget`, `approve_review`),
 and continue-as-new.
- [`DocumentProcessingWorkflow`](../src/j1/orchestration/workflows/document_processing.py)
 — single-document path: compile → enrich → index.

Both dispatch through Protocol-typed registries on the activity
classes (`ProcessingActivities(compilers={…}, enrichers={…}, …)`).
Workflows never import a concrete provider class. This is enforced
by the static guard
[`tests/extension/test_guards.py::test_workflows_do_not_import_concrete_providers`](../tests/extension/test_guards.py).

### How activities call contracts

Bundled activities use the legacy core protocols
(`KnowledgeCompiler`, `EnrichmentProcessor`, `GraphBuilder`,
`SearchIndexer`, `QueryProvider`). The shapes match the new
extension contracts (`CompilerAdapter` ↔ `KnowledgeCompiler`,
etc.), so an adapter built against either surface works in either
place.

### Adapter-driven workflow pattern

For project-specific orchestration over the extension surface,
build your own driver:

```python
# Outer-layer driver — not in J1 core.
def my_workflow(registry, ctx, question: str) -> dict:
 retriever = registry.find_by_role("primary-retrieve")[0].adapter
 reranker = (registry.find_by_role("primary-rerank") or [None])[0]
 formatter = registry.find_by_role("primary-format")[0].adapter
 evaluator = (registry.find_by_role("primary-evaluate") or [None])[0]

 evidences = retriever.retrieve(ctx, question).evidences
 if reranker is not None:
 evidences = reranker.adapter.rerank(ctx, question, evidences)

 output = formatter.format(ctx, question, evidences)

 if evaluator is not None:
 eval_result = evaluator.adapter.evaluate(ctx, question, evidences)
 if eval_result.passed is False:
 output["warning"] = "evaluation_failed"

 return output
```

A working end-to-end example over registered mock adapters lives
in [`tests/extension/test_e2e_mock_workflow.py`](../tests/extension/test_e2e_mock_workflow.py).

### Temporal-specific notes

When you wrap your driver in a Temporal activity:

- Keep it deterministic from the *workflow's* point of view — the
 activity is where I/O happens; the workflow only orchestrates.
- All shipped J1 activities are sync (`def`, not `async def`).
 Temporal requires a `concurrent.futures.Executor` (typically
 `ThreadPoolExecutor`) when sync activities are registered; pass
 one to `run_worker(...)`. See
 [`docs/operations/temporal.md`](operations/temporal.md) §3.2.
- For long-running custom activities, periodically call
 `j1.heartbeat` so Temporal knows the worker is alive. See
 [`docs/operations/temporal.md`](operations/temporal.md) §6.

---

## 9. Testing and conformance

Every integration ships with the following test classes. Treat
this list as a checklist when reviewing a PR.

### 9.1 Unit tests

One test file per adapter, exercising:

- Successful happy path with minimal valid inputs.
- Empty / missing input handling (each adapter must tolerate empty
 input gracefully — see the per-contract rules above).
- Error normalisation — uncaught exceptions become
 `ArtifactProcessingResult(status=FAILED)` (or `RetrievalResult` /
 `EvaluationResult` failures) at the adapter wrapper, never raw
 raises into the workflow.
- `ProviderUnavailable` propagation — vendor-missing errors propagate
 unchanged so operators see actionable messages.

### 9.2 Conformance tests

Use the shared harnesses from
[`j1.extension.conformance`](../src/j1/extension/conformance.py):

```python
from j1.extension.conformance import (
 assert_compiler_adapter_conformance,
 assert_retrieval_adapter_conformance,
 assert_evaluation_adapter_conformance,
 #... one harness per contract
)

def test_my_compiler_conformance:
 assert_compiler_adapter_conformance(
 MyCompiler.from_test_config, ctx, document_id="doc-1",
 )
```

The harnesses verify: contract shape, return-type correctness,
empty-input tolerance, secret-leakage scan, determinism (for
evaluators), input non-mutation (for rerankers), and a 5s
wall-clock deadline to catch hangs.

### 9.3 Mock-adapter tests

If you build mock helpers for *your* tests, follow the rules J1's
own mocks follow:

- Deterministic — no clocks, no random, no network.
- Domain-neutral.
- Live in your test package, not in your production package.
- Implement the same Protocol the real adapter implements so you
 can swap them at composition time.

J1's bundled mocks under [`j1.extension.mocks`](../src/j1/extension/mocks.py)
are reference implementations.

### 9.4 Empty-result handling

Every adapter must accept "nothing here" inputs without raising:

| Contract | Empty input | Expected behaviour |
|---|---|---|
| `SourceConnector.list` | (always) | Returns `[]` |
| `CompilerAdapter.compile` | empty `document_id` | `ArtifactProcessingResult(status=FAILED, error=…)` |
| `GraphAdapter.build` | `[]` | `ArtifactProcessingResult(status=SUCCEEDED, drafts=[])` (or empty graph) |
| `RetrievalAdapter.retrieve` | empty `question` | `RetrievalResult(status=SUCCEEDED, evidences=[])` or `FAILED` |
| `RerankerAdapter.rerank` | `[]` | `[]` |
| `EmbeddingProviderAdapter.embed` | `[]` | `[]` |
| `OutputFormatter.format` | `evidences=[]` | A coherent dict (e.g. `{"answer": null, "reason": "no-evidence"}`) |
| `EvaluationAdapter.evaluate` | `evidences=[]` | `EvaluationResult(score=0.0, passed=False, findings=[…])` |

### 9.5 Timeout handling

The framework does not impose timeouts on individual adapter calls
(the Temporal activity layer is the appropriate timeout boundary).
Your adapter should, however:

- Honour an injected `timeout: float` parameter if the manifest
 declares one in `optional_config_keys`.
- Default to a sensible vendor timeout (10–60s typical) rather
 than blocking forever.
- Translate transport timeouts to either a `FAILED` result or a
 `ProviderUnavailable` (depending on whether the user could
 reasonably retry).

### 9.6 No secret leakage

The conformance harness scans adapter outputs for known secret
prefixes (`sk-test-`, `ghp_test_`, `xoxb-test-`, `AKIATEST`) and
fails the test if it finds them. Your adapter must:

- Never echo API keys / tokens into `metadata`, `error`, `text`,
 or any other field of any returned primitive.
- Never log secrets at INFO level. DEBUG-level secret-shaped logs
 are discouraged but not blocked.

### 9.7 Citation / evidence validation

Retrieval adapters must:

- Populate `Citation.document_id` for every evidence item that
 references a registered document.
- Populate `Citation.locator` (chunk id, page number, byte range,
 URL fragment, …) when the source supports it.
- Avoid synthetic `document_id`s that don't resolve to a registry
 entry — that breaks downstream lineage.

If your adapter cannot produce citations (e.g. a generative-only
provider), set `evidences=[]` and surface the answer through your
own driver, not through `RetrievalAdapter`.

### 9.8 End-to-end workflow test

For each project, ship at least one test that:

- Builds a `CapabilityRegistry` with all your adapters (or mocks
 for those with external dependencies).
- Drives the full workflow shape (fetch → compile → … → format →
 evaluate) through the registry.
- Asserts the final output meets the project's success criterion.

The framework's own example —
[`tests/extension/test_e2e_mock_workflow.py`](../tests/extension/test_e2e_mock_workflow.py)
— is ~100 lines and a useful template.

---

## 10. Security and governance

### 10.1 Secrets

- **Never** commit secrets to manifests, `.env`, source files, or
 test fixtures. Use `required_secret_keys` to declare what secrets
 the adapter expects, then resolve them from your secret manager
 at composition time.
- The `AdapterManifest` validator runs a best-effort secret-shape
 scan against `metadata` values and refuses obvious paste-ins.
- Use the `_FILE` env-var variants (`J1_AUTH_API_KEYS_FILE`,
 `J1_WEBHOOK_SUBSCRIPTIONS_FILE`) when the secret is a JSON blob
 mounted from a secret manager.
- For programmatic secret resolution at startup, see
 [`docs/security.md`](security.md).

### 10.2 PII redaction

If your project handles PII:

- Use a `DomainPolicy.redact(...)` implementation to mask
 fields before evidence reaches the formatter.
- Never store PII in `Citation.snippet` if downstream consumers
 aren't authorised to see it.
- If your evidence content itself is PII-bearing, serve it through
 a formatter that emits IDs only and resolves to content
 server-side under the caller's authorisation.

### 10.3 Audit logging

Every J1 stage emits append-only audit events under
`<workspace>/audit/events.jsonl`. Your adapter does NOT need to
emit additional audit events for normal operation — the framework
records `processing.compile.completed`, `processing.enrich.completed`,
etc. automatically.

If your adapter performs an action with extra-audit-worthy
semantics (a destructive operation on the source, a denied
request), surface it through the framework's `AuditRecorder`
exposed via `Bootstrap.audit` rather than writing your own log.

### 10.4 Permission context

Every external request carries a `SecurityContext` (subject,
tenant_id, scopes, auth_type). It flows from the inbound
authenticator through the integration layer into the application
services.

Your adapter can consume `SecurityContext` (when injected by your
driver) for authorisation decisions. It must not invent its own
parallel security model.

### 10.5 Source visibility

Some sources are visible only to specific tenants / users. If your
`SourceConnector` enforces source-side ACLs:

- Pass the `SecurityContext` (or a derived credential) down at
 fetch time.
- Translate source-side authorisation failures into either
 `ProviderUnavailable` (operator should re-grant) or a `Source`
 with empty content + `metadata.extra={"reason": "forbidden"}`.

### 10.6 Data retention

J1's workspace areas have explicit retention semantics
([`WorkspaceArea`](../src/j1/workspace/layout.py)):

- Durable: `raw/`, `compiled/`, `enriched/`, `graph/`, `audit/`,
 `runtime/`.
- Rebuildable cache: `search/`.

Your adapter must not write outside these areas. If you need a
new area, propose it as a generic addition; don't carve a
project-specific subfolder.

### 10.7 External LLM / provider usage

When your adapter calls an external LLM:

- Document the provider in the manifest's `description`.
- List any data leaving the deployment in your project's
 data-flow document — J1 doesn't enforce this; your governance
 process must.
- Honour the deployment's per-call cost budget — record cost
 events through the `CostRecorder` exposed via the `Bootstrap`.
- Be explicit about which calls go to a third party vs an
 on-prem model — capabilities (`hosted-third-party`,
 `on-premises`) on the manifest are a useful place to declare it.

---

## 11. Anti-patterns

The patterns below are common drift toward "let's just put it in
core". All of them are wrong, and many are caught statically by
[`tests/extension/test_guards.py`](../tests/extension/test_guards.py).

### 11.1 Core importing domain modules

```python
# Anywhere under src/j1/ (excluding profiles/) — FORBIDDEN
import acme_domain
from acme_domain.policy import AcmePolicy
```

The dependency arrow points outward. Domain modules import J1; J1
never imports a domain module. Caught by
`test_core_does_not_import_extension` and the `j1.integration`
direction guards.

### 11.2 Hardcoding domain names in core

```python
# In any core module — FORBIDDEN
DEFAULT_TENANT = "civil_engineering_co" # NO
PROFILE_NAME = "acme_legal_v2" # NO
class CivilWorkflow(Workflow): # NO
```

The bundled `default` profile is generic; everything vertical lives
in your domain module. Caught by `test_no_domain_terms_in_j1_core`.

### 11.3 Workflow code naming a concrete provider

```python
# In a workflow / activity module — FORBIDDEN
from j1.providers.raganything import RAGAnythingCompiler
def compile_activity(input):
 return RAGAnythingCompiler(...).compile(...)
```

Workflows dispatch by `kind` through Protocol-typed registries.
Caught by `test_workflows_do_not_import_concrete_providers`.

### 11.4 Vendor objects leaking past the adapter

```python
# In a CompilerAdapter return — FORBIDDEN
return ArtifactProcessingResult(
 status=ResultStatus.SUCCEEDED,
 drafts=[ArtifactDraft(
 kind="compiled.text",
 content=b"...",
 metadata={"vendor_response": vendor_response_obj}, # NO
 )],
)
```

The vendor object is an opaque type from the vendor SDK. It will
break Temporal serialisation, observability, and the "core is
vendor-free" rule. Convert to plain dict / str / int at the
adapter boundary.

### 11.5 Bypassing evidence and citation models

```python
# In a custom retrieval driver — FORBIDDEN
def my_retrieve(...):
 return {"answer": "...", "raw_blob":...} # NO — bypasses Evidence/Citation
```

Every retrieval result must come back as `RetrievalResult` with
typed `Evidence` items carrying typed `Citation`s. Downstream
formatters, evaluators, audit, and lineage all depend on it.

### 11.6 Project-specific folders inside core

```
src/j1/
├── extension/
├── processing/
└── acme_special/ ← FORBIDDEN (project-specific folder in core)
```

Your project's code lives in your own package. Profiles configure
domain behaviour without code; everything else is an adapter or a
domain module.

### 11.7 Domain policy modifying core through conditionals

```python
# In core — FORBIDDEN
if ctx.tenant_id == "acme":
 do_something_special # NO — domain logic in core
```

Branching on tenant id, project id, or any project-shaped string in
core code is a domain-leakage smell. Push the decision into a
`DomainPolicy.<method>` and have the relevant seam consult it.

### 11.8 Mock adapters living outside `j1.extension.mocks`

```python
# In src/j1/processing/something.py — FORBIDDEN
class MockCompilerAdapter(CompilerAdapter): # NO...
```

Mocks belong in [`j1.extension.mocks`](../src/j1/extension/mocks.py)
or in your own `tests/`. Caught by
`test_mocks_only_in_extension_mocks_module`.

### 11.9 Adding fields to canonical primitives

```python
# Modifying j1.extension.primitives.Evidence — FORBIDDEN
@dataclass(frozen=True)
class Evidence:
 content: str
 score: float
 citations: list[Citation]
 metadata: dict
 acme_priority: int # NO — add to metadata instead
```

Canonical primitives are stable contract surface. Project-specific
fields belong in `metadata`. If a field is *generic* enough to
warrant first-class treatment, propose it as a contract change.

### 11.10 Re-exporting extension contracts from `j1.__init__`

The names `CompilerAdapter` / `GraphAdapter` are also used (with a
different shape) in `j1.connectors.*`. Re-exporting the extension
contracts from `j1.__init__` would create an import-time clash.
Always use `from j1.extension import …` or `from j1.extension.contracts
import …`.

---

## 12. Integration checklist

Use this checklist before opening a PR for a new adapter, connector,
domain module, or workflow extension.

### Code

- [ ] My adapter implements the relevant Protocol from
 [`j1.extension.contracts`](../src/j1/extension/contracts.py)
 (or the legacy core protocol where the bundled workflows
 need it).
- [ ] My adapter has a `kind: str` matching the manifest's `name`.
- [ ] My adapter ships an `AdapterManifest` (typically as a
 `MANIFEST` class attribute).
- [ ] My adapter takes a typed settings object in its constructor —
 not a raw env mapping.
- [ ] My adapter lazy-imports any vendor SDK and raises
 `ProviderUnavailable` with a pip-install hint when missing.
- [ ] My adapter's exceptions are translated at the boundary into
 `ProviderUnavailable` (infra) or a `*Result(status=FAILED, …)`
 (per-call) — never raw raises into the workflow.
- [ ] My adapter does not return vendor-specific objects in any
 canonical primitive.
- [ ] My adapter is registered in the `CapabilityRegistry` at
 composition time (not from inside core).

### Domain

- [ ] No industry vocabulary, customer name, or "phase"-style
 label appears in any file under `src/j1/` (other than
 `src/j1/profiles/`).
- [ ] My domain rules live in a `DomainPolicy` implementation in
 my own package.
- [ ] My profile (taxonomy, prompts, schemas, report templates)
 lives in my own package or under
 `<my-domain>/profiles/<name>/`.

### Tests

- [ ] Each adapter has unit tests covering happy path, empty
 input, exception normalisation, `ProviderUnavailable`
 propagation.
- [ ] Each adapter has a conformance test calling the matching
 `assert_*_conformance(...)` harness from
 [`j1.extension.conformance`](../src/j1/extension/conformance.py).
- [ ] At least one end-to-end test drives the workflow over my
 registered adapters (using mocks where the real adapter has
 external dependencies).
- [ ] No secret-shaped strings appear in any test fixture.
- [ ] Determinism: re-running the same test produces the same
 result.
- [ ] Full J1 suite passes: `.venv/bin/pytest`.

### Configuration

- [ ] Every new env var I added is documented in
 [`docs/configuration/environment.md`](configuration/environment.md)
 AND added to [`.env.example`](../.env.example).
- [ ] No secrets are committed to any file.
- [ ] My adapter works with the framework's anonymous-mode default
 OR I've documented the required authenticator wiring.

### Workflow

- [ ] My workflow / driver dispatches via the registry — no
 concrete provider class is named in workflow code.
- [ ] Selection between adapters happens by `role` or `kind`
 string (typically from env), not by hardcoded import.
- [ ] If I added new Temporal activities, they are sync-friendly
 and registered under the same task queue as the rest of the
 worker.

### Documentation

- [ ] If I added a new contract / surface, I've linked from
 [`docs/extension/overview.md`](extension/overview.md) and
 this guide.
- [ ] If I added a new adapter type, I've added a row to the
 tables in §3 here and in
 [`docs/extension/contracts.md`](extension/contracts.md).
- [ ] If I added a new env var, I've added it to
 [`docs/configuration/environment.md`](configuration/environment.md).

### Security

- [ ] Secrets are declared as `required_secret_keys` in the
 manifest and resolved by the deployment.
- [ ] PII handling (if applicable) goes through
 `DomainPolicy.redact(...)`.
- [ ] My adapter does not invent a parallel security model.
- [ ] My adapter does not write outside `J1_DATA_ROOT`.

---

## 13. Cross-references

- [`README.md`](../README.md) — repo identity + quickstart
- [`docs/architecture.md`](architecture.md) — full architecture
- [`docs/extension/overview.md`](extension/overview.md) — 5-layer
 extension model
- [`docs/extension/contracts.md`](extension/contracts.md) — per-contract
 reference
- [`docs/extension/manifest-and-registry.md`](extension/manifest-and-registry.md)
 — manifest schema + registry API
- [`docs/extension/conformance-tests.md`](extension/conformance-tests.md)
 — conformance harnesses
- [`docs/extension/add-a-provider.md`](extension/add-a-provider.md) —
 per-provider recipe
- [`docs/extension/domain-module-isolation.md`](extension/domain-module-isolation.md)
 — what stays outside core
- [`docs/configuration/environment.md`](configuration/environment.md)
 — every `J1_*` env var
- [`docs/operations/temporal.md`](operations/temporal.md) — Temporal
 worker operations
- [`docs/security.md`](security.md) — auth + scopes
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — PR rules + checklist
- [`tests/extension/test_e2e_mock_workflow.py`](../tests/extension/test_e2e_mock_workflow.py)
 — runnable end-to-end example over the extension surface
