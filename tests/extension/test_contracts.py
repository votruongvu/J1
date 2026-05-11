"""Verify each extension contract is a runtime-checkable Protocol with
the expected attribute set, and that the bundled mocks satisfy it.

Tests in this module guard against accidental drift in the contract
shapes (renaming methods, dropping attributes, etc.). They do NOT
re-implement conformance — that's the job of `test_conformance_*.py`.
"""

from __future__ import annotations

import inspect
from typing import Protocol

import pytest

from j1.extension import (
    CompilerAdapter,
    DomainPolicy,
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


CONTRACTS = [
    SourceConnector, CompilerAdapter, EnrichmentAdapter, GraphAdapter,
    RetrievalAdapter, RerankerAdapter, LLMProviderAdapter,
    EmbeddingProviderAdapter, VisionProviderAdapter,
    OutputFormatter, EvaluationAdapter, DomainPolicy,
]

# Each contract's required method (the one beyond `kind`).
METHOD_NAMES = {
    SourceConnector: ("list", "fetch"),
    CompilerAdapter: ("compile",),
    EnrichmentAdapter: ("enrich",),
    GraphAdapter: ("build",),
    RetrievalAdapter: ("retrieve",),
    RerankerAdapter: ("rerank",),
    LLMProviderAdapter: ("generate",),
    EmbeddingProviderAdapter: ("embed", "dimension"),
    VisionProviderAdapter: ("analyze",),
    OutputFormatter: ("format",),
    EvaluationAdapter: ("evaluate",),
    DomainPolicy: ("should_index", "requires_review", "redact"),
}


@pytest.mark.parametrize("contract", CONTRACTS)
def test_each_contract_is_a_protocol(contract):
    """Every contract is a Protocol class and is @runtime_checkable.

 `_is_protocol` / `_is_runtime_protocol` are CPython-internal but
 are the documented way to introspect Protocol classes.
 """
    assert getattr(contract, "_is_protocol", False), (
        f"{contract.__name__} is not a Protocol class"
    )
    assert getattr(contract, "_is_runtime_protocol", False), (
        f"{contract.__name__} must be decorated @runtime_checkable so "
        f"deployment code can isinstance-check adapters"
    )
    # `kind` is declared as an annotation on every Protocol; it's not
    # a concrete class attribute (Protocol annotations describe the
    # required *implementation* shape).
    assert "kind" in contract.__annotations__, (
        f"{contract.__name__} must declare `kind` annotation"
    )


@pytest.mark.parametrize(
    "contract,methods",
    [(c, m) for c, m in METHOD_NAMES.items()],
)
def test_each_contract_declares_expected_methods(contract, methods):
    for name in methods:
        assert hasattr(contract, name), (
            f"{contract.__name__} is missing method {name!r}"
        )


def test_kinds_are_documented_strings():
    """Every Protocol's `kind` annotation is typed as `str`.

 With `from __future__ import annotations` (PEP 563), annotations
 are stored as their source-string form; verify against either
 the type or the literal string.
 """
    for contract in CONTRACTS:
        annotation = contract.__annotations__.get("kind")
        assert annotation in (str, "str"), (
            f"{contract.__name__}.kind annotation must be `str`, got {annotation!r}"
        )


# ---- Mock adapters satisfy the contracts via duck typing -----------


def test_mocks_satisfy_their_contracts():
    """Each mock should be `isinstance` of its declared contract.

 Protocols with `@runtime_checkable` allow this.
 """
    pairs = [
        (MockSourceConnector(), SourceConnector),
        (MockCompilerAdapter(), CompilerAdapter),
        (MockEnrichmentAdapter(), EnrichmentAdapter),
        (MockGraphAdapter(), GraphAdapter),
        (MockRetrievalAdapter(), RetrievalAdapter),
        (MockRerankerAdapter(), RerankerAdapter),
        (MockLLMProviderAdapter(), LLMProviderAdapter),
        (MockEmbeddingProviderAdapter(), EmbeddingProviderAdapter),
        (MockVisionProviderAdapter(), VisionProviderAdapter),
        (MockOutputFormatter(), OutputFormatter),
        (MockEvaluationAdapter(), EvaluationAdapter),
    ]
    for instance, contract in pairs:
        assert isinstance(instance, contract), (
            f"{type(instance).__name__} does not satisfy "
            f"{contract.__name__} runtime check"
        )


def test_legacy_protocols_satisfied_by_new_mocks_via_duck_typing():
    """A class built against the new extension contracts also satisfies
 the legacy core Protocols (`KnowledgeCompiler`, `EnrichmentProcessor`,
 `GraphBuilder`) — via duck typing.

 The legacy Protocols are NOT decorated `@runtime_checkable`, so
 `isinstance` checks against them raise. We verify the structural
 contract by checking the methods exist with the expected names.
 """
    mock_compiler = MockCompilerAdapter()
    mock_enricher = MockEnrichmentAdapter()
    mock_graph = MockGraphAdapter()

    # `KnowledgeCompiler` shape: kind: str + compile(ctx, document_id)
    assert isinstance(mock_compiler.kind, str) and mock_compiler.kind
    assert callable(mock_compiler.compile)

    # `EnrichmentProcessor` shape: kind: str + enrich(ctx, artifact_id)
    assert isinstance(mock_enricher.kind, str) and mock_enricher.kind
    assert callable(mock_enricher.enrich)

    # `GraphBuilder` shape: kind: str + build(ctx, artifact_ids)
    assert isinstance(mock_graph.kind, str) and mock_graph.kind
    assert callable(mock_graph.build)
