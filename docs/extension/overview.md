# J1 Extension Model — Overview

How J1 is designed to **grow**: new providers, new connectors, new
domain logic land *outside* the core engine, behind a small set of
stable contracts. The core itself stays domain-neutral.

If you are about to add code, this is the page that tells you which
seam to use.

---

## 1. The five layers

```
┌─────────────────────────────────────────────────────────────────┐
│  Domain modules (your package, e.g. `acme_domain`)              │
│    Profiles, prompts, report templates, vertical enrichers,     │
│    DomainPolicy implementation                                  │
└─────────────────────────┬───────────────────────────────────────┘
                          │ depends on
┌─────────────────────────▼───────────────────────────────────────┐
│  Adapters / connectors / providers                              │
│    Implement the 12 contracts in `j1.extension.contracts`       │
│    (CompilerAdapter, RetrievalAdapter, LLMProviderAdapter, …)   │
└─────────────────────────┬───────────────────────────────────────┘
                          │ depends on
┌─────────────────────────▼───────────────────────────────────────┐
│  J1 extension surface (`j1.extension`)                          │
│    Contracts + canonical primitives + AdapterManifest +         │
│    CapabilityRegistry + conformance harness                     │
└─────────────────────────┬───────────────────────────────────────┘
                          │ depends on
┌─────────────────────────▼───────────────────────────────────────┐
│  J1 core (`j1.processing`, `j1.intake`, `j1.orchestration`, …)  │
│    Workflow shape, persistence, audit / cost, security,         │
│    workspace, registries — domain-neutral by construction       │
└─────────────────────────┬───────────────────────────────────────┘
                          │ uses
┌─────────────────────────▼───────────────────────────────────────┐
│  J1 outer transport adapters (`j1.adapters.rest`, …)            │
│    REST + SSE + webhooks; map transport requests to ports       │
└─────────────────────────────────────────────────────────────────┘
```

Dependency arrow points strictly downward. Tests
([`tests/test_integration_layer.py`](../../tests/test_integration_layer.py),
[`tests/extension/test_guards.py`](../../tests/extension/test_guards.py))
enforce the inner half of this rule statically.

---

## 2. What belongs where

| Concern | Layer | Examples |
|---|---|---|
| Generic processing pipeline orchestration | Core | `ProcessingService`, `ProjectProcessingWorkflow` |
| Workspace layout, registries, audit / cost | Core | `WorkspaceResolver`, `JsonArtifactRegistry`, `JsonlAuditSink` |
| Outer-transport adapters | Outer adapters | `j1.adapters.rest`, `j1.adapters.webhook` |
| Stable contracts every adapter implements | Extension surface | The 12 Protocols in `j1.extension.contracts` |
| Canonical primitives flowing across contracts | Extension surface | `Document`, `Artifact`, `Source`, `Evidence`, `RetrievalResult`, `EvaluationResult`, … |
| Manifest schema + registry + conformance harness | Extension surface | `AdapterManifest`, `CapabilityRegistry`, `assert_*_conformance(…)` |
| Concrete vendor / in-house compilers, graph builders, retrievers, LLM clients | Adapter / connector / provider | `RAGAnythingCompiler`, `GraphifyGraphBuilder`, `OpenAICompatTextLLMClient`, your own |
| Domain ontologies, vertical schemas, custom prompts, report templates, vertical enrichers | Domain module | Your own package; mounted via profiles |
| Domain decision hooks (indexing filters, review escalation, redaction) | Domain module | A `DomainPolicy` implementation |

---

## 3. The 12 contracts

| Contract | Purpose |
|---|---|
| `SourceConnector` | Fetch documents from external systems |
| `CompilerAdapter` | Turn raw documents into compiled artifacts |
| `EnrichmentAdapter` | Extract structured fields from compiled artifacts |
| `GraphAdapter` | Build a knowledge graph from artifacts |
| `RetrievalAdapter` | Retrieve evidence for a question |
| `RerankerAdapter` | Re-order / filter retrieved evidence |
| `LLMProviderAdapter` | Generic text-generation provider |
| `EmbeddingProviderAdapter` | Generic embedding provider |
| `VisionProviderAdapter` | Generic vision provider |
| `OutputFormatter` | Render answer + citations into a chosen output shape |
| `EvaluationAdapter` | Score / validate retrieval or final output |
| `DomainPolicy` | Pluggable, side-effect-free decision hooks (indexing / review / redaction) |

Full reference: [`docs/extension/contracts.md`](contracts.md).

---

## 4. The generic workflow shape

A workflow built on the extension surface follows this sequence;
**every step is optional** if the corresponding role isn't
registered:

```
                ┌──────────────────────────┐
                │ SourceConnector.fetch    │   (optional — many
                └────────────┬─────────────┘    deployments ingest
                             │                  via DocumentIntakeService
                             ▼                  directly instead)
                ┌──────────────────────────┐
                │ CompilerAdapter.compile  │
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ EnrichmentAdapter.enrich │   (optional)
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ GraphAdapter.build       │   (optional, configured)
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ RetrievalAdapter.retrieve│
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ RerankerAdapter.rerank   │   (optional)
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ OutputFormatter.format   │
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │ EvaluationAdapter.evaluate│   (optional)
                └────────────┬─────────────┘
                             │
                             ▼
                       structured output
```

A complete worked example over registered mock adapters lives in
[`tests/extension/test_e2e_mock_workflow.py`](../../tests/extension/test_e2e_mock_workflow.py)
— roughly 100 lines of Python that drive the entire pipeline
through the registry, with no Temporal / no real I/O.

The bundled Temporal workflows
(`ProjectProcessingWorkflow`, `DocumentProcessingWorkflow`) already
dispatch to processor *kinds* (Protocol-typed registries on the
activity classes — `ProcessingActivities(compilers={…},
enrichers={…}, …)`). They satisfy the same general shape; they are
**not** rewritten on top of the new extension layer because that
would be a churn-only change.

---

## 5. How to extend J1

| You want to … | Read |
|---|---|
| Add a new compiler / graph / retrieval / LLM provider | [`add-a-provider.md`](add-a-provider.md) |
| Add a new contract to the framework | Open an issue first; this is rare |
| Build a domain module on top of J1 | [`domain-module-isolation.md`](domain-module-isolation.md) |
| Verify your adapter against the contract | [`conformance-tests.md`](conformance-tests.md) |
| Register your adapter at composition time | [`manifest-and-registry.md`](manifest-and-registry.md) |
| Configure a workflow using your adapters | This page → § 4, plus the example in [`tests/extension/test_e2e_mock_workflow.py`](../../tests/extension/test_e2e_mock_workflow.py) |

---

## 6. Anti-patterns the layer prevents

| Anti-pattern | Why it's wrong | Caught by |
|---|---|---|
| Core importing project-specific modules | Couples the framework to a deployment | [`test_core_modules_do_not_import_external_layer`](../../tests/test_integration_layer.py) |
| Hardcoded domain names inside core (e.g. "civil", "training_phase") | Pollutes the domain-neutral contract | [`test_no_domain_terms_in_j1_core`](../../tests/extension/test_guards.py) |
| Workflow code naming a concrete provider class | Workflows must dispatch via Protocol-typed registries | [`test_workflows_do_not_import_concrete_providers`](../../tests/extension/test_guards.py) |
| Adapter leaking vendor-specific objects into core primitives | Breaks Temporal serialisation, observability, and the "core is vendor-free" rule | Code review + the conformance harness's secret-leakage check |
| `DomainPolicy` modifying core behaviour through conditionals inside core | The policy is a hook, not a code-modifier; core consults it via the registry | Code review + the dependency-direction guard |
| Mock adapters defined outside `j1.extension.mocks` | A test fixture has leaked into the core surface | [`test_mocks_only_in_extension_mocks_module`](../../tests/extension/test_guards.py) |
| Core depending on the extension layer | Inverts the dependency arrow | [`test_core_does_not_import_extension`](../../tests/extension/test_guards.py) |

---

## 7. Cross-references

- [`docs/extension/contracts.md`](contracts.md) — the 12 contracts
- [`docs/extension/manifest-and-registry.md`](manifest-and-registry.md)
- [`docs/extension/conformance-tests.md`](conformance-tests.md)
- [`docs/extension/add-a-provider.md`](add-a-provider.md)
- [`docs/extension/domain-module-isolation.md`](domain-module-isolation.md)
- [`docs/architecture.md`](../architecture.md)
- [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
