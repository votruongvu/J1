"""Tests for the retrieval-broadening A/B harness.

The harness is runner-agnostic by design — tests inject a stub
runner that records env state + a canned response per call, so we
can verify:

  * Each query runs TWICE (baseline + variant).
  * The env flag is set correctly for each call.
  * The env flag is restored after the batch (no leakage).
  * Stub runner that doesn't mutate state proves the harness is
    read-only by contract.
  * Scope-safe by construction — the harness records the scope it
    was given verbatim and never narrows / widens it.
  * Missing diagnostics surface as warnings, never as crashes.
  * Summary counts roll up correctly from per-query outcomes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.query.orchestrator import ENV_QUERY_EXPANSION_ENABLED
from j1.tools.evaluate_retrieval_broadening import (
    EvaluationReport,
    QueryInput,
    RetrievalBroadeningEvaluator,
    load_queries,
)


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---- Stub runner --------------------------------------------------


class _RecordingRunner:
    """Records every call's env state + returns canned responses.

    ``responses`` is a list of ``(retrieved_chunks, augmentation)``
    tuples. The harness reads:
      * ``retrieved_chunks`` length → retrieved_count
      * ``debug.orchestrator_trace.augmentation`` → diagnostics
    so the stub mirrors that shape."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, question):
        # Record the env state at call time — proves the harness
        # toggled the flag.
        env_value = os.environ.get(ENV_QUERY_EXPANSION_ENABLED)
        record = {
            "question": question,
            "env_query_expansion_enabled": env_value,
        }
        self.calls.append(record)
        if not self._responses:
            return _empty_response()
        chunks, augmentation = self._responses.pop(0)
        return {
            "retrieved_chunks": chunks,
            "evidence_sent_to_llm": chunks[:2],
            "debug": {
                "orchestrator_trace": {
                    "augmentation": augmentation,
                },
            },
        }


def _chunk(chunk_id: str, preview: str = ""):
    return {"chunk_id": chunk_id, "preview": preview}


def _augmentation(
    *,
    applied: bool = False,
    aliases: list | None = None,
    enrichment_available: int = 0,
    enrichment_matched: list | None = None,
    retrieval_counts: dict | None = None,
    expansions: list | None = None,
    source: str = "domain_pack",
):
    return {
        "source": source,
        "expansions": expansions or [],
        "aliases": aliases or [],
        "applied_to_retrieval": applied,
        "retrieval_counts": retrieval_counts or {
            "original": 0, "expanded": 0, "deduplicated_total": 0,
        },
        "final_evidence_distribution": {
            "original_only": 0, "expanded_only": 0, "both": 0,
        },
        "enrichment_aliases_available": enrichment_available,
        "enrichment_aliases_matched": enrichment_matched or [],
    }


def _empty_response():
    return {
        "retrieved_chunks": [],
        "evidence_sent_to_llm": [],
        "debug": {"orchestrator_trace": {"augmentation": _augmentation()}},
    }


def _build_evaluator(runner, scope=None):
    return RetrievalBroadeningEvaluator(
        runner=runner,
        scope=scope or {"project_id": "alpha"},
        now=lambda: _NOW,
    )


# ---- Tests --------------------------------------------------------


def test_runs_each_query_twice_with_correct_env(monkeypatch):
    """The harness must toggle ``J1_QUERY_EXPANSION_ENABLED`` for
    each of the two passes — once "false" (baseline), once "true"
    (variant). The stub runner records the value seen at call
    time, so a quick check of the call log proves the toggle is
    real."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        ([_chunk("c-1")], _augmentation(applied=False)),  # baseline
        ([_chunk("c-1"), _chunk("c-2")],
         _augmentation(applied=True, expansions=["alt"])),  # variant
    ])
    evaluator = _build_evaluator(runner)

    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])

    # Both modes ran.
    assert len(runner.calls) == 2
    assert runner.calls[0]["env_query_expansion_enabled"] == "false"
    assert runner.calls[1]["env_query_expansion_enabled"] == "true"
    # Report carries both outcomes + a delta.
    [result] = report.results
    assert result.baseline.retrieved_count == 1
    assert result.alias_broadening.retrieved_count == 2
    assert result.delta["retrieved_count"] == 1


def test_env_state_restored_after_evaluation(monkeypatch):
    """Spec rule: the harness is read-only. That includes the
    process env — a partial / completed batch must leave
    ``J1_QUERY_EXPANSION_ENABLED`` exactly as it was."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    runner = _RecordingRunner([
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1")], _augmentation(applied=True)),
    ])
    evaluator = _build_evaluator(runner)

    evaluator.evaluate([QueryInput(id="q1", question="anything")])
    # The harness toggled the env DURING execution but restored
    # the original "true" value on the way out.
    assert os.environ[ENV_QUERY_EXPANSION_ENABLED] == "true"


def test_env_state_unset_after_evaluation_when_initially_absent(
    monkeypatch,
):
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1")], _augmentation(applied=True)),
    ])
    evaluator = _build_evaluator(runner)
    evaluator.evaluate([QueryInput(id="q1", question="anything")])
    # No env var was set originally — the harness must NOT leave
    # one behind.
    assert ENV_QUERY_EXPANSION_ENABLED not in os.environ


def test_harness_is_read_only(monkeypatch):
    """The stub runner doesn't mutate any persistent state, so a
    successful evaluation proves the harness's wire doesn't write
    elsewhere either. Concretely: the runner is the ONLY object
    the harness invokes, and the evaluator's public surface
    doesn't expose any writer. Pinned via a count-of-runner-calls
    assertion — every operation goes through the runner and
    nothing else."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)

    calls: list[str] = []

    def runner(question):
        calls.append(question)
        return _empty_response()

    evaluator = _build_evaluator(runner)
    evaluator.evaluate([
        QueryInput(id="q1", question="a"),
        QueryInput(id="q2", question="b"),
    ])
    # Two queries × two modes = four runner calls. The harness
    # interacts ONLY with the runner — no other side channels.
    assert calls == ["a", "a", "b", "b"]


def test_scope_recorded_verbatim_in_report(monkeypatch):
    """The harness MUST NOT narrow / widen the scope it was given.
    Whatever scope dict construction received lands in the report
    verbatim — operators reading the report can verify scope
    safety at a glance."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        ([_chunk("c-1")], _augmentation()),
        ([_chunk("c-1")], _augmentation()),
    ])
    scope = {
        "project_id": "alpha",
        "document_id": "doc-1",
        "snapshot_id": "snap-active",
    }
    evaluator = _build_evaluator(runner, scope=scope)
    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])
    assert report.scope == scope


def test_missing_diagnostics_surfaces_as_warning(monkeypatch):
    """A runner whose response carries no
    ``debug.orchestrator_trace.augmentation`` block must NOT crash
    the harness. The report records the gap as a warning string
    so operators can spot it."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)

    def broken_runner(question):
        # Missing the `augmentation` block entirely.
        return {
            "retrieved_chunks": [_chunk("c-1")],
            "evidence_sent_to_llm": [],
            "debug": {"orchestrator_trace": {}},
        }

    evaluator = _build_evaluator(broken_runner)
    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])
    # Result is still recorded; diagnostics fall back to empty.
    [result] = report.results
    assert result.baseline.diagnostics == {}
    # The warning surface specifically calls out the missing
    # block. There are two warnings (one per mode).
    assert any(
        "no augmentation diagnostics" in w
        for w in report.warnings
    )


def test_missing_retrieved_chunks_surfaces_as_warning(monkeypatch):
    """Equivalent gap on the retrieval side: a response without
    ``retrieved_chunks`` records ``retrieved_count=None`` plus a
    warning. The delta computer handles ``None`` cleanly."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)

    def broken_runner(_question):
        return {
            "evidence_sent_to_llm": [],
            "debug": {"orchestrator_trace": {"augmentation": _augmentation()}},
        }

    evaluator = _build_evaluator(broken_runner)
    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])
    [result] = report.results
    assert result.baseline.retrieved_count is None
    assert result.delta["retrieved_count"] is None
    assert any("retrieved_chunks" in w for w in report.warnings)


def test_summary_counts_more_same_fewer(monkeypatch):
    """Pin the rollup arithmetic: three queries with different
    baseline/variant relationships produce ``(more, same, fewer)
    = (1, 1, 1)``."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        # q1 baseline=1, variant=3 → more
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1"), _chunk("c-2"), _chunk("c-3")],
         _augmentation(applied=True)),
        # q2 baseline=2, variant=2 → same
        ([_chunk("c-a"), _chunk("c-b")], _augmentation(applied=False)),
        ([_chunk("c-a"), _chunk("c-b")], _augmentation(applied=True)),
        # q3 baseline=4, variant=1 → fewer (rare, but valid)
        ([_chunk(str(i)) for i in range(4)], _augmentation(applied=False)),
        ([_chunk("only-one")], _augmentation(applied=True)),
    ])
    evaluator = _build_evaluator(runner)
    report = evaluator.evaluate([
        QueryInput(id="q1", question="x"),
        QueryInput(id="q2", question="y"),
        QueryInput(id="q3", question="z"),
    ])
    summary = report.summary
    assert summary["query_count"] == 3
    assert summary["queries_with_more_results"] == 1
    assert summary["queries_with_same_results"] == 1
    assert summary["queries_with_fewer_results"] == 1
    # Average rounds to 2 decimals.
    assert summary["baseline_avg_retrieved_count"] == round(
        (1 + 2 + 4) / 3, 2,
    )
    assert summary["alias_broadening_avg_retrieved_count"] == round(
        (3 + 2 + 1) / 3, 2,
    )


def test_summary_counts_enrichment_availability(monkeypatch):
    """The summary distinguishes "queries with ANY enrichment
    aliases on the active snapshot" from "queries where one
    matched and was applied"."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        # q1: no aliases available
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1")], _augmentation(applied=True)),
        # q2: aliases available + matched
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1"), _chunk("c-2")],
         _augmentation(
             applied=True,
             enrichment_available=2,
             enrichment_matched=[
                 {"canonical": "bill of quantities", "alias": "BOQ"},
             ],
         )),
        # q3: aliases available but none matched
        ([_chunk("c-1")], _augmentation(applied=False)),
        ([_chunk("c-1")], _augmentation(
            applied=False,
            enrichment_available=2,
            enrichment_matched=[],
        )),
    ])
    evaluator = _build_evaluator(runner)
    report = evaluator.evaluate([
        QueryInput(id="q1", question="x"),
        QueryInput(id="q2", question="y"),
        QueryInput(id="q3", question="z"),
    ])
    summary = report.summary
    # q2 + q3 have enrichment_available > 0.
    assert summary["queries_with_enrichment_aliases_available"] == 2
    # Only q2 had a match that was applied.
    assert summary["queries_with_enrichment_aliases_applied"] == 1


def test_top_k_preview_truncates_to_limit(monkeypatch):
    """The harness preview list is capped — keeps the report
    small. Default is 5; constructor lets callers tune."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    chunks = [_chunk(f"c-{i}", "sample") for i in range(8)]
    runner = _RecordingRunner([
        (chunks, _augmentation()),
        (chunks, _augmentation()),
    ])
    evaluator = RetrievalBroadeningEvaluator(
        runner=runner,
        scope={"project_id": "alpha"},
        top_k_preview_limit=3,
        now=lambda: _NOW,
    )
    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])
    [result] = report.results
    assert len(result.baseline.top_k_preview) == 3
    assert len(result.alias_broadening.top_k_preview) == 3


def test_report_to_dict_is_json_serialisable(monkeypatch):
    """Pin the wire shape: ``EvaluationReport.to_dict()`` →
    ``json.dumps`` round-trip without raising."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _RecordingRunner([
        ([_chunk("c-1")], _augmentation()),
        ([_chunk("c-1")], _augmentation(applied=True)),
    ])
    evaluator = _build_evaluator(runner)
    report = evaluator.evaluate([QueryInput(id="q1", question="hi")])
    encoded = json.dumps(report.to_dict())
    decoded = json.loads(encoded)
    assert decoded["scope"]["project_id"] == "alpha"
    assert decoded["config"]["baseline"]["alias_broadening_enabled"] is False
    assert decoded["config"]["variant"]["alias_broadening_enabled"] is True


def test_runner_exception_records_warning_and_continues(monkeypatch):
    """A single query that raises must NOT abort the batch. The
    harness logs a warning and proceeds to the next query."""
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)

    state = {"count": 0}

    def runner(_question):
        state["count"] += 1
        if state["count"] == 1:
            raise RuntimeError("transient")
        return _empty_response()

    evaluator = _build_evaluator(runner)
    report = evaluator.evaluate([
        QueryInput(id="q1", question="boom"),
        QueryInput(id="q2", question="ok"),
    ])
    # q1 captured a warning; q2 produced a normal result.
    assert any("q1" in w for w in report.warnings)
    assert len(report.results) == 1
    assert report.results[0].query_id == "q2"


# ---- Queries file parsing -----------------------------------------


def test_load_queries_accepts_json_object(tmp_path: Path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "queries": [
            {"id": "q1", "question": "What is BOQ?"},
            {"id": "q2", "question": "How does it relate?"},
        ],
    }))
    out = load_queries(path)
    assert [q.id for q in out] == ["q1", "q2"]
    assert out[0].question == "What is BOQ?"


def test_load_queries_accepts_jsonl(tmp_path: Path):
    path = tmp_path / "queries.jsonl"
    path.write_text(
        json.dumps({"id": "q1", "question": "a"}) + "\n"
        + json.dumps({"id": "q2", "question": "b"}) + "\n"
    )
    out = load_queries(path)
    assert [q.id for q in out] == ["q1", "q2"]


def test_load_queries_auto_ids_when_missing(tmp_path: Path):
    """An entry without ``id`` gets a synthesized ``q<index>``."""
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({
        "queries": [{"question": "a"}, {"question": "b"}],
    }))
    out = load_queries(path)
    assert out[0].id == "q1"
    assert out[1].id == "q2"


def test_load_queries_rejects_entry_without_question(tmp_path: Path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({"queries": [{"id": "q1"}]}))
    with pytest.raises(ValueError, match="missing 'question'"):
        load_queries(path)


def test_load_queries_rejects_malformed_json(tmp_path: Path):
    path = tmp_path / "queries.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_queries(path)


def test_load_queries_empty_file_returns_empty_list(tmp_path: Path):
    path = tmp_path / "empty.json"
    path.write_text("")
    assert load_queries(path) == []
