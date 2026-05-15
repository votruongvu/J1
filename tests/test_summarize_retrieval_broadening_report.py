"""Tests for the A/B report summarizer.

Pins the spec's required behaviours:

  1. Valid report → printed summary with the right counts.
  2. Empty results → zero counts, no crash.
  3. Missing optional fields → graceful "—" / 0 fallbacks.
  4. Invalid JSON → non-zero exit, message on stderr.
  5. Suspicious cases → detected per the five heuristics.

The summarizer is pure (in-memory dict → ``ReportSummary`` →
formatted string) so the unit tests don't need to spin up the
CLI or the filesystem — except for the file-handling test cases
which use ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from j1.tools.summarize_retrieval_broadening_report import (
    QuerySummary,
    ReportSummary,
    SuspicionFlag,
    format_summary,
    main,
    summarize_report,
)


# ---- Test fixtures -----------------------------------------------


def _query(
    *,
    query_id: str = "q1",
    question: str = "What is X?",
    baseline_retrieved: int | None = 3,
    variant_retrieved: int | None = 5,
    baseline_evidence: int | None = 2,
    variant_evidence: int | None = 3,
    retrieved_delta: int | None = None,
    evidence_delta: int | None = None,
    enrichment_available: int = 0,
    enrichment_applied: int = 0,
) -> dict[str, Any]:
    """Construct a per-query report entry matching what the
    harness emits. Optional fields can be passed as ``None`` to
    simulate a partial report row."""
    if retrieved_delta is None and baseline_retrieved is not None and variant_retrieved is not None:
        retrieved_delta = variant_retrieved - baseline_retrieved
    if evidence_delta is None and baseline_evidence is not None and variant_evidence is not None:
        evidence_delta = variant_evidence - baseline_evidence
    return {
        "query_id": query_id,
        "question": question,
        "baseline": {
            "retrieved_count": baseline_retrieved,
            "evidence_count": baseline_evidence,
            "diagnostics": {},
            "top_k_preview": [],
        },
        "alias_broadening": {
            "retrieved_count": variant_retrieved,
            "evidence_count": variant_evidence,
            "diagnostics": {
                "enrichment_alias_pairs_available": enrichment_available,
                "enrichment_alias_pairs_applied": enrichment_applied,
            },
            "top_k_preview": [],
        },
        "delta": {
            "retrieved_count": retrieved_delta,
            "evidence_count": evidence_delta,
            "enrichment_alias_pairs_applied": enrichment_applied,
        },
    }


def _report(
    *,
    scope: dict[str, Any] | None = None,
    results: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": "2026-05-20T12:00:00+00:00",
        "scope": scope if scope is not None else {
            "tenant_id": "acme",
            "project_id": "alpha",
            "document_id": "doc-1",
            "snapshot_id": "snap-active",
        },
        "config": {
            "baseline": {"alias_broadening_enabled": False},
            "variant": {"alias_broadening_enabled": True},
        },
        "summary": {},
        "results": results or [],
        "warnings": warnings or [],
    }


# ---- 1. Valid report ---------------------------------------------


def test_summarize_basic_report_counts_buckets_correctly():
    """Three queries: one increased, one same, one decreased."""
    report = _report(results=[
        _query(query_id="up", baseline_retrieved=3, variant_retrieved=5),
        _query(query_id="same", baseline_retrieved=4, variant_retrieved=4),
        _query(query_id="down", baseline_retrieved=5, variant_retrieved=2),
    ])
    summary = summarize_report(report)
    assert summary.total_queries == 3
    assert summary.queries_increased == 1
    assert summary.queries_decreased == 1
    assert summary.queries_same == 1
    assert summary.warning_count == 0


def test_summarize_records_scope_verbatim():
    report = _report(scope={
        "tenant_id": "t", "project_id": "p",
        "document_id": "d", "snapshot_id": "s",
    })
    summary = summarize_report(report)
    assert summary.scope == {
        "tenant_id": "t", "project_id": "p",
        "document_id": "d", "snapshot_id": "s",
    }


def test_format_summary_renders_every_section():
    summary = summarize_report(_report(results=[
        _query(query_id="q1", baseline_retrieved=2, variant_retrieved=4),
    ]))
    text = format_summary(summary)
    assert "Retrieval-broadening A/B report summary" in text
    assert "Scope:" in text
    assert "tenant_id: acme" in text
    assert "Counts:" in text
    assert "total queries: 1" in text
    # No suspicious cases → "(none)" line renders.
    assert "(none)" in text


def test_format_summary_includes_per_case_when_suspicious():
    """Per-case rows surface the query id + retrieved delta +
    flag set so the operator can spot the row at a glance."""
    report = _report(results=[
        _query(query_id="q-down", baseline_retrieved=5,
               variant_retrieved=1),
    ])
    text = format_summary(summarize_report(report))
    assert "q-down" in text
    assert "retrieved Δ=-4" in text
    assert SuspicionFlag.DECREASED_RETRIEVAL in text


# ---- 2. Empty / zero-case reports --------------------------------


def test_empty_results_summarizes_to_zero():
    report = _report(results=[])
    summary = summarize_report(report)
    assert summary.total_queries == 0
    assert summary.queries_increased == 0
    assert summary.queries_decreased == 0
    assert summary.queries_same == 0
    assert summary.suspicious_cases == ()


def test_empty_report_renders_without_crashing():
    text = format_summary(summarize_report({}))
    assert "total queries: 0" in text
    # No scope → fallback line.
    assert "no scope" in text.lower()


def test_warnings_only_report_still_summarizes():
    report = _report(results=[], warnings=["something happened"])
    summary = summarize_report(report)
    assert summary.warning_count == 1
    assert summary.total_queries == 0


# ---- 3. Missing-field tolerance ----------------------------------


def test_missing_baseline_block_does_not_crash():
    report = _report(results=[{
        "query_id": "q1",
        "question": "x",
        # No baseline / variant / delta blocks.
    }])
    summary = summarize_report(report)
    [case] = summary.suspicious_cases
    # MISSING_COUNTS flag fires since baseline/variant are None.
    assert SuspicionFlag.MISSING_COUNTS in case.suspicion_flags


def test_missing_diagnostics_block_does_not_crash():
    """The variant's diagnostics block is absent; the summarizer
    treats enrichment counts as 0 + emits no enrichment-not-
    applied flag."""
    report = _report(results=[{
        "query_id": "q1",
        "question": "x",
        "baseline": {"retrieved_count": 3, "evidence_count": 2},
        "alias_broadening": {"retrieved_count": 5, "evidence_count": 3},
        # No diagnostics block.
    }])
    summary = summarize_report(report)
    assert summary.total_queries == 1
    assert summary.queries_with_enrichment_available_not_applied == 0


def test_null_counts_flagged_missing():
    report = _report(results=[
        _query(
            query_id="q1",
            baseline_retrieved=None,
            variant_retrieved=None,
            baseline_evidence=None,
            variant_evidence=None,
        ),
    ])
    summary = summarize_report(report)
    [case] = summary.suspicious_cases
    assert SuspicionFlag.MISSING_COUNTS in case.suspicion_flags


def test_unexpected_field_types_are_tolerated():
    """A malformed report (strings where ints should be, dicts
    where lists should be) must NOT crash. Counts fall back to
    None / 0."""
    report = {
        "scope": "not a dict",
        "results": "not a list",
        "warnings": "not a list",
    }
    summary = summarize_report(report)
    assert summary.total_queries == 0
    assert summary.warning_count == 0
    assert summary.scope == {}


# ---- 4. Invalid JSON exits non-zero ------------------------------


def test_main_exits_nonzero_on_missing_file(tmp_path: Path, capsys):
    code = main(["--input", str(tmp_path / "does_not_exist.json")])
    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read" in captured.err


def test_main_exits_nonzero_on_invalid_json(tmp_path: Path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("not json at all", encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "not valid JSON" in captured.err


def test_main_exits_nonzero_when_top_level_not_object(
    tmp_path: Path, capsys,
):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "not an object" in captured.err


def test_main_prints_to_stdout_on_success(tmp_path: Path, capsys):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_report(results=[
        _query(query_id="q1"),
    ])), encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "total queries: 1" in captured.out


def test_main_writes_to_output_file_when_provided(tmp_path: Path):
    in_path = tmp_path / "in.json"
    out_path = tmp_path / "summary.txt"
    in_path.write_text(json.dumps(_report(results=[
        _query(query_id="q1"),
    ])), encoding="utf-8")
    code = main(["--input", str(in_path), "--output", str(out_path)])
    assert code == 0
    rendered = out_path.read_text(encoding="utf-8")
    assert "total queries: 1" in rendered


# ---- 5. Suspicious-case detection --------------------------------


def test_decreased_retrieval_flagged():
    report = _report(results=[
        _query(query_id="q-down", baseline_retrieved=5,
               variant_retrieved=1),
    ])
    [case] = summarize_report(report).suspicious_cases
    assert SuspicionFlag.DECREASED_RETRIEVAL in case.suspicion_flags
    assert case.retrieved_delta == -4


def test_enrichment_available_but_not_applied_flagged():
    """The expansion list might be empty even when enrichment
    aliases exist — e.g. the query didn't mention any alias form.
    Flag it so the operator can audit."""
    report = _report(results=[
        _query(
            query_id="q-enrich",
            baseline_retrieved=3, variant_retrieved=3,
            enrichment_available=2, enrichment_applied=0,
        ),
    ])
    [case] = summarize_report(report).suspicious_cases
    assert (
        SuspicionFlag.ENRICHMENT_AVAILABLE_NOT_APPLIED
        in case.suspicion_flags
    )


def test_query_with_warnings_flagged():
    """The harness emits warnings as ``f"query {id!r}: ..."`` so
    the summarizer matches via ``repr(query_id)`` substring."""
    report = _report(
        results=[_query(query_id="q-warn")],
        warnings=["query 'q-warn' (baseline): no augmentation"],
    )
    [case] = summarize_report(report).suspicious_cases
    assert SuspicionFlag.HAS_WARNINGS in case.suspicion_flags


def test_missing_counts_flagged():
    report = _report(results=[
        _query(
            query_id="q-missing",
            baseline_retrieved=None,
            variant_retrieved=5,
        ),
    ])
    [case] = summarize_report(report).suspicious_cases
    assert SuspicionFlag.MISSING_COUNTS in case.suspicion_flags


def test_retrieval_up_but_evidence_flat_flagged():
    """If broadening pulls more candidates but the evidence
    builder rejects all of them, the operator should review the
    rerank / sufficiency gate. Spec rule #5."""
    report = _report(results=[
        _query(
            query_id="q-pad",
            baseline_retrieved=3, variant_retrieved=8,
            baseline_evidence=2, variant_evidence=2,
        ),
    ])
    [case] = summarize_report(report).suspicious_cases
    assert (
        SuspicionFlag.RETRIEVAL_UP_EVIDENCE_FLAT
        in case.suspicion_flags
    )


def test_retrieval_up_with_evidence_up_is_clean():
    """Mirror of the above — when both go up, no false positive."""
    report = _report(results=[
        _query(
            query_id="q-good",
            baseline_retrieved=3, variant_retrieved=8,
            baseline_evidence=2, variant_evidence=5,
        ),
    ])
    summary = summarize_report(report)
    assert summary.suspicious_cases == ()


def test_multiple_flags_compose_on_one_query():
    """One query can carry several flags simultaneously — the
    formatter renders them all."""
    report = _report(
        results=[_query(
            query_id="q-bad",
            baseline_retrieved=5, variant_retrieved=2,
            enrichment_available=3, enrichment_applied=0,
        )],
        warnings=["query 'q-bad' (alias_broadening): something off"],
    )
    [case] = summarize_report(report).suspicious_cases
    assert SuspicionFlag.DECREASED_RETRIEVAL in case.suspicion_flags
    assert (
        SuspicionFlag.ENRICHMENT_AVAILABLE_NOT_APPLIED
        in case.suspicion_flags
    )
    assert SuspicionFlag.HAS_WARNINGS in case.suspicion_flags


# ---- 6. Read-only / no-bootstrap guard ---------------------------


def test_summarizer_has_no_bootstrap_imports():
    """AST guard: the summarizer module must NOT import the
    validation service, the registry, the orchestrator, or any
    other production-bootstrap surface. Pinned per spec."""
    import inspect
    from j1.tools import summarize_retrieval_broadening_report as mod
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
            f"summarizer leaked production import {token!r}"
        )


# ---- 7. Cap on suspicious-case printing --------------------------


def test_suspicious_case_print_cap_overflow_footer():
    """Many suspicious cases — the formatter caps the visible
    list and prints a ``...and N more`` footer."""
    results = [
        _query(
            query_id=f"q-{i}",
            baseline_retrieved=10, variant_retrieved=1,
        )
        for i in range(15)
    ]
    summary = summarize_report(_report(results=results))
    assert len(summary.suspicious_cases) == 15
    text = format_summary(summary)
    assert "and 5 more" in text


def test_main_handles_real_harness_report_shape(tmp_path: Path, capsys):
    """End-to-end smoke against a faithfully-shaped report — the
    same key set the harness writes. Pins the consumer side of
    the harness's output contract."""
    report = {
        "generated_at": "2026-05-20T12:00:00+00:00",
        "scope": {
            "tenant_id": "acme", "project_id": "alpha",
            "document_id": "doc-1", "snapshot_id": "snap-active",
        },
        "config": {
            "baseline": {"alias_broadening_enabled": False},
            "variant": {"alias_broadening_enabled": True},
        },
        "summary": {
            "query_count": 2,
            "baseline_avg_retrieved_count": 3.5,
            "alias_broadening_avg_retrieved_count": 5.0,
            "queries_with_more_results": 1,
            "queries_with_same_results": 1,
            "queries_with_fewer_results": 0,
            "queries_with_enrichment_aliases_available": 1,
            "queries_with_enrichment_aliases_applied": 1,
        },
        "results": [
            {
                "query_id": "q1",
                "question": "What is RC?",
                "baseline": {
                    "retrieved_count": 3,
                    "evidence_count": 2,
                    "diagnostics": {},
                    "top_k_preview": [],
                },
                "alias_broadening": {
                    "retrieved_count": 5,
                    "evidence_count": 4,
                    "diagnostics": {
                        "applied_to_retrieval": True,
                        "enrichment_alias_pairs_available": 1,
                        "enrichment_alias_pairs_applied": 1,
                    },
                    "top_k_preview": [],
                },
                "delta": {
                    "retrieved_count": 2,
                    "evidence_count": 2,
                    "enrichment_alias_pairs_applied": 1,
                },
            },
            {
                "query_id": "q2",
                "question": "What is BOQ?",
                "baseline": {
                    "retrieved_count": 4,
                    "evidence_count": 3,
                    "diagnostics": {},
                },
                "alias_broadening": {
                    "retrieved_count": 4,
                    "evidence_count": 3,
                    "diagnostics": {
                        "applied_to_retrieval": False,
                    },
                },
                "delta": {"retrieved_count": 0, "evidence_count": 0},
            },
        ],
        "warnings": [],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    code = main(["--input", str(path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "total queries: 2" in captured.out
    assert "retrieved count increased: 1" in captured.out
    assert "retrieved count unchanged: 1" in captured.out
