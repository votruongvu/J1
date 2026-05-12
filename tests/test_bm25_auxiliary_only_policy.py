"""Role-policy tests: BM25 is auxiliary-only in the answer path.

These tests pin the post-clarification rule: BM25 may surface
chunks / citations / metadata-quality diagnostics but MUST NOT
drive the user-visible answer text unless an explicit fallback /
debug / observability engine is selected.

Coverage:
  * Default ``lightrag_native`` engine: BM25 never runs, BM25
    flags are false / null.
  * ``lightrag_native_with_quality_evidence`` (native success):
    BM25 ran for evidence, but ``bm25_participated_in_answer=False``
    and ``bm25_purpose="data_quality_evidence_inspection"``.
  * ``lightrag_native_with_quality_evidence`` (native failure,
    fallback OFF): answer is empty, synthesis is suppressed, BM25
    evidence is still on the response shell for inspection.
  * ``lightrag_native_with_quality_evidence`` (native failure,
    fallback ON): ``bm25_participated_in_answer=True`` and
    ``bm25_purpose="fallback_answer"``.
  * ``bm25_quality_debug``: BM25 IS the answer (debug-only); flag
    is true with ``lexical_debug_answer`` purpose.
  * ``hybrid_ab``: BM25 IS the stable answer (observability);
    flag is true with ``observability_answer`` purpose.
  * ``data_quality_evidence`` section is attached whenever BM25
    runs and surfaces metadata-quality flags.
  * Missing metadata on BM25 chunks is flagged as a STORAGE /
    REGISTRATION issue, not a BM25 issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from j1.processing.results import QueryResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.query.models import QueryResponse, SourceReference
from j1.runs.models import IngestionRun, RunStatus
from j1.validation.dtos import ManualTestQueryRequest, RetrievedChunkRefDTO
from j1.validation.service import (
    BM25_PURPOSE_DATA_QUALITY,
    BM25_PURPOSE_FALLBACK_ANSWER,
    BM25_PURPOSE_LEXICAL_DEBUG,
    BM25_PURPOSE_OBSERVABILITY,
    IngestionValidationService,
    QUERY_ENGINE_BM25_QUALITY_DEBUG,
    QUERY_ENGINE_HYBRID_AB,
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


def _bm25_engine(
    answer: str = "(bm25 answer)",
    *,
    with_sources: bool = True,
    incomplete_metadata: bool = False,
):
    """Stub BM25 engine. ``incomplete_metadata=True`` simulates the
    storage-layer-regression case where a chunk lacks one of its
    identifiers."""
    engine = MagicMock()
    sources: list[SourceReference] = []
    if with_sources:
        sources = [
            SourceReference(
                artifact_id="a-1",
                artifact_type="chunk",
                title="chunk/a-1",
                chunk_id="c-1",
                run_id=None if incomplete_metadata else "run-1",
                source_document_id="doc-1",
                score=0.9,
            ),
        ]
    engine.query.return_value = QueryResponse(
        answer=answer,
        mode_used="knowledge_first",
        sources=sources,
        graph_paths=[],
    )
    return engine


@dataclass
class _NativeProviderSpy:
    canned_result: QueryResult
    calls: list[dict]

    def __init__(self, *, canned_result):
        self.canned_result = canned_result
        self.calls = []

    def query(self, ctx, question, *, max_results=None,
              document_id=None, run_id=None):
        self.calls.append({
            "ctx": ctx, "question": question, "max_results": max_results,
            "document_id": document_id, "run_id": run_id,
        })
        return self.canned_result

    def workspace_path_for(self, ctx, document_id, run_id):
        return "/workdir/runs/t1/p1/doc-1/run-1"


def _build(*, native=None, engine=None,
           engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
           enable_bm25_evidence=False, enable_bm25_fallback=False,
           synthesizer=None):
    """Build a service with mocked dependencies."""
    run_store = MagicMock()
    run_store.get.return_value = _make_run()
    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = []
    workspace = MagicMock()
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
        enable_bm25_evidence=enable_bm25_evidence,
        enable_bm25_fallback=enable_bm25_fallback,
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )


def _ask(svc, ctx, *, synthesize=False, question="q?"):
    return svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(
            question=question, top_k=5, synthesize=synthesize,
        ),
    )


# ---- A. Default lightrag_native: BM25 untouched ---------------------


def test_default_lightrag_native_bm25_never_runs(ctx):
    """Pure native engine: BM25 engine method is never invoked, and
    every BM25 flag reports the auxiliary-only contract."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native answer",
    ))
    bm25_engine = _bm25_engine()
    svc = _build(native=spy, engine=bm25_engine)
    response = _ask(svc, ctx)
    debug = response.debug

    assert bm25_engine.query.call_count == 0
    assert debug["bm25_used"] is False
    assert debug["bm25_participated_in_answer"] is False
    assert debug["bm25_purpose"] is None
    assert debug["final_answer_source"] == "native"
    assert debug["query_engine"] == QUERY_ENGINE_LIGHTRAG_NATIVE
    # The data-quality section is NOT attached when BM25 didn't
    # run — keep the response shape honest.
    assert "data_quality_evidence" not in debug


# ---- B. lightrag_native_with_quality_evidence (native success) ------


def test_with_quality_evidence_native_success_keeps_bm25_out_of_answer(
    ctx,
):
    """Native answer wins; BM25 ran for citations only.
    ``bm25_participated_in_answer`` MUST be False even though
    BM25 contributed citations. ``bm25_purpose`` is
    ``data_quality_evidence_inspection``."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native answer text",
    ))
    bm25_engine = _bm25_engine(answer="bm25 prose (should NOT win)")
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    )
    response = _ask(svc, ctx)
    debug = response.debug

    assert bm25_engine.query.call_count == 1  # BM25 ran for evidence
    assert debug["bm25_used"] is True
    assert debug["bm25_participated_in_answer"] is False
    assert debug["bm25_purpose"] == BM25_PURPOSE_DATA_QUALITY
    assert debug["final_answer_source"] == "native"
    assert response.synthesized_answer == "native answer text"
    # Data-quality section attached.
    assert "data_quality_evidence" in debug
    dq = debug["data_quality_evidence"]
    assert dq["source"] == "bm25"
    assert dq["purpose"] == BM25_PURPOSE_DATA_QUALITY


# ---- C. native failure, fallback OFF — strict policy ---------------


def test_with_quality_evidence_native_fail_no_fallback_empty_answer(
    ctx,
):
    """Native fails AND fallback is OFF: answer must be empty,
    synthesis is suppressed, BM25 evidence is still attached for
    inspection but ``bm25_participated_in_answer=False``."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="connection refused",
    ))
    bm25_engine = _bm25_engine(answer="bm25 would have answered")
    # Synthesizer must NEVER be called when suppress_synthesis is
    # active — assert by passing a MagicMock and inspecting it.
    synth = MagicMock()
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
        enable_bm25_fallback=False,
        synthesizer=synth,
    )
    response = _ask(svc, ctx, synthesize=True)
    debug = response.debug

    assert response.answer == ""
    assert response.synthesized_answer is None  # synthesis suppressed
    assert synth.synthesize.call_count == 0
    assert debug["bm25_used"] is True  # BM25 ran for evidence
    assert debug["bm25_participated_in_answer"] is False
    assert debug["bm25_purpose"] == BM25_PURPOSE_DATA_QUALITY
    assert debug["final_answer_source"] == "native_unavailable"
    assert debug["fallback_used"] is False
    assert "native_unavailable_no_fallback" in debug["warnings"]


def test_pure_native_fail_no_fallback_does_not_call_bm25(ctx):
    """``lightrag_native`` engine: a native failure without fallback
    does NOT trigger a BM25 call. The engine name is the contract."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="connection refused",
    ))
    bm25_engine = _bm25_engine()
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
        enable_bm25_fallback=False,
    )
    response = _ask(svc, ctx)
    debug = response.debug

    assert bm25_engine.query.call_count == 0
    assert debug["bm25_used"] is False
    assert debug["bm25_participated_in_answer"] is False
    assert debug["bm25_purpose"] is None
    assert debug["final_answer_source"] == "native_unavailable"


# ---- D. native failure, fallback ON — explicit opt-in path ---------


def test_native_fail_with_explicit_fallback_marks_bm25_in_answer(ctx):
    """Operator-level explicit opt-in: ``enable_bm25_fallback=True``
    AND native fails → BM25 supplies the answer. Audit fields must
    record this prominently."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="vendor offline",
    ))
    bm25_engine = _bm25_engine(answer="bm25 fallback answer")
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
        enable_bm25_fallback=True,
    )
    response = _ask(svc, ctx)
    debug = response.debug

    assert debug["bm25_used"] is True
    assert debug["fallback_used"] is True
    assert debug["bm25_participated_in_answer"] is True
    assert debug["bm25_purpose"] == BM25_PURPOSE_FALLBACK_ANSWER
    assert debug["final_answer_source"] == "bm25_fallback"
    assert "native_failed_fallback_to_bm25" in debug["warnings"]


# ---- E. bm25_quality_debug: explicit debug engine -------------------


def test_bm25_quality_debug_engine_marks_bm25_in_answer(ctx):
    """The diagnostic engine intentionally produces a BM25 answer.
    The flag must report that — the engine name + purpose tell
    operators not to treat it as a real answer."""
    bm25_engine = _bm25_engine(answer="bm25 debug answer")
    svc = _build(
        native=None,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_BM25_QUALITY_DEBUG,
    )
    response = _ask(svc, ctx)
    debug = response.debug

    assert debug["query_engine"] == QUERY_ENGINE_BM25_QUALITY_DEBUG
    assert debug["bm25_used"] is True
    assert debug["bm25_participated_in_answer"] is True
    assert debug["bm25_purpose"] == BM25_PURPOSE_LEXICAL_DEBUG
    assert debug["final_answer_source"] == "bm25"


# ---- F. hybrid_ab: observability engine ----------------------------


def test_hybrid_ab_marks_bm25_observability_purpose(ctx):
    """In the observability A/B engine BM25 IS the stable answer.
    The flag must say so — explicitly tagged as observability so
    nobody mistakes it for the production answer path."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="experimental native",
    ))
    bm25_engine = _bm25_engine(answer="stable bm25 answer")
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_HYBRID_AB,
    )
    response = _ask(svc, ctx)
    debug = response.debug

    assert debug["bm25_used"] is True
    assert debug["bm25_participated_in_answer"] is True
    assert debug["bm25_purpose"] == BM25_PURPOSE_OBSERVABILITY
    assert debug["final_answer_source"] == "bm25"


# ---- G. Data-quality section + metadata-quality flags --------------


def test_data_quality_section_reports_metadata_present(ctx):
    """When BM25 chunks carry every identifier, the metadata-quality
    flags are all true and no per-chunk warnings are emitted."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    bm25_engine = _bm25_engine()
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    )
    response = _ask(svc, ctx)
    dq = response.debug["data_quality_evidence"]
    assert dq["source"] == "bm25"
    assert dq["match_count"] >= 1
    assert dq["metadata_quality"]["run_id_present"] is True
    assert dq["metadata_quality"]["document_id_present"] is True
    assert dq["metadata_quality"]["artifact_id_present"] is True
    assert dq["warnings"] == []


def test_data_quality_section_flags_storage_layer_regression(ctx):
    """A BM25 chunk missing ``run_id`` indicates a STORAGE /
    REGISTRATION layer regression — BM25 only surfaces what was
    registered. The data-quality block must flag it so operators
    fix it upstream of BM25."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    bm25_engine = _bm25_engine(incomplete_metadata=True)
    svc = _build(
        native=spy,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    )
    response = _ask(svc, ctx)
    dq = response.debug["data_quality_evidence"]
    assert dq["metadata_quality"]["run_id_present"] is False
    assert "run_id_missing_on_some_chunks" in dq["warnings"]


# ---- H. Canonical metadata aliases ---------------------------------


def test_evidence_source_alias_present(ctx):
    """``evidence_source`` is the canonical name for citation /
    evidence provider. It mirrors ``citation_source`` so FE code
    can use either name."""
    bm25_engine = _bm25_engine()
    svc = _build(
        native=None,
        engine=bm25_engine,
        engine_mode=QUERY_ENGINE_BM25_QUALITY_DEBUG,
    )
    response = _ask(svc, ctx)
    debug = response.debug
    assert debug["evidence_source"] == debug["citation_source"]


def test_final_answer_source_alias_matches_answer_source(ctx):
    """``final_answer_source`` mirrors ``answer_source`` so the
    response shape is unambiguous across legacy + new callers."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native",
    ))
    svc = _build(native=spy, engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE)
    response = _ask(svc, ctx)
    debug = response.debug
    assert debug["final_answer_source"] == debug["answer_source"]


# ---- I. Legacy mode-name aliases keep working ----------------------


def test_legacy_bm25_debug_alias_resolves_to_quality_debug(ctx):
    """The previous V2 name ``bm25_debug`` (introduced after the
    audit-pass refactor) maps to the new canonical
    ``bm25_quality_debug``."""
    svc = _build(native=None, engine_mode="bm25_debug")
    assert svc._query_engine_mode == QUERY_ENGINE_BM25_QUALITY_DEBUG  # noqa: SLF001


def test_legacy_lightrag_native_with_bm25_evidence_alias_resolves(ctx):
    """The previous V2 name maps to
    ``lightrag_native_with_quality_evidence``."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    svc = _build(
        native=spy, engine_mode="lightrag_native_with_bm25_evidence",
    )
    assert (
        svc._query_engine_mode  # noqa: SLF001
        == QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
    )


def test_legacy_v1_aliases_still_work(ctx):
    """V1 strings (``bm25_primary`` / ``rag_native_primary``) still
    alias to the V3 canonical names."""
    svc_bm25 = _build(native=None, engine_mode="bm25_primary")
    assert (
        svc_bm25._query_engine_mode  # noqa: SLF001
        == QUERY_ENGINE_BM25_QUALITY_DEBUG
    )
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    svc_native = _build(native=spy, engine_mode="rag_native_primary")
    assert (
        svc_native._query_engine_mode  # noqa: SLF001
        == QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
    )
