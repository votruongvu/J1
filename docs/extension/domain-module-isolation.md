# Domain Module Isolation

J1's defining principle: **the core is domain-neutral.** Industry
vocabulary, vertical-specific schemas, deployment-specific
ontologies, and customer-specific naming live *outside* the J1 core
package — in domain modules, profiles, and deployment code.

This guide tells you what belongs where, what's forbidden in core,
and the recommended package layout for domain code that builds on
top of J1.

> Throughout this document, examples use the neutral placeholder
> names `example_domain` and `acme_domain`. Substitute your real
> domain when you implement.

---

## 1. Why isolation matters

J1 must be installable and useful for any document-intelligence
workload — legal, technical, financial, scientific, creative, etc. —
without forking. If domain assumptions leak into core, every
deployment pays for them: mismatched taxonomies, ambiguous prompt
slots, irrelevant default scopes, surprising review rules.

A clean separation also keeps tests fast and reasoning local: the
core doesn't have to know what an "obligation clause", a "balance
sheet line item", or any other vertical concept is — it knows about
documents, artifacts, kinds, and graph nodes.

---

## 2. What belongs in J1 core (`src/j1/`)

| Belongs in core | Why |
|---|---|
| Protocol definitions (`KnowledgeCompiler`, `EnrichmentProcessor`, `GraphBuilder`, `SearchIndexer`, `QueryProvider`, `TextLLMClient`, …) | Generic processing contracts; no vertical concept inside |
| Generic processing service + workflow orchestration | The framework's purpose |
| Workspace layout, registries, audit + cost sinks | Deployment-agnostic infrastructure |
| Outer transport adapters (REST, webhook) | Wire-format transport, not domain logic |
| Profile *loader* (the loading machinery, not the profile content) | Loading mechanism is generic |
| Bundled `default` profile — intentionally empty | A reference shape, not vertical content |
| LLM role abstraction (text / vision / embedding) | Roles are generic; vendor-specific clients are providers |
| Bundled vendor adapters (RAGAnything, Graphify) | Optional; their *contents* live in `j1.providers.*`, isolated from `j1.processing.*` and friends |

**Rule of thumb.** If renaming a string would make the framework
useless for a different industry, that string is domain-specific and
doesn't belong in core.

---

## 3. What belongs OUTSIDE J1 core

| Belongs outside core | Where it goes |
|---|---|
| Industry-specific node / edge / relationship taxonomies | Domain module's profile (`profiles/<your-profile>/graph_taxonomy.yaml`) |
| Vertical-specific JSON schemas | Domain module's profile (`profiles/<your-profile>/schemas/*.json`) |
| Custom prompt templates that mention vertical concepts | Domain module's profile (`profiles/<your-profile>/prompts/*.md`) |
| Custom report templates with vertical structure | Domain module's profile (`profiles/<your-profile>/report_templates/*.md`) |
| Domain-tuned enrichers (e.g. `ClauseExtractor` for legal) | Domain module's Python package (`example_domain/enrichers.py`) |
| Vendor-specific LLM provider implementations | `src/j1/llm/<vendor>.py` (in core) OR your own package (uses `register_trusted_prefix`) |
| Custom scope strings beyond `kb:*` | Domain module — but verify carefully; `kb:*` is broad enough to cover most use cases |
| Custom REST endpoints for vertical operations | Your own ASGI app that mounts both `create_rest_api(...)` and your domain routes |
| Customer-specific authenticator / verifier callable | Deployment glue (not committed to either J1 or your domain module) |
| Industry-specific event-type names | Domain module's event publisher delegate |

---

## 4. Suggested package layout for a domain module

A domain module is a Python package that *uses* J1, not a fork.

```
example_domain/                          # your package
├── pyproject.toml                       # depends on j1[raganything], etc.
├── src/example_domain/
│   ├── __init__.py
│   ├── enrichers.py                     # custom EnrichmentProcessor implementations
│   ├── providers/                       # custom compiler / graph / retrieval adapters
│   │   └── special_compiler.py
│   ├── llm/                             # custom LLM clients (if needed)
│   │   └── special_client.py
│   ├── prompts/                         # source of truth for prompt content
│   │   └── extract.md
│   ├── profiles/
│   │   └── example/                     # one profile directory per workload
│   │       ├── profile.yaml
│   │       ├── graph_taxonomy.yaml
│   │       ├── query_routing.yaml
│   │       ├── review_rules.yaml
│   │       ├── prompts/                 # uses files copied from ../../../prompts/
│   │       ├── schemas/
│   │       └── report_templates/
│   ├── workflows.py                     # any custom workflows (if you extend Temporal)
│   ├── compose.py                       # Bootstrap helper that wires your providers in
│   └── tests/
└── README.md
```

**Why a separate package, not a folder under `src/j1/`.** A separate
package means the J1 core can be upgraded independently of the
domain module, the domain module can be reused across multiple
deployments, and the test boundary is clear (the domain module's
tests can use real J1 fixtures without polluting J1's own test
suite).

---

## 5. Dependency direction

The dependency arrow points strictly **outward from J1 core**:

```
   ┌───────────────────────────────────────────┐
   │  Deployment glue (uvicorn, secrets, …)    │   may import everything below
   └────────────────────┬──────────────────────┘
                        │
   ┌────────────────────▼──────────────────────┐
   │  Domain module (example_domain)           │   may import j1 + your providers
   └────────────────────┬──────────────────────┘
                        │
   ┌────────────────────▼──────────────────────┐
   │  J1 outer adapters (j1.adapters.*)        │   may import j1.integration + j1.<core>
   └────────────────────┬──────────────────────┘
                        │
   ┌────────────────────▼──────────────────────┐
   │  J1 integration boundary (j1.integration) │   may import j1.<core>
   └────────────────────┬──────────────────────┘
                        │
   ┌────────────────────▼──────────────────────┐
   │  J1 core (j1.processing, j1.intake, …)    │   may import nothing above
   └───────────────────────────────────────────┘
```

J1 core does NOT import from your domain module. Tests in
[`tests/test_integration_layer.py`](../../tests/test_integration_layer.py)
and [`tests/test_external_integration_consistency.py`](../../tests/test_external_integration_consistency.py)
enforce the inner half of this rule statically; the outer halves are
your domain module's responsibility.

---

## 6. Profile / prompt / report-template isolation

Profiles are the highest-leverage isolation tool: a profile is a
directory of YAML / JSON / Markdown files loaded by `ProfileLoader`,
and the framework reads from it without knowing what's inside.

A domain module typically owns:

- **Graph taxonomy** — node types, edge types, validation rules
- **Query routing** — keyword → mode hints for the intent classifier
- **Review rules** — patterns that escalate findings to human review
- **Prompts** — stage-keyed prompt templates consumed by your
  enrichers / model-provider callers
- **Schemas** — JSON Schemas the connectors / enrichers validate
  against
- **Report templates** — template files for the `ReportGenerator`

To register your profile location:

```python
from j1 import ProfileLoader

loader = ProfileLoader(search_paths=[
    Path("/etc/example_domain/profiles"),
    Path(__file__).parent / "profiles",
])
profile = loader.load("example")
```

The framework's bundled `default` profile is intentionally empty.
Your domain profile is the source of vertical-specific configuration
— and it never lives in `src/j1/profiles/`.

---

## 7. Testing expectations

Your domain module's tests should:

- Use J1's hermetic fixtures (`tmp_path`, `make_test_environment`,
  per-domain `ProjectContext`s).
- Test the domain module's enrichers + providers in isolation, plus
  one end-to-end flow that exercises the J1 pipeline with your
  configuration.
- Never mutate `src/j1/`. If a test needs to modify J1 behaviour,
  it's a sign that the seam is missing — open an issue against J1
  to add the seam, don't patch the import path.

J1 itself enforces:

- `tests/test_integration_layer.py::test_core_modules_do_not_import_external_layer` — fails if any core module imports `j1.integration.*` or `j1.adapters.*`.
- `tests/test_external_integration_consistency.py::test_no_outer_layer_imports_in_core_subpackages` — second copy of the rule with an explicit allowlist.

These guards apply to every PR landing in J1; mirror them in your
domain module's CI if you have one.

---

## 8. Naming rules

| Rule | Reason |
|---|---|
| **No industry vocabulary in `src/j1/`** (no `civil`, no `legal`, no `clinical`, no `<industry>_<thing>`, no customer names) | A new deployment for a different industry must not need a fork. |
| **No phase-based names** (`phase1`, `phrase`, `step3`, `intro`, `final` — when used as core concepts) | "Phase" implies a workflow shape that may not match a deployment's reality. |
| **No vendor names in core paths** (no `j1.openai`, no `j1.langchain`, no `j1.raganything` outside `j1.providers/`) | Vendors belong behind providers; promoting them to core paths leaks the choice. |
| **Use `kind` strings, not Python types, for dispatch** | Allows pluggability via configuration; documented in core. |
| **`kb:*` is the canonical scope namespace for the J1 surface** | Used by the security layer; domain modules can introduce their own scopes for their own routes. |
| **Tenant + project IDs match `[A-Za-z0-9_-]+`** | Enforced by `validate_identifier`; domain modules should use the same shape for any of their own identifiers that flow through J1. |

---

## 9. Allowed patterns

These are encouraged ways to extend J1 without crossing the
isolation line:

- **Add a provider** under `j1.providers.<name>/` (or in your own
  package — see [`add-a-provider.md`](add-a-provider.md)).
- **Add an enricher** by implementing `EnrichmentProcessor` in your
  domain module and registering it in your worker's processor map.
- **Add a `DomainPolicy`** (extension surface) — implement
  [`j1.extension.contracts.DomainPolicy`](../../src/j1/extension/contracts.py)
  and register it in the
  [`CapabilityRegistry`](../../src/j1/extension/registry.py). The
  three hooks (`should_index` / `requires_review` / `redact`) cover
  the common indexing-filter / review-gate / output-redaction needs
  without core code changes.
- **Add a profile** with vertical-specific taxonomy / prompts /
  report templates.
- **Add an LLM client** under `j1.llm.<vendor>/` (or in your own
  package via `register_trusted_prefix`) implementing the role
  protocols.
- **Add a transport adapter** under `j1.adapters.<name>/` (or in your
  own package) that maps a transport's request → an
  `ApplicationFacade` port call. See
  [`docs/external-integration-architecture.md`](../external-integration-architecture.md)
  § 6.
- **Override a default bridge** via env-driven processor hooks
  (`J1_RAGANYTHING_*_PROCESSOR`, `J1_GRAPHIFY_GRAPH_PROCESSOR`).
- **Compose a custom worker** with your own activity registries
  passed into `Bootstrap`.

---

## 10. Forbidden patterns

These will be rejected in code review:

- Adding industry vocabulary or customer names to a J1 core module
  (anything under `src/j1/` excluding `src/j1/profiles/`).
- Importing your domain module from anywhere in `src/j1/`.
- Adding a vendor SDK import (`import openai`, `import langchain`,
  `import raganything`) outside `src/j1/llm/<vendor>.py`,
  `src/j1/providers/<vendor>/`, or `src/j1/adapters/<vendor>/`.
- Hardcoding a profile name (other than `default`) in core.
- Adding domain-specific routes to `src/j1/adapters/rest/` —
  domain HTTP routes belong in your own ASGI app that *also* mounts
  J1's `create_rest_api(...)`.
- Adding `kb:<vertical>` scopes to
  [`src/j1/integration/security/scopes.py`](../../src/j1/integration/security/scopes.py)
  — domain-specific scopes live in your domain module's scope
  catalogue.
- Polluting the bundled `default` profile with anything other than
  empty / generic placeholders.

---

## 11. Example: the wrong way and the right way

**Wrong** — adding a domain enricher to `src/j1/enrichers.py`:

```python
# src/j1/enrichers.py  ← DO NOT DO THIS
class ContractClauseExtractor(_StructuredEnricher):   # legal-specific
    OUTPUT_KIND = "enriched.legal.clauses"
    ...
```

**Right** — adding a domain enricher in your domain module:

```python
# example_domain/enrichers.py
from j1.enrichers import _StructuredEnricher

class ExampleClauseExtractor(_StructuredEnricher):
    OUTPUT_KIND = "enriched.example.clauses"
    ...

# example_domain/compose.py
from j1.compose import Bootstrap
from example_domain.enrichers import ExampleClauseExtractor

result = Bootstrap(
    enrichers={ExampleClauseExtractor.kind: ExampleClauseExtractor(...)},
).build()
```

The framework gets none of the legal-specific knowledge; the
deployment is the only place where the two concerns meet.

---

## 12. Cross-references

- [`docs/architecture.md`](../architecture.md) — the protocols
  + workspace + workflow shapes domain modules build against
- [`docs/extension/add-a-provider.md`](add-a-provider.md) — the
  provider-shape recipe
- [`docs/providers.md`](../providers.md) — bundled provider
  configuration; the same shape applies to your own
- [`src/j1/profiles/default/`](../../src/j1/profiles/default/) —
  reference for the profile directory shape
- [`src/j1/llm/classloader.py`](../../src/j1/llm/classloader.py) —
  `register_trusted_prefix` for safely loading callables from your
  domain module
