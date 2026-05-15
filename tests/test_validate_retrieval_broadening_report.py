"""Tests for the CI-friendly A/B report validator.

Pins the spec's failure conditions:

  1. Valid report passes with no guardrails.
  2. Warning threshold failure.
  3. Missing-counts failure.
  4. Broadening-regression failure.
  5. Minimum alias-applied threshold failure.
  6. Minimum query-count threshold failure.
  7. Invalid JSON exits non-zero (2).

Plus the read-only / no-bootstrap-import invariant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from j1.tools.validate_retrieval_broadening_report import (
    FAILURE_BELOW_MIN_ENRICHMENT_APPLIED,
    FAILURE_BELOW_MIN_QUERY_COUNT,
    FAILURE_QUERY_HAS_BROADENING_REGRESSION,
    FAILURE_QUERY_HAS_MISSING_COUNTS,
    FAILURE_WARNING_COUNT_EXCEEDS_MAX,
    GuardrailConfig,
    main,
    validate_report,
)


# ---- Builders -----------------------------------------------------


def _result(
    *,
    query_id: str = "q1",
    base_retrieved: int | None = 3,
    variant_retrieved: int | None = 5,
    base_evidence: int | None = 2,
    variant_evidence: int | None = 3,
    enrichment_applied: int = 0,
    enrichment_available: int = 0,
) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "question": "What?",
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
    results: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scope": {"tenant_id": "acme", "project_id": "alpha"},
        "results": results or [],
        "warnings": warnings or [],
    }


# ---- 1. No guardrails → PASS -------------------------------------


def test_no_guardrails_always_passes():
    """Spec: 'If no guardrails are provided, the tool should print
    a summary and exit zero.' Pinned via the pure validator first;
    CLI path covered separately below."""
    config = GuardrailConfig()
    assert config.any_enabled() is False
    outcome = validate_report(_report(results=[_result()]), config)
    assert outcome.passed is True
    assert outcome.failures == ()


def test_main_with_no_guardrails_exits_zero(tmp_path: Path, capsys):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_report(results=[_result()])), encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASSED" in captured.out


# ---- 2. Warning-count guardrail ----------------------------------


def test_warning_threshold_fails_when_exceeded():
    config = GuardrailConfig(max_warning_count=0)
    report = _report(
        results=[_result()],
        warnings=["query 'q1' (baseline): something broke"],
    )
    outcome = validate_report(report, config)
    assert outcome.passed is False
    codes = {f.code for f in outcome.failures}
    assert FAILURE_WARNING_COUNT_EXCEEDS_MAX in codes


def test_warning_threshold_at_limit_passes():
    config = GuardrailConfig(max_warning_count=2)
    report = _report(
        results=[_result()],
        warnings=["a", "b"],
    )
    outcome = validate_report(report, config)
    assert outcome.passed is True


def test_warning_threshold_zero_passes_when_no_warnings():
    config = GuardrailConfig(max_warning_count=0)
    report = _report(results=[_result()], warnings=[])
    assert validate_report(report, config).passed is True


# ---- 3. Missing-counts guardrail ---------------------------------


def test_missing_counts_fails_when_baseline_is_null():
    config = GuardrailConfig(fail_on_missing_counts=True)
    report = _report(results=[_result(
        query_id="q1", base_retrieved=None,
    )])
    outcome = validate_report(report, config)
    assert outcome.passed is False
    codes = {f.code for f in outcome.failures}
    assert FAILURE_QUERY_HAS_MISSING_COUNTS in codes
    # Failure message names the offending query.
    assert any("q1" in f.message for f in outcome.failures)


def test_missing_counts_fails_when_variant_is_null():
    config = GuardrailConfig(fail_on_missing_counts=True)
    report = _report(results=[_result(
        query_id="q1", variant_retrieved=None,
    )])
    outcome = validate_report(report, config)
    assert outcome.passed is False


def test_missing_counts_off_by_default_does_not_fail():
    """When the flag isn't set, missing-count queries don't trip
    a failure even though they appear in suspicious cases."""
    config = GuardrailConfig()
    report = _report(results=[_result(
        query_id="q1", variant_retrieved=None,
    )])
    assert validate_report(report, config).passed is True


# ---- 4. Broadening-regression guardrail --------------------------


def test_broadening_regression_fails_when_decreased():
    config = GuardrailConfig(fail_on_broadening_regressions=True)
    report = _report(results=[_result(
        query_id="q1", base_retrieved=5, variant_retrieved=2,
    )])
    outcome = validate_report(report, config)
    assert outcome.passed is False
    codes = {f.code for f in outcome.failures}
    assert FAILURE_QUERY_HAS_BROADENING_REGRESSION in codes
    # Failure message carries the delta so CI logs are
    # self-explanatory.
    assert any("Δ=" in f.message for f in outcome.failures)


def test_broadening_regression_does_not_fail_on_improvement():
    config = GuardrailConfig(fail_on_broadening_regressions=True)
    report = _report(results=[_result(
        query_id="q1", base_retrieved=3, variant_retrieved=7,
    )])
    assert validate_report(report, config).passed is True


def test_broadening_regression_does_not_fail_when_equal():
    config = GuardrailConfig(fail_on_broadening_regressions=True)
    report = _report(results=[_result(
        query_id="q1", base_retrieved=4, variant_retrieved=4,
    )])
    assert validate_report(report, config).passed is True


# ---- 5. Minimum enrichment-applied threshold ---------------------


def test_min_enrichment_applied_fails_when_below_threshold():
    """The guardrail counts how many queries had at least one
    enrichment alias APPLIED, not just available. Pinned per
    spec: 'queries_with_enrichment_aliases_applied'."""
    config = GuardrailConfig(
        min_queries_with_enrichment_aliases_applied=2,
    )
    report = _report(results=[
        _result(
            query_id="q1",
            enrichment_available=1, enrichment_applied=1,
        ),
        _result(
            query_id="q2",
            enrichment_available=1, enrichment_applied=0,
        ),
    ])
    outcome = validate_report(report, config)
    assert outcome.passed is False
    codes = {f.code for f in outcome.failures}
    assert FAILURE_BELOW_MIN_ENRICHMENT_APPLIED in codes


def test_min_enrichment_applied_passes_when_threshold_met():
    config = GuardrailConfig(
        min_queries_with_enrichment_aliases_applied=2,
    )
    report = _report(results=[
        _result(query_id="q1", enrichment_applied=1),
        _result(query_id="q2", enrichment_applied=1),
    ])
    assert validate_report(report, config).passed is True


# ---- 6. Minimum query-count threshold ----------------------------


def test_min_query_count_fails_when_below():
    config = GuardrailConfig(min_query_count=5)
    report = _report(results=[_result()])
    outcome = validate_report(report, config)
    assert outcome.passed is False
    codes = {f.code for f in outcome.failures}
    assert FAILURE_BELOW_MIN_QUERY_COUNT in codes


def test_min_query_count_passes_at_threshold():
    config = GuardrailConfig(min_query_count=2)
    report = _report(results=[_result(query_id="q1"), _result(query_id="q2")])
    assert validate_report(report, config).passed is True


# ---- 7. Invalid JSON / bad input → exit 2 ------------------------


def test_main_exit_code_2_when_file_missing(tmp_path: Path, capsys):
    code = main(["--input", str(tmp_path / "does_not_exist.json")])
    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read" in captured.err


def test_main_exit_code_2_when_invalid_json(tmp_path: Path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("garbage", encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "not valid JSON" in captured.err


def test_main_exit_code_2_when_top_level_not_object(tmp_path: Path, capsys):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 2


# ---- CLI exit codes for pass / fail ------------------------------


def test_main_exit_code_1_when_guardrail_fails(tmp_path: Path, capsys):
    """Per spec: 'exit non-zero when configured guardrails fail.'
    We pick exit code 1 (distinct from 2 = bad input) so CI can
    distinguish 'the report fails checks' from 'the file is
    broken'."""
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(
        results=[_result()],
        warnings=["warning 1", "warning 2"],
    )), encoding="utf-8")
    code = main([
        "--input", str(path),
        "--max-warning-count", "0",
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert "FAILED" in captured.out
    assert "warning_count 2 exceeds max 0" in captured.out


def test_main_exit_code_0_when_all_guardrails_pass(tmp_path: Path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report(
        results=[_result()],
    )), encoding="utf-8")
    code = main([
        "--input", str(path),
        "--max-warning-count", "0",
        "--fail-on-missing-counts",
        "--fail-on-broadening-regressions",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASSED" in captured.out


# ---- Composite: multiple guardrails fire independently -----------


def test_multiple_guardrails_each_emit_failures():
    """One report can violate multiple guardrails — each failure
    is recorded with its own code so CI can grep on the set."""
    config = GuardrailConfig(
        max_warning_count=0,
        fail_on_missing_counts=True,
        fail_on_broadening_regressions=True,
    )
    report = _report(
        results=[
            _result(
                query_id="q1",
                base_retrieved=5, variant_retrieved=2,
            ),
            _result(
                query_id="q2",
                base_retrieved=None,
            ),
        ],
        warnings=["something"],
    )
    outcome = validate_report(report, config)
    codes = {f.code for f in outcome.failures}
    assert FAILURE_WARNING_COUNT_EXCEEDS_MAX in codes
    assert FAILURE_QUERY_HAS_MISSING_COUNTS in codes
    assert FAILURE_QUERY_HAS_BROADENING_REGRESSION in codes


# ---- Read-only / bootstrap-free invariant ------------------------


def test_validator_has_no_bootstrap_imports():
    """AST guard: the validator must not pull in production query
    surface, registry, or bootstrap modules."""
    import inspect
    from j1.tools import validate_retrieval_broadening_report as mod
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
            f"validator leaked production import {token!r}"
        )
