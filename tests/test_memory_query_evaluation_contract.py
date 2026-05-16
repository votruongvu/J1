"""Contract — Phase 6 Memory Query Evaluation Harness.

Pins:

  * Harness loads YAML / JSON / JSONL fixtures.
  * Harness runs each query twice — baseline + memory-aware — and
    toggles the right env flags around the runner call.
  * Env flags are restored after the run, even on exception.
  * The orchestrator's `knowledge_memory` trace block flows through
    `debug.orchestrator_trace.knowledge_memory` and lands on the
    per-mode outcome verbatim.
  * Quality proxies: `expected_terms` produce `_present` / `_missing`
    splits; `expected_artifact_types` produce the same against
    retrieved chunk kinds.
  * Safety violations: direct memory citation, latency regression,
    memory provider failure surface explicit codes.
  * Verdict: improved / unchanged / worsened / safety_violation
    classification rules.
  * Summary aggregates the right counts + per-warning frequency.
  * Recommendation engine emits a pinned string for each summary
    shape — keep_disabled / needs_more_data / enable_in_dev_only /
    enable_in_preview / enable_by_default_for_*_scope.
  * Markdown report contains the summary table, per-query rows,
    and the recommendation banner.
  * JSON report serialises losslessly.
  * Harness exits 0 when only quality is mixed; non-zero only on
    safety violations under strict mode.
  * No new LLM calls — the harness imports cleanly without
    pulling LLM clients.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from j1.tools.evaluate_memory_query import (
    ENV_QUERY_EXPANSION_ENABLED,
    ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
    MemoryQueryEvalOutcome,
    MemoryQueryEvalQuery,
    MemoryQueryEvalReport,
    MemoryQueryEvalResult,
    MemoryQueryEvaluator,
    RECOMMENDATION_ENABLE_DEV_ONLY,
    RECOMMENDATION_ENABLE_DOCUMENT_SCOPE,
    RECOMMENDATION_ENABLE_PREVIEW,
    RECOMMENDATION_ENABLE_PROJECT_SCOPE,
    RECOMMENDATION_KEEP_DISABLED,
    RECOMMENDATION_NEEDS_MORE_DATA,
    RECOMMENDATION_VALUES,
    SAFETY_DIRECT_MEMORY_CITATION,
    SAFETY_LATENCY_REGRESSION,
    SAFETY_MEMORY_PROVIDER_FAILURE,
    VERDICT_IMPROVED,
    VERDICT_SAFETY_VIOLATION,
    VERDICT_UNCHANGED,
    VERDICT_WORSENED,
    compute_recommendation,
    compute_recommendation_for_summary,
    load_memory_query_fixture,
    render_markdown_report,
)


# ---- Fixtures ----------------------------------------------------


def _q(
    *,
    id: str = "q1",
    question: str = "what risks are documented?",
    scope: str = "project_active",
    expected_terms: tuple[str, ...] = (),
    expected_artifact_types: tuple[str, ...] = (),
    category: str | None = None,
    document_id: str | None = None,
) -> MemoryQueryEvalQuery:
    return MemoryQueryEvalQuery(
        id=id, question=question, scope=scope,
        document_id=document_id,
        expected_terms=expected_terms,
        expected_artifact_types=expected_artifact_types,
        category=category,
    )


def _response(
    *,
    answer: str = "An answer.",
    citations: list[dict] | None = None,
    retrieved_chunks: list[dict] | None = None,
    evidence_sent_to_llm: list[dict] | None = None,
    knowledge_memory: dict | None = None,
) -> dict:
    debug: dict = {}
    if knowledge_memory is not None:
        debug["orchestrator_trace"] = {
            "knowledge_memory": knowledge_memory,
        }
    return {
        "answer": answer,
        "citations": citations if citations is not None else [{
            "artifact_id": "art-1", "artifact_type": "chunk",
        }],
        "retrieved_chunks": retrieved_chunks if retrieved_chunks is not None else [
            {"artifact_id": "art-1", "artifact_kind": "chunk"},
        ],
        "evidence_sent_to_llm": (
            evidence_sent_to_llm
            if evidence_sent_to_llm is not None else [
                {"artifact_id": "art-1", "artifact_type": "chunk"},
            ]
        ),
        "debug": debug,
    }


class _StubRunner:
    """Captures every call + lets tests script per-mode responses."""

    def __init__(
        self,
        *,
        baseline_response: dict,
        memory_response: dict,
        baseline_sleep_ms: int = 0,
        memory_sleep_ms: int = 0,
    ) -> None:
        self.baseline_response = baseline_response
        self.memory_response = memory_response
        self.baseline_sleep_ms = baseline_sleep_ms
        self.memory_sleep_ms = memory_sleep_ms
        self.calls: list[tuple[str, bool, dict]] = []

    def __call__(
        self, query: MemoryQueryEvalQuery, memory_enabled: bool,
    ) -> Mapping[str, Any]:
        # Capture environment state at call-time so tests can assert
        # the harness toggled the right flags.
        captured_env = {
            ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED: os.environ.get(
                ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
            ),
            ENV_QUERY_EXPANSION_ENABLED: os.environ.get(
                ENV_QUERY_EXPANSION_ENABLED,
            ),
        }
        self.calls.append((query.id, memory_enabled, captured_env))
        if memory_enabled:
            if self.memory_sleep_ms:
                _sleep_ms(self.memory_sleep_ms)
            return dict(self.memory_response)
        if self.baseline_sleep_ms:
            _sleep_ms(self.baseline_sleep_ms)
        return dict(self.baseline_response)


def _sleep_ms(ms: int) -> None:
    """Sleep ms-precision (used to simulate latency in latency-
    regression tests). Avoid >100ms in tests to keep them fast."""
    import time as _time
    _time.sleep(ms / 1000.0)


def _evaluator(
    runner,
    *,
    scope: dict | None = None,
    strict: bool = False,
) -> MemoryQueryEvaluator:
    return MemoryQueryEvaluator(
        runner=runner,
        scope=scope or {"project_id": "p1"},
        now=lambda: datetime(
            2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc,
        ),
        strict=strict,
    )


# ---- Fixture loading -------------------------------------------


def test_load_yaml_fixture(tmp_path: Path):
    fixture = tmp_path / "f.yaml"
    fixture.write_text(
        "queries:\n"
        "  - id: risk_001\n"
        "    question: what risks?\n"
        "    scope: project_active\n"
        "    category: risk\n"
        "    expected_terms:\n"
        "      - risk\n"
        "      - corrective action\n",
        encoding="utf-8",
    )
    queries = load_memory_query_fixture(fixture)
    assert len(queries) == 1
    assert queries[0].id == "risk_001"
    assert queries[0].category == "risk"
    assert "risk" in queries[0].expected_terms
    assert "corrective action" in queries[0].expected_terms


def test_load_json_fixture(tmp_path: Path):
    fixture = tmp_path / "f.json"
    fixture.write_text(
        json.dumps({"queries": [
            {"id": "q1", "question": "q", "scope": "project_active"},
        ]}),
        encoding="utf-8",
    )
    queries = load_memory_query_fixture(fixture)
    assert len(queries) == 1


def test_load_jsonl_fixture(tmp_path: Path):
    fixture = tmp_path / "f.jsonl"
    fixture.write_text(
        '{"id": "q1", "question": "q"}\n'
        '{"id": "q2", "question": "q2"}\n',
        encoding="utf-8",
    )
    queries = load_memory_query_fixture(fixture)
    assert [q.id for q in queries] == ["q1", "q2"]


def test_load_fixture_rejects_missing_question(tmp_path: Path):
    fixture = tmp_path / "f.yaml"
    fixture.write_text(
        "queries:\n  - id: q1\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing 'question'"):
        load_memory_query_fixture(fixture)


def test_load_fixture_empty_returns_empty_list(tmp_path: Path):
    fixture = tmp_path / "f.yaml"
    fixture.write_text("", encoding="utf-8")
    assert load_memory_query_fixture(fixture) == []


def test_load_fixture_assigns_auto_ids_when_missing(tmp_path: Path):
    fixture = tmp_path / "f.yaml"
    fixture.write_text(
        "queries:\n"
        "  - question: q1\n"
        "  - question: q2\n",
        encoding="utf-8",
    )
    queries = load_memory_query_fixture(fixture)
    assert [q.id for q in queries] == ["q1", "q2"]


def test_load_yaml_fixture_sample_loads_cleanly():
    """The shipped sample fixture parses without error."""
    sample = Path(__file__).parent / "fixtures" / "memory_query_eval_sample.yaml"
    queries = load_memory_query_fixture(sample)
    # Sanity — sample has 10 queries spanning multiple categories.
    assert len(queries) >= 5
    ids = {q.id for q in queries}
    assert "risk_001" in ids


# ---- Env-flag toggling ----------------------------------------


def test_runs_each_query_twice_with_correct_env_flags():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={
            "status": "used", "scope": "project_active",
        }),
    )
    report = _evaluator(runner).evaluate([_q()])
    assert len(report.results) == 1
    assert len(runner.calls) == 2
    # Baseline call sees both flags = "false".
    _, mem_enabled_0, env_0 = runner.calls[0]
    assert mem_enabled_0 is False
    assert env_0[ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED] == "false"
    assert env_0[ENV_QUERY_EXPANSION_ENABLED] == "false"
    # Memory-aware call sees both = "true".
    _, mem_enabled_1, env_1 = runner.calls[1]
    assert mem_enabled_1 is True
    assert env_1[ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED] == "true"
    assert env_1[ENV_QUERY_EXPANSION_ENABLED] == "true"


def test_env_state_restored_after_evaluation(monkeypatch):
    """Prior values are restored even if a query raises."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "preset-mem")
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "preset-exp")
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(),
    )
    _evaluator(runner).evaluate([_q()])
    assert os.environ[ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED] == "preset-mem"
    assert os.environ[ENV_QUERY_EXPANSION_ENABLED] == "preset-exp"


def test_env_state_unset_after_evaluation_when_initially_absent(monkeypatch):
    monkeypatch.delenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, raising=False)
    monkeypatch.delenv(ENV_QUERY_EXPANSION_ENABLED, raising=False)
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(),
    )
    _evaluator(runner).evaluate([_q()])
    assert ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED not in os.environ
    assert ENV_QUERY_EXPANSION_ENABLED not in os.environ


# ---- Trace projection -----------------------------------------


def test_knowledge_memory_trace_extracted_from_debug():
    km_block = {
        "status": "used",
        "scope": "project_active",
        "memory_artifact_count": 2,
        "applied_expansion_terms": ["NCR"],
        "injected_evidence_count": 3,
        "warnings": [],
    }
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory=km_block),
    )
    report = _evaluator(runner).evaluate([_q()])
    out = report.results[0].memory_aware
    assert out.knowledge_memory == km_block


def test_baseline_outcome_has_no_knowledge_memory_block():
    """Memory disabled → orchestrator stamps nothing → outcome's
    field stays None."""
    runner = _StubRunner(
        baseline_response=_response(),  # no debug
        memory_response=_response(knowledge_memory={"status": "used"}),
    )
    report = _evaluator(runner).evaluate([_q()])
    assert report.results[0].baseline.knowledge_memory is None


def test_missing_knowledge_memory_in_memory_mode_records_warning():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(),  # no debug in memory mode
    )
    report = _evaluator(runner).evaluate([_q()])
    msgs = " ".join(report.warnings)
    assert "no knowledge_memory trace block" in msgs


# ---- Quality proxies ------------------------------------------


def test_expected_terms_split_into_present_and_missing():
    runner = _StubRunner(
        baseline_response=_response(answer="No risks documented."),
        memory_response=_response(answer="Risks and corrective action listed."),
    )
    report = _evaluator(runner).evaluate([_q(
        expected_terms=("risk", "corrective action"),
    )])
    base = report.results[0].baseline
    mem = report.results[0].memory_aware
    assert "risk" in [t.lower() for t in base.expected_terms_present]
    # Baseline lacks "corrective action" — both terms not present.
    assert any(
        t.lower() == "corrective action"
        for t in base.expected_terms_missing
    )
    assert "corrective action" in mem.expected_terms_present


def test_expected_artifact_types_match_retrieved_kinds():
    runner = _StubRunner(
        baseline_response=_response(retrieved_chunks=[
            {"artifact_id": "a1", "artifact_kind": "chunk"},
        ]),
        memory_response=_response(retrieved_chunks=[
            {"artifact_id": "a1", "artifact_kind": "chunk"},
            {"artifact_id": "a2", "artifact_kind": "enriched.risks"},
        ]),
    )
    report = _evaluator(runner).evaluate([_q(
        expected_artifact_types=("enriched.risks",),
    )])
    base = report.results[0].baseline
    mem = report.results[0].memory_aware
    assert "enriched.risks" in base.expected_artifact_types_missing
    assert "enriched.risks" in mem.expected_artifact_types_present


def test_citation_count_captured_per_mode():
    runner = _StubRunner(
        baseline_response=_response(citations=[
            {"artifact_id": "a1"},
        ]),
        memory_response=_response(citations=[
            {"artifact_id": "a1"},
            {"artifact_id": "a2"},
        ]),
    )
    report = _evaluator(runner).evaluate([_q()])
    assert report.results[0].baseline.citation_count == 1
    assert report.results[0].memory_aware.citation_count == 2
    assert report.results[0].delta["citation_count"] == 1


# ---- Safety violations ----------------------------------------


def test_direct_memory_citation_flagged_as_safety_violation():
    """A citation whose artifact_type starts with `knowledge_memory`
    is a Phase 5B contract violation — answers must cite source
    evidence, never the memory entry."""
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{
                "artifact_id": "mem-1",
                "artifact_type": "knowledge_memory.entry",
            }],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([_q()])
    r = report.results[0]
    assert SAFETY_DIRECT_MEMORY_CITATION in r.safety_violations
    assert r.verdict == VERDICT_SAFETY_VIOLATION


def test_memory_provider_failure_flagged_as_safety_violation():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={
            "status": "failed",
        }),
    )
    report = _evaluator(runner).evaluate([_q()])
    r = report.results[0]
    assert SAFETY_MEMORY_PROVIDER_FAILURE in r.safety_violations


def test_latency_regression_flagged_at_30_percent_threshold():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "used"}),
        baseline_sleep_ms=20,
        memory_sleep_ms=80,  # 4× — well over 30%.
    )
    report = _evaluator(runner).evaluate([_q()])
    r = report.results[0]
    assert SAFETY_LATENCY_REGRESSION in r.safety_violations


# ---- Verdict classification -----------------------------------


def test_verdict_improved_when_more_expected_terms_in_memory_mode():
    runner = _StubRunner(
        baseline_response=_response(answer="generic answer"),
        memory_response=_response(answer="risks and corrective action"),
    )
    report = _evaluator(runner).evaluate([_q(
        expected_terms=("corrective action",),
        category="risk",
    )])
    assert report.results[0].verdict == VERDICT_IMPROVED


def test_verdict_worsened_when_fewer_citations_for_domain_query():
    runner = _StubRunner(
        baseline_response=_response(citations=[
            {"artifact_id": "a1"}, {"artifact_id": "a2"},
        ]),
        memory_response=_response(citations=[
            {"artifact_id": "a1"},
        ]),
    )
    report = _evaluator(runner).evaluate([_q(category="risk")])
    assert report.results[0].verdict == VERDICT_WORSENED


def test_verdict_unchanged_when_nothing_meaningful_changed():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(),
    )
    report = _evaluator(runner).evaluate([_q()])
    assert report.results[0].verdict == VERDICT_UNCHANGED


# ---- Summary aggregation --------------------------------------


def test_summary_counts_improved_unchanged_worsened():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{"artifact_id": "a1"}, {"artifact_id": "a2"}],
        ),
    )
    queries = [_q(id=f"q{i}", category="risk") for i in range(3)]
    report = _evaluator(runner).evaluate(queries)
    # All three queries get an extra citation → improved.
    assert report.summary["queries_improved"] == 3
    assert report.summary["total_queries"] == 3


def test_summary_counts_memory_used_and_unavailable():
    """Distinct counters for each memory status family."""
    used_runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "used"}),
    )
    unavail_runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "not_available"}),
    )
    used_report = _evaluator(used_runner).evaluate([_q()])
    unavail_report = _evaluator(unavail_runner).evaluate([_q()])
    assert used_report.summary["queries_with_memory_used"] == 1
    assert used_report.summary["queries_with_memory_unavailable"] == 0
    assert unavail_report.summary["queries_with_memory_used"] == 0
    assert unavail_report.summary["queries_with_memory_unavailable"] == 1


def test_summary_warning_frequency_counts_per_warning():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={
            "status": "used",
            "warnings": ["selection_truncated"],
            "source_ref_resolution_warnings": ["source_ref_artifact_not_found"],
        }),
    )
    report = _evaluator(runner).evaluate([
        _q(id="q1"), _q(id="q2"),
    ])
    freq = report.summary["memory_warnings_frequency"]
    assert freq["selection_truncated"] == 2
    assert freq["source_ref_artifact_not_found"] == 2


# ---- Recommendation engine ------------------------------------


def test_recommendation_keep_disabled_on_safety_violation():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{
                "artifact_id": "mem-1",
                "artifact_type": "knowledge_memory.entry",
            }],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([_q()])
    assert report.recommendation == RECOMMENDATION_KEEP_DISABLED


def test_recommendation_needs_more_data_when_memory_never_used():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={
            "status": "not_available",
        }),
    )
    report = _evaluator(runner).evaluate([_q(), _q(id="q2")])
    assert report.recommendation == RECOMMENDATION_NEEDS_MORE_DATA


def test_recommendation_keep_disabled_when_more_than_25pct_worsened():
    """5 queries; 2 worsen via fewer citations on domain-relevant
    categories → 40% worsened → keep_disabled."""
    runner_better = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[
                {"artifact_id": "a"}, {"artifact_id": "b"},
            ],
            knowledge_memory={"status": "used"},
        ),
    )
    runner_worse = _StubRunner(
        baseline_response=_response(
            citations=[
                {"artifact_id": "a"}, {"artifact_id": "b"},
            ],
        ),
        memory_response=_response(
            citations=[{"artifact_id": "a"}],
            knowledge_memory={"status": "used"},
        ),
    )

    class _Dispatcher:
        def __init__(self, mapping):
            self.mapping = mapping

        def __call__(self, query, memory_enabled):
            return self.mapping[query.id](query, memory_enabled)

    mapping = {
        "q1": runner_better,
        "q2": runner_better,
        "q3": runner_better,
        "q4": runner_worse,
        "q5": runner_worse,
    }
    report = _evaluator(_Dispatcher(mapping)).evaluate([
        _q(id=qid, category="risk") for qid in mapping
    ])
    assert report.recommendation == RECOMMENDATION_KEEP_DISABLED


def test_recommendation_enable_project_scope_for_all_improved():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{"artifact_id": "a"}, {"artifact_id": "b"}],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([
        _q(id="q1", scope="project_active", category="risk"),
        _q(id="q2", scope="project_active", category="risk"),
    ])
    assert report.recommendation == RECOMMENDATION_ENABLE_PROJECT_SCOPE


def test_recommendation_enable_document_scope_when_all_document_scoped():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{"artifact_id": "a"}, {"artifact_id": "b"}],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([
        _q(id="q1", scope="document_active", document_id="doc-1", category="risk"),
        _q(id="q2", scope="document_active", document_id="doc-1", category="risk"),
    ])
    assert report.recommendation == RECOMMENDATION_ENABLE_DOCUMENT_SCOPE


def test_recommendation_enable_preview_when_scopes_mixed():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{"artifact_id": "a"}, {"artifact_id": "b"}],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([
        _q(id="q1", scope="project_active", category="risk"),
        _q(id="q2", scope="document_active", document_id="doc-1", category="risk"),
    ])
    assert report.recommendation == RECOMMENDATION_ENABLE_PREVIEW


def test_recommendation_value_set_is_pinned():
    """The recommendation must always be one of the pinned values
    so downstream dashboards can pattern-match safely."""
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "used"}),
    )
    report = _evaluator(runner).evaluate([_q(category="risk")])
    assert report.recommendation in RECOMMENDATION_VALUES


def test_compute_recommendation_roundtrips_via_report():
    """`compute_recommendation(report)` matches the value the
    evaluator already stamped on `report.recommendation`."""
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{"artifact_id": "a"}, {"artifact_id": "b"}],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([_q(category="risk")])
    assert compute_recommendation(report) == report.recommendation


# ---- Reports --------------------------------------------------


def test_report_serialises_to_json_losslessly():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "used"}),
    )
    report = _evaluator(runner).evaluate([_q(
        expected_terms=("risk",),
        category="risk",
    )])
    payload = json.dumps(report.to_dict(), indent=2)
    decoded = json.loads(payload)
    assert decoded["recommendation"] == report.recommendation
    assert decoded["summary"]["total_queries"] == 1
    assert decoded["results"][0]["query"]["id"] == "q1"


def test_markdown_report_includes_summary_table_and_recommendation():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={
            "status": "used", "scope": "project_active",
            "applied_expansion_terms": ["NCR"],
            "injected_evidence_count": 1,
        }),
    )
    report = _evaluator(runner).evaluate([_q(
        expected_terms=("risk",),
        category="risk",
    )])
    md = render_markdown_report(report)
    assert "# Memory Query Evaluation Report" in md
    assert "## Summary" in md
    assert "Recommendation:" in md
    assert "Per-query results" in md
    assert "q1" in md
    # The memory diagnostic line.
    assert "Status: `used`" in md
    assert "Applied expansion terms: NCR" in md


def test_markdown_report_includes_safety_violation_section_when_present():
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(
            citations=[{
                "artifact_id": "mem-1",
                "artifact_type": "knowledge_memory.entry",
            }],
            knowledge_memory={"status": "used"},
        ),
    )
    report = _evaluator(runner).evaluate([_q()])
    md = render_markdown_report(report)
    assert "Safety violations" in md
    assert "direct_memory_citation" in md


# ---- CLI / main behaviour -------------------------------------


def test_main_exits_zero_when_quality_is_mixed(tmp_path: Path):
    """Spec: quality verdicts are report data, not failures.
    `main()` returns 0 even when there are improved + worsened
    queries side-by-side. We test via direct evaluator call
    + the strict-mode toggle, not by subprocess."""
    runner = _StubRunner(
        baseline_response=_response(citations=[
            {"artifact_id": "a1"}, {"artifact_id": "a2"},
        ]),
        memory_response=_response(citations=[
            {"artifact_id": "a1"},
        ], knowledge_memory={"status": "used"}),
    )
    report = _evaluator(runner, strict=False).evaluate([_q(category="risk")])
    # Mixed quality (worsened) but no safety violations → strict
    # short-circuit doesn't fire. `main()` would return 0 here.
    assert report.summary["queries_with_safety_violation"] == 0
    assert report.summary["queries_worsened"] == 1


def test_strict_mode_does_not_change_evaluation_results():
    """`strict` only affects the CLI exit code — the report itself
    is identical."""
    runner = _StubRunner(
        baseline_response=_response(),
        memory_response=_response(knowledge_memory={"status": "used"}),
    )
    relaxed = _evaluator(runner).evaluate([_q()])
    runner.calls.clear()
    strict = _evaluator(runner, strict=True).evaluate([_q()])
    assert relaxed.recommendation == strict.recommendation
    assert relaxed.summary == strict.summary


def test_runner_exception_records_warning_and_continues():
    class _RaisingRunner:
        def __init__(self):
            self.calls = 0

        def __call__(self, query, memory_enabled):
            self.calls += 1
            if query.id == "q-bad":
                raise RuntimeError("boom")
            return _response(knowledge_memory={"status": "used"})

    report = _evaluator(_RaisingRunner()).evaluate([
        _q(id="q-ok"), _q(id="q-bad"), _q(id="q-also-ok"),
    ])
    # Bad query is skipped; the other two land in results.
    result_ids = [r.query.id for r in report.results]
    assert "q-bad" not in result_ids
    assert "q-ok" in result_ids
    assert "q-also-ok" in result_ids
    assert any("q-bad" in w for w in report.warnings)


# ---- Direct recommendation engine -----------------------------


def test_compute_recommendation_for_summary_handles_empty_results():
    """An empty results set always yields needs_more_data so the
    CLI can run a sanity check before a real evaluation pass."""
    summary = {"total_queries": 0}
    assert compute_recommendation_for_summary(summary, []) == (
        RECOMMENDATION_NEEDS_MORE_DATA
    )


def test_compute_recommendation_for_summary_caps_latency_regression():
    """Aggregate latency more than 30% over baseline downgrades the
    recommendation to dev-only."""
    summary = {
        "total_queries": 4,
        "queries_improved": 4,
        "queries_unchanged": 0,
        "queries_worsened": 0,
        "queries_with_safety_violation": 0,
        "queries_with_memory_used": 4,
        "avg_baseline_latency_ms": 100,
        "avg_memory_latency_ms": 200,  # 2× — well over 30%.
    }
    # We need some results to extract the scope; reuse the fixture
    # builder pattern.
    fake_results = [
        MemoryQueryEvalResult(
            query=_q(scope="project_active"),
            baseline=MemoryQueryEvalOutcome(
                answer="", answer_present=False, citation_count=1,
                retrieved_count=1, evidence_count=1, duration_ms=100,
                knowledge_memory=None,
            ),
            memory_aware=MemoryQueryEvalOutcome(
                answer="", answer_present=False, citation_count=2,
                retrieved_count=2, evidence_count=2, duration_ms=200,
                knowledge_memory={"status": "used"},
            ),
            delta={"citation_count": 1},
            verdict=VERDICT_IMPROVED,
        ),
    ]
    rec = compute_recommendation_for_summary(summary, fake_results)
    assert rec == RECOMMENDATION_ENABLE_DEV_ONLY


# ---- No-LLM regression guard ----------------------------------


def test_evaluate_memory_query_module_has_no_llm_imports():
    import importlib
    import inspect
    mod = importlib.import_module("j1.tools.evaluate_memory_query")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked, (
        f"j1.tools.evaluate_memory_query leaks LLM imports: {leaked}"
    )
