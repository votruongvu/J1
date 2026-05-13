"""Retrieval-route adapter tests.

The adapters wrap existing backends (RAGAnything, SqliteSearchIndexer,
ArtifactRegistry) and project their results into the orchestrator's
``EvidenceCandidate`` shape. Tests use in-memory stubs — the
adapters MUST stay small enough to test without spinning up real
infra."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.projects.context import ProjectContext
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalJob,
    RetrievalRouteKind,
)
from j1.query.retrieval_routes import (
    ArtifactLookupAdapter,
    BM25Adapter,
    RAGAnythingAdapter,
    RouteContext,
    RouteRunner,
)
from j1.query.scope import RunScope, WorkspaceScope


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


@pytest.fixture
def route_ctx(ctx: ProjectContext) -> RouteContext:
    return RouteContext(
        ctx=ctx,
        scope=RunScope(run_id="run-1"),
        eligible_run_ids=frozenset({"run-1"}),
        document_id="doc-1",
        run_id="run-1",
    )


# ---- RAGAnything adapter -----------------------------------------


@dataclass
class _StubRAGResult:
    answer: str
    citations: list[str]
    metadata: dict[str, Any]


class _StubRAGProvider:
    """Stand-in for ``RAGAnythingQueryProvider``. Returns whatever
    we hand it; tests check the adapter projection."""

    def __init__(self, result: _StubRAGResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def query(self, ctx, question, *, max_results, document_id, run_id):
        self.calls.append({
            "question": question,
            "max_results": max_results,
            "document_id": document_id,
            "run_id": run_id,
        })
        return self.result


def test_raganything_adapter_projects_evidence_chunks(route_ctx):
    """The adapter must ignore the native answer and return one
    EvidenceCandidate per evidence_chunk in the metadata. Native
    answer is advisory only."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="LightRAG says: not in retrieved evidence.",  # ignored
        citations=["a1", "a2"],
        metadata={
            "evidence_chunks": [
                {"artifact_id": "a1", "artifact_kind": "chunk",
                 "chunk_id": "c1", "text": "60% design deliverables include drawings.",
                 "score": 0.91, "run_id": "run-1", "document_id": "doc-1",
                 "section_path": "Sec 3.2"},
                {"artifact_id": "a2", "artifact_kind": "chunk",
                 "chunk_id": "c2", "text": "100% design cost estimate class 1.",
                 "score": 0.88, "run_id": "run-1", "document_id": "doc-1",
                 "section_path": "Sec 4.1"},
            ],
        },
    ))
    adapter = RAGAnythingAdapter(provider)
    job = RetrievalJob(
        route=RetrievalRouteKind.RAGANYTHING,
        query="deliverables 60% 90% 100% design cost estimate",
        max_results=10,
        label="primary",
    )
    candidates = adapter.execute(job, route_ctx)
    assert len(candidates) == 2
    assert candidates[0].artifact_id == "a1"
    assert candidates[0].route == RetrievalRouteKind.RAGANYTHING
    assert candidates[0].chunk_id == "c1"
    assert "60% design" in candidates[0].text_preview
    # The full body lands in extra so the synthesizer can read it.
    assert candidates[0].extra["body"].startswith("60% design")
    # The provider got the per-run scope (so workspace path is
    # correct).
    assert provider.calls[0]["run_id"] == "run-1"
    assert provider.calls[0]["document_id"] == "doc-1"


def test_raganything_adapter_falls_back_to_citations(route_ctx):
    """When the bridge didn't surface evidence_chunks, the adapter
    falls back to citation strings — one candidate per citation
    with empty body. Better to surface lineage than nothing."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="x",
        citations=["a1", "a2"],
        metadata={},
    ))
    adapter = RAGAnythingAdapter(provider)
    job = RetrievalJob(
        route=RetrievalRouteKind.RAGANYTHING,
        query="q", max_results=5,
    )
    candidates = adapter.execute(job, route_ctx)
    assert len(candidates) == 2
    assert {c.artifact_id for c in candidates} == {"a1", "a2"}
    # No bodies in this path → text_preview is empty.
    assert all(c.text_preview == "" for c in candidates)


def test_raganything_adapter_prefers_chunks_over_native_answer(route_ctx):
    """When evidence_chunks is non-empty, the native answer is NOT
    surfaced. Real chunks always win."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="A WHOLE PROSE ANSWER",
        citations=[],
        metadata={"evidence_chunks": [
            {"artifact_id": "a1", "artifact_kind": "chunk",
             "text": "real chunk body", "score": 0.9},
        ]},
    ))
    adapter = RAGAnythingAdapter(provider)
    candidates = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        route_ctx,
    )
    assert len(candidates) == 1
    assert candidates[0].artifact_id == "a1"
    # The native prose answer is not turned into a candidate when
    # real chunks exist.
    assert not any(
        "A WHOLE PROSE ANSWER" in (c.text_preview or "")
        for c in candidates
    )


def test_raganything_adapter_surfaces_native_answer_as_advisory(route_ctx):
    """When the bridge returns ONLY a prose answer (the current
    default — chunks/citations not surfaced), we expose the answer
    as a SINGLE advisory candidate so the trace shows what
    LightRAG-native produced. Marked ``advisory_only=True`` so the
    evidence builder can downrank it when real evidence exists."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="Native LightRAG produced this answer.",
        citations=[],
        metadata={"evidence_chunks": []},
    ))
    adapter = RAGAnythingAdapter(provider)
    candidates = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        route_ctx,
    )
    assert len(candidates) == 1
    assert candidates[0].artifact_kind == "raganything.native_answer"
    assert candidates[0].extra["advisory_only"] is True
    assert candidates[0].extra["raganything_native_answer"] is True
    assert "Native LightRAG" in candidates[0].text_preview


def test_raganything_adapter_marks_empty_response_for_diagnostics(route_ctx):
    """Empty bridge response surfaces a single ``empty_response``
    marker candidate (score 0.0) so the trace shows the route ran
    but RAGAnything returned nothing. Operators need this to
    distinguish "route didn't run" from "route ran, got no data"."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="",
        citations=[],
        metadata={"working_dir": "/var/lib/j1/raganything/runs/x"},
    ))
    adapter = RAGAnythingAdapter(provider)
    candidates = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        route_ctx,
    )
    assert len(candidates) == 1
    assert candidates[0].artifact_kind == "raganything.empty_response"
    assert candidates[0].score == 0.0
    assert (
        candidates[0].extra["raganything_working_dir"]
        == "/var/lib/j1/raganything/runs/x"
    )


# ---- BM25 adapter ------------------------------------------------


@dataclass
class _StubSearchHit:
    artifact_id: str
    artifact_type: str
    title: str = ""
    source_document_id: str | None = None
    source_location: str | None = None
    confidence: float = 1.0
    review_status: str = "not_required"
    checksum: str = ""
    created_at: str = ""
    byte_size: int = 0
    extracted_text: str = ""
    score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    chunk_id: str | None = None


class _StubIndexer:
    def __init__(self, hits: list[_StubSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def search(
        self, ctx, query, *,
        artifact_types=None, max_results=20,
        scope=None, eligible_run_ids=None,
    ):
        self.calls.append({
            "query": query,
            "artifact_types": artifact_types,
            "max_results": max_results,
            "scope": scope,
            "eligible_run_ids": eligible_run_ids,
        })
        return self.hits


def test_bm25_adapter_surfaces_lexical_hits(route_ctx):
    """BM25 returns one candidate per SearchHit. Route metadata is
    BM25 so the orchestrator can mark the answer as BM25-assisted
    if it lands in synthesis evidence."""
    indexer = _StubIndexer([
        _StubSearchHit(
            artifact_id="a3", artifact_type="chunk",
            extracted_text="60% design submittal includes geotech report.",
            score=12.4, run_id="run-1", chunk_id="c3",
            source_document_id="doc-1",
        ),
    ])
    adapter = BM25Adapter(indexer)
    job = RetrievalJob(
        route=RetrievalRouteKind.BM25,
        query="60% design",
        max_results=5,
        label="bm25_anchor:60% design",
    )
    candidates = adapter.execute(job, route_ctx)
    assert len(candidates) == 1
    assert candidates[0].route == RetrievalRouteKind.BM25
    assert candidates[0].score == 12.4
    assert "60% design" in candidates[0].matched_anchors
    # Eligibility gate threaded through.
    assert indexer.calls[0]["eligible_run_ids"] == frozenset({"run-1"})


def test_bm25_adapter_passes_artifact_kind_filter(route_ctx):
    indexer = _StubIndexer([])
    adapter = BM25Adapter(indexer)
    adapter.execute(
        RetrievalJob(
            route=RetrievalRouteKind.BM25,
            query="req",
            filters={"artifact_kind": "enriched.requirements"},
        ),
        route_ctx,
    )
    assert indexer.calls[0]["artifact_types"] == [
        "enriched.requirements",
    ]


def test_bm25_adapter_workspace_scope_does_not_pass_run_filter(ctx):
    """The legacy diagnostic path uses WorkspaceScope + no eligibility
    set — verify the adapter forwards the scope without forcing a
    run filter."""
    indexer = _StubIndexer([])
    adapter = BM25Adapter(indexer)
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.BM25, query="q"),
        RouteContext(ctx=ctx, scope=WorkspaceScope()),
    )
    assert indexer.calls[0]["eligible_run_ids"] is None


# ---- Artifact-lookup adapter -------------------------------------


@dataclass
class _StubArtifact:
    artifact_id: str
    kind: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_document_ids: list[str] = field(default_factory=list)


class _StubArtifactRegistry:
    def __init__(self, records: list[_StubArtifact]) -> None:
        self.records = records

    def list_artifacts(self, ctx):
        return self.records


def test_artifact_lookup_filters_by_kind_and_run(route_ctx):
    """The route honors run scope — artifacts produced by a
    different run must NOT surface."""
    records = [
        _StubArtifact(
            artifact_id="r1",
            kind="enriched.requirements",
            metadata={"run_id": "run-1"},
            source_document_ids=["doc-1"],
        ),
        _StubArtifact(
            artifact_id="r2",
            kind="enriched.requirements",
            # Different run — must be filtered out.
            metadata={"run_id": "run-OTHER"},
        ),
        _StubArtifact(
            artifact_id="r3",
            kind="enriched.risks",  # wrong kind
            metadata={"run_id": "run-1"},
        ),
    ]
    adapter = ArtifactLookupAdapter(
        _StubArtifactRegistry(records),
        body_loader=lambda r: f"body for {r.artifact_id}",
    )
    job = RetrievalJob(
        route=RetrievalRouteKind.ARTIFACT_LOOKUP,
        query="x",
        max_results=10,
        filters={"artifact_kind": "enriched.requirements"},
    )
    candidates = adapter.execute(job, route_ctx)
    assert [c.artifact_id for c in candidates] == ["r1"]
    assert candidates[0].extra["body"] == "body for r1"


def test_artifact_lookup_returns_empty_when_no_kind_filter(route_ctx):
    """Without an artifact_kind filter the route has nothing to
    look up — return empty rather than dumping the whole registry."""
    adapter = ArtifactLookupAdapter(_StubArtifactRegistry([]))
    out = adapter.execute(
        RetrievalJob(
            route=RetrievalRouteKind.ARTIFACT_LOOKUP, query="x",
        ),
        route_ctx,
    )
    assert out == []


# ---- RouteRunner --------------------------------------------------


class _RaisingRoute:
    kind = RetrievalRouteKind.BM25

    def execute(self, job, ctx):
        raise RuntimeError("backend gone")


def test_runner_records_route_failures_without_aborting(route_ctx):
    """One failed route must not kill the whole query. The runner
    records the failure on the trace and proceeds."""
    runner = RouteRunner({
        RetrievalRouteKind.BM25: _RaisingRoute(),
    })
    records = runner.run_all(
        (
            RetrievalJob(route=RetrievalRouteKind.BM25, query="q",
                         label="bm25_anchor:x"),
        ),
        route_ctx,
    )
    assert len(records) == 1
    assert records[0].error is not None
    assert "backend gone" in records[0].error
    assert records[0].candidates == ()


def test_runner_reports_missing_adapter(route_ctx):
    """A plan that asks for a route the runner doesn't have wired
    yields an explicit error, not a silent skip."""
    runner = RouteRunner({})
    records = runner.run_all(
        (RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),),
        route_ctx,
    )
    assert records[0].error is not None
    assert "no adapter registered" in records[0].error


def test_runner_dispatches_all_jobs(route_ctx):
    indexer = _StubIndexer([
        _StubSearchHit(
            artifact_id="a", artifact_type="chunk",
            extracted_text="x", score=1.0, run_id="run-1",
        ),
    ])
    runner = RouteRunner({
        RetrievalRouteKind.BM25: BM25Adapter(indexer),
    })
    records = runner.run_all(
        (
            RetrievalJob(route=RetrievalRouteKind.BM25, query="a"),
            RetrievalJob(route=RetrievalRouteKind.BM25, query="b"),
            RetrievalJob(route=RetrievalRouteKind.BM25, query="c"),
        ),
        route_ctx,
    )
    assert len(records) == 3
    assert all(r.error is None for r in records)
    assert all(len(r.candidates) == 1 for r in records)
