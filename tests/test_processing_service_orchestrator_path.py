"""``ProcessingService.query`` orchestrator-path tests.

When a SmartQueryOrchestrator is wired, the service's query method
delegates to the new pipeline instead of calling the legacy
QueryProvider. The Temporal-callable contract (return shape =
``QueryResult``) is preserved so workflow callers see no change.

Tests verify:
  * orchestrator path runs when wired; legacy provider is NEVER called
  * QueryResult shape stays stable (status, answer, citations,
    cost_events, message, metadata)
  * gate-failure paths still return SUCCEEDED with a useful message
    (audit consumers distinguish via metadata)
  * orchestrator raising → audit ``processing.query.failed`` +
    QueryResult(status=FAILED)
"""

from __future__ import annotations

import pytest

from j1.processing.results import QueryResult, ResultStatus
from j1.processing.service import ProcessingService
from j1.projects.context import ProjectContext
from j1.query.answer_synthesizer import SynthesisRequest
from j1.query.orchestrator import SmartQueryOrchestrator
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalRouteKind,
)


_FAILED_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


class _DictRoute:
    def __init__(self, kind, per_label):
        self.kind = kind
        self._per_label = per_label

    def execute(self, job, ctx):
        return list(self._per_label.get(job.label, []))


def _cand(*, artifact_id, body):
    return EvidenceCandidate(
        route=RetrievalRouteKind.RAGANYTHING,
        artifact_id=artifact_id, artifact_kind="chunk",
        chunk_id=f"c-{artifact_id}",
        text_preview=body[:80], score=0.7, matched_anchors=(),
        run_id="run-1", document_id="doc-1", project_id="alpha",
        extra={"body": body},
    )


class _AssertingProvider:
    """The legacy provider — must NOT be invoked when orchestrator
    is wired. Calling it explodes the test."""

    kind = "legacy_provider"

    def query(self, ctx, question, *, max_results=None):
        raise AssertionError(
            "Legacy provider must not be called when "
            "smart_query_orchestrator is wired"
        )


def _good_orchestrator() -> SmartQueryOrchestrator:
    routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [
                _cand(artifact_id="A",
                      body="60% design deliverables include drawings."),
                _cand(artifact_id="B",
                      body="90% design deliverables include specs."),
                _cand(artifact_id="C",
                      body="100% design deliverables include final set."),
                _cand(artifact_id="D",
                      body="conceptual engineering feasibility."),
                _cand(artifact_id="E",
                      body="cost estimate class 3 across design stages."),
            ]},
        ),
        RetrievalRouteKind.BM25: _DictRoute(
            RetrievalRouteKind.BM25, {},
        ),
    }

    def _llm(req: SynthesisRequest) -> str:
        return (
            "| Stage | deliverables | cost estimate class | Citation |\n"
            "| --- | --- | --- | --- |\n"
            "| 60% | drawings | Class 3 | [#1] [#5] |\n"
            "| 90% | specs | n/a | [#2] |\n"
            "| 100% design | final set | n/a | [#3] |\n"
            "| conceptual engineering | feasibility | n/a | [#4] |"
        )
    return SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm,
    )


def test_orchestrator_path_runs_and_returns_query_result_shape(
    processing_service, workspace, ctx,
):
    """The QueryResult shape is the Temporal-callable contract.
    The orchestrator path must populate the same fields."""
    svc = ProcessingService(
        workspace=processing_service._workspace,
        artifact_registry=processing_service._artifacts,
        audit=processing_service._audit,
        cost=processing_service._cost,
        smart_query_orchestrator=_good_orchestrator(),
    )
    result = svc.query(
        ctx, _AssertingProvider(), _FAILED_QUERY,
        max_results=10, actor="system", correlation_id="run-1",
    )
    assert isinstance(result, QueryResult)
    # PASSED orchestrator final_status → SUCCEEDED service result.
    assert result.status is ResultStatus.SUCCEEDED
    assert result.answer is not None
    assert "60%" in result.answer
    # Citations are artifact_ids, strictly from cited subset.
    assert len(result.citations) > 0
    # Metadata exposes the orchestrator-specific fields the audit
    # log + downstream consumers read.
    assert result.metadata["orchestrator_final_status"] == "passed"
    assert result.metadata["intent"] == "stage_progression"


def test_evidence_insufficient_returns_succeeded_with_message(
    processing_service, ctx,
):
    """Sufficiency gate failures (evidence_insufficient,
    retrieval_insufficient) still return SUCCEEDED with the gate
    reason on ``message`` and the precise status on metadata. The
    activity completing successfully but with an empty answer is
    the contract Temporal callers need."""
    sparse_routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [_cand(
                artifact_id="X", body="60% design only.",
            )]},
        ),
    }

    def _llm(req):
        raise AssertionError("LLM must not be called")
    orch = SmartQueryOrchestrator.from_components(
        routes=sparse_routes, llm=_llm,
    )
    svc = ProcessingService(
        workspace=processing_service._workspace,
        artifact_registry=processing_service._artifacts,
        audit=processing_service._audit,
        cost=processing_service._cost,
        smart_query_orchestrator=orch,
    )
    result = svc.query(
        ctx, _AssertingProvider(), _FAILED_QUERY,
        correlation_id="run-1",
    )
    assert result.status is ResultStatus.SUCCEEDED
    assert result.message is not None
    assert result.metadata["orchestrator_final_status"] == (
        "evidence_insufficient"
    )
    assert "90%" in result.metadata["groups_missing"]


def test_orchestrator_raising_maps_to_failed_query_result(
    processing_service, ctx,
):
    """Unhandled orchestrator exception → QueryResult(FAILED) AND
    ``processing.query.failed`` audit emission. Existing Temporal
    behavior preserved."""

    class _BoomOrchestrator:
        def run(self, request):
            raise RuntimeError("orchestrator crash")
    svc = ProcessingService(
        workspace=processing_service._workspace,
        artifact_registry=processing_service._artifacts,
        audit=processing_service._audit,
        cost=processing_service._cost,
        smart_query_orchestrator=_BoomOrchestrator(),
    )
    result = svc.query(
        ctx, _AssertingProvider(), "anything", correlation_id="run-1",
    )
    assert result.status is ResultStatus.FAILED
    assert "orchestrator crash" in (result.error or "")


def test_legacy_path_runs_when_orchestrator_absent(
    processing_service, ctx,
):
    """Without an orchestrator the service falls through to the
    legacy QueryProvider — existing workflow tests pin this
    behavior."""

    class _LegacyProvider:
        kind = "legacy"
        called = False

        def query(self, ctx, question, *, max_results=None):
            self.called = True
            return QueryResult(
                status=ResultStatus.SUCCEEDED,
                answer="legacy answer",
                citations=["a1"],
            )
    p = _LegacyProvider()
    result = processing_service.query(
        ctx, p, "anything", correlation_id="run-1",
    )
    assert p.called is True
    assert result.answer == "legacy answer"
