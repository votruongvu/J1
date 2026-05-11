"""Mock adapters used by the extension test suite and runnable examples.

Every adapter here is:

 * Deterministic — same input → same output, no clocks, no
 random, no network.
 * Domain-neutral — uses only the canonical primitives.
 * Cheap — no I/O beyond what's required to participate in a
 workflow.

These mocks are intended for tests / docs ONLY. They are exported
from `j1.extension` for reuse in downstream test suites.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from j1.extension.contracts import (
    CompilerAdapter,
    EmbeddingProviderAdapter,
    EnrichmentAdapter,
    EvaluationAdapter,
    GraphAdapter,
    LLMProviderAdapter,
    OutputFormatter,
    RerankerAdapter,
    RetrievalAdapter,
    SourceConnector,
    VisionProviderAdapter,
)
from j1.extension.manifest import AdapterManifest
from j1.extension.primitives import (
    ArtifactDraft,
    ArtifactProcessingResult,
    Citation,
    Evidence,
    EvaluationResult,
    ProcessingResult,
    ProjectContext,
    ResultStatus,
    RetrievalResult,
    Source,
    SourceMetadata,
)


# ---- Helpers --------------------------------------------------------


def _stable_score(text: str) -> float:
    """Deterministic 0..1 score derived from the text — useful for
 evidence ranking in mocks without introducing nondeterminism."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Use first byte as 0..1 fraction.
    return digest[0] / 255.0


# ---- SourceConnector ------------------------------------------------


@dataclass
class MockSourceConnector(SourceConnector):
    """Returns a fixed in-memory list of sources.

 `kind` defaults to `"mock"` to match `MANIFEST.name`.
 """

    sources: list[Source] = field(default_factory=list)
    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="source-connector",
        version="0.1.0",
        capabilities=("listing", "fetch"),
        output_types=("text/plain",),
        description="In-memory source connector for tests + examples.",
    )

    def list(
        self, ctx: ProjectContext, *, query: dict[str, Any] | None = None,
    ) -> list[SourceMetadata]:
        return [s.metadata for s in self.sources]

    def fetch(
        self, ctx: ProjectContext, metadata: SourceMetadata,
    ) -> Source:
        for source in self.sources:
            if source.metadata.uri == metadata.uri:
                return source
        # Conformant on missing input: return an empty source rather
        # than raising. Real connectors should `raise FileNotFoundError`
        # or similar — mocks stay friendly to tests.
        return Source(content=b"", metadata=metadata)


# ---- CompilerAdapter ------------------------------------------------


@dataclass
class MockCompilerAdapter(CompilerAdapter):
    """Emits exactly one `compiled.text` draft per `compile` call."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="compiler",
        version="0.1.0",
        capabilities=("text",),
        output_types=("compiled.text",),
        description="In-memory compiler that produces one draft per document.",
    )

    def compile(
        self, ctx: ProjectContext, document_id: str,
    ) -> ArtifactProcessingResult:
        if not document_id:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED,
                error="empty document_id",
                metadata={"adapter": self.kind},
            )
        draft = ArtifactDraft(
            kind="compiled.text",
            content=f"compiled:{document_id}".encode("utf-8"),
            suggested_extension=".txt",
            source_document_ids=[document_id],
            metadata={"adapter": self.kind},
        )
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[draft],
            metadata={"adapter": self.kind},
        )


# ---- EnrichmentAdapter ----------------------------------------------


@dataclass
class MockEnrichmentAdapter(EnrichmentAdapter):
    """Echoes a single `enriched.text` draft tagged with the artifact id."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="enrichment",
        version="0.1.0",
        capabilities=("text",),
        output_types=("enriched.text",),
        description="In-memory enrichment that tags artifacts.",
    )

    def enrich(
        self, ctx: ProjectContext, artifact_id: str,
    ) -> ArtifactProcessingResult:
        if not artifact_id:
            return ArtifactProcessingResult(
                status=ResultStatus.FAILED, error="empty artifact_id",
                metadata={"adapter": self.kind},
            )
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(
                kind="enriched.text",
                content=f"enriched:{artifact_id}".encode("utf-8"),
                source_artifact_ids=[artifact_id],
                metadata={"adapter": self.kind},
            )],
            metadata={"adapter": self.kind},
        )


# ---- GraphAdapter ---------------------------------------------------


@dataclass
class MockGraphAdapter(GraphAdapter):
    """Builds a tiny `graph_json` draft listing the input artifact ids
 as nodes."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="graph",
        version="0.1.0",
        capabilities=("synchronous",),
        output_types=("graph_json",),
        description="In-memory graph builder.",
    )

    def build(
        self, ctx: ProjectContext, artifact_ids: list[str],
    ) -> ArtifactProcessingResult:
        nodes = [{"id": aid} for aid in artifact_ids]
        edges: list[dict] = []
        payload = b'{"nodes":' + str(nodes).encode("ascii") + b',"edges":[]}'
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[ArtifactDraft(
                kind="graph_json",
                content=payload,
                suggested_extension=".json",
                source_artifact_ids=list(artifact_ids),
                metadata={
                    "adapter": self.kind,
                    "node_count": str(len(nodes)),
                    "edge_count": str(len(edges)),
                },
            )],
            metadata={"adapter": self.kind},
        )


# ---- RetrievalAdapter -----------------------------------------------


@dataclass
class MockRetrievalAdapter(RetrievalAdapter):
    """Returns one `Evidence` per registered `corpus` entry whose
 content overlaps the question word-by-word.

 Deterministic ranking: scores are derived from a stable hash of
 each evidence's content (no actual relevance — exists to exercise
 the contract end-to-end).
 """

    corpus: list[Evidence] = field(default_factory=list)
    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="retrieval",
        version="0.1.0",
        capabilities=("synchronous",),
        description="In-memory keyword-overlap retriever for tests.",
    )

    def retrieve(
        self,
        ctx: ProjectContext,
        question: str,
        *,
        max_results: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        words = set((question or "").lower().split())
        if not words:
            return RetrievalResult(
                status=ResultStatus.SUCCEEDED, evidences=[],
                metadata={"adapter": self.kind, "reason": "empty-question"},
            )
        scored: list[Evidence] = []
        for ev in self.corpus:
            ev_words = set(ev.content.lower().split())
            if words & ev_words:
                # Re-score deterministically while preserving citations.
                scored.append(Evidence(
                    content=ev.content,
                    score=_stable_score(ev.content),
                    citations=list(ev.citations),
                    metadata=dict(ev.metadata),
                ))
        scored.sort(key=lambda e: e.score, reverse=True)
        if max_results is not None:
            scored = scored[:max_results]
        return RetrievalResult(
            status=ResultStatus.SUCCEEDED, evidences=scored,
            metadata={"adapter": self.kind},
        )


# ---- RerankerAdapter ------------------------------------------------


@dataclass
class MockRerankerAdapter(RerankerAdapter):
    """Stable sort by score, descending. Pure — no input mutation."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="reranker",
        version="0.1.0",
        capabilities=("score-descending",),
        description="Stable score-descending reranker.",
    )

    def rerank(
        self, ctx: ProjectContext, question: str, evidences: list[Evidence],
        *, max_results: int | None = None,
    ) -> list[Evidence]:
        out = sorted(evidences, key=lambda e: e.score, reverse=True)
        if max_results is not None:
            out = out[:max_results]
        return out


# ---- LLMProviderAdapter ---------------------------------------------


@dataclass
class MockLLMProviderAdapter(LLMProviderAdapter):
    """Returns a deterministic echo of the prompt prefixed with `mock:`."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="llm",
        version="0.1.0",
        capabilities=("text-generation",),
        description="Deterministic echo LLM for tests.",
    )

    def generate(
        self,
        ctx: ProjectContext,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = f"mock:{prompt}"
        if max_tokens is not None and max_tokens >= 0:
            text = text[: max_tokens or 0]
        return {
            "text": text,
            "model": "mock-llm",
            "metadata": {"adapter": self.kind, "system_used": system is not None},
        }


# ---- EmbeddingProviderAdapter ---------------------------------------


@dataclass
class MockEmbeddingProviderAdapter(EmbeddingProviderAdapter):
    """Returns deterministic 8-dim vectors derived from sha256(text)."""

    kind: str = "mock"
    _DIM: int = 8

    MANIFEST = AdapterManifest(
        name="mock",
        type="embedding",
        version="0.1.0",
        capabilities=("synchronous",),
        description="Deterministic mock embeddings (8-dim hash buckets).",
    )

    def embed(self, ctx: ProjectContext, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def dimension(self) -> int:
        return self._DIM

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[: self._DIM]]


# ---- VisionProviderAdapter ------------------------------------------


@dataclass
class MockVisionProviderAdapter(VisionProviderAdapter):
    """Returns a fixed description; ignores the image bytes."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="vision",
        version="0.1.0",
        capabilities=("describe",),
        description="Deterministic mock vision adapter.",
    )

    def analyze(
        self, ctx: ProjectContext, image_bytes: bytes,
        *, prompt: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "text": f"mock-vision:{len(image_bytes)}-bytes",
            "metadata": {"adapter": self.kind, "prompted": prompt is not None},
        }


# ---- OutputFormatter ------------------------------------------------


@dataclass
class MockOutputFormatter(OutputFormatter):
    """Returns a tidy dict summarising the question + top evidence."""

    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="output-formatter",
        version="0.1.0",
        capabilities=("json",),
        description="Mock output formatter that returns a JSON-friendly dict.",
    )

    def format(
        self,
        ctx: ProjectContext,
        question: str,
        evidences: list[Evidence],
        *,
        citations: list[Citation] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not evidences:
            return {
                "question": question, "answer": None, "citations": [],
                "metadata": {"adapter": self.kind, "reason": "no-evidence"},
            }
        top = evidences[0]
        merged_citations = list(top.citations)
        if citations:
            merged_citations.extend(citations)
        return {
            "question": question,
            "answer": top.content,
            "citations": [
                {"document_id": c.document_id, "locator": c.locator}
                for c in merged_citations
            ],
            "metadata": {"adapter": self.kind, "top_score": top.score},
        }


# ---- EvaluationAdapter ----------------------------------------------


@dataclass
class MockEvaluationAdapter(EvaluationAdapter):
    """Pass iff at least one evidence whose score >= `threshold`."""

    threshold: float = 0.0
    kind: str = "mock"

    MANIFEST = AdapterManifest(
        name="mock",
        type="evaluation",
        version="0.1.0",
        capabilities=("threshold",),
        description="Threshold-based pass/fail evaluator.",
    )

    def evaluate(
        self,
        ctx: ProjectContext,
        question: str,
        evidences: list[Evidence],
        *,
        expected: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationResult:
        if not evidences:
            return EvaluationResult(
                status=ResultStatus.SUCCEEDED,
                score=0.0, passed=False,
                findings=[{"reason": "no-evidence"}],
                metadata={"adapter": self.kind},
            )
        top_score = max(e.score for e in evidences)
        return EvaluationResult(
            status=ResultStatus.SUCCEEDED,
            score=top_score,
            passed=top_score >= self.threshold,
            findings=[],
            metadata={
                "adapter": self.kind,
                "threshold": self.threshold,
                "evidences_considered": len(evidences),
            },
        )


__all__ = [
    "MockCompilerAdapter",
    "MockEmbeddingProviderAdapter",
    "MockEnrichmentAdapter",
    "MockEvaluationAdapter",
    "MockGraphAdapter",
    "MockLLMProviderAdapter",
    "MockOutputFormatter",
    "MockRerankerAdapter",
    "MockRetrievalAdapter",
    "MockSourceConnector",
    "MockVisionProviderAdapter",
]
