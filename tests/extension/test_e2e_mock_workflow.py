"""End-to-end mock workflow over registered adapters.

This test demonstrates the acceptance criterion:

 > A mock end-to-end workflow can run using only registered mock
 > adapters.

The workflow shape mirrors the generic shape documented in the
extension guide:

 fetch source → compile → enrich (optional) → build graph (optional)
 → retrieve evidence → format output → evaluate result → return.

The orchestration is done in plain Python over the registry — no
Temporal, no real I/O. The point is to prove the adapter contracts
are sufficient on their own to drive a complete pipeline.
"""

from __future__ import annotations

from typing import Any

from j1.extension import (
    AdapterManifest,
    CapabilityRegistry,
    Citation,
    Evidence,
    ProjectContext,
    Source,
    SourceMetadata,
    EvaluationResult,
    RetrievalResult,
)
from j1.extension.mocks import (
    MockCompilerAdapter,
    MockEnrichmentAdapter,
    MockEvaluationAdapter,
    MockGraphAdapter,
    MockOutputFormatter,
    MockRerankerAdapter,
    MockRetrievalAdapter,
    MockSourceConnector,
)
from j1.extension.primitives import ResultStatus


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _build_registry() -> CapabilityRegistry:
    """Wire one of every adapter we need into a fresh registry."""
    reg = CapabilityRegistry()

    sources = [
        Source(content=b"hello world", metadata=SourceMetadata(uri="mem://1")),
        Source(content=b"hello again", metadata=SourceMetadata(uri="mem://2")),
    ]
    connector = MockSourceConnector(sources=sources)
    reg.register(connector.MANIFEST, connector, role="primary-source")

    compiler = MockCompilerAdapter()
    reg.register(compiler.MANIFEST, compiler, role="primary-compile")

    enricher = MockEnrichmentAdapter()
    reg.register(enricher.MANIFEST, enricher, role="primary-enrich")

    grapher = MockGraphAdapter()
    reg.register(grapher.MANIFEST, grapher, role="primary-graph")

    retrieval_corpus = [
        Evidence(content="hello world", score=0.0,
                 citations=[Citation(document_id="d-1", locator="0")]),
        Evidence(content="goodbye world", score=0.0,
                 citations=[Citation(document_id="d-2", locator="0")]),
    ]
    retriever = MockRetrievalAdapter(corpus=retrieval_corpus)
    reg.register(retriever.MANIFEST, retriever, role="primary-retrieve")

    reranker = MockRerankerAdapter()
    reg.register(reranker.MANIFEST, reranker, role="primary-rerank")

    formatter = MockOutputFormatter()
    reg.register(formatter.MANIFEST, formatter, role="primary-format")

    evaluator = MockEvaluationAdapter(threshold=0.0)
    reg.register(evaluator.MANIFEST, evaluator, role="primary-evaluate")

    return reg


def _resolve_role(reg: CapabilityRegistry, role: str):
    entries = reg.find_by_role(role)
    assert entries, f"no adapter registered for role {role!r}"
    return entries[0].adapter


def test_mock_end_to_end_workflow_runs_via_registry():
    """Drive a full pipeline by reading adapters from the registry only.

 No imports of concrete mock classes inside the orchestration —
 everything goes through the registry, demonstrating the
 extension-driven workflow shape.
 """
    ctx = _ctx()
    reg = _build_registry()

    # Step 1: fetch sources
    connector = _resolve_role(reg, "primary-source")
    listed = connector.list(ctx)
    assert len(listed) == 2
    fetched: list[Source] = [connector.fetch(ctx, m) for m in listed]
    assert all(isinstance(s, Source) for s in fetched)

    # Step 2: compile each fetched source (we use the URI as a stand-in
    # for document_id; in a real pipeline DocumentIntakeService would
    # mint canonical ids first).
    compiler = _resolve_role(reg, "primary-compile")
    compile_results = [
        compiler.compile(ctx, source.metadata.uri) for source in fetched
    ]
    assert all(r.status is ResultStatus.SUCCEEDED for r in compile_results)
    artifact_ids = [
        f"art-{i}" for i, _ in enumerate(compile_results)
    ]

    # Step 3: enrich
    enricher = _resolve_role(reg, "primary-enrich")
    enrich_results = [enricher.enrich(ctx, aid) for aid in artifact_ids]
    assert all(r.status is ResultStatus.SUCCEEDED for r in enrich_results)

    # Step 4: build graph
    grapher = _resolve_role(reg, "primary-graph")
    graph_result = grapher.build(ctx, artifact_ids)
    assert graph_result.status is ResultStatus.SUCCEEDED
    assert any(d.kind == "graph_json" for d in graph_result.drafts)

    # Step 5: retrieve evidence
    retriever = _resolve_role(reg, "primary-retrieve")
    retrieval = retriever.retrieve(ctx, "hello", max_results=5)
    assert isinstance(retrieval, RetrievalResult)
    assert retrieval.status is ResultStatus.SUCCEEDED
    assert len(retrieval.evidences) >= 1

    # Step 6: rerank
    reranker = _resolve_role(reg, "primary-rerank")
    reranked = reranker.rerank(ctx, "hello", retrieval.evidences, max_results=3)
    assert len(reranked) <= 3
    # Score-descending invariant
    scores = [e.score for e in reranked]
    assert scores == sorted(scores, reverse=True)

    # Step 7: format output
    formatter = _resolve_role(reg, "primary-format")
    output = formatter.format(ctx, "hello", reranked)
    assert isinstance(output, dict)
    assert output["question"] == "hello"
    assert output["answer"] is not None

    # Step 8: evaluate
    evaluator = _resolve_role(reg, "primary-evaluate")
    eval_result: EvaluationResult = evaluator.evaluate(ctx, "hello", reranked)
    assert eval_result.status is ResultStatus.SUCCEEDED
    assert eval_result.passed is True

    # The structured output the workflow returns
    final: dict[str, Any] = {
        "output": output,
        "evaluation": {
            "passed": eval_result.passed,
            "score": eval_result.score,
        },
    }
    assert final["output"]["answer"] is not None
    assert final["evaluation"]["passed"] is True


def test_workflow_resilient_to_missing_optional_steps():
    """Compile + retrieve + format alone is a valid degenerate flow.

 Demonstrates that the workflow shape is composable: skipping
 enrich / graph / rerank / evaluate is still a coherent pipeline
 if the deployment hasn't registered those roles.
 """
    ctx = _ctx()
    reg = CapabilityRegistry()

    compiler = MockCompilerAdapter()
    reg.register(compiler.MANIFEST, compiler, role="primary-compile")

    retriever = MockRetrievalAdapter(corpus=[
        Evidence(content="hello", score=0.0,
                 citations=[Citation(document_id="d-1")]),
    ])
    reg.register(retriever.MANIFEST, retriever, role="primary-retrieve")

    formatter = MockOutputFormatter()
    reg.register(formatter.MANIFEST, formatter, role="primary-format")

    # Driver: only resolve the roles we actually need.
    compile_result = _resolve_role(reg, "primary-compile").compile(ctx, "doc-1")
    assert compile_result.status is ResultStatus.SUCCEEDED

    retrieval = _resolve_role(reg, "primary-retrieve").retrieve(ctx, "hello")
    assert retrieval.status is ResultStatus.SUCCEEDED

    out = _resolve_role(reg, "primary-format").format(ctx, "hello", retrieval.evidences)
    assert out["answer"] == "hello"
