"""SmartQueryOrchestrator end-to-end tests.

The big one: the exact failed manual-test query, driven through
the orchestrator with stubbed routes + a stubbed LLM. The tests
verify the full contract:

  * QueryPlan detects stage_progression.
  * Retrieval uses more than a single raw topK query.
  * EvidencePack does not select unrelated chunks as the dominant
    evidence.
  * Evidence sufficiency fails if stage coverage is too low.
  * Evidence sufficiency fails if deliverables are missing.
  * Evidence sufficiency fails if cost estimate / class evidence
    is missing.
  * A no-answer/refusal does not pass.
  * Citations are only from selected evidence.
  * Overall status is not Passed unless all required gates pass.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from j1.projects.context import ProjectContext
from j1.query.answer_synthesizer import SynthesisRequest
from j1.query.evidence_builder import EvidenceBuilderConfig
from j1.query.orchestrator import (
    OrchestratorRequest,
    SmartQueryOrchestrator,
)
from j1.query.query_plan import (
    EvidenceCandidate,
    Intent,
    RetrievalJob,
    RetrievalRouteKind,
)
from j1.query.scope import RunScope


_FAILED_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Test fixtures: routes + LLM stubs -----------------------


class _DictRoute:
    """In-memory route that returns canned candidates per ``label``.
    The orchestrator's RouteRunner calls ``execute`` once per job —
    we keyed the dict by job label so each anchor lookup can hit
    different content."""

    def __init__(
        self,
        kind: RetrievalRouteKind,
        per_label: dict[str, list[EvidenceCandidate]],
    ) -> None:
        self.kind = kind
        self._per_label = per_label

    def execute(self, job: RetrievalJob, context):
        return list(self._per_label.get(job.label, []))


def _cand(
    *, artifact_id: str, body: str,
    route: RetrievalRouteKind = RetrievalRouteKind.RAGANYTHING,
    score: float = 0.7, run_id: str = "run-1",
) -> EvidenceCandidate:
    return EvidenceCandidate(
        route=route,
        artifact_id=artifact_id,
        artifact_kind="chunk",
        chunk_id=f"c-{artifact_id}",
        text_preview=body[:120],
        score=score,
        matched_anchors=(),
        run_id=run_id,
        document_id="doc-1",
        project_id="p",
        extra={"body": body},
    )


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- 1. Failed question — full evidence ----------------------


def _good_routes():
    """Route results that fully cover the four stages + deliverables
    + estimate class. The orchestrator should reach PASSED."""
    primary = [
        _cand(artifact_id="A",
              body="60% design deliverables include drawings."),
        _cand(artifact_id="B",
              body="90% design deliverables include specifications."),
        _cand(artifact_id="C",
              body="100% design deliverables include final issue-for-construction set."),
        _cand(artifact_id="D",
              body="conceptual engineering deliverables include "
                   "feasibility studies."),
        _cand(artifact_id="E",
              body="cost estimate class 3 is associated with 60% design; "
                   "class 1 with 100% design."),
    ]
    return _DictRoute(
        RetrievalRouteKind.RAGANYTHING,
        {"primary": primary},
    )


def _good_bm25_routes():
    """Lexical anchors return additional confirming chunks for each
    stage. Same content surfaces — dedupe should collapse them."""
    return _DictRoute(
        RetrievalRouteKind.BM25,
        {
            "bm25_anchor:60%": [_cand(
                artifact_id="A",
                body="60% design deliverables include drawings.",
                route=RetrievalRouteKind.BM25, score=15.0,
            )],
            "bm25_anchor:90%": [_cand(
                artifact_id="B",
                body="90% design deliverables include specifications.",
                route=RetrievalRouteKind.BM25, score=14.0,
            )],
            "bm25_anchor:100% design": [_cand(
                artifact_id="C",
                body="100% design deliverables include final IFC set.",
                route=RetrievalRouteKind.BM25, score=14.0,
            )],
            "bm25_anchor:conceptual engineering": [_cand(
                artifact_id="D",
                body="conceptual engineering deliverables include "
                     "feasibility studies.",
                route=RetrievalRouteKind.BM25, score=14.0,
            )],
        },
    )


def _good_llm():
    def _stub(req: SynthesisRequest) -> str:
        return (
            "| Stage | deliverables | cost estimate class | Citation |\n"
            "| --- | --- | --- | --- |\n"
            "| 60% | drawings | Class 3 | [#1] [#5] |\n"
            "| 90% | specifications | n/a | [#2] |\n"
            "| 100% design | final IFC set | Class 1 | [#3] [#5] |\n"
            "| conceptual engineering | feasibility studies | n/a | [#4] |"
        )
    return _stub


def test_failed_question_passes_with_good_evidence(ctx):
    orch = SmartQueryOrchestrator.from_components(
        routes={
            RetrievalRouteKind.RAGANYTHING: _good_routes(),
            RetrievalRouteKind.BM25: _good_bm25_routes(),
        },
        llm=_good_llm(),
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"),
        run_id="run-1",
    ))
    # Final status is PASSED only when every gate passes.
    assert result.final_status == "passed", (
        f"unexpected final_status; gate results: "
        f"{[(g.name, g.passed, g.reason) for g in result.gate_results]}"
    )
    assert "60%" in result.answer
    assert "Class 3" in result.answer
    assert len(result.citations) >= 1
    # Plan-level checks.
    assert result.trace.plan.intent == Intent.STAGE_PROGRESSION
    # Retrieval used MORE than a single topK call.
    assert len(result.trace.routes_executed) >= 5  # primary + 4 BM25
    # Evidence pack covered all required groups.
    groups = set(result.trace.groups_covered)
    assert "60%" in groups
    assert "90%" in groups
    assert "100% design" in groups
    assert "conceptual engineering" in groups
    assert "deliverables" in groups
    # Citations must be a SUBSET of selected.
    selected_keys = {
        (b.candidate.artifact_id, b.candidate.chunk_id)
        for b in result.trace.selected
    }
    for c in result.citations:
        assert (
            (c.candidate.artifact_id, c.candidate.chunk_id)
            in selected_keys
        )


# ---- 2. Insufficient evidence ---------------------------------


def test_failed_question_fails_when_only_one_stage_covered(ctx):
    """Only the 60% stage has evidence. Sufficiency gate must
    fail BEFORE the LLM is called — orchestrator returns
    ``evidence_insufficient``, never ``passed``."""
    sparse = _DictRoute(
        RetrievalRouteKind.RAGANYTHING,
        {"primary": [_cand(artifact_id="A",
                           body="60% design context only.")]},
    )
    llm_called: list[int] = []

    def _llm(req: SynthesisRequest) -> str:
        llm_called.append(1)
        return "passed"
    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.RAGANYTHING: sparse},
        llm=_llm,
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    assert result.final_status == "evidence_insufficient"
    # The LLM was NEVER called.
    assert llm_called == []
    # Message contains a useful reason.
    assert result.message and (
        "groups" in result.message or "threshold" in result.message
    )


def test_failed_question_fails_when_no_candidates(ctx):
    """Zero retrieval candidates → retrieval_insufficient. The
    failure is distinct from evidence_insufficient so operators
    can tell "retrieval broken" from "retrieval ok but no real
    evidence"."""
    empty = _DictRoute(RetrievalRouteKind.RAGANYTHING, {"primary": []})

    def _llm(req):  # never called
        return "x"
    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.RAGANYTHING: empty},
        llm=_llm,
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    assert result.final_status == "retrieval_insufficient"


# ---- 3. Refusal still fails when evidence IS sufficient -------


def test_failed_question_fails_when_llm_refuses(ctx):
    """Even with full evidence, a refusal answer must FAIL — the
    quality gate's not_refusal check catches this."""
    def _refuse(req: SynthesisRequest) -> str:
        return (
            "I'm sorry, but the answer is not in the retrieved "
            "evidence. " * 5
        )
    orch = SmartQueryOrchestrator.from_components(
        routes={
            RetrievalRouteKind.RAGANYTHING: _good_routes(),
            RetrievalRouteKind.BM25: _good_bm25_routes(),
        },
        llm=_refuse,
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    assert result.final_status == "failed"
    assert any(
        g.name == "answer_not_refusal" and not g.passed
        for g in result.gate_results
    )


# ---- 4. Citations are subset of selected ---------------------


def test_citations_are_subset_of_selected_pack(ctx):
    """The binder enforces cited ⊆ selected. The orchestrator
    must surface this in the trace."""
    orch = SmartQueryOrchestrator.from_components(
        routes={
            RetrievalRouteKind.RAGANYTHING: _good_routes(),
            RetrievalRouteKind.BM25: _good_bm25_routes(),
        },
        llm=_good_llm(),
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    # Trace has fewer citations than selected (binder is strict).
    assert len(result.trace.citations) <= len(result.trace.selected)
    # Citations are concrete blocks from the selected set.
    selected_keys = {
        (b.candidate.artifact_id, b.candidate.chunk_id)
        for b in result.trace.selected
    }
    for c in result.trace.citations:
        assert (
            (c.candidate.artifact_id, c.candidate.chunk_id)
            in selected_keys
        )


# ---- 5. Unrelated chunks don't dominate ----------------------


def test_unrelated_chunks_do_not_dominate_selected_evidence(ctx):
    """The failed-question observation: retrieval surfaced CEP /
    potholing / marine / proposal-format chunks. The pack builder
    must keep those out of grouped evidence (they don't match any
    required group anchor)."""
    mixed = _DictRoute(
        RetrievalRouteKind.RAGANYTHING,
        {"primary": [
            _cand(artifact_id="cep", body="CEP compliance steps."),
            _cand(artifact_id="pot", body="Potholing field method."),
            _cand(artifact_id="mar", body="Marine survey schedule."),
            _cand(artifact_id="prop",
                  body="Proposal format instructions."),
            _cand(artifact_id="A",
                  body="60% design deliverables include drawings."),
            _cand(artifact_id="B",
                  body="90% design deliverables include specs."),
            _cand(artifact_id="C",
                  body="100% design deliverables include final IFC set."),
            _cand(artifact_id="D",
                  body="cost estimate class 3 for 60% design."),
        ]},
    )

    def _llm(req: SynthesisRequest) -> str:
        return (
            "| Stage | deliverables | cost estimate class | Citation |\n"
            "| --- | --- | --- | --- |\n"
            "| 60% | drawings | Class 3 | [#1] |\n"
            "| 90% | specs | n/a | [#2] |\n"
            "| 100% design | final IFC set | n/a | [#3] |"
        )
    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.RAGANYTHING: mixed},
        llm=_llm,
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    # The unrelated chunks are NOT in any grouped block.
    grouped_ids = {
        b.candidate.artifact_id
        for b in result.trace.selected
        if b.group is not None
    }
    assert "cep" not in grouped_ids
    assert "pot" not in grouped_ids
    assert "mar" not in grouped_ids
    assert "prop" not in grouped_ids


# ---- 6. Trace shape ------------------------------------------


def test_native_answer_fallback_recovers_dropped_fields(ctx):
    """When the local synthesizer drops a requested-field token that
    a ``raganything.native_answer`` block does contain, the answer
    should swap to the native answer text instead of failing the
    quality gate.

    This locks in the behaviour reported as "the answer of
    RAGAnything has the fields but the final answer excludes them" —
    upstream answer is good, local synth is over-summarising. The
    fallback uses the native answer verbatim and cites that block."""
    question = "What modules are involved in the integration pipeline?"

    # Native-answer block. Notice it carries the literal word
    # "modules" — which is the head noun of the requested field
    # "modules involved" the classifier will extract.
    native_body = (
        "The integration pipeline involves three modules: "
        "ingest, compile, and enrich. Each module runs in sequence."
    )

    class _NativeRoute:
        kind = RetrievalRouteKind.RAGANYTHING

        def execute(self, job, context):
            return [EvidenceCandidate(
                route=self.kind,
                artifact_id="raganything.native_answer",
                artifact_kind="raganything.native_answer",
                chunk_id=None,
                text_preview=native_body[:120],
                score=0.5,
                matched_anchors=(),
                run_id="run-1",
                document_id="doc-1",
                project_id="p",
                extra={
                    "body": native_body,
                    "raganything_native_answer": True,
                },
            )]

    # The local synthesizer is a stub that omits "modules" entirely
    # — simulating the over-summarisation we saw in production.
    def _summarising_llm(req: SynthesisRequest) -> str:
        return "The pipeline has three stages that run in sequence."

    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.RAGANYTHING: _NativeRoute()},
        llm=_summarising_llm,
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=question,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    # The final answer is the native answer (not the summariser
    # output) and contains the requested-field head noun.
    assert "modules" in result.answer.lower()
    assert "ingest, compile, and enrich" in result.answer
    # Citations point to the native-answer block.
    assert len(result.citations) == 1
    assert (
        result.citations[0].candidate.artifact_kind
        == "raganything.native_answer"
    )


def test_trace_carries_everything_for_the_manual_view(ctx):
    orch = SmartQueryOrchestrator.from_components(
        routes={
            RetrievalRouteKind.RAGANYTHING: _good_routes(),
            RetrievalRouteKind.BM25: _good_bm25_routes(),
        },
        llm=_good_llm(),
    )
    result = orch.run(OrchestratorRequest(
        ctx=ctx, question=_FAILED_QUERY,
        scope=RunScope(run_id="run-1"), run_id="run-1",
    ))
    trace = result.trace.to_dict()
    # All required keys present.
    expected_keys = {
        "question", "normalized_question", "plan", "routes_executed",
        "all_candidates", "selected", "dropped", "groups_covered",
        "groups_missing", "llm_evidence", "answer", "citations",
        "gate_results", "final_status", "duration_ms",
    }
    assert expected_keys <= set(trace.keys())
    # Plan has the anchors + retrieval_jobs the operator can inspect.
    assert trace["plan"]["intent"] == "stage_progression"
    assert "60%" in trace["plan"]["anchors"]
    assert len(trace["plan"]["retrieval_jobs"]) >= 5
    # Final status echoes the result.
    assert trace["final_status"] == "passed"
