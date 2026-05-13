"""Batch-validation runner orchestrator-path tests.

When ``smart_query_orchestrator`` is wired, ``DefaultValidationRunner``
bypasses the legacy ``query_engine`` + ``run_checks`` +
``aggregate_status`` pipeline entirely. Per-case execution flows
through the orchestrator; case-specific ``expected_*`` checks layer
on top."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from j1.artifacts.registry import JsonArtifactRegistry
from j1.projects.context import ProjectContext
from j1.query.answer_synthesizer import SynthesisRequest
from j1.query.orchestrator import SmartQueryOrchestrator
from j1.query.query_plan import EvidenceCandidate, RetrievalRouteKind
from j1.validation.dtos import (
    ValidationSetDTO,
    ValidationTestCaseDTO,
)
from j1.validation.runner import DefaultValidationRunner


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


class _AssertingEngine:
    """Legacy engine — must NOT be called when orchestrator wired."""

    def query(self, ctx, req):
        raise AssertionError(
            "Legacy query_engine must not be called when "
            "smart_query_orchestrator is wired"
        )


def _good_orchestrator() -> SmartQueryOrchestrator:
    routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [
                _cand(artifact_id="A",
                      body="60% design deliverables drawings."),
                _cand(artifact_id="B",
                      body="90% design deliverables specs."),
                _cand(artifact_id="C",
                      body="100% design deliverables final set."),
                _cand(artifact_id="D",
                      body="conceptual engineering feasibility."),
                _cand(artifact_id="E",
                      body="cost estimate class 3 across design."),
            ]},
        ),
        RetrievalRouteKind.BM25: _DictRoute(
            RetrievalRouteKind.BM25, {},
        ),
    }

    def _llm(req):
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


@pytest.fixture
def runner_with_orchestrator(workspace, artifact_registry):
    return DefaultValidationRunner(
        query_engine=_AssertingEngine(),
        artifact_registry=artifact_registry,
        smart_query_orchestrator=_good_orchestrator(),
        workspace=workspace,
    )


def _make_case(
    *,
    test_case_id: str,
    question: str,
    case_type: str = "answer",
    expected_behavior: str = "answer the question",
) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id=test_case_id,
        question=question,
        type=case_type,
        priority="P1",
        expected_behavior=expected_behavior,
    )


def _set_with_case(case: ValidationTestCaseDTO) -> ValidationSetDTO:
    return ValidationSetDTO(
        validation_set_id="vs-1",
        run_id="run-1",
        document_ids=["doc-1"],
        source="generated",
        status="ready",
        created_at="2026-05-13T00:00:00+00:00",
        created_by="test",
        generator_version="test",
        artifacts_content_hash=None,
        test_cases=[case],
    )


def test_runner_uses_orchestrator_for_positive_case(
    ctx, runner_with_orchestrator,
):
    case = _make_case(
        test_case_id="tc-1", question=_FAILED_QUERY,
        expected_behavior="stage progression with cost classes",
    )
    vrun = runner_with_orchestrator.run(ctx, _set_with_case(case))
    assert vrun.execution_status == "completed"
    assert vrun.validation_status == "passed"
    assert vrun.results[0].status == "passed"
    assert "60%" in vrun.results[0].answer


def test_runner_negative_case_passes_when_orchestrator_abstains(
    ctx, workspace, artifact_registry,
):
    """A negative test expects an abstain. When the orchestrator's
    LLM returns a refusal, the runner inverts the
    ``answer_not_refusal`` gate via
    ``negative_answer_abstains``."""
    routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING, {"primary": []},
        ),
    }

    def _llm(req):
        return "I don't know."
    orch = SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm,
    )
    runner = DefaultValidationRunner(
        query_engine=_AssertingEngine(),
        artifact_registry=artifact_registry,
        smart_query_orchestrator=orch,
        workspace=workspace,
    )
    case = _make_case(
        test_case_id="tc-neg",
        question="What is the answer to a question not in the docs?",
        case_type="negative",
        expected_behavior="abstain",
    )
    vrun = runner.run(ctx, _set_with_case(case))
    abstain_check = next(
        c for c in vrun.results[0].checks
        if c.name == "negative_answer_abstains"
    )
    assert abstain_check.passed is True


def test_runner_evidence_insufficient_maps_to_failed(
    ctx, workspace, artifact_registry,
):
    """Sufficiency gate failure → orchestrator returns
    ``evidence_insufficient`` → runner maps to ``failed`` status."""
    sparse = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [_cand(artifact_id="X",
                               body="60% design only.")]},
        ),
    }

    def _llm(req):
        raise AssertionError("LLM must not be called")
    orch = SmartQueryOrchestrator.from_components(
        routes=sparse, llm=_llm,
    )
    runner = DefaultValidationRunner(
        query_engine=_AssertingEngine(),
        artifact_registry=artifact_registry,
        smart_query_orchestrator=orch,
        workspace=workspace,
    )
    case = _make_case(
        test_case_id="tc-1", question=_FAILED_QUERY,
        expected_behavior="stage progression",
    )
    vrun = runner.run(ctx, _set_with_case(case))
    assert vrun.results[0].status == "failed"
    assert vrun.validation_status == "failed"


def test_runner_legacy_path_runs_when_orchestrator_unwired(
    ctx, workspace, artifact_registry,
):
    """Without an orchestrator, the legacy ``query_engine`` path
    runs — locks in backward compatibility."""

    class _LegacyEngine:
        called = False

        def query(self, ctx, req):
            self.called = True
            from j1.query.models import QueryResponse
            return QueryResponse(
                answer="legacy answer.", mode_used="auto",
            )

    eng = _LegacyEngine()
    runner = DefaultValidationRunner(
        query_engine=eng,
        artifact_registry=artifact_registry,
        workspace=workspace,
        # No orchestrator wired.
    )
    case = _make_case(
        test_case_id="tc-1", question="anything",
        expected_behavior="x",
    )
    runner.run(ctx, _set_with_case(case))
    assert eng.called is True
