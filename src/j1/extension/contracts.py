"""Extension-layer contracts.

The 12 stable Protocol surfaces J1 grows through. Each contract:

 * Takes only domain-neutral primitives from `j1.extension.primitives`.
 * Carries a `kind: str` identifier used by the capability registry.
 * Belongs in *its own module* in your codebase (or a vendor's
 package). Implementations are wired into J1 via the
 `CapabilityRegistry`; the core never imports your concrete class.

Naming note: a few of the names below collide with legacy
connector-layer types under `j1.connectors.*` (the existing
`CompilerAdapter` / `GraphAdapter` are *Adapter Pattern* helpers, not
extension contracts). To avoid an import-time clash, **never**
re-export these contracts unqualified from `j1.__init__`. Always
import them via `from j1.extension.contracts import...`.

Where an existing core Protocol matches the extension contract
verbatim, the extension contract is defined as a separate Protocol
with the same shape — implementations satisfying the existing
Protocol automatically satisfy the extension contract via Python's
structural subtyping (Protocol). This keeps the two layers
decoupled while remaining mechanically interchangeable.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from j1.extension.primitives import (
    ArtifactProcessingResult,
    Citation,
    Document,
    Evidence,
    EvaluationResult,
    GraphEdge,
    GraphNode,
    ProcessingResult,
    ProjectContext,
    QueryResult,
    RetrievalResult,
    Source,
    SourceMetadata,
)


# ---- Source side ----------------------------------------------------


@runtime_checkable
class SourceConnector(Protocol):
    """Fetches `Source`s from an external system.

 Every connector is responsible for materialising bytes + a
 `SourceMetadata`. Persistence into J1 is the framework's job —
 the connector does not call `DocumentIntakeService` itself.

 `kind` identifies the connector at registration time
 (e.g. `"http"`, `"s3"`, `"local-fs"`).
 """

    kind: str

    def list(
        self, ctx: ProjectContext, *, query: dict[str, Any] | None = None,
    ) -> list[SourceMetadata]: ...

    def fetch(
        self, ctx: ProjectContext, metadata: SourceMetadata,
    ) -> Source: ...


# ---- Compile / Enrich / Graph --------------------------------------


@runtime_checkable
class CompilerAdapter(Protocol):
    """Compile a registered document into one or more artifact drafts.

 Mirrors the legacy `j1.processing.contracts.KnowledgeCompiler`
 Protocol shape — implementations of the legacy Protocol satisfy
 this contract automatically (and vice versa).
 """

    kind: str

    def compile(
        self, ctx: ProjectContext, document_id: str,
    ) -> ArtifactProcessingResult: ...


@runtime_checkable
class EnrichmentAdapter(Protocol):
    """Enrich a single artifact (e.g. extract structured fields).

 Mirrors the legacy `EnrichmentProcessor` Protocol shape.
 """

    kind: str

    def enrich(
        self, ctx: ProjectContext, artifact_id: str,
    ) -> ArtifactProcessingResult: ...


@runtime_checkable
class GraphAdapter(Protocol):
    """Build a knowledge graph from a set of artifact ids.

 Mirrors the legacy `GraphBuilder` Protocol shape. The output
 `ArtifactProcessingResult` typically carries one or more
 `graph_json` `ArtifactDraft`s; richer graph adapters MAY also
 surface `nodes` / `edges` in `metadata` (the contract does not
 constrain the metadata schema).
 """

    kind: str

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str],
    ) -> ArtifactProcessingResult: ...


# ---- Retrieval / Reranking -----------------------------------------


@runtime_checkable
class RetrievalAdapter(Protocol):
    """Retrieve evidence for a question.

 Returns the canonical `RetrievalResult` (richer than the legacy
 `QueryResult`-shaped `QueryProvider` Protocol — `RetrievalResult`
 surfaces `Evidence` items the formatter / evaluator can reason
 over, instead of a single pre-baked answer string).

 Adapters that are happy to emit a single answer can wrap one
 `Evidence(content=answer, score=1.0)` and return it.
 """

    kind: str

    def retrieve(
        self,
        ctx: ProjectContext,
        question: str,
        *,
        max_results: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult: ...


@runtime_checkable
class RerankerAdapter(Protocol):
    """Re-order or filter `Evidence` items returned by a retriever.

 Implementations MUST be pure with respect to the input list:
 return a (possibly shorter) list, never mutate inputs.
 """

    kind: str

    def rerank(
        self,
        ctx: ProjectContext,
        question: str,
        evidences: list[Evidence],
        *,
        max_results: int | None = None,
    ) -> list[Evidence]: ...


# ---- LLM / Embedding / Vision providers ----------------------------


@runtime_checkable
class LLMProviderAdapter(Protocol):
    """Generic text-generation provider.

 Distinct from `j1.llm.clients.TextLLMClient` (which carries an
 `(text, usage)` return tuple and is consumed by the bootstrap +
 role registry). `LLMProviderAdapter` is the manifest/registry-
 facing surface — it returns a plain dict with `text` + free-form
 metadata so adapter authors don't need to construct J1's
 `LLMUsage`. A thin shim turns one into the other when needed.
 """

    kind: str

    def generate(
        self,
        ctx: ProjectContext,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class EmbeddingProviderAdapter(Protocol):
    """Generic text-embedding provider.

 Returns a list of equal-length float vectors in the same order
 as the input texts.
 """

    kind: str

    def embed(
        self, ctx: ProjectContext, texts: list[str],
    ) -> list[list[float]]: ...

    def dimension(self) -> int: ...


@runtime_checkable
class VisionProviderAdapter(Protocol):
    """Generic vision provider — describes / answers about image bytes."""

    kind: str

    def analyze(
        self,
        ctx: ProjectContext,
        image_bytes: bytes,
        *,
        prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


# ---- Output / evaluation / policy ----------------------------------


@runtime_checkable
class OutputFormatter(Protocol):
    """Render a final answer + citations into a deployment-chosen shape.

 The output dict's schema is the formatter's concern — J1 does not
 constrain it (a formatter for a chat UI returns one shape; a
 formatter for an API contract returns another).
 """

    kind: str

    def format(
        self,
        ctx: ProjectContext,
        question: str,
        evidences: list[Evidence],
        *,
        citations: list[Citation] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class EvaluationAdapter(Protocol):
    """Score / validate a `RetrievalResult` (or a final formatted output).

 Used by:
 * Workflow gating ("don't surface if score < 0.5")
 * Offline evals
 * Continuous quality monitoring

 Implementations MUST be deterministic with respect to inputs +
 explicit configuration — non-determinism (LLM-judge calls, etc.)
 must be surfaced via `metadata` so callers know what they're
 looking at.
 """

    kind: str

    def evaluate(
        self,
        ctx: ProjectContext,
        question: str,
        evidences: list[Evidence],
        *,
        expected: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationResult: ...


@runtime_checkable
class DomainPolicy(Protocol):
    """A pluggable, side-effect-free decision hook a deployment registers
 so workflow steps can branch without the core knowing about the
 domain.

 Three Protocol methods cover the common cases. Default implementations
 on a base `DomainPolicy` (in your domain module) usually delegate to
 `True` / no-op so partial implementations are easy.

 `should_index(...)`: per-artifact filter — `True` to index, `False`
 to skip.
 `requires_review(...)`: per-artifact-or-result review gate — `True`
 pauses for human review.
 `redact(...)`: returns a (possibly modified) `Evidence` list — used
 to drop / mask content before output formatting. MUST NOT mutate
 inputs.
 """

    kind: str

    def should_index(
        self, ctx: ProjectContext, artifact_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool: ...

    def requires_review(
        self, ctx: ProjectContext, target_kind: str, target_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool: ...

    def redact(
        self, ctx: ProjectContext, evidences: list[Evidence],
    ) -> list[Evidence]: ...


# ---- Aliases for legacy core protocols (round-tripping) -------------
#
# These re-exports let extension-aware code import everything from
# `j1.extension.contracts`. They are NOT redefinitions — they're the
# original Protocols, with the same identity. Implementations
# satisfying either name satisfy both.

from j1.processing.contracts import (  # noqa: E402,F401
    EnrichmentProcessor as LegacyEnrichmentProcessor,
    GraphBuilder as LegacyGraphBuilder,
    KnowledgeCompiler as LegacyKnowledgeCompiler,
    ModelProvider as LegacyModelProvider,
    QueryProvider as LegacyQueryProvider,
    SearchIndexer as LegacySearchIndexer,
)


__all__ = [
    # Source side
    "SourceConnector",
    # Compile / Enrich / Graph
    "CompilerAdapter",
    "EnrichmentAdapter",
    "GraphAdapter",
    # Retrieval / Reranking
    "RetrievalAdapter",
    "RerankerAdapter",
    # LLM / Embedding / Vision
    "LLMProviderAdapter",
    "EmbeddingProviderAdapter",
    "VisionProviderAdapter",
    # Output / evaluation / policy
    "OutputFormatter",
    "EvaluationAdapter",
    "DomainPolicy",
    # Legacy aliases
    "LegacyEnrichmentProcessor",
    "LegacyGraphBuilder",
    "LegacyKnowledgeCompiler",
    "LegacyModelProvider",
    "LegacyQueryProvider",
    "LegacySearchIndexer",
]
