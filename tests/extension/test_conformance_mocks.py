"""Conformance suite — runs every shared harness against every mock.

This file proves both:
  * the harness functions in `j1.extension.conformance` work, and
  * each mock adapter ships in a conformant state.

Vendor / domain test suites should mirror this file against their
own adapters — see `docs/extension/conformance-tests.md`.
"""

from __future__ import annotations

from j1.extension.conformance import (
    assert_compiler_adapter_conformance,
    assert_embedding_provider_adapter_conformance,
    assert_enrichment_adapter_conformance,
    assert_evaluation_adapter_conformance,
    assert_graph_adapter_conformance,
    assert_llm_provider_adapter_conformance,
    assert_output_formatter_conformance,
    assert_reranker_adapter_conformance,
    assert_retrieval_adapter_conformance,
    assert_source_connector_conformance,
    assert_vision_provider_adapter_conformance,
)
from j1.extension.mocks import (
    MockCompilerAdapter,
    MockEmbeddingProviderAdapter,
    MockEnrichmentAdapter,
    MockEvaluationAdapter,
    MockGraphAdapter,
    MockLLMProviderAdapter,
    MockOutputFormatter,
    MockRerankerAdapter,
    MockRetrievalAdapter,
    MockSourceConnector,
    MockVisionProviderAdapter,
)
from j1.extension.primitives import (
    Citation,
    Evidence,
    ProjectContext,
    Source,
    SourceMetadata,
)


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


# ---- Required-by-task: harnesses for the 5 named contracts ---------


def test_source_connector_conformance():
    sources = [
        Source(content=b"hello", metadata=SourceMetadata(uri="mem://1")),
        Source(content=b"world", metadata=SourceMetadata(uri="mem://2")),
    ]
    assert_source_connector_conformance(MockSourceConnector(sources=sources), _ctx())


def test_compiler_adapter_conformance():
    assert_compiler_adapter_conformance(MockCompilerAdapter(), _ctx(), "doc-1")


def test_retrieval_adapter_conformance():
    corpus = [
        Evidence(content="hello world", score=0.9,
                 citations=[Citation(document_id="d-1")]),
        Evidence(content="another item", score=0.5,
                 citations=[Citation(document_id="d-2")]),
    ]
    assert_retrieval_adapter_conformance(
        MockRetrievalAdapter(corpus=corpus), _ctx(), "hello",
    )


def test_llm_provider_adapter_conformance():
    assert_llm_provider_adapter_conformance(MockLLMProviderAdapter(), _ctx())


def test_evaluation_adapter_conformance():
    assert_evaluation_adapter_conformance(
        MockEvaluationAdapter(threshold=0.0), _ctx(),
    )


# ---- Bonus harnesses (cover the remaining contracts) ---------------


def test_enrichment_adapter_conformance():
    assert_enrichment_adapter_conformance(MockEnrichmentAdapter(), _ctx(), "art-1")


def test_graph_adapter_conformance():
    assert_graph_adapter_conformance(
        MockGraphAdapter(), _ctx(), ["art-1", "art-2"],
    )


def test_reranker_adapter_conformance():
    assert_reranker_adapter_conformance(MockRerankerAdapter(), _ctx())


def test_embedding_provider_adapter_conformance():
    assert_embedding_provider_adapter_conformance(
        MockEmbeddingProviderAdapter(), _ctx(),
    )


def test_vision_provider_adapter_conformance():
    assert_vision_provider_adapter_conformance(
        MockVisionProviderAdapter(), _ctx(),
    )


def test_output_formatter_conformance():
    assert_output_formatter_conformance(MockOutputFormatter(), _ctx())
