"""Operator-facing summarizer for the alias-broadening A/B report.

Pure read-only CLI. Reads a JSON report emitted by
:mod:`j1.tools.evaluate_retrieval_broadening` and prints a compact
operator-friendly summary plus a "top suspicious cases" list.

Design rules:

* **No service / bootstrap imports.** The summarizer reads JSON
  off disk and walks plain dicts. No validation service, no
  registry, no query pipeline.
* **Forgiving.** Missing optional fields surface as ``"—"`` or
  ``0`` rather than tracebacks. The only conditions that exit
  non-zero are "cannot read the file" / "not valid JSON".
* **No mutation.** The report file is opened read-only; no temp
  files; nothing is written to the filesystem beyond stdout (or
  the optional ``--output`` file).

Suspicious-case heuristics (kept simple per spec; no scoring
model):

1. Alias broadening decreased retrieved count.
2. Enrichment aliases were available but applied count is zero.
3. The query has warnings (matched via ``query_id`` substring on
   the report's top-level ``warnings`` list).
4. Baseline or variant retrieved/evidence counts are missing.
5. Alias broadening increased retrieved count but evidence count
   did not increase.

Usage:

::

    python -m j1.tools.summarize_retrieval_broadening_report \\
        --input broadening-ab-report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_log = logging.getLogger("j1.tools.summarize_retrieval_broadening_report")


__all__ = [
    "QuerySummary",
    "ReportSummary",
    "SuspicionFlag",
    "summarize_report",
    "format_summary",
    "main",
]


# Cap on suspicious-case rows printed. Anything above this count
# gets a "+ N more" footer line so the report stays tight.
_SUSPICIOUS_PRINT_CAP = 10


# ---- Domain shapes ------------------------------------------------


class SuspicionFlag:
    """Stable string identifiers the suspicious-case detector
    attaches to each flagged query. Kept as class attributes (not
    an Enum) so the report renders them verbatim with no extra
    indirection."""

    DECREASED_RETRIEVAL = "decreased_retrieval"
    ENRICHMENT_AVAILABLE_NOT_APPLIED = "enrichment_available_not_applied"
    HAS_WARNINGS = "has_warnings"
    MISSING_COUNTS = "missing_counts"
    RETRIEVAL_UP_EVIDENCE_FLAT = "retrieval_up_evidence_flat"


@dataclass(frozen=True)
class QuerySummary:
    """Per-query roll-up the summarizer assembles. Carries the
    delta numbers the printer renders plus the list of suspicion
    flags the heuristic attached."""

    query_id: str
    question: str
    baseline_retrieved: int | None
    variant_retrieved: int | None
    baseline_evidence: int | None
    variant_evidence: int | None
    retrieved_delta: int | None
    evidence_delta: int | None
    enrichment_available: int
    enrichment_applied: int
    suspicion_flags: tuple[str, ...]


@dataclass(frozen=True)
class ReportSummary:
    """Whole-report roll-up — the data the formatter renders."""

    scope: dict[str, Any]
    total_queries: int
    warning_count: int
    queries_increased: int
    queries_decreased: int
    queries_same: int
    queries_with_enrichment_available_not_applied: int
    suspicious_cases: tuple[QuerySummary, ...] = ()


# ---- Pure summarizer ----------------------------------------------


def summarize_report(report: Mapping[str, Any]) -> ReportSummary:
    """Project a parsed report dict into a :class:`ReportSummary`.

    Pure function: takes a dict, returns a dataclass. No I/O.
    Missing fields default to ``None`` / ``0`` so a partial /
    malformed report doesn't crash the summarizer; the printer
    surfaces the gaps as ``"—"`` rows."""
    scope = report.get("scope") if isinstance(report, Mapping) else {}
    if not isinstance(scope, Mapping):
        scope = {}
    raw_results = report.get("results") if isinstance(report, Mapping) else ()
    if not isinstance(raw_results, list):
        raw_results = []
    warnings = report.get("warnings") if isinstance(report, Mapping) else []
    if not isinstance(warnings, list):
        warnings = []

    per_query: list[QuerySummary] = []
    for entry in raw_results:
        if not isinstance(entry, Mapping):
            continue
        per_query.append(_summarize_query(entry, warnings))

    increased = sum(
        1 for q in per_query
        if q.retrieved_delta is not None and q.retrieved_delta > 0
    )
    decreased = sum(
        1 for q in per_query
        if q.retrieved_delta is not None and q.retrieved_delta < 0
    )
    same = sum(
        1 for q in per_query
        if q.retrieved_delta is not None and q.retrieved_delta == 0
    )
    enrich_not_applied = sum(
        1 for q in per_query
        if q.enrichment_available > 0 and q.enrichment_applied == 0
    )

    suspicious = tuple(q for q in per_query if q.suspicion_flags)
    return ReportSummary(
        scope=dict(scope),
        total_queries=len(per_query),
        warning_count=len(warnings),
        queries_increased=increased,
        queries_decreased=decreased,
        queries_same=same,
        queries_with_enrichment_available_not_applied=enrich_not_applied,
        suspicious_cases=suspicious,
    )


def _summarize_query(
    entry: Mapping[str, Any], warnings: list,
) -> QuerySummary:
    query_id = str(entry.get("query_id") or "")
    question = str(entry.get("question") or "")
    baseline = entry.get("baseline") or {}
    variant = entry.get("alias_broadening") or {}
    delta = entry.get("delta") or {}
    if not isinstance(baseline, Mapping):
        baseline = {}
    if not isinstance(variant, Mapping):
        variant = {}
    if not isinstance(delta, Mapping):
        delta = {}

    baseline_retrieved = _coerce_int(baseline.get("retrieved_count"))
    variant_retrieved = _coerce_int(variant.get("retrieved_count"))
    baseline_evidence = _coerce_int(baseline.get("evidence_count"))
    variant_evidence = _coerce_int(variant.get("evidence_count"))
    retrieved_delta = _coerce_int(delta.get("retrieved_count"))
    if retrieved_delta is None:
        retrieved_delta = _subtract(variant_retrieved, baseline_retrieved)
    evidence_delta = _coerce_int(delta.get("evidence_count"))
    if evidence_delta is None:
        evidence_delta = _subtract(variant_evidence, baseline_evidence)

    variant_diag = variant.get("diagnostics") or {}
    if not isinstance(variant_diag, Mapping):
        variant_diag = {}
    enrichment_available = (
        _coerce_int(variant_diag.get("enrichment_alias_pairs_available"))
        or 0
    )
    enrichment_applied = (
        _coerce_int(variant_diag.get("enrichment_alias_pairs_applied"))
        or 0
    )

    flags: list[str] = []
    if retrieved_delta is not None and retrieved_delta < 0:
        flags.append(SuspicionFlag.DECREASED_RETRIEVAL)
    if enrichment_available > 0 and enrichment_applied == 0:
        flags.append(SuspicionFlag.ENRICHMENT_AVAILABLE_NOT_APPLIED)
    if _query_in_warnings(query_id, warnings):
        flags.append(SuspicionFlag.HAS_WARNINGS)
    if baseline_retrieved is None or variant_retrieved is None:
        flags.append(SuspicionFlag.MISSING_COUNTS)
    if (
        retrieved_delta is not None and retrieved_delta > 0
        and (evidence_delta is None or evidence_delta <= 0)
    ):
        flags.append(SuspicionFlag.RETRIEVAL_UP_EVIDENCE_FLAT)
    return QuerySummary(
        query_id=query_id,
        question=question,
        baseline_retrieved=baseline_retrieved,
        variant_retrieved=variant_retrieved,
        baseline_evidence=baseline_evidence,
        variant_evidence=variant_evidence,
        retrieved_delta=retrieved_delta,
        evidence_delta=evidence_delta,
        enrichment_available=enrichment_available,
        enrichment_applied=enrichment_applied,
        suspicion_flags=tuple(flags),
    )


def _query_in_warnings(query_id: str, warnings: list) -> bool:
    """The harness emits warnings as free-form strings that
    include the query id verbatim (``"query 'q1' (mode): ..."``).
    Substring match is sufficient — operators reading the report
    pair them with the query row themselves."""
    if not query_id:
        return False
    needle = repr(query_id)  # matches the harness's `f"query {id!r}"`
    return any(needle in str(w) for w in warnings)


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None if value is None else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _subtract(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return a - b


# ---- Formatter ----------------------------------------------------


def format_summary(summary: ReportSummary) -> str:
    """Render the summary as plain text. Sectioned, no colours,
    deterministic — easy to diff between runs."""
    lines: list[str] = []
    lines.append("Retrieval-broadening A/B report summary")
    lines.append("=" * 39)
    lines.append("")
    lines.append("Scope:")
    if summary.scope:
        for key in ("tenant_id", "project_id", "document_id", "snapshot_id"):
            value = summary.scope.get(key)
            if value:
                lines.append(f"  {key}: {value}")
    else:
        lines.append("  (no scope recorded)")
    lines.append("")
    lines.append("Counts:")
    lines.append(f"  total queries: {summary.total_queries}")
    lines.append(f"  warnings: {summary.warning_count}")
    lines.append(f"  retrieved count increased: {summary.queries_increased}")
    lines.append(f"  retrieved count decreased: {summary.queries_decreased}")
    lines.append(f"  retrieved count unchanged: {summary.queries_same}")
    lines.append(
        f"  enrichment aliases available but not applied: "
        f"{summary.queries_with_enrichment_available_not_applied}"
    )
    lines.append("")
    lines.append(f"Suspicious cases ({len(summary.suspicious_cases)}):")
    if not summary.suspicious_cases:
        lines.append("  (none)")
    else:
        for case in summary.suspicious_cases[:_SUSPICIOUS_PRINT_CAP]:
            lines.append(_format_case(case))
        overflow = (
            len(summary.suspicious_cases) - _SUSPICIOUS_PRINT_CAP
        )
        if overflow > 0:
            lines.append(f"  ...and {overflow} more")
    return "\n".join(lines) + "\n"


def _format_case(case: QuerySummary) -> str:
    delta = (
        case.retrieved_delta
        if case.retrieved_delta is not None else "—"
    )
    flags = ", ".join(case.suspicion_flags) or "none"
    qid = case.query_id or "(no id)"
    preview = case.question
    if len(preview) > 60:
        preview = preview[:57] + "..."
    return (
        f"  - {qid} | retrieved Δ={delta} | flags: {flags}\n"
        f"      Q: {preview}"
    )


# ---- CLI ----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.summarize_retrieval_broadening_report",
        description=(
            "Read the JSON report produced by "
            "``j1.tools.evaluate_retrieval_broadening`` and print "
            "a compact operator-facing summary."
        ),
    )
    parser.add_argument(
        "--input", required=True, type=Path,
        help="Path to the A/B report JSON.",
    )
    parser.add_argument(
        "--output", default=None, type=Path,
        help=(
            "Write the formatted summary here instead of stdout. "
            "Optional."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        raw = args.input.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"error: cannot read {args.input}: {exc}\n",
        )
        return 2
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"error: {args.input} is not valid JSON: {exc}\n",
        )
        return 2
    if not isinstance(report, Mapping):
        sys.stderr.write(
            f"error: {args.input} top-level JSON is not an object\n",
        )
        return 2
    summary = summarize_report(report)
    text = format_summary(summary)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
