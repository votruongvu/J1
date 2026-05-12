"""Regression tests for the three-way query-provider-mode
dispatcher in ``IngestionValidationService``.

Covers the user-listed scenarios 1–9:

  1. Default compatibility — no env overrides → bm25_primary.
  2. Native provider wiring — rag_native_primary → native called.
  3. Native fallback — native fails/times out → BM25 fallback.
  4. Hybrid A/B — both run, BM25 stable, native in debug.
  5. Per-run workspace isolation — native gets run_id/document_id.
  6. BM25 candidate K decoupling — small top_k still floors.
  7. Evidence cap — evidence_max_blocks enforced.
  8. No fake citations — native answer doesn't fabricate sources.
  9. Existing validation checks still fire.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from j1.processing.results import QueryResult, ResultStatus
from j1.projects.context import ProjectContext
from j1.query.models import QueryRequest, QueryResponse, SourceReference
from j1.runs.models import IngestionRun, RunStatus
from j1.validation.dtos import ManualTestQueryRequest
from j1.validation.service import (
    IngestionValidationService,
    QUERY_PROVIDER_MODE_BM25,
    QUERY_PROVIDER_MODE_HYBRID_AB,
    QUERY_PROVIDER_MODE_NATIVE,
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


def _bm25_engine(sources=None):
    """Stub engine that returns a fixed QueryResponse — represents
    the BM25 / HybridQueryEngine path."""
    engine = MagicMock()
    src = sources or [
        SourceReference(
            artifact_id="a-1", artifact_type="chunk",
            title="chunk/a-1", chunk_id="c-1", run_id="run-1", score=0.9,
        ),
    ]
    engine.query.return_value = QueryResponse(
        answer="(bm25 deterministic answer)",
        mode_used="knowledge_first",
        sources=src,
        graph_paths=[],
    )
    return engine


@dataclass
class _NativeProviderSpy:
    """Captures every native call so tests can assert on it."""
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


def _build_service(*, engine=None, native=None, mode=QUERY_PROVIDER_MODE_BM25,
                   fallback=True, timeout=30.0):
    """Build a minimal IngestionValidationService — most deps mocked."""
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
        answer_synthesizer=None,  # no synthesizer needed for these tests
        native_query_provider=native,
        query_provider_mode=mode,
        native_query_timeout_seconds=timeout,
        native_query_fallback_to_bm25=fallback,
        validation_candidate_top_k=20,
        validation_evidence_max_blocks=5,
    )


# ---- 1. Default compatibility ----------------------------------


def test_1_default_mode_is_bm25_primary_and_native_unused(ctx):
    """No env override / no native provider → default = bm25_primary.
    Native provider never invoked even when not None."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    # Service constructed with mode=bm25_primary (the default),
    # native_query_provider attached anyway — should NOT be called.
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_BM25)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["query_provider_mode"] == QUERY_PROVIDER_MODE_BM25
    assert response.debug["native_query_used"] is False
    assert response.debug["fallback_used"] is False
    assert response.debug["citation_augmentation_used"] is False
    assert spy.calls == []


def test_1_unknown_mode_falls_back_to_bm25_with_warning(caplog, ctx):
    """A misconfigured mode env var → constructor warns and demotes
    to the post-audit default (``lightrag_native``) when a native
    provider is wired; further demotes to ``bm25_debug`` when it
    isn't."""
    import logging
    from j1.validation.service import QUERY_ENGINE_LIGHTRAG_NATIVE
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    with caplog.at_level(logging.WARNING, logger="j1.validation"):
        svc = _build_service(native=spy, mode="not_a_real_mode")
    # With native wired, fallback lands on the new default.
    assert svc._query_provider_mode == QUERY_ENGINE_LIGHTRAG_NATIVE  # noqa: SLF001
    assert any("unknown query_engine" in r.message for r in caplog.records)


def test_1_native_mode_without_provider_falls_back_to_bm25(caplog, ctx):
    """Mode says native but provider isn't wired → warn + demote to
    ``bm25_debug``. Preserves the "still works" contract for
    deployments that don't ship RAGAnything."""
    import logging
    with caplog.at_level(logging.WARNING, logger="j1.validation"):
        svc = _build_service(native=None, mode=QUERY_PROVIDER_MODE_NATIVE)
    assert svc._query_provider_mode == QUERY_PROVIDER_MODE_BM25  # noqa: SLF001


# ---- 2. Native provider wiring ---------------------------------


def test_2_native_mode_calls_native_provider(ctx):
    """rag_native_primary → native provider gets the call. Question,
    run_id, document_id all flow through."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED,
        answer="The proposal due date is 20 May 2026.",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_NATIVE)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(
            question="What is the proposal due date?",
            top_k=5, synthesize=False,
        ),
    )
    assert response.debug["query_provider_mode"] == QUERY_PROVIDER_MODE_NATIVE
    assert response.debug["native_query_used"] is True
    assert response.debug["citation_augmentation_used"] is True
    # Native saw the question + scoping fields.
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["question"] == "What is the proposal due date?"
    assert call["run_id"] == "run-1"
    assert call["document_id"] == "doc-1"
    # Native answer overrode synthesized_answer.
    assert response.synthesized_answer == "The proposal due date is 20 May 2026."


# ---- 3. Native fallback ----------------------------------------


def test_3_native_failure_falls_back_to_bm25(ctx):
    """Native returns FAILED status → BM25 is used. Debug records
    the failure reason."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED,
        error="connection refused",
        message="vendor unreachable",
    ))
    svc = _build_service(
        native=spy, mode=QUERY_PROVIDER_MODE_NATIVE, fallback=True,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["native_query_used"] is False
    assert response.debug["fallback_used"] is True
    assert "connection refused" in response.debug["native_query_failed_reason"]
    # synthesized_answer NOT overridden — no native answer to use.
    assert response.synthesized_answer is None


def test_3_native_timeout_triggers_fallback(ctx):
    """Native takes too long → dispatcher cancels via timeout →
    fallback path."""

    class _SlowNative:
        def query(self, *args, **kwargs):  # noqa: ARG002
            time.sleep(2)  # exceeds the 0.5s timeout below
            return QueryResult(
                status=ResultStatus.SUCCEEDED, answer="late",
            )

    svc = _build_service(
        native=_SlowNative(),
        mode=QUERY_PROVIDER_MODE_NATIVE,
        timeout=0.5,
        fallback=True,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["fallback_used"] is True
    assert "timeout" in (
        response.debug.get("native_query_failed_reason") or ""
    ).lower()


def test_3_native_empty_answer_triggers_fallback(ctx):
    """Native returned successfully but with an empty answer →
    treated as a failure for routing purposes."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="",
    ))
    svc = _build_service(
        native=spy, mode=QUERY_PROVIDER_MODE_NATIVE, fallback=True,
    )
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["fallback_used"] is True
    assert response.debug["native_query_failed_reason"] == (
        "native_query_empty_answer"
    )


# ---- 4. Hybrid A/B ---------------------------------------------


def test_4_hybrid_ab_runs_both_paths_and_returns_bm25_answer(ctx):
    """hybrid_ab → BM25 is the stable answer; native answer surfaces
    only in debug. Both latencies recorded."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native experimental)",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_HYBRID_AB)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    # BM25 still drove the answer.
    assert response.answer.startswith("(bm25")
    # Native ran for observability.
    assert len(spy.calls) == 1
    assert response.debug["query_provider_mode"] == QUERY_PROVIDER_MODE_HYBRID_AB
    assert response.debug["native_query_used"] is True
    assert response.debug["bm25_latency_ms"] >= 0
    assert response.debug["native_latency_ms"] >= 0
    # Both previews present.
    assert "hybrid_ab_native_answer_preview" in response.debug
    assert "hybrid_ab_bm25_answer_preview" in response.debug
    # synthesized_answer NOT overridden in hybrid_ab.
    assert response.synthesized_answer is None


def test_4_hybrid_ab_native_failure_does_not_break_response(ctx):
    """In hybrid_ab, a failing native call is a debug entry, not
    a request failure."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="boom",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_HYBRID_AB)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    # Response still returns successfully with BM25 content.
    assert response.answer.startswith("(bm25")
    assert response.debug["native_query_used"] is False
    assert "boom" in response.debug["native_query_failed_reason"]


# ---- 5. Per-run workspace isolation -----------------------------


def test_5_native_call_receives_per_run_scoping(ctx):
    """The native provider always gets run_id + document_id —
    LightRAG uses them to scope its working_dir per-run, preventing
    cross-run leakage."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(ok)",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_NATIVE)
    svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    call = spy.calls[0]
    assert call["run_id"] == "run-1"
    assert call["document_id"] == "doc-1"


# ---- 6. BM25 candidate K decoupling (already in earlier round)


def test_6_bm25_fallback_path_still_uses_candidate_top_k(ctx):
    """When native fails and BM25 is the fallback, the candidate
    floor is still honoured (not the small request top_k). This is
    the decoupling we shipped earlier — pinned here to confirm the
    new dispatch chain didn't regress it."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.FAILED, error="x",
    ))
    engine = _bm25_engine()
    svc = _build_service(
        engine=engine, native=spy,
        mode=QUERY_PROVIDER_MODE_NATIVE, fallback=True,
    )
    svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=3, synthesize=False),
    )
    # Engine was asked for the candidate floor (20), not the
    # requested top_k (3).
    qr: QueryRequest = engine.query.call_args.args[1]
    assert qr.max_results == 20


# ---- 7. Evidence cap -------------------------------------------


def test_7_evidence_cap_enforced_independent_of_top_k(ctx, tmp_path):
    """Final ``evidence_sent_to_llm`` length never exceeds
    ``validation_evidence_max_blocks`` regardless of how many
    candidates retrieval returned."""
    # Stub a synthesizer so build_evidence_blocks runs.
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus
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
    records = []
    sources = []
    for i in range(20):
        ndjson = chunk_dir / f"a-{i}.ndjson"
        ndjson.write_text(
            '{"chunk_id":"c-' + str(i)
            + '","body":"Body content number ' + str(i) + '","page_start":1}\n',
            encoding="utf-8",
        )
        records.append(ArtifactRecord(
            artifact_id=f"a-{i}", project=ctx, kind="chunk",
            location=f"compiled/a-{i}.ndjson",
            content_hash=f"sha:a-{i}", byte_size=100,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1, created_at=now, updated_at=now,
            metadata={"run_id": "run-1"},
        ))
        sources.append(SourceReference(
            artifact_id=f"a-{i}", artifact_type="chunk",
            title=f"chunk/a-{i}", chunk_id=f"c-{i}",
            run_id="run-1", score=1.0 - i * 0.01,
        ))

    engine = _bm25_engine(sources=sources)
    artifacts = MagicMock()
    artifacts.list_artifacts.return_value = records

    def _get(ctx_, artifact_id):  # noqa: ARG001
        for r in records:
            if r.artifact_id == artifact_id:
                return r
        from j1.artifacts.registry import ArtifactNotFoundError
        raise ArtifactNotFoundError(artifact_id)

    artifacts.get = _get
    workspace = MagicMock()
    workspace.area.return_value = chunk_dir
    run_store = MagicMock()
    run_store.get.return_value = _make_run()

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
        ManualTestQueryRequest(question="content", top_k=3, synthesize=True),
    )
    assert response.debug["fts_returned_count"] == 20
    assert response.debug["evidence_max_blocks"] == 5
    assert len(response.evidence_sent_to_llm) <= 5


# ---- 8. No fake citations --------------------------------------


def test_8_native_does_not_invent_citations(ctx):
    """When native produces an answer, citations come from BM25
    (not from native — LightRAG doesn't return them in J1's
    shape). Debug records ``citation_augmentation_used=True``
    so operators can tell what happened."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native)",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_NATIVE)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["citation_augmentation_used"] is True
    # Citations came from BM25 (the engine returned one source).
    assert len(response.citations) == 1
    # That citation is real lineage from the BM25 engine, NOT
    # invented from the native answer. (camelCase wire shape.)
    assert response.citations[0]["artifactId"] == "a-1"


def test_8_no_native_no_augmentation(ctx):
    """bm25_primary mode → no citation augmentation, no native
    overlay. The flag stays False."""
    svc = _build_service(mode=QUERY_PROVIDER_MODE_BM25)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    assert response.debug["citation_augmentation_used"] is False


# ---- 9. Existing validation checks still fire ------------------


def test_9_isolation_and_required_checks_present_in_native_mode(ctx):
    """The dispatcher feeds the same BM25-shaped response into
    ``run_checks`` regardless of mode. ``retrieved_chunks_belong_to_run``
    / ``citations_belong_to_run`` /
    ``no_cross_tenant_or_cross_project_leak`` /
    ``retrieved_chunks_present`` / ``answer_non_empty`` must all
    still appear and pass."""
    spy = _NativeProviderSpy(canned_result=QueryResult(
        status=ResultStatus.SUCCEEDED, answer="(native ok)",
    ))
    svc = _build_service(native=spy, mode=QUERY_PROVIDER_MODE_NATIVE)
    response = svc.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="q?", top_k=5, synthesize=False),
    )
    names = {c.name for c in response.checks}
    # Required checks fire in every mode.
    assert "retrieved_chunks_belong_to_run" in names
    assert "citations_belong_to_run" in names
    assert "no_cross_tenant_or_cross_project_leak" in names
    assert "retrieved_chunks_present" in names
    assert "answer_non_empty" in names
    # All isolation checks pass on the stub data.
    for c in response.checks:
        if c.name in {
            "retrieved_chunks_belong_to_run",
            "citations_belong_to_run",
            "no_cross_tenant_or_cross_project_leak",
        }:
            assert c.passed, f"isolation regressed in native mode: {c}"
