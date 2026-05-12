"""Integration test: graph-only retrieval → manual query response.

Replays the production failure path end-to-end:
   * engine returns a ``QueryResponse`` with sources that are ALL
     ``graph_json`` (so the textual evidence builder drops them
     via ``_SKIP_KINDS``) AND a non-empty ``graph_paths`` list;
   * the service should now emit a synthetic ``graph_paths``
     evidence block so the synthesizer has prose to ground on,
     instead of falling through to ``no_evidence``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from j1.projects.context import ProjectContext
from j1.query.models import GraphPath, QueryResponse, SourceReference
from j1.validation.dtos import ManualTestQueryRequest


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


@dataclass
class _StubUsage:
    provider: str = "stub"
    model: str = "stub-model"
    input_tokens: int = 10
    output_tokens: int = 5


class _SpySynthesizer:
    """Records what the synthesizer is called with."""

    captured_evidence = None

    def synthesize(self, *, question, evidence):
        from j1.validation.synthesis import SynthesisResult
        self.captured_evidence = list(evidence)
        if not evidence:
            return SynthesisResult(
                answer=None, provider="stub", model="m",
                latency_ms=0, prompt_tokens=0, completion_tokens=0,
                error="no_evidence",
            )
        return SynthesisResult(
            answer="The graph shows several relationships [1].",
            provider="stub", model="m",
            latency_ms=10, prompt_tokens=20, completion_tokens=5,
            error=None,
        )


def test_graph_only_retrieval_does_not_emit_no_evidence(ctx):
    """Headline regression. Walk a graph-typed manual query through
    the service and assert the synthesizer was NOT given an empty
    evidence list."""
    from j1.validation.service import IngestionValidationService
    from j1.runs.models import IngestionRun, RunStatus

    # Stub run-store: returns a run when asked.
    run = IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        metadata={"run_id": "run-1"},
    )
    run_store = MagicMock()
    run_store.get.return_value = run

    # Engine returns a graph-only response: sources are graph_json
    # (which _SKIP_KINDS drops) + a non-empty graph_paths list.
    engine = MagicMock()
    engine.query.return_value = QueryResponse(
        answer="Graph relationships:\n- A → B",
        mode_used="graph_first",
        sources=[
            SourceReference(
                artifact_id="g-1",
                artifact_type="graph_json",
                title="graph-1",
                run_id="run-1",
            ),
        ],
        graph_paths=[
            GraphPath(nodes=["J1 Platform", "MinerU"], edges=["uses"]),
            GraphPath(
                nodes=["J1 Platform", "RAGAnything"], edges=["uses"],
            ),
        ],
    )

    spy = _SpySynthesizer()
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []

    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine,
        workspace=workspace,
        answer_synthesizer=spy,
    )

    request = ManualTestQueryRequest(
        question="What are the main entities and relationships?",
        synthesize=True,
        top_k=10,
    )
    response = svc.run_manual_test_query(ctx, "run-1", request)

    # The synthesizer was called with NON-EMPTY evidence — the
    # synthetic graph_paths block is in the list.
    assert spy.captured_evidence is not None
    assert len(spy.captured_evidence) >= 1
    types = {b.artifact_type for b in spy.captured_evidence}
    assert "graph_paths" in types
    # The synthesizer produced a real answer (not no_evidence).
    assert response.synthesized_answer is not None
    assert "graph" in response.synthesized_answer.lower()
    # And it landed in evidence_sent_to_llm so the FE can show it.
    assert len(response.evidence_sent_to_llm) >= 1


def test_debug_payload_reports_graph_fallback_signals(ctx):
    """The debug counters expose the modality split and whether the
    graph-paths fallback fired — operators can diagnose graph-only
    cases without reading server logs."""
    from j1.validation.service import IngestionValidationService
    from j1.runs.models import IngestionRun, RunStatus

    run = IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        metadata={"run_id": "run-1"},
    )
    run_store = MagicMock()
    run_store.get.return_value = run

    engine = MagicMock()
    engine.query.return_value = QueryResponse(
        answer="Graph relationships:\n- A → B",
        mode_used="graph_first",
        sources=[
            SourceReference(
                artifact_id="g-1", artifact_type="graph_json",
                title="g-1", run_id="run-1",
            ),
            SourceReference(
                artifact_id="g-2", artifact_type="graph_json",
                title="g-2", run_id="run-1",
            ),
        ],
        graph_paths=[GraphPath(nodes=["A", "B"], edges=["x"])],
    )

    spy = _SpySynthesizer()
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")
    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []

    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine,
        workspace=workspace,
        answer_synthesizer=spy,
    )

    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="Q?", synthesize=True, top_k=10),
    )
    debug = response.debug

    # Modality split.
    assert debug["retrieved_count"] == 2
    assert debug["graph_result_count"] == 2
    assert debug["text_chunk_result_count"] == 0
    # The graph-paths fallback fired.
    assert debug["graph_paths_fallback_used"] is True
    # Skipped kinds catalogue still surfaces the graph_json drops.
    assert "graph_json" in debug["skipped_kinds"]
    # Drop-reason counter records two skipped graph_json items.
    assert debug["dropped_result_reasons"].get("skipped:graph_json") == 2


def test_no_retrieval_and_no_graph_paths_still_emits_no_evidence(ctx):
    """Defense: when retrieval IS empty AND there are no graph
    paths, the synthesizer correctly reports ``no_evidence``.
    Pinning the contract — the fallback is additive, never masks
    a genuine empty result."""
    from j1.validation.service import IngestionValidationService
    from j1.runs.models import IngestionRun, RunStatus

    run = IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        metadata={"run_id": "run-1"},
    )
    run_store = MagicMock()
    run_store.get.return_value = run

    engine = MagicMock()
    engine.query.return_value = QueryResponse(
        answer="No results.",
        mode_used="knowledge_first",
        sources=[],
        graph_paths=[],
    )
    spy = _SpySynthesizer()
    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")
    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []

    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine,
        workspace=workspace,
        answer_synthesizer=spy,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="Q?", synthesize=True, top_k=10),
    )
    # No retrieval AND no graph paths → no evidence at all → service
    # correctly hits the no_evidence path.
    assert response.synthesized_answer is None
    assert response.debug["fallback_reason"] == "no_retrieval"
