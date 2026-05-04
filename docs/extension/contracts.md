# Extension Contracts and Primitives

J1 grows through a stable surface of 12 contracts and a small set of
canonical primitives. Concrete adapters / connectors / providers /
domain policies satisfy these contracts; the framework wires them
into workflows via the [capability registry](manifest-and-registry.md).

> **Source of truth.** Every name in this document is exported from
> `j1.extension`. Always import via `from j1.extension import …` —
> the contracts are deliberately NOT re-exported from `j1.__init__`
> to avoid clashing with the legacy `CompilerAdapter` / `GraphAdapter`
> names in `j1.connectors.*` (which are a different concept — see
> [`docs/architecture.md`](../architecture.md) § 8 + § 10).

---

## 1. The 12 contracts

Each contract is a `@runtime_checkable` `Protocol` with a `kind: str`
attribute (used as the registry key) plus one or more methods.

| Type | Contract | Method shape | Notes |
|---|---|---|---|
| Source | [`SourceConnector`](#sourceconnector) | `list(ctx) → list[SourceMetadata]`, `fetch(ctx, metadata) → Source` | Fetches bytes from external systems. Persistence into J1 is the framework's job. |
| Compile | [`CompilerAdapter`](#compileradapter) | `compile(ctx, document_id) → ArtifactProcessingResult` | Mirrors the legacy `KnowledgeCompiler`. |
| Enrich | [`EnrichmentAdapter`](#enrichmentadapter) | `enrich(ctx, artifact_id) → ArtifactProcessingResult` | Mirrors the legacy `EnrichmentProcessor`. |
| Graph | [`GraphAdapter`](#graphadapter) | `build(ctx, artifact_ids) → ArtifactProcessingResult` | Mirrors the legacy `GraphBuilder`. |
| Retrieve | [`RetrievalAdapter`](#retrievaladapter) | `retrieve(ctx, question, *, max_results, filters) → RetrievalResult` | Returns evidence (richer than the legacy `QueryProvider`). |
| Rerank | [`RerankerAdapter`](#rerankeradapter) | `rerank(ctx, question, evidences, *, max_results) → list[Evidence]` | MUST NOT mutate inputs. |
| LLM | [`LLMProviderAdapter`](#llmprovideradapter) | `generate(ctx, prompt, *, system, max_tokens, metadata) → dict` | Generic single-method LLM. |
| Embed | [`EmbeddingProviderAdapter`](#embeddingprovideradapter) | `embed(ctx, texts) → list[list[float]]`, `dimension() → int` | |
| Vision | [`VisionProviderAdapter`](#visionprovideradapter) | `analyze(ctx, image_bytes, *, prompt, metadata) → dict` | |
| Format | [`OutputFormatter`](#outputformatter) | `format(ctx, question, evidences, *, citations, metadata) → dict` | Output-shape is the formatter's choice. |
| Evaluate | [`EvaluationAdapter`](#evaluationadapter) | `evaluate(ctx, question, evidences, *, expected, metadata) → EvaluationResult` | MUST be deterministic for a given input. |
| Policy | [`DomainPolicy`](#domainpolicy) | `should_index(...)`, `requires_review(...)`, `redact(ctx, evidences) → list[Evidence]` | Pluggable, side-effect-free decision hooks. |

---

### SourceConnector

```python
@runtime_checkable
class SourceConnector(Protocol):
    kind: str
    def list(self, ctx: ProjectContext, *, query=None) -> list[SourceMetadata]: ...
    def fetch(self, ctx: ProjectContext, metadata: SourceMetadata) -> Source: ...
```

Connectors materialise bytes + metadata. They DO NOT call
`DocumentIntakeService` themselves — the framework persists the
`Source` and returns a canonical `Document` (`DocumentRecord`).

### CompilerAdapter

Same shape as the legacy
[`KnowledgeCompiler`](../../src/j1/processing/contracts.py).
A class that satisfies one satisfies the other (Protocol structural
subtyping).

### EnrichmentAdapter / GraphAdapter

Same relationship to `EnrichmentProcessor` / `GraphBuilder`.

### RetrievalAdapter

Returns the canonical `RetrievalResult`:

```python
@dataclass(frozen=True)
class RetrievalResult:
    status: ResultStatus
    evidences: list[Evidence] = []
    error: str | None = None
    metadata: dict = {}
```

Strictly richer than the legacy `QueryResult` (which carries a
single answer string). Adapters that only have an answer can wrap
it as `Evidence(content=answer, score=1.0)`.

### RerankerAdapter

```python
def rerank(self, ctx, question, evidences, *, max_results=None) -> list[Evidence]:
```

**MUST be pure.** Returns a (possibly shorter) reordered list; never
mutates the input list or the contained `Evidence` instances.

### LLMProviderAdapter

```python
def generate(self, ctx, prompt, *, system=None, max_tokens=None,
             metadata=None) -> dict[str, Any]:
```

Returns a plain dict with at minimum a `text: str` key. Distinct
from the role-specific `j1.llm.clients.TextLLMClient` (which is the
internal shape the bootstrap consumes). A thin shim turns one into
the other when both surfaces are needed.

### EmbeddingProviderAdapter

```python
def embed(self, ctx, texts: list[str]) -> list[list[float]]: ...
def dimension(self) -> int: ...
```

Returns vectors in input order, all of length `dimension()`. Empty
input → empty list (no exception).

### VisionProviderAdapter

```python
def analyze(self, ctx, image_bytes: bytes, *, prompt=None,
            metadata=None) -> dict[str, Any]:
```

### OutputFormatter

```python
def format(self, ctx, question, evidences, *, citations=None,
           metadata=None) -> dict[str, Any]:
```

The output dict's schema is the formatter's choice — a chat-UI
formatter returns one shape, an API-contract formatter returns
another. The framework does not constrain it.

### EvaluationAdapter

```python
def evaluate(self, ctx, question, evidences, *, expected=None,
             metadata=None) -> EvaluationResult:
```

`EvaluationResult` carries an optional `score` (0..1 if set), an
optional `passed` boolean verdict, and a free-form `findings` list.

**Determinism contract.** Repeated calls with the same input MUST
return the same status + score. If your evaluator uses an LLM
judge or other non-deterministic source, surface that in
`metadata` so callers know what they're looking at — the conformance
harness fails if the same input yields different scores.

### DomainPolicy

```python
class DomainPolicy(Protocol):
    kind: str
    def should_index(self, ctx, artifact_id, metadata=None) -> bool: ...
    def requires_review(self, ctx, target_kind, target_id,
                        metadata=None) -> bool: ...
    def redact(self, ctx, evidences: list[Evidence]) -> list[Evidence]: ...
```

The pluggable hook for domain-side decisions. Three methods cover
the common cases:

- `should_index` — per-artifact indexing filter.
- `requires_review` — per-artifact / per-result human-review gate.
- `redact` — masks / drops content before output formatting. MUST
  return a new list; never mutate inputs.

A deployment registers a single `DomainPolicy` and the framework
calls it from workflow steps that have policy hooks. The core never
imports the policy directly.

---

## 2. Canonical primitives

Imported from `j1.extension`:

| Primitive | Where it lives | Used by |
|---|---|---|
| `Document` | alias for [`DocumentRecord`](../../src/j1/documents/models.py) | intake, compile |
| `Artifact` | alias for [`ArtifactRecord`](../../src/j1/artifacts/models.py) | every stage |
| `Source` | new — bytes + `SourceMetadata` | source connectors |
| `SourceMetadata` | new — uri + content-type + checksum + extra | source connectors |
| `Chunk` | new — addressable slice of a document | compilers / chunkers |
| `Collection` | new — named grouping of documents | source connectors / retrieval |
| `Evidence` | new — content + score + citations | retrieval, rerank, output |
| `Citation` | new — pointer back to source | retrieval, output |
| `RetrievalResult` | new — list of evidences + status | `RetrievalAdapter` |
| `GraphNode` / `GraphEdge` | new — typed graph primitives | `GraphAdapter` (optional) |
| `WorkflowState` | new — generic workflow state snapshot | orchestration callers |
| `ProviderConfig` | new — name + type + options + secrets_ref | manifest / registry |
| `EvaluationResult` | new — status + score + passed + findings | `EvaluationAdapter` |
| `ProjectContext` | re-export of [`j1.projects.context.ProjectContext`](../../src/j1/projects/context.py) | every contract |
| `ArtifactDraft` / `ArtifactProcessingResult` | re-exports | compile / enrich / graph |
| `ResultStatus` | re-export | every status field |

> **Why aliases instead of new types?** Because the existing core
> types are already canonical and used throughout J1. A separate
> `extension.Document` would force a translation step at every
> boundary; aliasing keeps them mechanically the same.

---

## 3. Naming and scope rules

| Allowed | Forbidden |
|---|---|
| Defining your own contract that subclasses or wraps one of the 12 above | Modifying any of the 12 contract Protocols themselves |
| Adding fields to `metadata` dicts | Adding fields to the canonical primitive dataclasses (use `metadata` instead) |
| Implementing multiple contracts on one class | Importing concrete vendor SDKs in any extension contract module |
| Returning `metadata={"adapter": self.kind, …}` for traceability | Returning vendor objects (LangChain `Runnable`, OpenAI response) past the contract boundary |

---

## 4. Cross-references

- [`docs/extension/manifest-and-registry.md`](manifest-and-registry.md)
  — manifest schema + capability registry usage
- [`docs/extension/conformance-tests.md`](conformance-tests.md) —
  shared test harnesses and how to run them against your adapters
- [`docs/extension/add-a-provider.md`](add-a-provider.md) — recipe
  for plugging a real provider in (uses the contracts here)
- [`docs/extension/domain-module-isolation.md`](domain-module-isolation.md)
  — what belongs outside core
- [`src/j1/extension/`](../../src/j1/extension/) — the source
- [`src/j1/extension/mocks.py`](../../src/j1/extension/mocks.py) —
  reference implementations of every contract
