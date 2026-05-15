"""Smoke tests for the golden retrieval-broadening fixtures.

The summarizer / comparator / validator / Markdown-export tests
all build their own inline dicts for the specific edge cases
they pin. The fixtures here are the SHARED happy/sad-path inputs
the consumer tooling should accept end-to-end. This file proves:

  * Every fixture file is valid JSON with a well-known top-level
    shape.
  * The summarizer accepts every fixture and never raises.
  * The comparator accepts the regression pair end-to-end.
  * The validator accepts the warnings fixture with the
    matching guardrail.
  * The Markdown exporter renders each fixture without raising.

If a future PR adds a new fixture, append a row to the
parameterised tests below — the failure surfaces immediately if
the new file doesn't load cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.retrieval_broadening import (
    fixture_path,
    load_fixture,
)
from j1.tools.compare_retrieval_broadening_reports import (
    compare_reports,
    format_comparison,
)
from j1.tools.summarize_retrieval_broadening_report import (
    format_summary,
    render_markdown,
    summarize_report,
)
from j1.tools.validate_retrieval_broadening_report import (
    GuardrailConfig,
    validate_report,
)


_ALL_FIXTURE_NAMES = (
    "report_basic",
    "report_empty",
    "report_with_warnings",
    "report_missing_fields",
    "report_regression_before",
    "report_regression_after",
)


# ---- Fixture file shape ------------------------------------------


@pytest.mark.parametrize("name", _ALL_FIXTURE_NAMES)
def test_fixture_loads_as_object(name: str):
    """Every fixture file MUST be a JSON object — the tools never
    accept a top-level list. Pinned at module level so adding a
    bad fixture file fails the very first test."""
    payload = load_fixture(name)
    assert isinstance(payload, dict)


@pytest.mark.parametrize("name", _ALL_FIXTURE_NAMES)
def test_fixture_has_results_key(name: str):
    payload = load_fixture(name)
    assert "results" in payload
    assert isinstance(payload["results"], list)


def test_fixture_path_resolves_to_existing_file():
    path = fixture_path("report_basic")
    assert path.exists()
    assert path.suffix == ".json"


# ---- Summarizer accepts every fixture ----------------------------


@pytest.mark.parametrize("name", _ALL_FIXTURE_NAMES)
def test_summarizer_accepts_fixture(name: str):
    payload = load_fixture(name)
    summary = summarize_report(payload)
    rendered = format_summary(summary)
    # Text output always contains the header and the counts
    # section regardless of which fixture is used.
    assert "Retrieval-broadening A/B report summary" in rendered
    assert "Counts:" in rendered


# ---- Comparator accepts the regression pair ----------------------


def test_comparator_regression_pair_surfaces_one_regression():
    """The ``before`` / ``after`` fixtures intentionally form a
    pair: query ``compare_alias_002`` regresses while
    ``compare_alias_001`` improves. Pinned here so a future fixture
    edit that accidentally invalidates the regression scenario
    surfaces immediately."""
    base = load_fixture("report_regression_before")
    candidate = load_fixture("report_regression_after")
    report = compare_reports(base, candidate)
    matched = set(report.matched_ids)
    assert {
        "compare_alias_001", "compare_alias_002", "compare_neutral_003",
    } <= matched
    regression_ids = {c.query_id for c in report.regressions}
    improvement_ids = {c.query_id for c in report.improvements}
    assert "compare_alias_002" in regression_ids
    assert "compare_alias_001" in improvement_ids
    # And the rendered text mentions BOTH the regressions section
    # and the improvements section.
    text = format_comparison(report)
    assert "Top regressions" in text
    assert "Top improvements" in text


# ---- Validator accepts the warnings fixture ----------------------


def test_validator_warnings_fixture_trips_max_warning_count_guardrail():
    """``report_with_warnings`` ships two warnings, so a guardrail
    of ``max-warning-count=0`` MUST fail. Conversely, the same
    fixture with no guardrails MUST pass."""
    report = load_fixture("report_with_warnings")
    failing = validate_report(
        report, GuardrailConfig(max_warning_count=0),
    )
    assert failing.passed is False
    passing = validate_report(report, GuardrailConfig())
    assert passing.passed is True


def test_validator_basic_fixture_passes_under_strict_guardrails():
    """The ``basic`` fixture is the happy path — every guardrail
    should pass. Pinned so any future fixture edit that breaks
    the happy path fails fast."""
    report = load_fixture("report_basic")
    outcome = validate_report(
        report,
        GuardrailConfig(
            max_warning_count=0,
            fail_on_missing_counts=True,
            fail_on_broadening_regressions=True,
            min_query_count=1,
        ),
    )
    assert outcome.passed is True


def test_validator_missing_fields_fixture_trips_missing_counts():
    report = load_fixture("report_missing_fields")
    outcome = validate_report(
        report, GuardrailConfig(fail_on_missing_counts=True),
    )
    assert outcome.passed is False


# ---- Markdown export against every fixture -----------------------


@pytest.mark.parametrize("name", _ALL_FIXTURE_NAMES)
def test_markdown_export_renders_for_fixture(name: str):
    payload = load_fixture(name)
    rendered = render_markdown(payload)
    # Every Markdown rendering is deterministic + carries the
    # canonical sections, even on the empty fixture (which renders
    # the "no results" copy).
    assert rendered.startswith("# Retrieval Broadening A/B Summary")
    assert "## Scope" in rendered
    assert "## Summary" in rendered
    assert "## Query Results" in rendered
    assert "## Notes" in rendered
    # Re-render the same fixture → byte-identical output.
    again = render_markdown(payload)
    assert rendered == again


# ---- File-on-disk integration (validates the path helper) -------


def test_validator_main_accepts_fixture_file(tmp_path: Path, capsys):
    """The CLI accepts the on-disk fixture path verbatim — pinned
    so future test refactors keep using the shared fixtures
    instead of inlining JSON."""
    from j1.tools.validate_retrieval_broadening_report import main
    code = main([
        "--input", str(fixture_path("report_basic")),
        "--max-warning-count", "0",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASSED" in captured.out


def test_summarizer_main_accepts_fixture_file_markdown(
    tmp_path: Path, capsys,
):
    from j1.tools.summarize_retrieval_broadening_report import main
    code = main([
        "--input", str(fixture_path("report_basic")),
        "--format", "markdown",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "# Retrieval Broadening A/B Summary" in captured.out
