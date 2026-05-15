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

    def query(
        self, ctx, question, *,
        max_results, document_id, run_id, snapshot_id=None,
    ):
        self.calls.append({
            "question": question,
            "max_results": max_results,
            "document_id": document_id,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
        })
        return self.result


def _pairs_resolver(*pairs: tuple[str, str]):
    """Build an ``eligible_snapshot_pairs_resolver`` for tests that
    don't care about the resolver mechanics — they just need the
    adapter to fan out against a known ``(document_id, snapshot_id)``
    pair so the provider's ``query`` gets called."""
    frozen = frozenset(pairs)
    return lambda ctx, scope: frozen


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
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(("doc-1", "snap-1")),
    )
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
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(("doc-1", "snap-1")),
    )
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
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(("doc-1", "snap-1")),
    )
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
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(("doc-1", "snap-1")),
    )
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
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(("doc-1", "snap-1")),
    )
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


# ---- RAGAnything snapshot-scope guarantees (audit-fix) ----------


def test_raganything_adapter_passes_snapshot_id_to_provider(route_ctx):
    """The adapter MUST pass ``snapshot_id`` (not just ``run_id``) into
    the provider's query call. The bridge keys its working_dir on
    snapshot_id; without it the bridge raises ``WorkspaceScopeMissing``
    and the query silently falls back to global."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="ok", citations=[], metadata={},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(
            ("doc-1", "snap-active"),
        ),
    )
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        route_ctx,
    )
    assert len(provider.calls) == 1
    assert provider.calls[0]["snapshot_id"] == "snap-active"
    assert provider.calls[0]["document_id"] == "doc-1"


def test_raganything_adapter_fans_out_per_eligible_snapshot(ctx):
    """Project-wide query with two attached documents → one provider
    call per ``(document_id, snapshot_id)`` pair. Each call sees the
    correct pair so the bridge resolves the per-snapshot workdir."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={"evidence_chunks": []},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(
            ("doc-A", "snap-A"), ("doc-B", "snap-B"),
        ),
    )
    # Workspace-wide scope, no pinned document_id.
    workspace_ctx = RouteContext(ctx=ctx, scope=WorkspaceScope())
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        workspace_ctx,
    )
    assert len(provider.calls) == 2
    pairs_called = {
        (c["document_id"], c["snapshot_id"]) for c in provider.calls
    }
    assert pairs_called == {("doc-A", "snap-A"), ("doc-B", "snap-B")}


def test_raganything_adapter_refuses_when_no_eligible_snapshot(route_ctx):
    """No resolver → empty pair set → adapter refuses (no provider
    call) and surfaces a marker candidate for the trace. This is the
    fail-closed invariant: the adapter NEVER falls back to a global
    workspace just because eligibility came back empty."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="should never see this", citations=[], metadata={},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(),  # empty
    )
    candidates = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        route_ctx,
    )
    assert provider.calls == []
    assert len(candidates) == 1
    assert candidates[0].artifact_kind == "raganything.no_eligible_snapshot"
    assert candidates[0].score == 0.0


def test_no_eligible_snapshot_message_is_scope_aware_for_run_scope(ctx):
    """When the scope is ``RunScope``, the refusal message must NOT
    mention 'attached documents with an active snapshot' — the user
    explicitly named a run and the project-active eligibility
    predicate is irrelevant. Regression for the Run Detail bug."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(),  # empty
    )
    run_scope_ctx = RouteContext(
        ctx=ctx, scope=RunScope(run_id="run-X"), run_id="run-X",
    )
    [candidate] = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        run_scope_ctx,
    )
    assert candidate.artifact_kind == "raganything.no_eligible_snapshot"
    assert "attached documents" not in candidate.text_preview
    assert "Selected run" in candidate.text_preview
    assert candidate.extra["raganything_refused_reason"] == (
        "no_queryable_run_snapshot"
    )


def test_no_eligible_snapshot_message_is_scope_aware_for_active_scope(ctx):
    """``ActiveScope`` failure says the DOCUMENT has no active
    snapshot — not the project."""
    from j1.query.scope import ActiveScope
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(),
    )
    active_ctx = RouteContext(
        ctx=ctx, scope=ActiveScope(document_id="doc-1"),
        document_id="doc-1",
    )
    [candidate] = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        active_ctx,
    )
    assert "Document has no active snapshot" in candidate.text_preview
    assert candidate.extra["raganything_refused_reason"] == (
        "no_active_document_snapshot"
    )


def test_no_eligible_snapshot_message_is_scope_aware_for_project_scope(ctx):
    """The legacy project-active message stays — but ONLY for
    ``WorkspaceScope``. This is the regression pin so the message
    can't drift back into being scope-blind."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(),
    )
    workspace_ctx = RouteContext(ctx=ctx, scope=WorkspaceScope())
    [candidate] = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        workspace_ctx,
    )
    assert "No attached documents" in candidate.text_preview
    assert candidate.extra["raganything_refused_reason"] == (
        "no_active_project_snapshot"
    )


def test_raganything_adapter_prefers_eligible_snapshot_pairs_over_resolver(ctx):
    """When ``RouteContext.eligible_snapshot_pairs`` is supplied, the
    adapter MUST bypass the scope-driven resolver and fan out against
    the explicit pairs. This is the Run Detail / ``snapshot_explicit``
    bug repro: the resolver only sees ACTIVE snapshots, but the
    caller is validating a CANDIDATE snapshot that hasn't been
    promoted yet — without the bypass the intersection is empty and
    the adapter falsely emits ``no_eligible_snapshot``."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[],
        metadata={"evidence_chunks": [{
            "artifact_id": "a1", "artifact_kind": "chunk", "chunk_id": "c1",
            "text": "candidate snapshot content",
            "score": 0.9, "run_id": "run-X", "document_id": "doc-1",
            "section_path": "Sec 1",
        }]},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        # Resolver returns ONLY the active snapshot (snap-active),
        # mirroring `_resolve_workspace_scope` over a doc whose
        # active_snapshot_id is the previous run's output.
        eligible_snapshot_pairs_resolver=_pairs_resolver(
            ("doc-1", "snap-active"),
        ),
    )
    # Caller (validation service for `snapshot_explicit`) hands the
    # adapter the candidate snapshot pair directly.
    ctx_with_explicit_pairs = RouteContext(
        ctx=ctx,
        scope=WorkspaceScope(),
        eligible_snapshot_pairs=frozenset({("doc-1", "snap-candidate")}),
    )
    candidates = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        ctx_with_explicit_pairs,
    )
    assert len(provider.calls) == 1
    assert provider.calls[0]["snapshot_id"] == "snap-candidate"
    assert provider.calls[0]["document_id"] == "doc-1"
    # And the chunk projection lands — no `no_eligible_snapshot` marker.
    assert [c.artifact_kind for c in candidates] == ["chunk"]


def test_raganything_adapter_narrows_to_pinned_document_id(ctx):
    """When ``RouteContext.document_id`` is set, only the matching
    pair is queried — a single-document detail page must not fan
    out to other documents' snapshots."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={"evidence_chunks": []},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(
            ("doc-A", "snap-A"), ("doc-B", "snap-B"),
        ),
    )
    pinned = RouteContext(
        ctx=ctx, scope=WorkspaceScope(), document_id="doc-A",
    )
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        pinned,
    )
    assert len(provider.calls) == 1
    assert provider.calls[0]["document_id"] == "doc-A"
    assert provider.calls[0]["snapshot_id"] == "snap-A"


def test_raganything_adapter_respects_eligible_snapshot_id_allowlist(ctx):
    """When the orchestrator pre-resolves ``eligible_snapshot_ids``,
    the adapter narrows the resolver's pairs to that allowlist. Lets
    one eligibility computation feed both BM25 and RAGAnything
    instead of resolving twice."""
    provider = _StubRAGProvider(_StubRAGResult(
        answer="", citations=[], metadata={"evidence_chunks": []},
    ))
    adapter = RAGAnythingAdapter(
        provider,
        eligible_snapshot_pairs_resolver=_pairs_resolver(
            ("doc-A", "snap-A"), ("doc-B", "snap-B"),
        ),
    )
    # Orchestrator narrowed eligibility to only snap-A.
    narrow = RouteContext(
        ctx=ctx, scope=WorkspaceScope(),
        eligible_snapshot_ids=frozenset({"snap-A"}),
    )
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.RAGANYTHING, query="q"),
        narrow,
    )
    assert len(provider.calls) == 1
    assert provider.calls[0]["snapshot_id"] == "snap-A"


def test_raganything_provider_propagates_workspace_scope_missing():
    """The provider MUST re-raise ``WorkspaceScopeMissing`` from the
    bridge, not swallow it as a generic FAILED result. Operators need
    the scope-missing reason to surface in the trace."""
    from j1.providers.errors import WorkspaceScopeMissing
    from j1.providers.raganything.retrieval import (
        RAGAnythingQueryProvider, RAGAnythingQueryRequest,
    )
    from j1.providers.raganything.settings import RAGAnythingSettings

    class _StubLLMRegistry:
        def text(self): return None
        def try_embedding(self): return None

    def _raising_callable(request: RAGAnythingQueryRequest):
        raise WorkspaceScopeMissing("no snapshot id supplied")

    settings = RAGAnythingSettings()
    provider = RAGAnythingQueryProvider(
        llm_registry=_StubLLMRegistry(),
        settings=settings,
        query_callable=_raising_callable,
    )
    with pytest.raises(WorkspaceScopeMissing):
        provider.query(
            ProjectContext(tenant_id="t", project_id="p", profile=None),
            "q",
        )


# ---- Lexical evidence adapter (was: BM25 adapter) ----------------


@dataclass
class _StubEvidenceHit:
    """Mimics ``j1.search.postgres_fts.EvidenceHit``."""
    artifact_id: str
    chunk_id: str | None
    snapshot_id: str
    document_id: str
    tenant_id: str = "t"
    project_id: str = "p"
    content: str = ""
    score: float = 0.0
    created_by_run_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class _StubEvidenceAdapter:
    """In-memory stand-in for the canonical evidence adapter."""
    def __init__(self, hits: list[_StubEvidenceHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def search(
        self, ctx, *, query, allowed_snapshot_ids, max_results,
    ):
        self.calls.append({
            "query": query,
            "allowed_snapshot_ids": list(allowed_snapshot_ids),
            "max_results": max_results,
        })
        return self.hits


@pytest.fixture
def snapshot_route_ctx(ctx):
    """A context with an explicit snapshot allowlist — the canonical
    Phase-6 path."""
    return RouteContext(
        ctx=ctx,
        scope=WorkspaceScope(),
        eligible_snapshot_ids=frozenset({"snap-1"}),
    )


def test_lexical_adapter_surfaces_evidence_hits(snapshot_route_ctx):
    """Phase 6: the lexical adapter returns one candidate per
    evidence hit. Score, snapshot lineage, and content all
    propagate to the EvidenceCandidate."""
    from j1.query.retrieval_routes import LexicalEvidenceAdapter

    backend = _StubEvidenceAdapter([
        _StubEvidenceHit(
            artifact_id="a3", chunk_id="c3", snapshot_id="snap-1",
            document_id="doc-1",
            content="60% design submittal includes geotech report.",
            score=12.4, created_by_run_id="run-1",
        ),
    ])
    adapter = LexicalEvidenceAdapter(backend)
    job = RetrievalJob(
        route=RetrievalRouteKind.BM25,
        query="60% design",
        max_results=5,
        label="bm25_anchor:60% design",
    )
    candidates = adapter.execute(job, snapshot_route_ctx)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.route == RetrievalRouteKind.BM25
    assert cand.score == 12.4
    assert "60% design" in cand.matched_anchors
    assert cand.extra["snapshot_id"] == "snap-1"
    assert cand.extra["lexical_ranker"] == "ts_rank_cd"
    # Snapshot allowlist threaded through to the backend.
    assert backend.calls[0]["allowed_snapshot_ids"] == ["snap-1"]


def test_lexical_adapter_refuses_with_empty_snapshot_allowlist(ctx):
    """An empty allowlist → no visible snapshots → return [] without
    consulting the backend. The adapter never falls through to an
    unfiltered query."""
    from j1.query.retrieval_routes import LexicalEvidenceAdapter

    backend = _StubEvidenceAdapter([
        _StubEvidenceHit(
            artifact_id="a1", chunk_id=None, snapshot_id="snap-1",
            document_id="doc-1", content="anything", score=1.0,
        ),
    ])
    adapter = LexicalEvidenceAdapter(backend)
    out = adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.BM25, query="q"),
        RouteContext(
            ctx=ctx, scope=WorkspaceScope(),
            eligible_snapshot_ids=frozenset(),
        ),
    )
    assert out == []
    assert backend.calls == []


def test_lexical_adapter_calls_resolver_when_context_has_no_allowlist(ctx):
    """The resolver fallback handles the validation diagnostic path
    where the orchestrator didn't pre-compute the allowlist."""
    from j1.query.retrieval_routes import LexicalEvidenceAdapter

    backend = _StubEvidenceAdapter([])

    resolved = {"n": 0}

    def _resolver(_ctx, _scope):
        resolved["n"] += 1
        return frozenset({"snap-resolved"})

    adapter = LexicalEvidenceAdapter(
        backend, eligible_snapshot_ids_resolver=_resolver,
    )
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.BM25, query="q"),
        RouteContext(ctx=ctx, scope=WorkspaceScope()),
    )
    assert resolved["n"] == 1
    assert backend.calls[0]["allowed_snapshot_ids"] == ["snap-resolved"]


def test_bm25_adapter_alias_still_works(snapshot_route_ctx):
    """Phase 6 compat alias: ``BM25Adapter`` is now a synonym for
    ``LexicalEvidenceAdapter``. External callers that imported the
    old name keep working."""
    from j1.query.retrieval_routes import BM25Adapter, LexicalEvidenceAdapter
    assert BM25Adapter is LexicalEvidenceAdapter


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


def test_runner_dispatches_all_jobs(snapshot_route_ctx):
    """Phase 6: the runner dispatches multiple jobs through the
    lexical evidence adapter. Each job yields one candidate."""
    from j1.query.retrieval_routes import LexicalEvidenceAdapter

    backend = _StubEvidenceAdapter([
        _StubEvidenceHit(
            artifact_id="a", chunk_id=None, snapshot_id="snap-1",
            document_id="doc-1", content="x", score=1.0,
            created_by_run_id="run-1",
        ),
    ])
    runner = RouteRunner({
        RetrievalRouteKind.BM25: LexicalEvidenceAdapter(backend),
    })
    records = runner.run_all(
        (
            RetrievalJob(route=RetrievalRouteKind.BM25, query="a"),
            RetrievalJob(route=RetrievalRouteKind.BM25, query="b"),
            RetrievalJob(route=RetrievalRouteKind.BM25, query="c"),
        ),
        snapshot_route_ctx,
    )
    assert len(records) == 3
    assert all(r.error is None for r in records)
    assert all(len(r.candidates) == 1 for r in records)
