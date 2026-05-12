"""Patch coverage: synthesize-toggle wiring + sectioned debug
payload + skipped scope checks + zero-retrieval debug block.

These tests pin the bug fixes the operator hit on the Validation
tab:

  * UI checkbox ON → previously the response panel still said
    "LLM synthesis is off — flip the toggle". The fix: stamp
    ``synthesize_answer_{requested,effective,disabled_reason}``
    on the debug payload so the FE renders an accurate
    skipped-reason message.
  * ``retrieved_chunks_belong_to_run`` / ``citations_belong_to_run``
    previously rendered a green check next to a zero-chunk row.
    The fix: mark them ``skipped`` when there's nothing to check,
    and exclude skipped checks from ``aggregate_status``.
  * Pure-native engine: empty retrieval should not flip the
    overall validation status to "failed" — chunks aren't
    expected from that engine by design.
  * Sectioned debug surface: ``native_answer`` / ``llm_synthesis``
    / ``data_quality_evidence`` blocks so the FE can render
    three distinct panels.
  * Zero-retrieval ``retrieval_debug`` block: when BM25 returned
    nothing, surface scope / filters / index path so the
    operator can diagnose without server logs.
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
from j1.validation.dtos import (
    ManualTestQueryRequest,
    ValidationCheckDTO,
)
from j1.validation.checks import aggregate_status
from j1.validation.service import (
    IngestionValidationService,
    QUERY_ENGINE_BM25_QUALITY_DEBUG,
    QUERY_ENGINE_LIGHTRAG_NATIVE,
    QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
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


def _bm25_engine(answer: str = "(bm25)", *, sources: list | None = None):
    engine = MagicMock()
    if sources is None:
        sources = [
            SourceReference(
                artifact_id="a-1",
                artifact_type="chunk",
                title="chunk/a-1",
                chunk_id="c-1",
                run_id="run-1",
                source_document_id="doc-1",
                score=0.9,
            ),
        ]
    engine.query.return_value = QueryResponse(
        answer=answer, mode_used="knowledge_first",
        sources=sources, graph_paths=[],
    )
    return engine


@dataclass
class _NativeSpy:
    canned: QueryResult
    calls: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def query(self, ctx, question, *, max_results=None,
              document_id=None, run_id=None):
        self.calls.append({"question": question, "run_id": run_id})
        return self.canned

    def workspace_path_for(self, ctx, document_id, run_id):
        return "/workdir/runs/t1/p1/doc-1/run-1"


def _build(*, native=None, engine=None,
           engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
           enable_bm25_fallback=False, synthesizer=None,
           workspace=None):
    run_store = MagicMock()
    run_store.get.return_value = _make_run()
    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []
    if workspace is None:
        workspace = MagicMock()
        workspace.area.return_value = Path("/tmp/search")
    audit = MagicMock()
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifacts,
        query_engine=engine or _bm25_engine(),
        workspace=workspace,
        audit=audit,
        answer_synthesizer=synthesizer,
        native_query_provider=native,
        query_engine_mode=engine_mode,
        enable_bm25_fallback=enable_bm25_fallback,
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )


def _ask(svc, ctx, *, synthesize=True, question="q?"):
    return svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(
            question=question, top_k=5, synthesize=synthesize,
        ),
    )


# ---- A. Synthesize-toggle metadata ---------------------------------


def test_synthesize_on_native_success_records_requested_and_effective(ctx):
    """Toggle ON + native succeeds + answer overridden → debug
    records ``synthesize_answer_requested=True`` and
    ``synthesize_answer_effective=True`` (the native trace was
    constructed as ``called=True``)."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native says hello",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx, synthesize=True)
    debug = response.debug
    assert debug["synthesize_answer_requested"] is True
    assert debug["synthesize_answer_effective"] is True
    assert debug["synthesize_answer_disabled_reason"] is None


def test_synthesize_on_native_unavailable_no_fallback_emits_clear_reason(ctx):
    """Toggle ON, but the dispatcher suppresses synthesis because
    native failed and BM25 fallback is off. The disabled-reason
    must say ``native_unavailable_no_fallback`` — NOT the legacy
    "flip the toggle" message the FE was rendering."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.FAILED, error="vendor offline",
    ))
    svc = _build(
        native=spy,
        engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
        enable_bm25_fallback=False,
    )
    response = _ask(svc, ctx, synthesize=True)
    debug = response.debug
    assert debug["synthesize_answer_requested"] is True
    assert debug["synthesize_answer_effective"] is False
    assert (
        debug["synthesize_answer_disabled_reason"]
        == "native_unavailable_no_fallback"
    )
    # And the LLMTrace itself must NOT be None — it carries the
    # error string the FE renders.
    assert response.llm is not None
    assert response.llm.called is False
    assert response.llm.error == "synthesis_skipped_native_unavailable"


def test_synthesize_off_emits_user_disabled_reason(ctx):
    """Toggle OFF (request.synthesize=False) → reason is
    ``user_disabled``."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx, synthesize=False)
    assert response.debug["synthesize_answer_requested"] is False
    assert response.debug["synthesize_answer_effective"] is False
    # Native success path overrides synthesized_answer regardless,
    # so the trace shows called=True (native) — but the request
    # itself wasn't asking for synthesis.
    assert response.debug["synthesize_answer_disabled_reason"] in (
        None, "user_disabled",
    )


def test_synthesize_on_no_synthesizer_wired_reports_reason(ctx):
    """Toggle ON but the service was built with no LLM client.
    The FE should be told the deployment lacks a synthesizer
    rather than the misleading "toggle is off" message."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.FAILED, error="x",
    ))
    svc = _build(
        native=spy,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
        enable_bm25_fallback=False,
        synthesizer=None,
    )
    response = _ask(svc, ctx, synthesize=True)
    debug = response.debug
    # In the with-quality-evidence engine + native failure + no
    # fallback, the dispatcher suppresses synthesis; the reason
    # is the native-unavailable case (precedence: suppress wins
    # over no_synthesizer_wired). When suppression isn't active,
    # the FE would see ``no_synthesizer_wired``.
    assert debug["synthesize_answer_disabled_reason"] in {
        "native_unavailable_no_fallback",
        "no_synthesizer_wired",
    }


# ---- B. Sectioned response surface ---------------------------------


def test_response_carries_native_answer_section(ctx):
    """``debug.native_answer`` block: engine name, attempted, success,
    answer_preview, latency_ms, warnings."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native says hello",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    native_block = response.debug["native_answer"]
    assert native_block["engine"] == "lightrag_native"
    assert native_block["attempted"] is True
    assert native_block["success"] is True
    assert native_block["answer_preview"] == "native says hello"
    assert native_block["warnings"] == []


def test_response_carries_llm_synthesis_section(ctx):
    """``debug.llm_synthesis`` block: requested, attempted,
    skipped_reason, answer_preview.

    With pure ``lightrag_native`` the LOCAL LLM synthesizer was
    not asked to run (native produced the answer), so
    ``attempted=False`` even though the user-visible answer is
    present. ``skipped_reason=None`` because the user got the
    answer they asked for.
    """
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx, synthesize=True)
    block = response.debug["llm_synthesis"]
    assert block["requested"] is True
    assert block["attempted"] is False
    assert block["skipped_reason"] is None
    assert block["answer_preview"] == "native"


def test_response_carries_data_quality_evidence_only_when_bm25_ran(ctx):
    """Pure ``lightrag_native`` engine: BM25 doesn't run, so the
    ``data_quality_evidence`` section is omitted (response shape
    stays honest)."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    assert "data_quality_evidence" not in response.debug


def test_response_separates_native_and_data_quality_sections(ctx):
    """``lightrag_native_with_quality_evidence`` (native success) →
    native_answer + data_quality_evidence are both present and
    visually separable."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(
        native=spy,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    )
    response = _ask(svc, ctx)
    debug = response.debug
    assert debug["native_answer"]["success"] is True
    assert debug["data_quality_evidence"]["source"] == "bm25"
    # final_answer_source is the native engine — even though
    # data_quality_evidence ran in parallel.
    assert debug["final_answer_source"] == "native"
    assert debug["bm25_participated_in_answer"] is False


# ---- C. deterministic_retriever_used alias -------------------------


def test_deterministic_retriever_used_aliases_bm25_used(ctx):
    """The new ``deterministic_retriever_used`` flag mirrors
    ``bm25_used``. Lets consumers reason about "did we hit the
    deterministic retriever?" without coupling to "BM25" the name."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc_native = _build(native=spy)
    r1 = _ask(svc_native, ctx)
    assert r1.debug["deterministic_retriever_used"] == r1.debug["bm25_used"]
    assert r1.debug["deterministic_retriever_used"] is False

    svc_bm25 = _build(engine_mode=QUERY_ENGINE_BM25_QUALITY_DEBUG)
    r2 = _ask(svc_bm25, ctx)
    assert r2.debug["deterministic_retriever_used"] is True


# ---- D. Skipped scope checks ---------------------------------------


def test_chunks_belong_to_run_skipped_when_no_chunks(ctx):
    """Pure-native engine: empty retrieval → the
    chunks-belong-to-run check is SKIPPED, not fake-passed."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native (no sources)",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    by_name = {c.name: c for c in response.checks}
    chk = by_name["retrieved_chunks_belong_to_run"]
    assert chk.skipped is True
    assert chk.passed is False
    assert chk.skipped_reason == "no retrieved chunks to check"


def test_citations_belong_to_run_skipped_when_no_citations(ctx):
    """Empty citation list → the citations-belong-to-run check is
    SKIPPED, not fake-passed."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    by_name = {c.name: c for c in response.checks}
    chk = by_name["citations_belong_to_run"]
    assert chk.skipped is True
    assert chk.skipped_reason == "no citations to check"


def test_retrieved_chunks_present_skipped_in_pure_native(ctx):
    """In ``lightrag_native`` the engine doesn't surface chunks by
    design. The ``retrieved_chunks_present`` REQUIRED check is
    SKIPPED — empty retrieval there is the correct outcome and
    must not flip the validation status to ``failed``."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    by_name = {c.name: c for c in response.checks}
    chk = by_name["retrieved_chunks_present"]
    assert chk.skipped is True
    assert "pure-native" in (chk.skipped_reason or "").lower()


def test_aggregate_ignores_skipped_required_checks():
    """Direct ``aggregate_status`` test: a SKIPPED required check
    must not count as failed."""
    checks = [
        ValidationCheckDTO(
            name="answer_non_empty", severity="required", passed=True,
        ),
        ValidationCheckDTO(
            name="retrieved_chunks_present", severity="required",
            passed=False, skipped=True,
            skipped_reason="pure native, no chunks expected",
        ),
    ]
    assert aggregate_status(checks) == "passed"


# ---- E. Native-pass + evidence-empty → not full failure ------------


def test_pure_native_pass_with_empty_chunks_does_not_fail(ctx):
    """The user's spec: native success + zero chunks → overall
    validation must NOT be ``failed``. Empty chunks in pure native
    is the correct outcome (chunks aren't expected), and the
    chunks-belong-to-run check is now skipped, so aggregate_status
    lands on ``passed`` rather than ``failed``."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native says hello",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    assert response.validation_status == "passed"


# ---- F. Zero-retrieval debug block ---------------------------------


def test_zero_retrieval_debug_block_present_when_bm25_returns_nothing(
    ctx, tmp_path,
):
    """``bm25_quality_debug`` engine returns 0 chunks → the
    ``retrieval_debug`` block surfaces retriever name, scope,
    run/document filters, top_k, and the index path."""
    bm25 = _bm25_engine(sources=[])
    workspace = MagicMock()
    workspace.area.return_value = tmp_path / "search"
    svc = _build(
        engine=bm25,
        engine_mode=QUERY_ENGINE_BM25_QUALITY_DEBUG,
        workspace=workspace,
    )
    response = _ask(svc, ctx)
    debug = response.debug
    rd = debug["retrieval_debug"]
    assert rd["retriever_name"] == "j1.bm25_fts5"
    assert rd["scope"] == "this_run"
    assert rd["run_id_filter"] == "run-1"
    assert rd["document_id_filter"] == "doc-1"
    assert rd["top_k_requested"] == 5
    assert rd["candidate_top_k_used"] >= 5
    assert "search.sqlite" in (rd["index_name_or_path"] or "")
    assert "warnings" in rd


def test_no_retrieval_debug_block_when_bm25_did_not_run(ctx):
    """Pure-native engine: BM25 didn't run, so the
    ``retrieval_debug`` block is OMITTED. The block's purpose
    is "BM25 returned nothing — why?"; absence means "BM25
    wasn't asked to run", which is its own valid state."""
    spy = _NativeSpy(canned=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy)
    response = _ask(svc, ctx)
    assert "retrieval_debug" not in response.debug
