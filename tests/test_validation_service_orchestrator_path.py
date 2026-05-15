"""IngestionValidationService manual-query orchestrator-path tests.

When a SmartQueryOrchestrator is wired into the service, the
manual-test query path delegates to it. The legacy
aggregate_status + refusal-regex path is bypassed entirely. These
tests lock the new behavior in:

  * The failed-question's evidence_insufficient case maps to
    ``validation_status="failed"``.
  * A successful orchestrator run maps to ``validation_status="passed"``
    with the new pipeline's citations + evidence_sent_to_llm.
  * The trace lands on ``debug['orchestrator_trace']``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.registry import JsonArtifactRegistry
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.projects.context import ProjectContext
from j1.query.answer_synthesizer import SynthesisRequest
from j1.query.orchestrator import SmartQueryOrchestrator
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalRouteKind,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import ManualTestQueryRequest
from j1.validation.service import IngestionValidationService


_FAILED_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Test fixtures ---------------------------------------------


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
        run_id="run-1", document_id="doc-1", project_id="p",
        extra={"body": body, "section_path": "Sec 3"},
    )


@pytest.fixture
def run(ctx: ProjectContext, workspace) -> IngestionRun:
    store = JsonlIngestionRunStore(workspace)
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    record = IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-run-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=now,
        updated_at=now,
    )
    store.upsert(ctx, record)
    return record


def _make_service(
    *,
    workspace,
    ctx,
    orchestrator: SmartQueryOrchestrator | None,
) -> IngestionValidationService:
    """Minimal service — only the orchestrator path is exercised so
    most dependencies are placeholders."""
    audit = DefaultAuditRecorder(JsonlAuditSink(workspace))
    artifacts = JsonArtifactRegistry(workspace)
    run_store = JsonlIngestionRunStore(workspace)
    # HybridQueryEngine is required by the constructor; we pass a
    # stub that never gets called when the orchestrator branch
    # fires.

    class _NoopEngine:
        def query(self, ctx, req):
            raise AssertionError(
                "legacy query engine should not be called when the "
                "orchestrator branch is active"
            )
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        audit=audit,
        workspace=workspace,
        smart_query_orchestrator=orchestrator,
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


def _sparse_orchestrator() -> SmartQueryOrchestrator:
    """Only one stage covered — sufficiency gate must fail."""
    routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [_cand(
                artifact_id="X",
                body="60% design only — nothing else covered.",
            )]},
        ),
        RetrievalRouteKind.BM25: _DictRoute(
            RetrievalRouteKind.BM25, {},
        ),
    }

    def _llm(req):
        raise AssertionError(
            "LLM must not be called when sufficiency fails"
        )
    return SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm,
    )


# ---- Tests -----------------------------------------------------


def test_orchestrator_passed_path_maps_to_validation_passed(
    ctx, workspace, run,
):
    service = _make_service(
        workspace=workspace, ctx=ctx,
        orchestrator=_good_orchestrator(),
    )
    result = service.run_manual_test_query(
        ctx, run.run_id,
        ManualTestQueryRequest(question=_FAILED_QUERY),
        actor="tester",
    )
    assert result.validation_status == "passed"
    assert "60%" in result.answer
    assert result.synthesized_answer is not None
    assert result.mode_used == "smart_query_orchestrator"
    # Citations come from the orchestrator's cited subset — not the
    # full retrieved set.
    assert len(result.citations) > 0
    assert len(result.citations) <= len(result.retrieved_chunks)
    # Evidence-sent-to-LLM is the orchestrator's llm_evidence.
    assert len(result.evidence_sent_to_llm) > 0
    # Trace lands on debug.
    assert "orchestrator_trace" in result.debug
    assert result.debug["orchestrator_trace"]["plan"]["intent"] == (
        "stage_progression"
    )


def test_orchestrator_evidence_insufficient_maps_to_validation_failed(
    ctx, workspace, run,
):
    service = _make_service(
        workspace=workspace, ctx=ctx,
        orchestrator=_sparse_orchestrator(),
    )
    result = service.run_manual_test_query(
        ctx, run.run_id,
        ManualTestQueryRequest(question=_FAILED_QUERY),
        actor="tester",
    )
    # The legacy aggregate_status would have happily passed this
    # ("zero checks failed when no synthesizer ran"). The new gate
    # surfaces it as a failure.
    assert result.validation_status == "failed"
    assert not result.synthesized_answer
    # The LLM wasn't called → llm_trace.called is False.
    assert result.llm is not None
    assert result.llm.called is False
    # The debug trace carries the missing groups so the manual view
    # can render them.
    trace = result.debug["orchestrator_trace"]
    missing = set(trace["groups_missing"])
    assert "90%" in missing
    assert "100% design" in missing


def test_orchestrator_response_carries_diagnostic_warnings_list(
    ctx, workspace, run,
):
    """PR-01: every manual-test response MUST carry a
    ``debug['diagnostic_warnings']`` array, even when empty. The
    FE banner reads from this key — an absent key would silently
    hide warnings instead of showing 'all green'."""
    service = _make_service(
        workspace=workspace, ctx=ctx,
        orchestrator=_good_orchestrator(),
    )
    result = service.run_manual_test_query(
        ctx, run.run_id,
        ManualTestQueryRequest(question=_FAILED_QUERY),
        actor="tester",
    )
    assert "diagnostic_warnings" in result.debug
    assert isinstance(result.debug["diagnostic_warnings"], list)


def test_diagnostic_warnings_surface_missing_eligible_snapshot_ids(
    ctx, workspace, run,
):
    """When the orchestrator's eligibility resolver returns no
    snapshots (refusal path), the trace's
    ``snapshot_scope.eligible_snapshot_ids`` lands empty. PR-01
    requires that empty state surface in
    ``diagnostic_warnings`` so operators tracking down 'no eligible
    snapshot' refusals see the load-bearing cause without paging
    through the raw trace.

    The orchestrator under test never stamps an eligible snapshot
    id (its trace defaults to empty), so this exercise pins the
    builder's contract end-to-end through the service."""
    service = _make_service(
        workspace=workspace, ctx=ctx,
        orchestrator=_sparse_orchestrator(),
    )
    result = service.run_manual_test_query(
        ctx, run.run_id,
        ManualTestQueryRequest(question=_FAILED_QUERY),
        actor="tester",
    )
    warnings = result.debug["diagnostic_warnings"]
    assert any(
        "snapshot_scope.eligible_snapshot_ids" in w
        and "empty" in w.lower()
        for w in warnings
    ), f"expected eligible_snapshot_ids warning; got {warnings!r}"


def test_manual_query_without_orchestrator_raises(
    ctx, workspace, run,
):
    """The legacy manual-query path was removed. Calling
    ``run_manual_test_query`` without a wired orchestrator must
    raise — silent fallback would mask a misconfigured deploy."""
    service = IngestionValidationService(
        run_store=JsonlIngestionRunStore(workspace),
        artifact_registry=JsonArtifactRegistry(workspace),
        audit=DefaultAuditRecorder(JsonlAuditSink(workspace)),
        workspace=workspace,
    )
    with pytest.raises(RuntimeError, match="SmartQueryOrchestrator"):
        service.run_manual_test_query(
            ctx, run.run_id,
            ManualTestQueryRequest(question="anything"),
            actor="tester",
        )


def test_orchestrator_citations_subset_of_retrieved(
    ctx, workspace, run,
):
    """The 20-citations-for-4-blocks legacy bug is fixed: citations
    are STRICTLY a subset of what the LLM cited from the selected
    pack. The retrieved_chunks list is broader but citations is
    always equal-or-narrower."""
    service = _make_service(
        workspace=workspace, ctx=ctx,
        orchestrator=_good_orchestrator(),
    )
    result = service.run_manual_test_query(
        ctx, run.run_id,
        ManualTestQueryRequest(question=_FAILED_QUERY),
        actor="tester",
    )
    cited_ids = {c["artifactId"] for c in result.citations}
    retrieved_ids = {c.artifact_id for c in result.retrieved_chunks}
    assert cited_ids <= retrieved_ids
