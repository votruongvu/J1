# Adding a Provider to J1

How to plug a new compiler, graph builder, retrieval provider, or LLM
client into J1 without touching the framework core.

The framework's defining principle: **vendor SDKs stay behind
providers; the core never imports them**. This guide shows how to
honour that across each provider type.

> **Two surfaces.** J1 exposes both:
>
> - The **legacy core protocols** in
> [`src/j1/processing/contracts.py`](../../src/j1/processing/contracts.py)
> and [`src/j1/llm/clients.py`](../../src/j1/llm/clients.py), which
> are wired into the bundled workflows + bootstrap. Use these when
> integrating with the existing pipeline.
> - The **uniform extension contracts** in
> [`j1.extension.contracts`](../../src/j1/extension/contracts.py),
> which are `@runtime_checkable` Protocols that adapt to a
> manifest + capability-registry style of registration. Use these
> when you want isinstance-checkable contracts, conformance
> harnesses, or to publish your adapter for downstream reuse.
>
> Implementations satisfying one surface satisfy the other when the
> shapes match (`KnowledgeCompiler` ↔ `CompilerAdapter`,
> `EnrichmentProcessor` ↔ `EnrichmentAdapter`, `GraphBuilder` ↔
> `GraphAdapter`). For the bigger picture see
> [`docs/extension/overview.md`](overview.md).

> RAGAnything and Graphify are **examples** of provider integrations,
> not core identity. The same recipe applies to any vendor /
> in-house compiler, graph builder, or LLM you want to wire in.

---

## 1. Provider taxonomy

| Provider type | Protocol | Lives under | Examples bundled |
|---|---|---|---|
| Knowledge compiler | `KnowledgeCompiler` | `src/j1/providers/<name>/compiler.py` (or `src/j1/connectors/compiler/`) | RAGAnything compiler |
| Graph builder | `GraphBuilder` | `src/j1/providers/<name>/graph.py` (or `src/j1/connectors/graph/`) | RAGAnything graph builder, Graphify graph builder |
| Retrieval / query provider | `QueryProvider` | `src/j1/providers/<name>/retrieval.py` | RAGAnything query provider |
| Search indexer | `SearchIndexer` | (typically core) | `SqliteSearchIndexer` |
| Text LLM client | `TextLLMClient` | `src/j1/llm/<vendor>.py` | OpenAI-compat, LangChain |
| Vision LLM client | `VisionLLMClient` | `src/j1/llm/<vendor>.py` | OpenAI-compat, LangChain |
| Embedding client | `EmbeddingClient` | `src/j1/llm/<vendor>.py` | OpenAI-compat, LangChain |
| Enrichment processor | `EnrichmentProcessor` | `src/j1/enrichers.py` (or a new module) | 9 built-in `_StructuredEnricher` subclasses |
| Generic model provider | `ModelProvider` | (consumed by enrichers + cost router; no concrete impl ships) | None bundled — implementations are deployment-supplied |

**`ModelProvider` vs the LLM role clients — when to use which.** Both
shapes are current:

- **Role clients** (`TextLLMClient` / `VisionLLMClient` /
 `EmbeddingClient` from [`src/j1/llm/clients.py`](../../src/j1/llm/clients.py))
 carry role-specific signatures (`generate(prompt, …)`,
 `analyze_image(bytes, …)`, `embed_batch(texts) → vectors`). Use these
 whenever you're plugging an LLM behind one of the three named
 roles — they integrate with `LLMProviderRegistry`, the bootstrap
 validation, and the OpenAI-compat / LangChain bridges.
- **`ModelProvider`** ([`src/j1/processing/contracts.py`](../../src/j1/processing/contracts.py))
 is the generic, single-method `complete(ctx, prompt, *, model=None, …)
 → ModelResponse` Protocol used by:
 - [`_StructuredEnricher`](../../src/j1/enrichers.py) — its optional
 `model: ModelProvider | None` parameter,
 - [`ModelRouter`](../../src/j1/cost/router.py) — its
 `Mapping[str, ModelProvider]` registry keyed by `TaskCategory`,
 - and as a forward-looking integration point flagged in
 [`adapters/rest/app.py`](../../src/j1/adapters/rest/app.py) and
 [`integration/streaming/service.py`](../../src/j1/integration/streaming/service.py).
 Use `ModelProvider` when you're wiring a router-style abstraction
 over multiple LLMs (cost-aware routing per task category) or when
 the consumer is the enricher's optional model slot.

A deployment is free to wrap a single LLM behind both surfaces (a
`TextLLMClient` for the bootstrap + a thin `ModelProvider` shim for
the router) — the framework never collapses them.

For the protocols themselves, read
[`src/j1/processing/contracts.py`](../../src/j1/processing/contracts.py)
(`KnowledgeCompiler`, `EnrichmentProcessor`, `GraphBuilder`,
`SearchIndexer`, `QueryProvider`, `ModelProvider`) and
[`src/j1/llm/clients.py`](../../src/j1/llm/clients.py)
(`TextLLMClient`, `VisionLLMClient`, `EmbeddingClient`).

---

## 2. Layering rules every provider MUST honour

1. **Lazy-import vendor packages.** Top-level `import vendor_sdk`
 means the framework can't be installed without the optional
 dependency. Do the import inside the function that needs it, and
 raise `ProviderUnavailable` with a pip-install hint when missing.
2. **Never leak vendor types past the provider boundary.** The
 provider returns canonical J1 types only — `ArtifactDraft`,
 `ArtifactProcessingResult`, `QueryResult`, `(text, usage)` tuples,
 etc. Vendor objects (LangChain runnables, OpenAI response
 objects, RAGAnything instances, …) stay inside the provider
 module.
3. **Translate exceptions at the boundary.** Vendor-side errors
 become either `ProviderUnavailable` (for actionable infra
 failures) or `ArtifactProcessingResult(status=FAILED, …)` (for
 per-call failures). Never let a vendor exception escape into
 `ProcessingService` or a workflow activity.
4. **Take config as a typed settings object.** Provider settings
 live next to the provider (`src/j1/providers/<name>/settings.py`)
 and are loaded by a `load_<name>_settings(env=...)` helper. The
 provider constructor takes the settings object — never the env
 directly.
5. **Provide a test seam.** The provider class accepts an injectable
 callable in its constructor (`compile_callable=`,
 `graph_callable=`, `query_callable=`). Tests pass fakes; the
 default factory (`from_default(...)`) wires the real bridge.

---

## 3. Recipe — Knowledge compiler

A compiler turns a raw document into one or more compiled artifacts.

### 3.1 Required surface

```python
class MyCompiler:
 kind: str = "mycompiler"

 def __init__(
 self, *,
 llm_registry: LLMProviderRegistry,
 settings: MyCompilerSettings,
 compile_callable: Callable[[MyCompileRequest], ArtifactProcessingResult],
 ) -> None:...

 @classmethod
 def from_default(
 cls, *, llm_registry: LLMProviderRegistry, settings: MyCompilerSettings,
 ) -> "MyCompiler":
 # Lazy-import vendor; resolve override seam (env-driven processor)
 # → wire `compile_callable` accordingly....

 def compile(
 self, ctx: ProjectContext, document_id: str,
 ) -> ArtifactProcessingResult:...
```

### 3.2 Returning artifacts

Construct `ArtifactDraft`s — the framework persists them, computes
content hashes, and creates `ArtifactRecord` entries:

```python
ArtifactDraft(
 kind="compiled.text",
 content=b"...",
 suggested_extension=".md",
 source_document_ids=[document_id],
 metadata={"provider": MyCompiler.kind, "stage": "compile"},
)
```

Wrap them in:

```python
ArtifactProcessingResult(
 status=ResultStatus.SUCCEEDED,
 drafts=[draft,...],
 metadata={"provider": MyCompiler.kind},
)
```

### 3.3 Error handling

| Failure | Surface |
|---|---|
| Vendor package missing | `raise ProviderUnavailable("install with: pip install j1[mycompiler]")` |
| Per-document failure (corrupt input, vendor-side 500, etc.) | `ArtifactProcessingResult(status=FAILED, error=str(exc), message=type(exc).__name__, drafts=[], metadata={"provider": MyCompiler.kind})` |
| Async loop conflict (you're inside an event loop) | `raise ProviderUnavailable("…wire your own compile_callable that awaits on the existing loop")` |

The `compile` wrapper in your provider class should catch
`Exception` and convert to FAILED — but re-raise `ProviderUnavailable`
unchanged so operators see actionable infra errors.

### 3.4 Registration

Compose your worker / API with the provider registered under its
`kind`:

```python
from j1.compose import Bootstrap

result = Bootstrap(
 compilers={MyCompiler.kind: MyCompiler.from_default(
 llm_registry=registry, settings=load_mycompiler_settings,
 )},
).build
```

The framework's bootstrap then routes `J1_DEFAULT_COMPILER=mycompiler`
through the new provider.

### 3.5 Testing expectations

Three tests at minimum:

1. **Injected-callable test** — pass a fake `compile_callable`; assert
 the request value object carries the right document ID + LLM
 clients.
2. **Negative default-path test** — call `from_default(...).compile(...)`
 with the vendor module absent; assert `ProviderUnavailable` is
 raised with a `pip install` substring.
3. **Positive boundary test** — inject a fake at the *vendor* seam
 (`monkeypatch.setitem(sys.modules, "vendor", fake)` or
 `monkeypatch.setattr(subprocess, "run", fake_run)`), call
 `from_default(...).compile(...)`, assert the vendor entry point
 was actually invoked. Mocking the whole adapter callable in this
 test defeats the purpose.

[`tests/test_providers.py`](../../tests/test_providers.py) is the
reference for all three patterns.

---

## 4. Recipe — Graph builder

Same pattern as the compiler. Surface:

```python
class MyGraphBuilder:
 kind: str = "mygraph"

 def __init__(
 self, *,
 settings: MyGraphSettings,
 graph_callable: Callable[[MyGraphRequest], ArtifactProcessingResult],
 ) -> None:...

 @classmethod
 def from_default(cls, *, settings: MyGraphSettings) -> "MyGraphBuilder":...

 def build(
 self, ctx: ProjectContext, artifact_ids: list[str],
 ) -> ArtifactProcessingResult:...
```

Output: one `graph_json` `ArtifactDraft` (or several, one per graph
shard) with `source_artifact_ids` populated.

If your builder shells out to a CLI, follow the Graphify pattern:
build `argv` deterministically, never use `shell=True`, cap stderr
output, return FAILED on non-zero exit. See
[`src/j1/providers/graphify/_bridge.py`](../../src/j1/providers/graphify/_bridge.py)
for the reference implementation.

---

## 5. Recipe — Retrieval / query provider

Implement `QueryProvider`:

```python
class MyQueryProvider:
 kind: str = "myquery"

 def query(
 self, ctx: ProjectContext, question: str, *, max_results: int | None = None,
 ) -> QueryResult:...
```

Return:

```python
QueryResult(
 status=ResultStatus.SUCCEEDED,
 answer="...",
 sources=[SourceReference(...),...], # optional but encouraged
 metadata={"provider": MyQueryProvider.kind, "mode": "..."},
)
```

If your provider can't handle a particular query mode, return a
`SUCCEEDED` result with empty `answer` + `sources` rather than
raising — the `HybridQueryEngine` falls back across providers and a
raised exception breaks that.

---

## 6. Recipe — Text / Vision / Embedding LLM client

LLM clients implement role-specific protocols from
[`src/j1/llm/clients.py`](../../src/j1/llm/clients.py). The shapes
are deliberately minimal so any vendor SDK can be wrapped:

| Role | Protocol method | Returns |
|---|---|---|
| Text | `generate(prompt, *, system=None, **opts) -> tuple[str, TokenUsage]` | Generated text + usage |
| Vision | `analyze_image(image_bytes, *, prompt=None, **opts) -> tuple[str, TokenUsage]` | Description + usage |
| Embedding | `embed_batch(texts) -> tuple[list[list[float]], TokenUsage]` | Vectors + usage |
| Embedding (cont.) | `dimension -> int` | Vector dimension |

### 6.1 OpenAI-compatible vendors

If your provider exposes the OpenAI REST surface
(`/chat/completions`, `/embeddings`), no new code is needed —
configure it via `J1_TEXT_LLM_PROVIDER=openai_compat` plus the
`J1_*_BASE_URL` / `J1_*_MODEL` / `J1_*_API_KEY` env vars. See
[`docs/configuration/environment.md`](../configuration/environment.md)
§ 5–7.

### 6.2 LangChain-wrapped vendors

If your vendor has a LangChain class, add the alias to the
class-loader catalog (in
[`src/j1/llm/classloader.py`](../../src/j1/llm/classloader.py)) — or
register your top-level package as a trusted prefix and use a
fully-qualified `module:Class` path:

```python
import j1
j1.register_trusted_prefix("mycompany_llm")
```

Then configure via env:

```bash
J1_TEXT_LLM_PROVIDER=langchain
J1_TEXT_LLM_LANGCHAIN_CONFIG={"class":"mycompany_llm.MyChat","api_key":"..."}
```

### 6.3 Brand-new client (no LangChain, not OpenAI-compatible)

Implement the role protocol directly:

```python
class MyTextClient:
 provider = "myvendor"
 model = "..."

 def generate(self, prompt: str, *, system: str | None = None, **opts):
 # vendor SDK call, return (text, TokenUsage(...))...
```

Wire it into the `LLMProviderRegistry` at composition time:

```python
from j1 import LLMProviderRegistry, LLM_ROLE_TEXT

registry = LLMProviderRegistry
registry.register(LLM_ROLE_TEXT, MyTextClient(...))
```

### 6.4 Required vs optional env

The text + embedding roles are required when the default RAGAnything
provider is selected. Vision is required only when visual enrichment
is enabled (`J1_ENRICH_IMAGES`, `J1_ENRICH_DIAGRAMS`,
`J1_ENRICH_SCANNED_PAGES`). The bootstrap raises `ConfigError` with
actionable messages when a required role is missing.

---

## 7. Configuration pattern

Every provider follows the same shape:

```python
# src/j1/providers/myvendor/settings.py
ENV_MYVENDOR_X = "J1_MYVENDOR_X"

@dataclass(frozen=True)
class MyVendorSettings:
 x: str | None = None
 workdir: str = "./data/myvendor"
 processor: str | None = None # override seam (J1_MYVENDOR_PROCESSOR)

def load_myvendor_settings(env: Mapping[str, str] | None = None) -> MyVendorSettings:
 source = env if env is not None else os.environ
 return MyVendorSettings(
 x=source.get(ENV_MYVENDOR_X),...
 )
```

Document each new env var in
[`docs/configuration/environment.md`](../configuration/environment.md)
**and** add it to [`.env.example`](../../.env.example) (with a
descriptive comment, no real secrets).

---

## 8. Minimal compiler skeleton

```python
# src/j1/providers/myvendor/compiler.py
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from j1.processing.results import ArtifactProcessingResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.providers.errors import ProviderUnavailable
from j1.llm.registry import LLMProviderRegistry
from j1.providers.myvendor.settings import MyVendorSettings

PROVIDER_NAME = "myvendor"


@dataclass(frozen=True)
class MyCompileRequest:
 ctx: ProjectContext
 document_id: str
 settings: MyVendorSettings
 text_client: Any
 embedding_client: Any | None


CompileCallable = Callable[[MyCompileRequest], ArtifactProcessingResult]


class MyCompiler:
 kind: str = PROVIDER_NAME

 def __init__(
 self, *,
 llm_registry: LLMProviderRegistry,
 settings: MyVendorSettings,
 compile_callable: CompileCallable,
 ) -> None:
 self._llm_registry = llm_registry
 self._settings = settings
 self._compile_callable = compile_callable

 @classmethod
 def from_default(
 cls, *, llm_registry: LLMProviderRegistry, settings: MyVendorSettings,
 ) -> "MyCompiler":
 if settings.processor:
 from j1.llm.classloader import resolve_callable
 compile_callable = resolve_callable(settings.processor)
 else:
 compile_callable = _default_callable
 return cls(
 llm_registry=llm_registry, settings=settings,
 compile_callable=compile_callable,
 )

 def compile(
 self, ctx: ProjectContext, document_id: str,
 ) -> ArtifactProcessingResult:
 request = MyCompileRequest(
 ctx=ctx, document_id=document_id, settings=self._settings,
 text_client=self._llm_registry.text,
 embedding_client=self._llm_registry.try_embedding,
 )
 try:
 return self._compile_callable(request)
 except ProviderUnavailable:
 raise
 except Exception as exc:
 return ArtifactProcessingResult(
 status=ResultStatus.FAILED,
 error=str(exc),
 message=type(exc).__name__,
 drafts=[],
 metadata={"provider": PROVIDER_NAME},
 )


def _default_callable -> CompileCallable:
 def _delegate(request: MyCompileRequest) -> ArtifactProcessingResult:
 from j1.providers.myvendor._bridge import default_compile
 return default_compile(request)
 return _delegate
```

Plus a `_bridge.py` next to it that does the actual vendor work
(lazy-import + call + normalise output).

---

## 9. Testing expectations

A new provider is incomplete without:

- **Unit tests** covering: injected-callable success, exception
 normalisation, `ProviderUnavailable` propagation, the `kind`
 attribute, the settings loader.
- **Negative default-path tests** covering: vendor-package missing,
 binary-not-on-`PATH` (CLI mode), unknown-mode error.
- **Positive boundary tests** covering: vendor entry point actually
 invoked (mocking ONLY at the vendor seam — `sys.modules`,
 `subprocess.run`, `shutil.which`, etc., never at the provider
 callable).

The reference is [`tests/test_providers.py`](../../tests/test_providers.py)
— follow the same patterns for new providers.

---

## 10. Anti-patterns

These will be flagged in code review and are usually a hint that
something belongs elsewhere:

| Anti-pattern | Why it's wrong | What to do instead |
|---|---|---|
| `import vendor_sdk` at the top of `j1.processing.*` or `j1.workflows.*` | Couples the core to an optional dep | Move the import into the provider; lazy-import in the bridge |
| Vendor objects in `ArtifactDraft.metadata` | Pickling / Temporal serialisation breaks | Convert to plain `str` / `int` / `dict` at the provider boundary |
| `from j1 import RAGAnythingCompiler` inside `j1.processing.service` | Core depends on a specific provider | Inject providers via `kind`-keyed mapping at composition time |
| Reading env vars inside a provider class | Provider becomes hard to test + tightly coupled to env layout | Take a typed settings object in the constructor; load env in the loader |
| `try:... except Exception: pass` around vendor calls | Silent failures hide bugs | Catch `Exception` at the *adapter wrapper* and convert to FAILED `ArtifactProcessingResult`; never swallow inside the bridge |
| `subprocess.run("graphify " + user_input, shell=True)` | Command injection | Compose `argv` as a list; never use `shell=True`; validate inputs |
| Defining new "kind" strings as module-level magic | Drift across the codebase | Define a `PROVIDER_NAME` constant in the provider's `__init__.py` and use it everywhere |
| Provider-specific config in `j1.compose.bootstrap.Bootstrap.build` | Pollutes the composition root | Put provider config in the provider's settings module; `Bootstrap` only resolves the selection |
| Skipping the test-seam constructor and only providing `from_default` | Hermetic tests can't run | Always offer `__init__(*, callable=...)` for tests AND `from_default(...)` for production |

---

## 11. Cross-references

- [`src/j1/processing/contracts.py`](../../src/j1/processing/contracts.py) — protocol definitions
- [`src/j1/llm/clients.py`](../../src/j1/llm/clients.py) — LLM client protocols
- [`src/j1/llm/registry.py`](../../src/j1/llm/registry.py) — `LLMProviderRegistry`
- [`src/j1/providers/raganything/`](../../src/j1/providers/raganything/) — full reference implementation (compiler / graph / retrieval)
- [`src/j1/providers/graphify/`](../../src/j1/providers/graphify/) — reference for CLI + Python integration modes
- [`src/j1/llm/classloader.py`](../../src/j1/llm/classloader.py) — class-loader allowlist + `register_trusted_prefix`
- [`docs/providers.md`](../providers.md) — provider configuration from the operator's perspective
- [`docs/configuration/environment.md`](../configuration/environment.md) — every `J1_*` env var
- [`docs/extension/domain-module-isolation.md`](domain-module-isolation.md) — when your work isn't a provider but a domain module
