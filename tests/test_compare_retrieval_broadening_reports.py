"""Tests for the A/B report comparator.

Pins the spec's required cases:

  1. Two valid reports compare successfully.
  2. Added + removed query ids are detected.
  3. Improvements are detected.
  4. Regressions are detected.
  5. Missing optional fields don't crash.
  6. Invalid JSON exits non-zero.

Plus a couple of small invariants — the rendered text + JSON
both surface enough signal for an operator / CI consumer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from j1.tools.compare_retrieval_broadening_reports import (
    ChangeFlag,
    ComparisonReport,
    compare_reports,
    format_comparison,
    main,
    render_comparison_json,
)


# ---- Builders -----------------------------------------------------


def _result(
    *,
    query_id: str,
    question: str = "What?",
    base_retrieved: int | None = 3,
    variant_retrieved: int | None = 5,
    base_evidence: int | None = 2,
    variant_evidence: int | None = 3,
    enrichment_available: int = 0,
    enrichment_applied: int = 0,
) -> dict[str, Any]:
    """One result entry matching the harness's emit shape."""
    return {
        "query_id": query_id,
        "question": question,
        "baseline": {
            "retrieved_count": base_retrieved,
            "evidence_count": base_evidence,
            "diagnostics": {},
        },
        "alias_broadening": {
            "retrieved_count": variant_retrieved,
            "evidence_count": variant_evidence,
            "diagnostics": {
                "enrichment_alias_pairs_available": enrichment_available,
                "enrichment_alias_pairs_applied": enrichment_applied,
            },
        },
    }


def _report(
    *,
    scope: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scope": scope if scope is not None else {
            "tenant_id": "acme", "project_id": "alpha",
        },
        "results": results or [],
        "warnings": warnings or [],
    }


# ---- 1. Two valid reports compare successfully -------------------


def test_compare_runs_on_matching_id_pair():
    """Both reports share one query id; the comparator emits one
    matched record with the right deltas."""
    base = _report(results=[_result(
        query_id="q1",
        variant_retrieved=3, variant_evidence=2,
    )])
    candidate = _report(results=[_result(
        query_id="q1",
        variant_retrieved=5, variant_evidence=3,
    )])
    report = compare_reports(base, candidate)
    assert report.matched_ids == ("q1",)
    assert report.added_ids == ()
    assert report.removed_ids == ()
    [comparison] = report.comparisons
    assert comparison.retrieved_delta == 2
    assert comparison.evidence_delta == 1
    assert comparison.is_improvement is True
    assert comparison.is_regression is False


def test_compare_records_both_scopes_verbatim():
    base = _report(scope={
        "tenant_id": "t1", "project_id": "p1",
    })
    candidate = _report(scope={
        "tenant_id": "t1", "project_id": "p2",
    })
    report = compare_reports(base, candidate)
    assert report.base_scope == {"tenant_id": "t1", "project_id": "p1"}
    assert report.candidate_scope == {"tenant_id": "t1", "project_id": "p2"}


# ---- 2. Added + removed ids detected -----------------------------


def test_added_ids_detected():
    base = _report(results=[_result(query_id="q1")])
    candidate = _report(results=[
        _result(query_id="q1"),
        _result(query_id="q2"),
    ])
    report = compare_reports(base, candidate)
    assert report.added_ids == ("q2",)
    assert report.removed_ids == ()


def test_removed_ids_detected():
    base = _report(results=[
        _result(query_id="q1"),
        _result(query_id="q2"),
    ])
    candidate = _report(results=[_result(query_id="q1")])
    report = compare_reports(base, candidate)
    assert report.added_ids == ()
    assert report.removed_ids == ("q2",)


def test_added_and_removed_both_detected():
    base = _report(results=[_result(query_id="q1"), _result(query_id="q2")])
    candidate = _report(results=[_result(query_id="q2"), _result(query_id="q3")])
    report = compare_reports(base, candidate)
    assert set(report.matched_ids) == {"q2"}
    assert set(report.added_ids) == {"q3"}
    assert set(report.removed_ids) == {"q1"}


def test_question_text_fallback_when_id_missing():
    """The harness should always emit an id, but if a report
    contains an entry without one the comparator falls back to
    matching by question text + emits a warning."""
    base = {
        "results": [{
            "question": "what is RC?",
            "baseline": {"retrieved_count": 3, "evidence_count": 2},
            "alias_broadening": {
                "retrieved_count": 4, "evidence_count": 3,
                "diagnostics": {},
            },
        }],
        "warnings": [],
    }
    candidate = {
        "results": [{
            "query_id": "q-rc",
            "question": "what is RC?",  # same question text
            "baseline": {"retrieved_count": 3, "evidence_count": 2},
            "alias_broadening": {
                "retrieved_count": 6, "evidence_count": 4,
                "diagnostics": {},
            },
        }],
        "warnings": [],
    }
    report = compare_reports(base, candidate)
    # The base entry's missing id raises a warning.
    assert any("no id" in w for w in report.warnings)


# ---- 3. Improvements detected ------------------------------------


def test_improvement_retrieved_count_increase():
    base = _report(results=[_result(
        query_id="q1", variant_retrieved=3,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_retrieved=7,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.RETRIEVED_COUNT_INCREASED in c.change_flags
    assert c.is_improvement is True


def test_improvement_enrichment_applied_gained():
    base = _report(results=[_result(
        query_id="q1",
        enrichment_available=1, enrichment_applied=0,
    )])
    candidate = _report(results=[_result(
        query_id="q1",
        enrichment_available=1, enrichment_applied=1,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.ENRICHMENT_APPLIED_GAINED in c.change_flags
    assert c.enrichment_applied_delta == 1


def test_improvement_warnings_cleared():
    base = _report(
        results=[_result(query_id="q1")],
        warnings=["query 'q1' (baseline): something weird"],
    )
    candidate = _report(
        results=[_result(query_id="q1")],
        warnings=[],
    )
    [c] = compare_reports(base, candidate).comparisons
    assert c.warning_status_change == "cleared"
    assert ChangeFlag.WARNINGS_CLEARED in c.change_flags


def test_improvement_counts_now_present():
    base = _report(results=[_result(
        query_id="q1", variant_retrieved=None,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_retrieved=4,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.COUNTS_NOW_PRESENT in c.change_flags


# ---- 4. Regressions detected -------------------------------------


def test_regression_retrieved_count_decrease():
    base = _report(results=[_result(
        query_id="q1", variant_retrieved=7,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_retrieved=2,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.RETRIEVED_COUNT_DECREASED in c.change_flags
    assert c.is_regression is True
    assert c.retrieved_delta == -5


def test_regression_enrichment_applied_lost():
    base = _report(results=[_result(
        query_id="q1",
        enrichment_available=1, enrichment_applied=1,
    )])
    candidate = _report(results=[_result(
        query_id="q1",
        enrichment_available=1, enrichment_applied=0,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.ENRICHMENT_APPLIED_LOST in c.change_flags


def test_regression_warnings_gained():
    base = _report(results=[_result(query_id="q1")], warnings=[])
    candidate = _report(
        results=[_result(query_id="q1")],
        warnings=["query 'q1' (alias_broadening): something broke"],
    )
    [c] = compare_reports(base, candidate).comparisons
    assert c.warning_status_change == "gained"
    assert ChangeFlag.WARNINGS_GAINED in c.change_flags


def test_regression_counts_now_missing():
    base = _report(results=[_result(
        query_id="q1", variant_retrieved=4,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_retrieved=None,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.COUNTS_NOW_MISSING in c.change_flags


def test_evidence_decrease_is_regression():
    base = _report(results=[_result(
        query_id="q1", variant_evidence=4,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_evidence=1,
    )])
    [c] = compare_reports(base, candidate).comparisons
    assert ChangeFlag.EVIDENCE_COUNT_DECREASED in c.change_flags


# ---- 5. Missing optional fields don't crash ----------------------


def test_missing_baseline_or_variant_blocks_dont_crash():
    base = _report(results=[{"query_id": "q1", "question": "x"}])
    candidate = _report(results=[{"query_id": "q1", "question": "x"}])
    report = compare_reports(base, candidate)
    assert report.matched_ids == ("q1",)
    # Counts are None on both sides → no count flags fire.
    [c] = report.comparisons
    # All retrieval/evidence deltas should resolve cleanly to None
    # or the count-transition flags.
    assert c.retrieved_delta is None
    assert c.evidence_delta is None


def test_garbage_input_does_not_raise():
    """Malformed scope / results / warnings shapes (strings where
    a dict / list belongs) must not crash the comparator."""
    report = compare_reports(
        {"results": "not a list", "scope": "not a dict"},
        {"results": "also not", "scope": None},
    )
    assert report.matched_ids == ()
    assert report.base_scope == {}
    assert report.candidate_scope == {}


def test_empty_reports_compare_cleanly():
    report = compare_reports({}, {})
    assert report.matched_ids == ()
    assert report.added_ids == ()
    assert report.removed_ids == ()
    assert report.regressions == ()
    assert report.improvements == ()


# ---- 6. CLI / file I/O -------------------------------------------


def test_main_exits_nonzero_when_base_missing(tmp_path: Path, capsys):
    code = main([
        "--base", str(tmp_path / "missing.json"),
        "--candidate", str(tmp_path / "also_missing.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read" in captured.err


def test_main_exits_nonzero_on_invalid_json(tmp_path: Path, capsys):
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text("not json", encoding="utf-8")
    cand.write_text(json.dumps(_report()), encoding="utf-8")
    code = main(["--base", str(base), "--candidate", str(cand)])
    captured = capsys.readouterr()
    assert code == 2
    assert "not valid JSON" in captured.err


def test_main_prints_text_summary(tmp_path: Path, capsys):
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text(json.dumps(_report(results=[_result(
        query_id="q1", variant_retrieved=3,
    )])), encoding="utf-8")
    cand.write_text(json.dumps(_report(results=[_result(
        query_id="q1", variant_retrieved=5,
    )])), encoding="utf-8")
    code = main(["--base", str(base), "--candidate", str(cand)])
    captured = capsys.readouterr()
    assert code == 0
    assert "Retrieval-broadening report comparison" in captured.out
    assert "matched ids: 1" in captured.out
    assert "Top improvements" in captured.out


def test_main_writes_json_when_format_json(tmp_path: Path, capsys):
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text(json.dumps(_report(results=[_result(
        query_id="q1", variant_retrieved=3,
    )])), encoding="utf-8")
    cand.write_text(json.dumps(_report(results=[_result(
        query_id="q1", variant_retrieved=5,
    )])), encoding="utf-8")
    out = tmp_path / "comp.json"
    code = main([
        "--base", str(base), "--candidate", str(cand),
        "--format", "json", "--output", str(out),
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["counts"]["matched"] == 1
    assert payload["improvements"][0]["query_id"] == "q1"


# ---- Pure-function guarantees ------------------------------------


def test_top_list_cap_overflow_footer():
    """Many regressions → the formatter caps the list with a
    ``...and N more`` footer."""
    base_results = []
    cand_results = []
    for i in range(15):
        base_results.append(_result(
            query_id=f"q-{i}", variant_retrieved=10,
        ))
        cand_results.append(_result(
            query_id=f"q-{i}", variant_retrieved=1,
        ))
    report = compare_reports(
        _report(results=base_results),
        _report(results=cand_results),
    )
    text = format_comparison(report)
    assert "Top regressions (15)" in text
    assert "and 5 more" in text


def test_render_json_is_valid_and_deterministic():
    base = _report(results=[_result(
        query_id="q1", variant_retrieved=3,
    )])
    candidate = _report(results=[_result(
        query_id="q1", variant_retrieved=5,
    )])
    report = compare_reports(base, candidate)
    payload = render_comparison_json(report)
    parsed = json.loads(payload)
    assert "counts" in parsed
    assert "improvements" in parsed
    assert "regressions" in parsed
    # Two renderings of the same report produce identical text —
    # CI consumers can diff the output across runs reliably.
    again = render_comparison_json(report)
    assert payload == again


def test_comparator_has_no_bootstrap_imports():
    """AST guard: no production-bootstrap surface leaks into the
    comparator."""
    import inspect
    from j1.tools import compare_retrieval_broadening_reports as mod
    src = inspect.getsource(mod)
    forbidden = (
        "IngestionValidationService",
        "ArtifactRegistry",
        "SmartQueryOrchestrator",
        "JsonlIngestionRunStore",
        "ProjectContext",
        "deploy.dev",
    )
    for token in forbidden:
        assert token not in src, (
            f"comparator leaked production import {token!r}"
        )
