"""Post-audit refactor coverage: pure ``lightrag_native`` engine,
canonical response metadata, native-debug-query endpoint, and
backward-compat alias mapping.

The earlier ``test_query_provider_mode_dispatch.py`` covers the
three legacy modes (``bm25_primary`` / ``rag_native_primary`` /
``hybrid_ab``). This file focuses on the post-audit additions:

  * ``lightrag_native`` — PURE native, no BM25 unless fallback is
    explicitly enabled.
  * Canonical metadata stamped on every response
    (``query_engine`` / ``answer_source`` / ``citation_source`` /
    ``bm25_used`` / ``workspace_path`` / ``warnings``).
  * Direct native-debug query path (service method + REST
    contract).
  * Legacy-alias mapping (old strings still accepted).
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
from j1.validation.dtos import ManualTestQueryRequest
from j1.validation.service import (
    IngestionValidationService,
    QUERY_ENGINE_BM25_DEBUG,
    QUERY_ENGINE_HYBRID_AB,
    QUERY_ENGINE_LIGHTRAG_NATIVE,
    QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE,
    QUERY_PROVIDER_MODE_BM25,
    QUERY_PROVIDER_MODE_NATIVE,
    _QUERY_ENGINE_ALIASES,
    _VALID_QUERY_ENGINES,
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


def _bm25_engine(answer: str = "(bm25 answer)", with_sources: bool = True):
    engine = MagicMock()
    sources = []
    if with_sources:
        sources = [
            SourceReference(
                artifact_id="a-1", artifact_type="chunk",
                title="chunk/a-1", chunk_id="c-1", run_id="run-1",
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
    workspace_path_value: str | None = "/workdir/runs/t1/p1/doc-1/run-1"

    def __init__(self, *, canned_result, workspace_path_value=None):
        self.canned_result = canned_result
        self.calls = []
        self.workspace_path_value = (
            workspace_path_value
            if workspace_path_value is not None
            else "/workdir/runs/t1/p1/doc-1/run-1"
        )

    def query(self, ctx, question, *, max_results=None,
              document_id=None, run_id=None):
        self.calls.append({
            "ctx": ctx, "question": question, "max_results": max_results,
            "document_id": document_id, "run_id": run_id,
        })
        return self.canned_result

    def workspace_path_for(self, ctx, document_id, run_id):
        return self.workspace_path_value


def _build(*, native=None, engine=None,
           engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
           enable_bm25_evidence=False, enable_bm25_fallback=False,
           timeout=30.0):
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
        answer_synthesizer=None,
        native_query_provider=native,
        query_engine_mode=engine_mode,
        enable_bm25_evidence=enable_bm25_evidence,
        enable_bm25_fallback=enable_bm25_fallback,
        native_query_timeout_seconds=timeout,
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )


# ---- 1. Default engine is lightrag_native ---------------------------


def test_default_engine_is_lightrag_native(ctx):
    """No engine_mode supplied + native provider wired → engine
    defaults to pure ``lightrag_native``. Audit-driven change."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native answer",
    ))
    svc = IngestionValidationService(
        run_store=MagicMock(get=MagicMock(return_value=_make_run())),
        artifact_registry=MagicMock(list_artifacts=MagicMock(return_value=[])),
        query_engine=_bm25_engine(),
        workspace=MagicMock(),
        audit=MagicMock(),
        answer_synthesizer=None,
        native_query_provider=spy,
    )
    assert svc._query_engine_mode == QUERY_ENGINE_LIGHTRAG_NATIVE  # noqa: SLF001


def test_default_without_native_provider_demotes_to_bm25_debug(ctx):
    """Default engine_mode + no native provider wired → demote to
    ``bm25_debug`` so deployments without RAGAnything still work."""
    svc = IngestionValidationService(
        run_store=MagicMock(get=MagicMock(return_value=_make_run())),
        artifact_registry=MagicMock(list_artifacts=MagicMock(return_value=[])),
        query_engine=_bm25_engine(),
        workspace=MagicMock(),
        audit=MagicMock(),
        answer_synthesizer=None,
        native_query_provider=None,
    )
    assert svc._query_engine_mode == QUERY_ENGINE_BM25_DEBUG  # noqa: SLF001


# ---- 2. Pure lightrag_native dispatch -------------------------------


def test_lightrag_native_success_does_not_call_bm25(ctx):
    """Pure native success → BM25 engine never invoked. Response
    carries empty sources (honest report: native didn't supply
    them)."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="pure native answer",
    ))
    engine = _bm25_engine()
    svc = _build(native=spy, engine=engine)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["query_engine"] == QUERY_ENGINE_LIGHTRAG_NATIVE
    assert debug["answer_source"] == "native"
    assert debug["bm25_used"] is False
    assert debug["citation_source"] == "none_or_native_unavailable"
    assert debug["fallback_used"] is False
    assert engine.query.call_count == 0
    # Sources / citations / retrieved are all empty.
    assert response.citations == []
    assert response.retrieved_chunks == []


def test_lightrag_native_failure_no_fallback_returns_native_unavailable(ctx):
    """Pure native failure + fallback off → answer_source=
    ``native_unavailable``, BM25 never runs."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="vendor down",
    ))
    engine = _bm25_engine()
    svc = _build(
        native=spy, engine=engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
        enable_bm25_fallback=False,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["answer_source"] == "native_unavailable"
    assert debug["bm25_used"] is False
    assert engine.query.call_count == 0
    assert "native_unavailable_no_fallback" in debug["warnings"]


def test_lightrag_native_failure_with_fallback_uses_bm25(ctx):
    """Pure native failure + fallback on → BM25 runs as a fallback;
    ``fallback_used=true``, ``bm25_used=true``, warning stamped."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="vendor down",
    ))
    engine = _bm25_engine(answer="bm25 fallback answer")
    svc = _build(
        native=spy, engine=engine,
        engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
        enable_bm25_fallback=True,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["fallback_used"] is True
    assert debug["bm25_used"] is True
    assert debug["answer_source"] == "bm25_fallback"
    assert engine.query.call_count == 1
    assert "native_failed_fallback_to_bm25" in debug["warnings"]


# ---- 3. enable_bm25_evidence promotion ------------------------------


def test_enable_bm25_evidence_promotes_to_native_with_evidence(ctx):
    """``lightrag_native`` + ``enable_bm25_evidence=True`` →
    constructor auto-promotes to
    ``lightrag_native_with_bm25_evidence`` so the operator can't
    accidentally request "native + citations" but silently get
    pure-native (zero citations)."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native answer",
    ))
    svc = _build(
        native=spy,
        engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE,
        enable_bm25_evidence=True,
    )
    assert (
        svc._query_engine_mode  # noqa: SLF001
        == QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE
    )


# ---- 4. Canonical metadata fields -----------------------------------


def test_canonical_metadata_bm25_debug(ctx):
    """``bm25_debug`` produces an answer from BM25 only — metadata
    surfaces that clearly."""
    engine = _bm25_engine()
    svc = _build(engine=engine, engine_mode=QUERY_ENGINE_BM25_DEBUG)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["query_engine"] == QUERY_ENGINE_BM25_DEBUG
    assert debug["answer_source"] == "bm25"
    assert debug["citation_source"] == "bm25"
    assert debug["bm25_used"] is True
    assert debug["run_id"] == "run-1"
    assert debug["document_id"] == "doc-1"
    assert debug["workspace_id"] == "t1/p1/doc-1/run-1"


def test_canonical_metadata_with_evidence_marks_citation_augmentation(ctx):
    """``lightrag_native_with_bm25_evidence`` → answer is native,
    citations are BM25-sourced; ``citation_source=
    'bm25_augmentation'`` and a warning is recorded."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="native answer",
    ))
    svc = _build(
        native=spy,
        engine_mode=QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    debug = response.debug
    assert debug["answer_source"] == "native"
    assert debug["citation_source"] == "bm25_augmentation"
    assert debug["bm25_used"] is True
    assert "citations_from_bm25_not_native" in debug["warnings"]


def test_workspace_path_stamped_from_native_provider(ctx):
    """When the native provider is wired, the resolved workspace
    path is stamped onto debug — operator can confirm scoping."""
    spy = _NativeProviderSpy(
        canned_result=QueryResult(
            status=ResultStatus.SUCCEEDED, answer="native",
        ),
        workspace_path_value="/wd/runs/t1/p1/doc-1/run-1",
    )
    svc = _build(native=spy, engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["workspace_path"] == "/wd/runs/t1/p1/doc-1/run-1"


def test_workspace_path_is_none_when_native_unwired(ctx):
    """No native provider → no derivable workspace path. Field is
    stamped as ``None`` rather than omitted."""
    engine = _bm25_engine()
    svc = _build(
        native=None, engine=engine, engine_mode=QUERY_ENGINE_BM25_DEBUG,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["workspace_path"] is None


# ---- 5. Backward-compat alias mapping -------------------------------


def test_alias_mapping_table_is_correct():
    """Legacy strings map to the new canonical names exactly. Pinned
    here so future refactors that touch the alias table can't
    silently break external configs."""
    assert _QUERY_ENGINE_ALIASES["bm25_primary"] == QUERY_ENGINE_BM25_DEBUG
    assert (
        _QUERY_ENGINE_ALIASES["rag_native_primary"]
        == QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE
    )
    # All canonical engines are in the valid set.
    assert QUERY_ENGINE_LIGHTRAG_NATIVE in _VALID_QUERY_ENGINES
    assert QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE in _VALID_QUERY_ENGINES
    assert QUERY_ENGINE_BM25_DEBUG in _VALID_QUERY_ENGINES
    assert QUERY_ENGINE_HYBRID_AB in _VALID_QUERY_ENGINES


def test_legacy_bm25_primary_string_still_accepted(ctx):
    """Passing the old ``bm25_primary`` engine name maps to the new
    canonical ``bm25_debug``. Existing env configs keep working."""
    svc = _build(
        native=None, engine_mode="bm25_primary",
    )
    assert svc._query_engine_mode == QUERY_ENGINE_BM25_DEBUG  # noqa: SLF001


def test_legacy_rag_native_primary_string_still_accepted(ctx):
    """``rag_native_primary`` maps to the new
    ``lightrag_native_with_bm25_evidence``."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    svc = _build(native=spy, engine_mode="rag_native_primary")
    assert (
        svc._query_engine_mode  # noqa: SLF001
        == QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE
    )


def test_legacy_query_provider_mode_kwarg_still_accepted(ctx):
    """``query_provider_mode=`` kwarg (legacy) still works alongside
    the new ``query_engine_mode=``."""
    svc = IngestionValidationService(
        run_store=MagicMock(get=MagicMock(return_value=_make_run())),
        artifact_registry=MagicMock(list_artifacts=MagicMock(return_value=[])),
        query_engine=_bm25_engine(),
        workspace=MagicMock(),
        audit=MagicMock(),
        answer_synthesizer=None,
        native_query_provider=None,
        query_provider_mode="bm25_primary",
    )
    assert svc._query_engine_mode == QUERY_ENGINE_BM25_DEBUG  # noqa: SLF001


def test_legacy_fallback_kwarg_wins_over_new_when_explicit(ctx):
    """When the deprecated ``native_query_fallback_to_bm25=True`` is
    passed alongside ``enable_bm25_fallback=False``, the legacy
    flag wins. Lets deployments with the old env var keep working
    without flipping behaviour silently."""
    svc = IngestionValidationService(
        run_store=MagicMock(get=MagicMock(return_value=_make_run())),
        artifact_registry=MagicMock(list_artifacts=MagicMock(return_value=[])),
        query_engine=_bm25_engine(),
        workspace=MagicMock(),
        audit=MagicMock(),
        answer_synthesizer=None,
        native_query_provider=None,
        enable_bm25_fallback=False,
        native_query_fallback_to_bm25=True,
    )
    assert svc._enable_bm25_fallback is True  # noqa: SLF001


# ---- 6. Native debug query path -------------------------------------


def test_native_debug_query_calls_native_only(ctx):
    """``run_native_debug_query`` invokes native directly and does
    NOT touch BM25, regardless of engine_mode."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="diagnostic answer",
    ))
    engine = _bm25_engine()
    svc = _build(
        native=spy, engine=engine, engine_mode=QUERY_ENGINE_BM25_DEBUG,
    )
    result = svc.run_native_debug_query(ctx, "run-1", "diagnostic?")
    assert result.answer == "diagnostic answer"
    assert result.native_query_used is True
    assert result.provider_wired is True
    assert result.native_query_failed_reason is None
    assert result.workspace_path == "/workdir/runs/t1/p1/doc-1/run-1"
    assert result.workspace_id == "t1/p1/doc-1/run-1"
    assert engine.query.call_count == 0
    assert len(spy.calls) == 1
    assert spy.calls[0]["run_id"] == "run-1"
    assert spy.calls[0]["document_id"] == "doc-1"


def test_native_debug_query_no_provider_returns_failure_shape(ctx):
    """No native provider → returns ``native_query_used=false`` with
    a ``provider_not_wired`` reason and ``provider_wired=false``.
    No exception."""
    svc = _build(native=None, engine_mode=QUERY_ENGINE_BM25_DEBUG)
    result = svc.run_native_debug_query(ctx, "run-1", "diagnostic?")
    assert result.native_query_used is False
    assert result.provider_wired is False
    assert result.native_query_failed_reason == "native_provider_not_wired"
    assert result.answer == ""


def test_native_debug_query_propagates_failure_reason(ctx):
    """Native returns FAILED → response carries the error string."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="connection refused",
    ))
    svc = _build(native=spy, engine_mode=QUERY_ENGINE_LIGHTRAG_NATIVE)
    result = svc.run_native_debug_query(ctx, "run-1", "diagnostic?")
    assert result.native_query_used is False
    assert "connection refused" in (result.native_query_failed_reason or "")
    assert result.provider_wired is True
