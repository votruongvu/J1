"""Regression tests for the refined debug payload that disambiguates
which provider produced the answer, evidence, and citations.

The previous debug surface lumped provider tags into a single
``query_provider_mode`` field — operators had to infer the rest
("did native produce the answer? did BM25 augment citations?").
This refinement exposes the answer provider, evidence provider,
citation provider, and per-provider answer previews as explicit
top-level fields so a single debug snapshot is unambiguous.

Also pins the Q4 / Q7 retest protocol: each mode produces a
stable shape so the operator can run the same question across
modes and compare apples-to-apples.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from j1.processing.results import QueryResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.query.models import QueryResponse, SourceReference
from j1.runs.models import IngestionRun, RunStatus
from j1.validation.dtos import ManualTestQueryRequest
from j1.validation.service import (
    IngestionValidationService,
    QUERY_PROVIDER_MODE_BM25,
    QUERY_PROVIDER_MODE_HYBRID_AB,
    QUERY_PROVIDER_MODE_NATIVE,
    _answer_preview,
    _query_anchors_in_evidence,
)


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _make_run() -> IngestionRun:
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


def _bm25_engine(answer="(bm25 deterministic)", sources=None):
    engine = MagicMock()
    src = sources or [
        SourceReference(
            artifact_id="a-1", artifact_type="chunk",
            title="chunk/a-1", chunk_id="c-1", run_id="run-1", score=0.9,
        ),
    ]
    engine.query.return_value = QueryResponse(
        answer=answer, mode_used="knowledge_first",
        sources=src, graph_paths=[],
    )
    return engine


@dataclass
class _NativeSpy:
    answer: str | None
    status: ResultStatus = ResultStatus.SUCCEEDED

    def query(self, ctx, question, *, max_results=None,
              document_id=None, run_id=None):
        return QueryResult(
            status=self.status,
            answer=self.answer,
            error=None if self.status is ResultStatus.SUCCEEDED else "boom",
        )


def _build(*, engine=None, native=None, mode=QUERY_PROVIDER_MODE_BM25,
           fallback=True):
    engine = engine or _bm25_engine()
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
        answer_synthesizer=None,
        native_query_provider=native,
        query_provider_mode=mode,
        native_query_fallback_to_bm25=fallback,
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )


# ---- _answer_preview helper ------------------------------------


def test_answer_preview_truncates_long_answers():
    long_answer = "X" * 500
    p = _answer_preview(long_answer)
    assert len(p) <= 241  # 240 + the trailing "…"
    assert p.endswith("…")


def test_answer_preview_passes_through_short_answers():
    assert _answer_preview("Short.") == "Short."


def test_answer_preview_handles_none_and_empty():
    assert _answer_preview(None) == ""
    assert _answer_preview("") == ""
    assert _answer_preview("   ") == ""


# ---- _query_anchors_in_evidence helper -------------------------


def test_query_anchors_in_evidence_finds_token_match():
    from j1.validation.dtos import EvidenceBlockDTO

    blocks = [EvidenceBlockDTO(
        artifact_id="a", artifact_type="chunk",
        text="The proposal due date is 20 May 2026.",
    )]
    assert _query_anchors_in_evidence(
        evidence_blocks=blocks,
        question="What is the proposal due date?",
    ) is True


def test_query_anchors_in_evidence_no_match():
    from j1.validation.dtos import EvidenceBlockDTO

    blocks = [EvidenceBlockDTO(
        artifact_id="a", artifact_type="chunk",
        text="An unrelated paragraph about clouds.",
    )]
    assert _query_anchors_in_evidence(
        evidence_blocks=blocks,
        question="What is the proposal due date?",
    ) is False


def test_query_anchors_in_evidence_empty_inputs():
    assert _query_anchors_in_evidence(
        evidence_blocks=[], question="anything",
    ) is False
    assert _query_anchors_in_evidence(
        evidence_blocks=[], question=None,
    ) is False


# ---- Debug provider-role fields (bm25_primary) -----------------


def test_bm25_primary_debug_marks_answer_provider_as_bm25(ctx):
    svc = _build(mode=QUERY_PROVIDER_MODE_BM25)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="anything?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["query_provider_mode"] == QUERY_PROVIDER_MODE_BM25
    assert debug["answer_provider"] == "bm25"
    assert debug["evidence_provider"] == "bm25"
    assert debug["citation_provider"] == "bm25"
    assert debug["bm25_query_used"] is True
    assert debug["native_query_used"] is False
    assert debug["fallback_used"] is False
    assert debug["citation_augmentation_used"] is False


def test_bm25_primary_debug_carries_bm25_answer_preview_only(ctx):
    svc = _build(
        engine=_bm25_engine(answer="The proposal date is 20 May 2026."),
        mode=QUERY_PROVIDER_MODE_BM25,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["bm25_answer_preview"] == "The proposal date is 20 May 2026."
    assert debug["native_answer_preview"] is None


# ---- Debug provider-role fields (rag_native_primary) -----------


def test_native_success_debug_marks_answer_provider_as_native(ctx):
    spy = _NativeSpy(answer="Native LightRAG answer.")
    svc = _build(native=spy, mode=QUERY_PROVIDER_MODE_NATIVE)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    # Answer text comes from native; evidence + citations from BM25.
    assert debug["answer_provider"] == "native"
    assert debug["evidence_provider"] == "bm25"
    assert debug["citation_provider"] == "bm25"
    # Both providers ran.
    assert debug["native_query_used"] is True
    assert debug["bm25_query_used"] is True
    # Augmentation marked.
    assert debug["citation_augmentation_used"] is True
    # Previews from both sides.
    assert debug["native_answer_preview"] == "Native LightRAG answer."
    assert debug["bm25_answer_preview"] is not None


def test_native_failure_with_fallback_marks_answer_provider_as_fallback(ctx):
    spy = _NativeSpy(answer=None, status=ResultStatus.FAILED)
    svc = _build(
        native=spy, mode=QUERY_PROVIDER_MODE_NATIVE, fallback=True,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    # Native attempted but didn't produce an answer.
    assert debug["answer_provider"] == "bm25_fallback"
    assert debug["native_query_used"] is False
    assert debug["bm25_query_used"] is True
    assert debug["fallback_used"] is True
    # Native answer preview cleared on failure; BM25 preview present.
    assert debug["native_answer_preview"] is None
    assert debug["bm25_answer_preview"] is not None
    assert "boom" in (debug.get("native_query_failed_reason") or "")


# ---- Debug provider-role fields (hybrid_ab) --------------------


def test_hybrid_ab_debug_marks_answer_provider_as_bm25_with_both_previews(ctx):
    spy = _NativeSpy(answer="Native experimental answer.")
    svc = _build(native=spy, mode=QUERY_PROVIDER_MODE_HYBRID_AB)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    # BM25 is the stable answer in hybrid_ab.
    assert debug["answer_provider"] == "bm25"
    # But native ran for observability.
    assert debug["native_query_used"] is True
    assert debug["bm25_query_used"] is True
    # Both previews carried at the top level (no longer just
    # under hybrid_ab_* legacy keys).
    assert debug["bm25_answer_preview"] is not None
    assert debug["native_answer_preview"] == "Native experimental answer."
    # Legacy aliases retained for one release.
    assert debug["hybrid_ab_bm25_answer_preview"] is not None
    assert debug["hybrid_ab_native_answer_preview"] is not None


# ---- K semantics fields ----------------------------------------


def test_debug_carries_explicit_k_breakdown_fields(ctx):
    svc = _build(mode=QUERY_PROVIDER_MODE_BM25)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=3, synthesize=False),
    )
    debug = response.debug
    # FE requested 3, candidate floor 20 → both visible.
    assert debug["requested_top_k"] == 3
    assert debug["candidate_top_k_used"] == 20
    # Both new + legacy field names for the count.
    assert "raw_candidate_count" in debug
    assert "fts_returned_count" in debug
    assert debug["raw_candidate_count"] == debug["fts_returned_count"]
    # Selection cap independent.
    assert debug["evidence_max_blocks"] == 5
    assert "selected_evidence_count" in debug
    # Per-spec selected-evidence preview field (alias of
    # ``top_evidence_preview``).
    assert "selected_evidence_preview" in debug
    assert debug["selected_evidence_preview"] == debug["top_evidence_preview"]
    # Selected-evidence kinds + raw-candidate kinds explicit.
    assert "raw_candidate_kinds" in debug
    assert "selected_evidence_kinds" in debug


# ---- query_anchors_in_evidence integration ---------------------


def test_query_anchors_in_evidence_field_present_in_debug(ctx):
    svc = _build(mode=QUERY_PROVIDER_MODE_BM25)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert "query_anchors_in_evidence" in response.debug
    # No synthesizer wired → no evidence loaded → flag is False.
    assert response.debug["query_anchors_in_evidence"] is False


# ---- Q4/Q7 retest protocol — shape verification ----------------
#
# Per the operator's request, each mode produces the SAME debug
# fields so a single retest harness can compare them
# apples-to-apples. These tests pin the field set across all
# three modes — if a future change drops one of these fields
# from one mode but not the others, the test fails loudly.

_REQUIRED_DEBUG_KEYS_ACROSS_MODES = frozenset({
    # Provider roles.
    "query_provider_mode",
    "answer_provider",
    "evidence_provider",
    "citation_provider",
    "native_query_enabled",
    "native_query_used",
    "bm25_query_used",
    "fallback_used",
    "citation_augmentation_used",
    # Latencies (both ms ints).
    "bm25_latency_ms",
    # Answer previews.
    "bm25_answer_preview",
    "native_answer_preview",
    # K semantics.
    "requested_top_k",
    "candidate_top_k_used",
    "evidence_max_blocks",
    "raw_candidate_count",
    "selected_evidence_count",
    "raw_candidate_kinds",
    "selected_evidence_kinds",
    "selected_evidence_preview",
    "query_anchors_in_evidence",
    # Isolation.
    "scope_run_id",
})


@pytest.mark.parametrize(
    "mode",
    [
        QUERY_PROVIDER_MODE_BM25,
        QUERY_PROVIDER_MODE_NATIVE,
        QUERY_PROVIDER_MODE_HYBRID_AB,
    ],
)
def test_retest_protocol_debug_shape_stable_across_modes(ctx, mode):
    """All three modes emit the SAME top-level debug field set.

    The Q4/Q7 retest harness reads these by name; if one mode
    starts producing a different shape, the retest comparison
    breaks. This test pins the contract.
    """
    spy = _NativeSpy(answer="Stub native answer.")
    svc = _build(native=spy, mode=mode)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    missing = _REQUIRED_DEBUG_KEYS_ACROSS_MODES - set(response.debug.keys())
    assert not missing, (
        f"debug payload for mode={mode!r} is missing required keys: "
        f"{sorted(missing)}"
    )
    # query_provider_mode reflects the configured mode.
    assert response.debug["query_provider_mode"] == mode


# ---- Cross-mode reproducibility shape --------------------------


def test_retest_protocol_answer_provider_by_mode(ctx):
    """Per-mode answer-provider mapping:
      bm25_primary       → "bm25"
      rag_native_primary → "native" on success
                         → "bm25_fallback" on native failure
      hybrid_ab          → "bm25" always (stable answer)
    """
    # bm25_primary
    svc1 = _build(
        native=_NativeSpy(answer="native"),
        mode=QUERY_PROVIDER_MODE_BM25,
    )
    r1 = svc1.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert r1.debug["answer_provider"] == "bm25"

    # rag_native_primary, success → native
    svc2 = _build(
        native=_NativeSpy(answer="native ok"),
        mode=QUERY_PROVIDER_MODE_NATIVE,
    )
    r2 = svc2.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert r2.debug["answer_provider"] == "native"

    # rag_native_primary, native fail → bm25_fallback
    svc3 = _build(
        native=_NativeSpy(answer=None, status=ResultStatus.FAILED),
        mode=QUERY_PROVIDER_MODE_NATIVE,
        fallback=True,
    )
    r3 = svc3.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert r3.debug["answer_provider"] == "bm25_fallback"

    # hybrid_ab → bm25 (stable) always
    svc4 = _build(
        native=_NativeSpy(answer="native experimental"),
        mode=QUERY_PROVIDER_MODE_HYBRID_AB,
    )
    r4 = svc4.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert r4.debug["answer_provider"] == "bm25"
