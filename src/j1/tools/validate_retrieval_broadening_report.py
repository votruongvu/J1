"""CI-friendly guardrail validator for retrieval-broadening reports.

Inspects an existing A/B report and exits non-zero when any
configured guardrail fails. All guardrails are opt-in — running
the validator with no guardrail flags emits a one-line ``PASSED``
summary and exits ``0``, so wiring it into CI is purely additive.

Read-only by contract — never mutates the report file, never imports
anything from the production query path / bootstrap / LLM clients.

Exit codes:

* ``0`` — every configured guardrail passed.
* ``1`` — at least one guardrail failed. Failure messages are
  written to stdout for CI capture.
* ``2`` — the report file couldn't be read or isn't valid JSON.
  Distinguishes "the file is bad" (operator error) from "the
  report failed checks" (regression).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from j1.tools.summarize_retrieval_broadening_report import (
    SuspicionFlag,
    summarize_report,
)


__all__ = [
    "GuardrailConfig",
    "ValidationFailure",
    "ValidationOutcome",
    "validate_report",
    "format_outcome",
    "main",
]


# ---- Config + outcome shapes -------------------------------------


@dataclass(frozen=True)
class GuardrailConfig:
    """The subset of CLI flags that gate failure.

    ``None`` / ``False`` means "this guardrail is not enabled" —
    the validator skips the corresponding check entirely. The CLI
    parser leaves every guardrail unset by default per spec
    ("all guardrails are opt-in")."""

    max_warning_count: int | None = None
    fail_on_missing_counts: bool = False
    fail_on_broadening_regressions: bool = False
    min_queries_with_enrichment_aliases_applied: int | None = None
    min_query_count: int | None = None

    def any_enabled(self) -> bool:
        return any((
            self.max_warning_count is not None,
            self.fail_on_missing_counts,
            self.fail_on_broadening_regressions,
            self.min_queries_with_enrichment_aliases_applied is not None,
            self.min_query_count is not None,
        ))


@dataclass(frozen=True)
class ValidationFailure:
    """One guardrail failure. ``code`` is a stable identifier so
    CI consumers can grep on it; ``message`` is the operator-
    readable line."""

    code: str
    message: str


@dataclass(frozen=True)
class ValidationOutcome:
    """The validator's final verdict."""

    passed: bool
    failures: tuple[ValidationFailure, ...] = field(default_factory=tuple)


# ---- Failure codes -----------------------------------------------


FAILURE_WARNING_COUNT_EXCEEDS_MAX = "warning_count_exceeds_max"
FAILURE_QUERY_HAS_MISSING_COUNTS = "query_has_missing_counts"
FAILURE_QUERY_HAS_BROADENING_REGRESSION = (
    "query_has_broadening_regression"
)
FAILURE_BELOW_MIN_QUERY_COUNT = "below_min_query_count"
FAILURE_BELOW_MIN_ENRICHMENT_APPLIED = (
    "below_min_enrichment_alias_pairs_applied"
)


# ---- Pure validator ----------------------------------------------


def validate_report(
    report: Mapping[str, Any],
    config: GuardrailConfig,
) -> ValidationOutcome:
    """Pure function: walk the report's summarised state, check
    every enabled guardrail, return a verdict. No I/O."""
    summary = summarize_report(report)
    failures: list[ValidationFailure] = []

    if (
        config.max_warning_count is not None
        and summary.warning_count > config.max_warning_count
    ):
        failures.append(ValidationFailure(
            code=FAILURE_WARNING_COUNT_EXCEEDS_MAX,
            message=(
                f"warning_count {summary.warning_count} exceeds "
                f"max {config.max_warning_count}"
            ),
        ))

    if (
        config.min_query_count is not None
        and summary.total_queries < config.min_query_count
    ):
        failures.append(ValidationFailure(
            code=FAILURE_BELOW_MIN_QUERY_COUNT,
            message=(
                f"query_count {summary.total_queries} below "
                f"min {config.min_query_count}"
            ),
        ))

    if config.min_queries_with_enrichment_aliases_applied is not None:
        actual = sum(
            1 for q in summary.suspicious_cases  # noqa: not just suspicious — re-check from report directly below
        )  # placeholder; the real check uses the report directly.
        # We re-walk results so we don't rely on suspicious_cases
        # for the threshold (suspicious_cases excludes queries
        # where enrichment was applied — by design).
        applied_count = _count_queries_with_enrichment_applied(report)
        if applied_count < config.min_queries_with_enrichment_aliases_applied:
            failures.append(ValidationFailure(
                code=FAILURE_BELOW_MIN_ENRICHMENT_APPLIED,
                message=(
                    f"queries_with_enrichment_aliases_applied "
                    f"{applied_count} below min "
                    f"{config.min_queries_with_enrichment_aliases_applied}"
                ),
            ))
        # Discard the placeholder we set above; this branch already
        # computed the real value via _count_queries_with_enrichment_applied.
        _ = actual

    if config.fail_on_missing_counts:
        for case in summary.suspicious_cases:
            if SuspicionFlag.MISSING_COUNTS in case.suspicion_flags:
                failures.append(ValidationFailure(
                    code=FAILURE_QUERY_HAS_MISSING_COUNTS,
                    message=(
                        f"query {case.query_id!r} has missing "
                        "retrieved_count on baseline or alias_broadening"
                    ),
                ))

    if config.fail_on_broadening_regressions:
        for case in summary.suspicious_cases:
            if (
                SuspicionFlag.DECREASED_RETRIEVAL
                in case.suspicion_flags
            ):
                delta = (
                    case.retrieved_delta
                    if case.retrieved_delta is not None else "?"
                )
                failures.append(ValidationFailure(
                    code=FAILURE_QUERY_HAS_BROADENING_REGRESSION,
                    message=(
                        f"query {case.query_id!r} alias_broadening "
                        f"retrieved_count regressed (Δ={delta})"
                    ),
                ))

    return ValidationOutcome(
        passed=not failures, failures=tuple(failures),
    )


def _count_queries_with_enrichment_applied(
    report: Mapping[str, Any],
) -> int:
    """How many queries had at least one enrichment alias pair
    actually applied in the variant mode. Walks the report
    directly so missing diagnostics surface as zero rather than
    raising."""
    results = report.get("results") if isinstance(report, Mapping) else ()
    if not isinstance(results, list):
        return 0
    count = 0
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        variant = entry.get("alias_broadening") or {}
        if not isinstance(variant, Mapping):
            continue
        diag = variant.get("diagnostics") or {}
        if not isinstance(diag, Mapping):
            continue
        applied = diag.get("enrichment_alias_pairs_applied")
        try:
            if int(applied) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


# ---- Renderer ----------------------------------------------------


def format_outcome(outcome: ValidationOutcome) -> str:
    """Single-block output suitable for CI logs. Deterministic."""
    if outcome.passed:
        return "Retrieval broadening report validation: PASSED\n"
    lines = ["Retrieval broadening report validation: FAILED"]
    for failure in outcome.failures:
        lines.append(f"- {failure.message}")
    return "\n".join(lines) + "\n"


# ---- CLI ----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.validate_retrieval_broadening_report",
        description=(
            "Validate a retrieval-broadening A/B report against "
            "CI-friendly guardrails. Exits 1 on guardrail failure, "
            "2 on bad input."
        ),
    )
    parser.add_argument(
        "--input", required=True, type=Path,
        help="Path to the A/B report JSON.",
    )
    parser.add_argument(
        "--max-warning-count", type=int, default=None,
        help=(
            "Fail when the report's top-level warning count "
            "exceeds this number."
        ),
    )
    parser.add_argument(
        "--fail-on-missing-counts", action="store_true",
        help=(
            "Fail when any query has a missing retrieved_count "
            "on baseline or alias_broadening."
        ),
    )
    parser.add_argument(
        "--fail-on-broadening-regressions", action="store_true",
        help=(
            "Fail when any query's alias_broadening retrieved "
            "count decreased vs baseline."
        ),
    )
    parser.add_argument(
        "--min-queries-with-enrichment-aliases-applied",
        type=int, default=None,
        help=(
            "Fail when the number of queries with at least one "
            "enrichment alias actually applied falls below this "
            "threshold."
        ),
    )
    parser.add_argument(
        "--min-query-count", type=int, default=None,
        help="Fail when the total query count is below this number.",
    )
    return parser.parse_args(argv)


def _read_report(path: Path) -> Mapping[str, Any] | int:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"error: cannot read {path}: {exc}\n")
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"error: {path} is not valid JSON: {exc}\n",
        )
        return 2
    if not isinstance(payload, Mapping):
        sys.stderr.write(
            f"error: {path} top-level JSON is not an object\n",
        )
        return 2
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    report = _read_report(args.input)
    if isinstance(report, int):
        return report
    config = GuardrailConfig(
        max_warning_count=args.max_warning_count,
        fail_on_missing_counts=args.fail_on_missing_counts,
        fail_on_broadening_regressions=args.fail_on_broadening_regressions,
        min_queries_with_enrichment_aliases_applied=(
            args.min_queries_with_enrichment_aliases_applied
        ),
        min_query_count=args.min_query_count,
    )
    if not config.any_enabled():
        # Per spec: "If no guardrails are provided, the tool should
        # print a summary and exit zero." We surface a clear PASS
        # line so CI logs always carry a signal.
        sys.stdout.write(format_outcome(ValidationOutcome(passed=True)))
        return 0
    outcome = validate_report(report, config)
    sys.stdout.write(format_outcome(outcome))
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
