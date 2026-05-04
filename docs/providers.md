# Provider Layer & Composition Root

How J1 wires LLM clients + compiler / graph / retrieval providers
into a runnable system. The framework's existing
[Compiler → Enrich → Graph → Persist](architecture.md) flow stays
unchanged — this document describes the **composition root** that
constructs the providers and the **LLM role abstraction** they
consume.

```
                  ┌──────────────────────────────────┐
                  │  j1.compose.Bootstrap            │
                  │   .build()                       │
                  └─────────┬────────────────────────┘
                            │ produces
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  LLMProviderRegistry   compilers /         StartupDiagnostics
                        graph_builders /
                        retrieval_providers
```

Two entrypoints share **one composition path** — the API and the worker
both call `bootstrap_from_env()` (or build a `Bootstrap` directly with
fakes for tests).

---

## 1. LLM provider layer

Three roles ship: text, vision, embedding. Each is a small Protocol
in [`j1.llm.clients`](../src/j1/llm/clients.py); concrete clients live
under [`j1.llm.openai_compat`](../src/j1/llm/openai_compat.py) and
[`j1.llm.langchain_adapter`](../src/j1/llm/langchain_adapter.py).

### Roles

| Role | Used by | Required when |
|---|---|---|
| `text` | RAGAnything compiler / graph / retrieval, enrichment cleanup, answer synthesis | Always (when RAGAnything is selected) |
| `vision` | Visual enrichment (images / diagrams / scanned pages) | `J1_ENRICH_IMAGES` / `J1_ENRICH_DIAGRAMS` / `J1_ENRICH_SCANNED_PAGES` is true |
| `embedding` | RAGAnything chunk indexing + vector retrieval | Always (when RAGAnything is selected) |

### Configuring an OpenAI-compatible provider

`openai_compat` works with any provider that exposes the OpenAI REST
surface (`/chat/completions`, `/embeddings`) — vLLM, Ollama, Together,
Azure, DashScope, OpenRouter, etc.

```bash
# Text role
J1_TEXT_LLM_PROVIDER=openai_compat
J1_TEXT_LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
J1_TEXT_LLM_API_KEY=sk-...
J1_TEXT_LLM_MODEL=qwen-plus
J1_TEXT_LLM_TEMPERATURE=0.2
J1_TEXT_LLM_MAX_OUTPUT_TOKENS=4096

# Vision role (only required when visual enrichment is enabled)
J1_VISION_LLM_PROVIDER=openai_compat
J1_VISION_LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
J1_VISION_LLM_API_KEY=sk-...
J1_VISION_LLM_MODEL=qwen-vl-plus

# Embedding role
J1_EMBEDDING_PROVIDER=openai_compat
J1_EMBEDDING_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
J1_EMBEDDING_API_KEY=sk-...
J1_EMBEDDING_MODEL=text-embedding-v3
J1_EMBEDDING_DIM=1024
J1_EMBEDDING_BATCH_SIZE=32
```

### Configuring a LangChain provider

LangChain is **optional** — the package is not pulled into J1's
runtime deps. The adapters lazy-import `langchain_core` at
construction and raise a clear `LLMProviderUnavailable` (with the
pip-install hint) if missing.

Two construction paths — pick the one that fits:

#### A) Env-driven auto-construction (recommended)

`Bootstrap` reads `J1_*_LLM_LANGCHAIN_CONFIG` env vars and
auto-instantiates LangChain clients via a safe class-loader. The
config carries a `class` field (a short alias from the built-in
catalog OR a fully-qualified `module:Class` path) plus any kwargs
you want passed to the constructor:

```bash
pip install j1[langchain-openai]   # or [langchain-anthropic], etc.

# Text role via LangChain's ChatOpenAI
J1_TEXT_LLM_PROVIDER=langchain
J1_TEXT_LLM_MODEL=gpt-4o-mini
J1_TEXT_LLM_TEMPERATURE=0.3
J1_TEXT_LLM_LANGCHAIN_CONFIG={"class":"ChatOpenAI","api_key":"sk-..."}

# Embedding via LangChain's OpenAIEmbeddings
J1_EMBEDDING_PROVIDER=langchain
J1_EMBEDDING_MODEL=text-embedding-3-small
J1_EMBEDDING_DIM=1536
J1_EMBEDDING_LANGCHAIN_CONFIG={"class":"OpenAIEmbeddings","api_key":"sk-..."}
```

The class-loader's allowlist accepts:
- Short aliases from
  [`CHAT_MODEL_CATALOG`](../src/j1/llm/classloader.py) /
  [`EMBEDDING_CATALOG`](../src/j1/llm/classloader.py) (e.g.
  `ChatOpenAI`, `ChatAnthropic`, `ChatOllama`, `OpenAIEmbeddings`,
  `HuggingFaceEmbeddings`, ...).
- Any fully-qualified path under `langchain*` or `j1.*`.
- Any path under a prefix the deployment registers via
  `j1.register_trusted_prefix("mycompany_kb")`.

This is the safest config-driven option: it can't accidentally
import `subprocess` or anything else off `sys.path`.

#### B) Hand-injected model (full control)

When the model needs constructor logic the env can't capture (custom
auth, callbacks, runtime parameter twiddling, …) the deployment
instantiates the LangChain model directly and hands it in:

```python
from langchain_openai import ChatOpenAI
from j1 import (
    Bootstrap, LangChainTextLLMClient, LLM_ROLE_TEXT,
    LLMProviderRegistry, TextLLMSettings,
)

chat_model = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)
text_client = LangChainTextLLMClient(
    chat_model,
    settings=TextLLMSettings(provider="langchain", model="gpt-4o-mini"),
)
registry = LLMProviderRegistry({LLM_ROLE_TEXT: text_client})
result = Bootstrap(llm_registry=registry).build()
```

Both paths produce the same `LLMProviderRegistry` shape; mix them per
role if you want (e.g. text via env, vision via hand-injection).

### Mixing providers per role

Nothing forces all three roles onto the same provider. A common
setup:

```bash
# Text via DashScope
J1_TEXT_LLM_PROVIDER=openai_compat
J1_TEXT_LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
J1_TEXT_LLM_MODEL=qwen-plus

# Vision via OpenAI proper
J1_VISION_LLM_PROVIDER=openai_compat
J1_VISION_LLM_BASE_URL=https://api.openai.com/v1
J1_VISION_LLM_MODEL=gpt-4o-mini

# Embeddings via local vLLM
J1_EMBEDDING_PROVIDER=openai_compat
J1_EMBEDDING_BASE_URL=http://vllm.internal:8000/v1
J1_EMBEDDING_MODEL=bge-large-en-v1.5
J1_EMBEDDING_DIM=1024
```

---

## 2. Provider registries

Three registries (compiler / graph / retrieval) are populated by the
composition root based on the `J1_DEFAULT_*` selection env vars.

| Variable | Default | Built-in providers |
|---|---|---|
| `J1_DEFAULT_COMPILER` | `raganything` | `raganything` |
| `J1_DEFAULT_GRAPH_PROVIDER` | `raganything` | `raganything`, `graphify` (optional) |
| `J1_DEFAULT_RETRIEVAL_PROVIDER` | `raganything` | `raganything` |

### RAGAnything (default)

Implements all three Protocols:

- `KnowledgeCompiler` → [`RAGAnythingCompiler`](../src/j1/providers/raganything/compiler.py)
- `GraphBuilder` → [`RAGAnythingGraphBuilder`](../src/j1/providers/raganything/graph.py)
- `QueryProvider` → [`RAGAnythingQueryProvider`](../src/j1/providers/raganything/retrieval.py)

Three construction paths — pick the one that fits:

#### A) Default bundled bridge (just install)

```bash
pip install j1[raganything]
```

That's it. With no processor hook configured, `from_default()` reaches
the bundled bridge in
[`j1.providers.raganything._bridge`](../src/j1/providers/raganything/_bridge.py),
which:
- Lazy-imports `raganything` (`ProviderUnavailable` with pip-install
  hint when missing).
- Constructs `RAGAnything(config=RAGAnythingConfig(...), llm_model_func,
  vision_model_func, embedding_func)` with adapters that translate
  J1's `TextLLMClient` / `VisionLLMClient` / `EmbeddingClient` into
  the `(prompt, **kwargs) -> str` and `(texts) -> list[list[float]]`
  shapes the vendor expects.
- Drives `process_document_complete(file_path, output_dir,
  parse_method="auto")` (compile / graph) or `aquery(question,
  mode="hybrid")` (retrieval).
- Walks the output / storage directory and emits one `ArtifactDraft`
  per file produced. The compile path tags `.json` outputs as
  `compiled.text.metadata`, `.md`/`.txt` as `compiled.text`, image
  outputs as `compiled.text.image`. The graph path surfaces
  `graph_chunk_entity_relation.json` and friends as `graph_json`.

The async API is driven via `asyncio.run`; if the framework is itself
running inside a live event loop (e.g. a custom worker), the bridge
raises `ProviderUnavailable` with a hint to use path B or C.

Required env: `J1_DATA_ROOT` (so the bridge can find the project's
`raw/` source files).

#### B) Env-driven processor hooks (override the default bridge)

When the deployment has its own integration logic — different vendor
version, custom LightRAG storage, an in-process async runner, …

```bash
pip install j1[raganything]

# Each hook names an importable callable. The adapter loads it via
# the safe class-loader and uses it as the compile / graph / query
# callable. Format: "module.path:name" or "module.path.name".
J1_RAGANYTHING_COMPILER_PROCESSOR=mypkg.processors:compile_doc
J1_RAGANYTHING_GRAPH_PROCESSOR=mypkg.processors:build_graph
J1_RAGANYTHING_RETRIEVAL_PROCESSOR=mypkg.processors:query
```

The deployment's processor module receives the framework's request
value object (`RAGAnythingCompileRequest`, `RAGAnythingGraphRequest`,
`RAGAnythingQueryRequest`) — already carrying the resolved LLM
clients, settings, and `ProjectContext` — and returns the canonical
`ArtifactProcessingResult` / `QueryResult`. The adapter layer
translates exceptions into FAILED results so workflow retries work.

For the loader to find your processor module, register your
top-level package as a trusted prefix once at startup:

```python
import j1
j1.register_trusted_prefix("mypkg")
```

#### C) Constructor-injected callable (tests / full programmatic control)

```python
from j1 import RAGAnythingCompiler, RAGAnythingSettings, LLMProviderRegistry
from j1.providers.raganything.compiler import RAGAnythingCompileRequest

def my_compile(request: RAGAnythingCompileRequest):
    return ArtifactProcessingResult(status=ResultStatus.SUCCEEDED, drafts=[...])

compiler = RAGAnythingCompiler(
    llm_registry=registry,
    settings=RAGAnythingSettings(),
    compile_callable=my_compile,    # full control, no env / class-loader
)
```

The framework's own hermetic test suite uses path C; most production
deployments use path A; complex deployments override with path B.

### Graphify (optional alternative)

Optional alternative graph provider. Off by default. To enable + select:

```bash
J1_GRAPHIFY_ENABLED=true
J1_DEFAULT_GRAPH_PROVIDER=graphify
```

Selecting Graphify without enabling it raises a clear startup error
naming both env vars.

The default bridge (in
[`j1.providers.graphify._bridge`](../src/j1/providers/graphify/_bridge.py))
supports two integration modes; pick via `J1_GRAPHIFY_MODE`:

#### Mode `cli` (default)

Spawns the Graphify binary as a subprocess. The bridge writes a
JSON input file (`{tenant_id, project_id, artifact_ids}`) to a
temp dir under `J1_GRAPHIFY_WORKDIR` and invokes:

```
$J1_GRAPHIFY_COMMAND \
    --input  <tmp>/input.json  \
    --output <tmp>/output.json \
    --workdir $J1_GRAPHIFY_WORKDIR
```

The output JSON is parsed into a single `graph_json` `ArtifactDraft`
preserving the input artifact IDs as `source_artifact_ids`.

```bash
J1_GRAPHIFY_MODE=cli                       # default
J1_GRAPHIFY_COMMAND=graphify               # or absolute path
J1_GRAPHIFY_WORKDIR=./data/graphify
```

`ProviderUnavailable` is raised (with an actionable message) if the
binary isn't on `$PATH`. Non-zero exits become `FAILED`
`ArtifactProcessingResult`s carrying the captured stderr (truncated
to 4 KB). No `shell=True`, no string interpolation into the command —
argv is fully composed from validated inputs.

#### Mode `python`

Lazy-imports a `graphify` Python package and looks for either a
top-level `build_graph` callable or a `Graphify` class with a
`build` / `build_graph` method. The bridge passes
`{tenant_id, project_id, artifact_ids, workdir}` as the payload and
expects a dict back with `nodes` / `edges` keys.

```bash
J1_GRAPHIFY_MODE=python
```

`ProviderUnavailable` (with pip-install hint) when the package is
missing; `FAILED` result when the vendor returns a non-dict.

#### Mode override: processor hook

Like RAGAnything, you can bypass the bridge entirely:

```bash
J1_GRAPHIFY_GRAPH_PROCESSOR=mypkg.processors:graphify_build
```

---

## 3. Composition root

[`j1.compose.Bootstrap`](../src/j1/compose/bootstrap.py) is one entry
point that:

1. Loads every `J1_*` setting (LLM roles + RAGAnything + Graphify +
   enrichment + selection)
2. Constructs LLM clients for whichever roles are configured
3. Constructs the selected compiler / graph / retrieval providers
4. **Validates** required roles per selection (text + embedding for
   RAGAnything; vision when visual enrichment is enabled; Graphify
   only when both selected and enabled)
5. Produces a `StartupDiagnostics` snapshot with no secrets in it

### Typical entrypoint use

```python
from j1 import bootstrap_from_env, render_startup_diagnostics
import logging

result = bootstrap_from_env()
for line in render_startup_diagnostics(result.diagnostics):
    logging.info(line)

# result.compilers["raganything"] is a registered KnowledgeCompiler
# result.graph_builders["raganything"] is a registered GraphBuilder
# result.retrieval_providers["raganything"] is a registered QueryProvider
# result.llm_registry.text() returns the text client
```

### Test use

Tests skip env loading entirely by injecting a registry of fake
clients:

```python
from j1 import Bootstrap, LLMProviderRegistry

class _FakeText: provider = "fake"; model = "fake-text"
class _FakeEmbed: provider = "fake"; model = "fake-embed"

reg = LLMProviderRegistry({"text": _FakeText(), "embedding": _FakeEmbed()})
result = Bootstrap(env={"J1_ENRICH_ENABLED": "false"}, llm_registry=reg).build()
```

The composition root uses *exactly the same path* in production and in
tests — so wiring bugs surface in either place equally.

---

## 4. Validation rules (the actionable ones)

| Condition | Failure mode |
|---|---|
| `J1_DEFAULT_COMPILER=raganything` AND no text LLM | `ConfigError: RAGAnything compiler requires text, embedding LLM role(s)` |
| `J1_DEFAULT_COMPILER=raganything` AND no embedding LLM | `ConfigError: RAGAnything compiler requires embedding LLM role(s)` |
| Visual enrichment enabled AND no vision LLM | `ConfigError: Visual enrichment is enabled but no vision LLM is configured` |
| `J1_DEFAULT_GRAPH_PROVIDER=graphify` AND `J1_GRAPHIFY_ENABLED` is unset / false | `ConfigError: graphify is selected but Graphify is not enabled. Set J1_GRAPHIFY_ENABLED=true` |
| `J1_DEFAULT_*=<unknown>` | `ConfigError: <name> is not a registered <kind> provider. Built-in providers: ...` |

Every error names the env var(s) the operator needs to set.

---

## 5. Startup diagnostics

A secrets-safe snapshot of what's wired:

```
J1 startup diagnostics:
  compilers: raganything
  graph providers: raganything
  retrieval providers: raganything
  enrichment providers: (none registered)
  selected compiler: raganything
  selected graph: raganything
  selected retrieval: raganything
  enrichment: enabled modalities=[images, tables, diagrams, scanned_pages]
  graphify: disabled
  llm[embedding]: provider=openai_compat model=text-embedding-v3 dim=1024
  llm[text]: provider=openai_compat model=qwen-plus
  llm[vision]: provider=openai_compat model=qwen-vl-plus
```

What's **never** printed: API keys, base URLs, LangChain config dicts,
document content, embedding vectors. The render function is the only
sanctioned formatter — operators log it line-by-line at INFO at
startup so misconfiguration surfaces in dashboards immediately.

---

## 6. Adding a new provider (recipe)

Same recipe whether it's a new compiler, graph builder, or retrieval
backend. Imagine wiring up a third-party "FooDocs" compiler:

1. Create `src/j1/providers/foodocs/`.
2. Implement the relevant Protocol from
   [`j1.processing.contracts`](../src/j1/processing/contracts.py)
   (`KnowledgeCompiler` for a compiler, `GraphBuilder` for a graph
   builder, `QueryProvider` for retrieval).
3. Receive any LLM client(s) via constructor — pull from
   `LLMProviderRegistry`, never read env vars.
4. Lazy-import the vendor library inside the default factory; raise
   `ProviderUnavailable("install foodocs")` if missing.
5. Add settings + `load_foodocs_settings(env=...)` mirroring the
   existing settings modules.
6. Register the provider in `j1.compose.bootstrap.Bootstrap.build()`
   under a new selection branch.

That's it. No core code changes; the existing
`ProcessingService`/`ProjectProcessingWorkflow`/`HybridQueryEngine`
keep working through the Protocol.
