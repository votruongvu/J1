"""Regression tests for the candidate / evidence top_k decoupling
in ``IngestionValidationService.run_manual_test_query``.

Bug fixed:
   ``request.top_k`` was used directly as the engine's
   ``max_results`` — which translates to the FTS5 ``LIMIT`` in
   ``SqliteSearchIndexer.search``. So a UI setting of top_k=3
   capped the raw candidate pool at 3 rows; the downstream
   reranker (``j1.validation.rerank``) couldn't recover a
   relevant chunk that ranked 5th in FTS.

   The minimal fix decouples the two:
     * the engine is asked for ``max(requested_top_k,
       validation_candidate_top_k)`` rows (default
       candidate floor is 20),
     * the final evidence count is independently capped at
       ``validation_evidence_max_blocks`` (default 5).

   Per the retrieval audit, this preserves the existing reranker
   AND existing isolation contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from j1.projects.context import ProjectContext
from j1.query.models import QueryRequest, QueryResponse, SourceReference
from j1.runs.models import IngestionRun, RunStatus
from j1.validation.dtos import ManualTestQueryRequest


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _make_run() -> IngestionRun:
    """Minimal IngestionRun fixture — the service only inspects
    ``run_id`` + ``document_id`` + ``status``."""
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    return IngestionRun(
        run_id="run-1",
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=now,
        updated_at=now,
        metadata={"run_id": "run-1"},
    )


@dataclass
class _SpyEngine:
    """Captures the ``QueryRequest`` the service hands to the
    engine — so the tests can assert on ``max_results`` without
    standing up a real ``HybridQueryEngine``."""

    sources: list[SourceReference]
    captured: list[QueryRequest]

    def __init__(self, sources):
        self.sources = sources
        self.captured = []

    def query(self, ctx, request: QueryRequest) -> QueryResponse:  # noqa: ARG002
        self.captured.append(request)
        # Truncate the canned source list to honour
        # ``max_results`` — mirrors what the real
        # ``SqliteSearchIndexer.search`` does with ``LIMIT``.
        return QueryResponse(
            answer="(stub answer)",
            mode_used="knowledge_first",
            sources=self.sources[: request.max_results],
            graph_paths=[],
        )


def _source(*, artifact_id, kind, run_id="run-1", score=0.0):
    """Source factory that uses the new ``score`` field on
    ``SourceReference`` so the rerank-feeding score propagates
    through ``_retrieved_chunks_from_response``."""
    return SourceReference(
        artifact_id=artifact_id,
        artifact_type=kind,
        title=f"{kind}/{artifact_id}",
        chunk_id=f"chunk-{artifact_id}" if kind == "chunk" else None,
        run_id=run_id,
        score=score,
    )


def _build_service(*, engine, candidate_top_k=20, evidence_max_blocks=5):
    """Build a minimal IngestionValidationService — only the
    manual-query path is exercised; everything else is mocked."""
    from j1.validation.service import IngestionValidationService

    run_store = MagicMock()
    run_store.get.return_value = _make_run()

    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []

    workspace = MagicMock()
    workspace.area.return_value = Path("/tmp")

    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine,
        workspace=workspace,
        # NO synthesizer wired — we're checking the retrieval
        # plumbing, not the LLM call. evidence_max_blocks is the
        # cap; without a synthesizer the evidence list returns
        # empty anyway, which is fine for these assertions.
        answer_synthesizer=None,
        validation_candidate_top_k=candidate_top_k,
        validation_evidence_max_blocks=evidence_max_blocks,
    )


# ---- Test A: small requested top_k still uses candidate floor ----


def test_a_requested_top_k_3_floors_to_candidate_top_k(ctx):
    """When the FE/caller asks for ``top_k=3``, the service must
    still ask the engine for at least ``validation_candidate_top_k``
    rows. This is the headline fix — FTS LIMIT is no longer
    starved by the UI value."""
    engine = _SpyEngine(sources=[
        _source(artifact_id=f"a-{i}", kind="chunk", score=1.0 - i * 0.01)
        for i in range(25)
    ])
    svc = _build_service(engine=engine, candidate_top_k=20)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(
            question="anything?",
            top_k=3,           # the FE knob
            synthesize=False,
        ),
    )

    # The engine was asked for 20, not 3.
    assert len(engine.captured) == 1
    assert engine.captured[0].max_results == 20

    # And the debug payload reports the breakdown so operators can
    # see both numbers.
    debug = response.debug
    assert debug["requested_top_k"] == 3
    assert debug["candidate_top_k_used"] == 20
    assert debug["fts_returned_count"] == 20
    assert debug["evidence_max_blocks"] == 5
    assert debug["scope_run_id"] == "run-1"


def test_a_requested_top_k_above_floor_honoured(ctx):
    """When the caller explicitly asks for MORE than the floor,
    honour the request — they want broader output. The floor is
    a minimum, not a ceiling."""
    engine = _SpyEngine(sources=[
        _source(artifact_id=f"a-{i}", kind="chunk") for i in range(40)
    ])
    svc = _build_service(engine=engine, candidate_top_k=20)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=30, synthesize=False),
    )
    # 30 > floor → honour 30. (Capped at the hard cap, but 30 < 50.)
    assert engine.captured[0].max_results == 30
    assert response.debug["candidate_top_k_used"] == 30


# ---- Test B: relevant chunk outside requested_top_k, inside candidate ----


def test_b_relevant_chunk_below_requested_but_inside_candidate_reaches_evidence(
    ctx, tmp_path: Path,
):
    """When the relevant chunk is at FTS rank 15 (outside
    ``requested_top_k=5``) but inside ``candidate_top_k=20``,
    the candidate pool DOES include it — proving the decoupling
    fix lets the reranker see it.

    We don't assert it makes the FINAL evidence (that depends on
    the synthesizer being wired, which we skip here). What
    matters is the proof that the candidate retrieval pool
    surfaces all 20 rows, and the relevant chunk's
    artifact_id is among them — the reranker now has it as
    selectable material.
    """
    sources = [
        _source(artifact_id=f"a-{i}", kind="chunk", score=1.0 - i * 0.01)
        for i in range(20)
    ]
    # Mark the 15th source as "the relevant one" — outside what
    # an FE-requested top_k=5 would have returned.
    relevant_id = "a-14"
    assert relevant_id in [s.artifact_id for s in sources]

    engine = _SpyEngine(sources=sources)
    svc = _build_service(engine=engine, candidate_top_k=20)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )

    # Engine asked for 20. Retrieval returned 20. The
    # relevant rank-15 chunk is in the pool.
    assert engine.captured[0].max_results == 20
    assert response.debug["fts_returned_count"] == 20
    retrieved_ids = [r.artifact_id for r in response.retrieved_chunks]
    assert relevant_id in retrieved_ids, (
        "candidate pool dropped the relevant chunk despite "
        "candidate_top_k=20 — decoupling fix is not in effect"
    )


# ---- Test C: final evidence capped by evidence_max_blocks --------


def test_c_evidence_max_blocks_caps_final_count(ctx, tmp_path: Path):
    """Even with 20 candidates retrieved, the FINAL evidence
    sent to the LLM is capped at ``validation_evidence_max_blocks``.

    The synthesizer isn't wired here, so the cap manifests as
    debug payload assertion. The cap is the contract operators
    rely on for budget predictability."""
    # Use a stubbed synthesizer so the evidence pipeline actually
    # builds blocks. The chunk projector resolves blocks per
    # retrieved chunk; we provide chunk artifacts for each.
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus

    sources = [
        _source(artifact_id=f"a-{i}", kind="chunk", score=1.0 - i * 0.01)
        for i in range(20)
    ]
    engine = _SpyEngine(sources=sources)

    # Need a synthesizer + chunk bodies for build_evidence_blocks
    # to produce real blocks. Use a stub synthesizer + a real-ish
    # chunk projector path.
    from j1.validation.synthesis import SynthesisResult

    class _Synth:
        def synthesize(self, *, question, evidence):  # noqa: ARG002
            return SynthesisResult(
                answer="(synth)", provider="stub", model="m",
                latency_ms=0, prompt_tokens=0, completion_tokens=0,
                error=None,
            )

    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    chunk_dir = tmp_path / "compiled"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    artifact_records: list[ArtifactRecord] = []
    for i in range(20):
        ndjson = chunk_dir / f"a-{i}.ndjson"
        ndjson.write_text(
            '{"chunk_id":"chunk-a-' + str(i)
            + '","body":"Body of chunk ' + str(i) + '","page_start":1}\n',
            encoding="utf-8",
        )
        artifact_records.append(ArtifactRecord(
            artifact_id=f"a-{i}",
            project=ctx,
            kind="chunk",
            location=f"compiled/a-{i}.ndjson",
            content_hash=f"sha:a-{i}",
            byte_size=100,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now, updated_at=now,
            metadata={"run_id": "run-1"},
        ))

    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = artifact_records

    def _get(ctx_, artifact_id):  # noqa: ARG001
        for r in artifact_records:
            if r.artifact_id == artifact_id:
                return r
        from j1.artifacts.registry import ArtifactNotFoundError
        raise ArtifactNotFoundError(artifact_id)

    artifacts.get = _get

    workspace = MagicMock()
    workspace.area.return_value = chunk_dir

    run_store = MagicMock()
    run_store.get.return_value = _make_run()

    from j1.validation.service import IngestionValidationService

    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine,
        workspace=workspace,
        answer_synthesizer=_Synth(),
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(
            question="discuss",
            top_k=3,
            synthesize=True,
        ),
    )

    # 20 retrieved candidates, but the cap holds.
    assert response.debug["fts_returned_count"] == 20
    assert response.debug["evidence_max_blocks"] == 5
    assert response.debug["selected_evidence_count"] <= 5
    assert len(response.evidence_sent_to_llm) <= 5


# ---- Test D: isolation checks still fire --------------------------


def test_d_isolation_checks_still_present(ctx):
    """``retrieved_chunks_belong_to_run`` /
    ``citations_belong_to_run`` /
    ``no_cross_tenant_or_cross_project_leak`` must continue to
    run on the response. The decoupling fix is upstream of these
    checks — it changes WHICH candidates the engine returns, not
    HOW they're validated."""
    sources = [
        _source(artifact_id="a-1", kind="chunk", run_id="run-1"),
        _source(artifact_id="a-2", kind="chunk", run_id="run-1"),
    ]
    engine = _SpyEngine(sources=sources)
    svc = _build_service(engine=engine, candidate_top_k=20)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=3, synthesize=False),
    )

    check_names = {c.name for c in response.checks}
    assert "retrieved_chunks_belong_to_run" in check_names
    assert "citations_belong_to_run" in check_names
    assert "no_cross_tenant_or_cross_project_leak" in check_names
    # All three should pass — every source carries run_id=run-1.
    for c in response.checks:
        if c.name in {
            "retrieved_chunks_belong_to_run",
            "citations_belong_to_run",
            "no_cross_tenant_or_cross_project_leak",
        }:
            assert c.passed, f"isolation check regressed: {c}"


# ---- BM25 score propagation -------------------------------------


def test_e_bm25_score_propagates_to_retrieved_chunk(ctx):
    """Sanity: ``SourceReference.score`` flows through to
    ``RetrievedChunkRefDTO.score`` instead of being zeroed at
    the projection boundary. The reranker reads this value as
    its ``raw_score`` input — earlier it was always 0.0."""
    sources = [
        _source(artifact_id="a-1", kind="chunk", score=0.87),
        _source(artifact_id="a-2", kind="chunk", score=0.42),
    ]
    engine = _SpyEngine(sources=sources)
    svc = _build_service(engine=engine, candidate_top_k=20)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    by_id = {r.artifact_id: r for r in response.retrieved_chunks}
    assert by_id["a-1"].score == pytest.approx(0.87)
    assert by_id["a-2"].score == pytest.approx(0.42)
