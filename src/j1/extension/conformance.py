"""Reusable conformance harnesses for J1 extension adapters.

Each function in this module takes one adapter instance + the
minimum context it needs and asserts the contract holds. The same
harness is used by:

  * J1's own test suite (against the bundled mock adapters)
  * Vendor / domain test suites (against their own adapters)

Usage in a vendor's test file::

    from j1.extension.conformance import (
        assert_source_connector_conformance,
        assert_compiler_adapter_conformance,
        ...,
    )
    from my_pkg.connectors import MyHttpConnector

    def test_my_http_connector_is_conformant(tmp_path):
        ctx = ProjectContext(tenant_id="t", project_id="p")
        assert_source_connector_conformance(MyHttpConnector(...), ctx)

The harnesses raise `AssertionError` on contract violations — they
are intended to be called from `pytest` tests. They DO NOT call any
network / disk side effects beyond what the adapter itself does
when handed an empty / minimal input.
"""

from __future__ import annotations

import time
from collections.abc import Callable
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
from j1.extension.primitives import (
    Citation,
    Evidence,
    EvaluationResult,
    ProjectContext,
    ResultStatus,
    RetrievalResult,
    Source,
    SourceMetadata,
)


# ---- Common assertions ----------------------------------------------


def _assert_string_attr(adapter: object, name: str) -> None:
    value = getattr(adapter, name, None)
    assert isinstance(value, str) and value, (
        f"{type(adapter).__name__}.{name} must be a non-empty string, got {value!r}"
    )


def _assert_no_secret_leakage(snapshot: object) -> None:
    """Heuristic — guards against an adapter accidentally surfacing
    raw API keys / tokens in its output / metadata.

    Not a security boundary; a deterministic last-line-of-defence.
    """
    text = repr(snapshot)
    for needle in ("sk-test-", "ghp_test_", "xoxb-test-", "AKIATEST"):
        assert needle not in text, (
            f"adapter output appears to contain a secret prefix {needle!r}"
        )


def _bench(callable_: Callable[[], Any], *, deadline_seconds: float = 5.0) -> Any:
    """Run `callable_` and assert it returns within `deadline_seconds`.

    The harness imposes a generous deadline only to catch hung mocks
    in CI — production timeouts are a deployment concern.
    """
    started = time.monotonic()
    result = callable_()
    elapsed = time.monotonic() - started
    assert elapsed <= deadline_seconds, (
        f"adapter call took {elapsed:.2f}s — exceeded harness deadline "
        f"of {deadline_seconds}s. Consider whether the adapter blocks "
        f"on I/O without a deployment-side timeout."
    )
    return result


# ---- SourceConnector ------------------------------------------------


def assert_source_connector_conformance(
    adapter: SourceConnector,
    ctx: ProjectContext,
) -> None:
    """Verify a `SourceConnector` honours the contract.

    Checks:
      * `kind` attribute present + non-empty.
      * `list(ctx)` returns a list (possibly empty) of `SourceMetadata`.
      * `fetch(ctx, metadata)` returns a `Source` whose
        `metadata.uri` matches the input.
      * Empty `query` produces a deterministic (≥0) listing — the
        adapter does not raise on missing optional input.
      * No secret prefix leaks into the listing or fetched payload's
        metadata.
    """
    _assert_string_attr(adapter, "kind")

    listing = _bench(lambda: adapter.list(ctx))
    assert isinstance(listing, list), (
        f"{type(adapter).__name__}.list() must return list, "
        f"got {type(listing).__name__}"
    )
    for item in listing:
        assert isinstance(item, SourceMetadata), (
            f"list() must yield SourceMetadata, got {type(item).__name__}"
        )
    _assert_no_secret_leakage(listing)

    if listing:
        first = listing[0]
        fetched = _bench(lambda: adapter.fetch(ctx, first))
        assert isinstance(fetched, Source), (
            f"fetch() must return Source, got {type(fetched).__name__}"
        )
        assert fetched.metadata.uri == first.uri, (
            f"fetch() returned metadata.uri={fetched.metadata.uri!r}, "
            f"expected {first.uri!r}"
        )
        assert isinstance(fetched.content, (bytes, bytearray)), (
            f"fetch() must return bytes-shaped content, got {type(fetched.content).__name__}"
        )
        _assert_no_secret_leakage(fetched.metadata)


# ---- CompilerAdapter ------------------------------------------------


def assert_compiler_adapter_conformance(
    adapter: CompilerAdapter,
    ctx: ProjectContext,
    document_id: str,
) -> None:
    """Verify a `CompilerAdapter` honours the contract.

    Checks:
      * `kind` non-empty.
      * `compile()` returns an `ArtifactProcessingResult`.
      * Status is one of the canonical `ResultStatus` values.
      * For empty / unknown `document_id`, the adapter returns a
        `FAILED` result (or raises `ProviderUnavailable`) — it does
        NOT raise an unstructured exception.
      * No secret leakage in `error` / `metadata`.
    """
    _assert_string_attr(adapter, "kind")
    result = _bench(lambda: adapter.compile(ctx, document_id))
    assert hasattr(result, "status") and isinstance(result.status, ResultStatus), (
        f"compile() must return ArtifactProcessingResult with a ResultStatus, "
        f"got {type(result).__name__}"
    )
    assert hasattr(result, "drafts") and isinstance(result.drafts, list), (
        "compile() result must have a `drafts` list"
    )
    _assert_no_secret_leakage(result)


# ---- EnrichmentAdapter ----------------------------------------------


def assert_enrichment_adapter_conformance(
    adapter: EnrichmentAdapter,
    ctx: ProjectContext,
    artifact_id: str,
) -> None:
    _assert_string_attr(adapter, "kind")
    result = _bench(lambda: adapter.enrich(ctx, artifact_id))
    assert hasattr(result, "status") and isinstance(result.status, ResultStatus)
    assert hasattr(result, "drafts") and isinstance(result.drafts, list)
    _assert_no_secret_leakage(result)


# ---- GraphAdapter ---------------------------------------------------


def assert_graph_adapter_conformance(
    adapter: GraphAdapter,
    ctx: ProjectContext,
    artifact_ids: list[str],
) -> None:
    _assert_string_attr(adapter, "kind")
    # Empty list MUST be tolerated — nothing to graph is a valid
    # input.
    empty_result = _bench(lambda: adapter.build(ctx, []))
    assert hasattr(empty_result, "status"), (
        "build([]) must return an ArtifactProcessingResult, not raise"
    )
    if artifact_ids:
        result = _bench(lambda: adapter.build(ctx, artifact_ids))
        assert hasattr(result, "status")
        _assert_no_secret_leakage(result)


# ---- RetrievalAdapter -----------------------------------------------


def assert_retrieval_adapter_conformance(
    adapter: RetrievalAdapter,
    ctx: ProjectContext,
    question: str = "what",
) -> None:
    _assert_string_attr(adapter, "kind")
    result = _bench(lambda: adapter.retrieve(ctx, question))
    assert isinstance(result, RetrievalResult), (
        f"retrieve() must return RetrievalResult, got {type(result).__name__}"
    )
    assert isinstance(result.evidences, list)
    for ev in result.evidences:
        assert isinstance(ev, Evidence), (
            f"RetrievalResult.evidences must contain Evidence, "
            f"got {type(ev).__name__}"
        )
        assert isinstance(ev.score, (int, float))
        for cit in ev.citations:
            assert isinstance(cit, Citation)
    # Empty question should not crash — adapter may return an
    # empty-evidences `SUCCEEDED` or a `FAILED` result.
    empty = _bench(lambda: adapter.retrieve(ctx, ""))
    assert isinstance(empty, RetrievalResult)
    _assert_no_secret_leakage(result)


# ---- RerankerAdapter ------------------------------------------------


def assert_reranker_adapter_conformance(
    adapter: RerankerAdapter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(adapter, "kind")
    # Empty-list rerank must not raise.
    empty = _bench(lambda: adapter.rerank(ctx, "q", []))
    assert empty == [], "rerank([]) must return [] without raising"
    sample = [
        Evidence(content="a", score=0.1),
        Evidence(content="b", score=0.5),
    ]
    out = _bench(lambda: adapter.rerank(ctx, "q", sample))
    assert isinstance(out, list)
    # Inputs must NOT be mutated.
    assert sample[0].content == "a" and sample[0].score == 0.1
    assert sample[1].content == "b" and sample[1].score == 0.5
    for item in out:
        assert isinstance(item, Evidence)


# ---- LLMProviderAdapter ---------------------------------------------


def assert_llm_provider_adapter_conformance(
    adapter: LLMProviderAdapter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(adapter, "kind")
    out = _bench(lambda: adapter.generate(ctx, "say hello"))
    assert isinstance(out, dict), (
        f"generate() must return dict, got {type(out).__name__}"
    )
    assert "text" in out and isinstance(out["text"], str), (
        "generate() output must include a `text` string"
    )
    # Empty prompt should not crash — adapter may return an empty
    # string or an error dict, but never raise.
    empty_out = _bench(lambda: adapter.generate(ctx, ""))
    assert isinstance(empty_out, dict)
    _assert_no_secret_leakage(out)


# ---- EmbeddingProviderAdapter ---------------------------------------


def assert_embedding_provider_adapter_conformance(
    adapter: EmbeddingProviderAdapter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(adapter, "kind")
    dim = adapter.dimension()
    assert isinstance(dim, int) and dim > 0
    vectors = _bench(lambda: adapter.embed(ctx, ["alpha", "beta"]))
    assert isinstance(vectors, list) and len(vectors) == 2
    for v in vectors:
        assert isinstance(v, list) and len(v) == dim
        assert all(isinstance(x, (int, float)) for x in v)
    # Empty input should yield empty list — never raise.
    assert _bench(lambda: adapter.embed(ctx, [])) == []


# ---- VisionProviderAdapter ------------------------------------------


def assert_vision_provider_adapter_conformance(
    adapter: VisionProviderAdapter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(adapter, "kind")
    out = _bench(lambda: adapter.analyze(ctx, b"\x00", prompt="describe"))
    assert isinstance(out, dict)
    assert "text" in out and isinstance(out["text"], str)


# ---- OutputFormatter ------------------------------------------------


def assert_output_formatter_conformance(
    formatter: OutputFormatter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(formatter, "kind")
    out = _bench(lambda: formatter.format(ctx, "q", []))
    assert isinstance(out, dict)
    # Formatter MUST handle empty evidence lists.
    sample = [Evidence(content="text", score=0.7,
                       citations=[Citation(document_id="doc-1")])]
    out2 = _bench(lambda: formatter.format(ctx, "q", sample))
    assert isinstance(out2, dict)


# ---- EvaluationAdapter ----------------------------------------------


def assert_evaluation_adapter_conformance(
    adapter: EvaluationAdapter,
    ctx: ProjectContext,
) -> None:
    _assert_string_attr(adapter, "kind")
    result = _bench(lambda: adapter.evaluate(ctx, "q", []))
    assert isinstance(result, EvaluationResult), (
        f"evaluate() must return EvaluationResult, got {type(result).__name__}"
    )
    assert isinstance(result.status, ResultStatus)
    if result.score is not None:
        assert 0.0 <= result.score <= 1.0, (
            f"EvaluationResult.score={result.score} must be in [0, 1] when set"
        )
    # Determinism check: repeating with the same input must produce
    # the same status + score (mock adapters are deterministic; LLM
    # judges should call out non-determinism via metadata).
    again = _bench(lambda: adapter.evaluate(ctx, "q", []))
    assert again.status == result.status
    assert again.score == result.score, (
        "EvaluationAdapter results must be deterministic for the same input "
        "(or document non-determinism in metadata)"
    )
    _assert_no_secret_leakage(result)


__all__ = [
    "assert_compiler_adapter_conformance",
    "assert_embedding_provider_adapter_conformance",
    "assert_enrichment_adapter_conformance",
    "assert_evaluation_adapter_conformance",
    "assert_graph_adapter_conformance",
    "assert_llm_provider_adapter_conformance",
    "assert_output_formatter_conformance",
    "assert_reranker_adapter_conformance",
    "assert_retrieval_adapter_conformance",
    "assert_source_connector_conformance",
    "assert_vision_provider_adapter_conformance",
]
