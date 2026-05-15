"""Pure read-only comparator for two retrieval-broadening A/B reports.

Reads a ``base`` and ``candidate`` report (both emitted by
:mod:`j1.tools.evaluate_retrieval_broadening`) and prints a compact
operator-facing comparison that highlights:

* matched / added / removed query ids
* per-query deltas (retrieved / evidence / enrichment-alias-applied)
* warning-count change
* top regressions + top improvements

Designed for CI usage too — ``--format json`` emits a deterministic
structure suitable for downstream tooling.

Design rules (mirrors the summarizer):

* **No service / bootstrap imports.** Just JSON files and plain
  dict traversal.
* **Forgiving.** Missing optional fields become ``None`` / ``0``;
  the comparator never raises on a malformed report row.
* **Read-only.** No file mutations; ``--output`` is the only write
  path, optional.
* Exit code ``2`` only on "cannot read the file" / "not valid
  JSON" / "top-level not an object".

Matching: by ``query_id``. If neither side has an id for a given
question, the comparator falls back to question-text match and
emits a warning explaining the heuristic.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_log = logging.getLogger("j1.tools.compare_retrieval_broadening_reports")


__all__ = [
    "ChangeFlag",
    "QueryComparison",
    "ComparisonReport",
    "compare_reports",
    "format_comparison",
    "render_comparison_json",
    "main",
]


# Cap on top-regression / top-improvement rows printed.
_TOP_LIST_CAP = 10


# ---- Change vocabulary --------------------------------------------


class ChangeFlag:
    """Stable identifiers attached to matched-query diffs. Class
    attributes (not Enum values) so the rendered output is the
    raw string — friendly to grep + stable for CI consumers."""

    # Regressions
    RETRIEVED_COUNT_DECREASED = "retrieved_count_decreased"
    EVIDENCE_COUNT_DECREASED = "evidence_count_decreased"
    WARNINGS_GAINED = "warnings_gained"
    ENRICHMENT_APPLIED_LOST = "enrichment_applied_lost"
    COUNTS_NOW_MISSING = "counts_now_missing"

    # Improvements
    RETRIEVED_COUNT_INCREASED = "retrieved_count_increased"
    EVIDENCE_COUNT_INCREASED = "evidence_count_increased"
    ENRICHMENT_APPLIED_GAINED = "enrichment_applied_gained"
    WARNINGS_CLEARED = "warnings_cleared"
    COUNTS_NOW_PRESENT = "counts_now_present"


_REGRESSION_FLAGS: frozenset[str] = frozenset({
    ChangeFlag.RETRIEVED_COUNT_DECREASED,
    ChangeFlag.EVIDENCE_COUNT_DECREASED,
    ChangeFlag.WARNINGS_GAINED,
    ChangeFlag.ENRICHMENT_APPLIED_LOST,
    ChangeFlag.COUNTS_NOW_MISSING,
})

_IMPROVEMENT_FLAGS: frozenset[str] = frozenset({
    ChangeFlag.RETRIEVED_COUNT_INCREASED,
    ChangeFlag.EVIDENCE_COUNT_INCREASED,
    ChangeFlag.ENRICHMENT_APPLIED_GAINED,
    ChangeFlag.WARNINGS_CLEARED,
    ChangeFlag.COUNTS_NOW_PRESENT,
})


# ---- Result shapes -----------------------------------------------


@dataclass(frozen=True)
class QueryComparison:
    """Per-query diff record for matched queries.

    ``base_*`` / ``candidate_*`` fields carry the raw retrieved /
    evidence counts so the formatter can render the absolute
    numbers alongside the deltas; downstream JSON consumers read
    them directly.
    """

    query_id: str
    question: str
    base_retrieved: int | None
    candidate_retrieved: int | None
    base_evidence: int | None
    candidate_evidence: int | None
    retrieved_delta: int | None
    evidence_delta: int | None
    enrichment_applied_delta: int
    warning_status_change: str  # "unchanged" / "gained" / "cleared"
    change_flags: tuple[str, ...]

    @property
    def is_regression(self) -> bool:
        return any(f in _REGRESSION_FLAGS for f in self.change_flags)

    @property
    def is_improvement(self) -> bool:
        return any(f in _IMPROVEMENT_FLAGS for f in self.change_flags)


@dataclass(frozen=True)
class ComparisonReport:
    """Whole-comparison roll-up the renderer consumes."""

    base_scope: dict[str, Any]
    candidate_scope: dict[str, Any]
    base_query_count: int
    candidate_query_count: int
    base_warning_count: int
    candidate_warning_count: int
    matched_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    comparisons: tuple[QueryComparison, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def regressions(self) -> tuple[QueryComparison, ...]:
        return tuple(c for c in self.comparisons if c.is_regression)

    @property
    def improvements(self) -> tuple[QueryComparison, ...]:
        return tuple(c for c in self.comparisons if c.is_improvement)


# ---- Pure comparator ----------------------------------------------


def compare_reports(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> ComparisonReport:
    """Pure projection. Returns a fully-populated
    :class:`ComparisonReport` — no I/O, no rendering, no exit
    codes."""
    base_scope = _scope_of(base)
    cand_scope = _scope_of(candidate)
    base_results = _results_of(base)
    cand_results = _results_of(candidate)
    base_warnings = _warnings_of(base)
    cand_warnings = _warnings_of(candidate)

    warnings: list[str] = []
    # Split each side into "has-id" and "no-id" partitions. Id
    # matching is the primary path; question-text matching is the
    # narrow fallback the spec allows ONLY when an entry has no
    # id at all. Letting the fallback compensate for id mismatches
    # (e.g. a renamed query) would silently match unrelated rows,
    # so the fallback strictly serves the no-id partition.
    base_by_id, base_no_id = _partition_by_id(
        base_results, warnings, label="base",
    )
    cand_by_id, cand_no_id = _partition_by_id(
        cand_results, warnings, label="candidate",
    )

    matched_ids: list[str] = []
    comparisons: list[QueryComparison] = []
    consumed_cand_no_id: set[int] = set()
    # 1. Id-keyed match.
    for qid, base_entry in base_by_id.items():
        cand_entry = cand_by_id.get(qid)
        if cand_entry is None:
            # Try the no-id fallback by question text on either
            # side. The candidate may legitimately have lost the
            # id; the base may carry a question that lands in the
            # candidate's no-id partition.
            cand_idx = _find_by_question_in_no_id(
                base_entry, cand_no_id, consumed_cand_no_id,
            )
            if cand_idx is None:
                continue
            cand_entry = cand_no_id[cand_idx]
            consumed_cand_no_id.add(cand_idx)
            warnings.append(
                f"matched base query {qid!r} to a candidate entry "
                "via question text (candidate entry had no id)"
            )
        matched_ids.append(qid)
        comparisons.append(_diff_one(
            query_id=qid,
            base=base_entry,
            candidate=cand_entry,
            base_warnings=base_warnings,
            candidate_warnings=cand_warnings,
        ))
    # 2. No-id base entries — only matched via question text.
    consumed_cand_by_id: set[str] = set()
    for base_entry in base_no_id:
        cand_entry, cand_match_id, cand_idx = _find_match_for_no_id(
            base_entry, cand_by_id, cand_no_id,
            consumed_by_id=consumed_cand_by_id,
            consumed_no_id=consumed_cand_no_id,
        )
        if cand_entry is None:
            continue
        if cand_match_id is not None:
            consumed_cand_by_id.add(cand_match_id)
            matched_id = cand_match_id
        else:
            assert cand_idx is not None
            consumed_cand_no_id.add(cand_idx)
            matched_id = _synthetic_match_id(base_entry)
        warnings.append(
            "matched a base entry with no id to candidate via "
            "question text"
        )
        matched_ids.append(matched_id)
        comparisons.append(_diff_one(
            query_id=matched_id,
            base=base_entry,
            candidate=cand_entry,
            base_warnings=base_warnings,
            candidate_warnings=cand_warnings,
        ))

    added_ids = tuple(
        qid for qid in cand_by_id.keys()
        if qid not in base_by_id and qid not in consumed_cand_by_id
    )
    removed_ids = tuple(
        qid for qid in base_by_id.keys()
        if qid not in cand_by_id and qid not in matched_ids
    )

    return ComparisonReport(
        base_scope=base_scope,
        candidate_scope=cand_scope,
        base_query_count=len(base_results),
        candidate_query_count=len(cand_results),
        base_warning_count=len(base_warnings),
        candidate_warning_count=len(cand_warnings),
        matched_ids=tuple(matched_ids),
        added_ids=added_ids,
        removed_ids=removed_ids,
        comparisons=tuple(comparisons),
        warnings=tuple(warnings),
    )


def _scope_of(report: Mapping[str, Any]) -> dict[str, Any]:
    scope = report.get("scope") if isinstance(report, Mapping) else {}
    return dict(scope) if isinstance(scope, Mapping) else {}


def _results_of(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = report.get("results") if isinstance(report, Mapping) else ()
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, Mapping)]


def _warnings_of(report: Mapping[str, Any]) -> list[str]:
    raw = report.get("warnings") if isinstance(report, Mapping) else ()
    if not isinstance(raw, list):
        return []
    return [str(w) for w in raw]


def _partition_by_id(
    results: list[Mapping[str, Any]],
    warnings: list[str],
    *,
    label: str,
) -> tuple[dict[str, Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split results into ``(by_id, no_id_list)``.

    Entries with a non-empty ``query_id`` go into the by-id map
    (last-write-wins on duplicates). Entries without one go into
    a positional list — they're matched later via question-text
    fallback. A warning is emitted per entry without an id so
    the operator sees how many fell back to that path.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    no_id: list[Mapping[str, Any]] = []
    for entry in results:
        qid = _id_of(entry)
        if qid:
            by_id[qid] = entry
        else:
            no_id.append(entry)
            warnings.append(
                f"{label} report has a result with no id; "
                "will match via question-text fallback"
            )
    return by_id, no_id


def _find_by_question_in_no_id(
    base_entry: Mapping[str, Any],
    candidate_no_id: list[Mapping[str, Any]],
    consumed: set[int],
) -> int | None:
    """Locate a candidate entry in the no-id partition with the
    same (lowercased, stripped) question text as ``base_entry``.
    Returns the index for consumption-tracking, or ``None`` when
    no match exists / every match is already consumed."""
    question = _question_of(base_entry).strip().lower()
    if not question:
        return None
    for idx, entry in enumerate(candidate_no_id):
        if idx in consumed:
            continue
        if _question_of(entry).strip().lower() == question:
            return idx
    return None


def _find_match_for_no_id(
    base_entry: Mapping[str, Any],
    cand_by_id: dict[str, Mapping[str, Any]],
    cand_no_id: list[Mapping[str, Any]],
    *,
    consumed_by_id: set[str],
    consumed_no_id: set[int],
) -> tuple[Mapping[str, Any] | None, str | None, int | None]:
    """Match a no-id base entry against the candidate side.

    Preference order:

      1. Candidate by-id entry with the same question text — the
         match carries a real id we can use as the report key.
      2. Candidate no-id entry with the same question text — the
         match key is synthesised.

    Returns ``(entry, by_id_key, no_id_index)``. Exactly one of
    the latter two is non-None when a match exists; both are
    None on miss."""
    question = _question_of(base_entry).strip().lower()
    if not question:
        return None, None, None
    for qid, entry in cand_by_id.items():
        if qid in consumed_by_id:
            continue
        if _question_of(entry).strip().lower() == question:
            return entry, qid, None
    for idx, entry in enumerate(cand_no_id):
        if idx in consumed_no_id:
            continue
        if _question_of(entry).strip().lower() == question:
            return entry, None, idx
    return None, None, None


def _synthetic_match_id(entry: Mapping[str, Any]) -> str:
    """Build a stable identifier for a no-id ↔ no-id match so the
    report's match-id list / diagnostics aren't anonymous."""
    question = _question_of(entry).strip()
    if not question:
        return "(unidentified)"
    preview = question if len(question) <= 32 else (question[:29] + "...")
    return f"(no-id) {preview}"


def _id_of(
    entry: Mapping[str, Any], *, fallback: str = "",
) -> str:
    val = entry.get("query_id")
    if isinstance(val, str) and val.strip():
        return val
    return fallback


def _question_of(entry: Mapping[str, Any]) -> str:
    val = entry.get("question")
    return str(val) if isinstance(val, str) else ""


def _diff_one(
    *,
    query_id: str,
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    base_warnings: list[str],
    candidate_warnings: list[str],
) -> QueryComparison:
    base_block = _block(base, "alias_broadening")
    cand_block = _block(candidate, "alias_broadening")
    base_baseline = _block(base, "baseline")
    cand_baseline = _block(candidate, "baseline")

    base_retrieved = _coerce_int(base_block.get("retrieved_count"))
    cand_retrieved = _coerce_int(cand_block.get("retrieved_count"))
    base_evidence = _coerce_int(base_block.get("evidence_count"))
    cand_evidence = _coerce_int(cand_block.get("evidence_count"))

    retrieved_delta = _subtract(cand_retrieved, base_retrieved)
    evidence_delta = _subtract(cand_evidence, base_evidence)

    base_diag = _block(base_block, "diagnostics")
    cand_diag = _block(cand_block, "diagnostics")
    base_applied = _coerce_int(
        base_diag.get("enrichment_alias_pairs_applied"),
    ) or 0
    cand_applied = _coerce_int(
        cand_diag.get("enrichment_alias_pairs_applied"),
    ) or 0
    applied_delta = cand_applied - base_applied

    base_has_warning = _query_in_warnings(query_id, base_warnings)
    cand_has_warning = _query_in_warnings(query_id, candidate_warnings)
    if base_has_warning == cand_has_warning:
        warning_status = "unchanged"
    elif cand_has_warning and not base_has_warning:
        warning_status = "gained"
    else:
        warning_status = "cleared"

    flags: list[str] = []
    if retrieved_delta is not None and retrieved_delta > 0:
        flags.append(ChangeFlag.RETRIEVED_COUNT_INCREASED)
    elif retrieved_delta is not None and retrieved_delta < 0:
        flags.append(ChangeFlag.RETRIEVED_COUNT_DECREASED)
    if evidence_delta is not None and evidence_delta > 0:
        flags.append(ChangeFlag.EVIDENCE_COUNT_INCREASED)
    elif evidence_delta is not None and evidence_delta < 0:
        flags.append(ChangeFlag.EVIDENCE_COUNT_DECREASED)
    if warning_status == "gained":
        flags.append(ChangeFlag.WARNINGS_GAINED)
    elif warning_status == "cleared":
        flags.append(ChangeFlag.WARNINGS_CLEARED)
    if applied_delta > 0 and base_applied == 0:
        flags.append(ChangeFlag.ENRICHMENT_APPLIED_GAINED)
    elif applied_delta < 0 and cand_applied == 0:
        flags.append(ChangeFlag.ENRICHMENT_APPLIED_LOST)
    # Missing-count transitions: both retrieved-count fields,
    # both modes' baseline + variant. Use the variant retrieved
    # count as the canonical signal since broadening is the
    # comparator's focus.
    if base_retrieved is not None and cand_retrieved is None:
        flags.append(ChangeFlag.COUNTS_NOW_MISSING)
    elif base_retrieved is None and cand_retrieved is not None:
        flags.append(ChangeFlag.COUNTS_NOW_PRESENT)

    return QueryComparison(
        query_id=query_id,
        question=_question_of(candidate) or _question_of(base),
        base_retrieved=base_retrieved,
        candidate_retrieved=cand_retrieved,
        base_evidence=base_evidence,
        candidate_evidence=cand_evidence,
        retrieved_delta=retrieved_delta,
        evidence_delta=evidence_delta,
        enrichment_applied_delta=applied_delta,
        warning_status_change=warning_status,
        change_flags=tuple(flags),
    )


def _block(entry: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = entry.get(key) if isinstance(entry, Mapping) else None
    return val if isinstance(val, Mapping) else {}


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


def _query_in_warnings(query_id: str, warnings: list[str]) -> bool:
    if not query_id:
        return False
    needle = repr(query_id)
    return any(needle in str(w) for w in warnings)


# ---- Formatters --------------------------------------------------


def format_comparison(report: ComparisonReport) -> str:
    """Plain-text comparison rendering. Sectioned, deterministic."""
    lines: list[str] = []
    lines.append("Retrieval-broadening report comparison")
    lines.append("=" * 38)
    lines.append("")
    lines.append("Scope:")
    lines.append(f"  base:      {_fmt_scope(report.base_scope)}")
    lines.append(f"  candidate: {_fmt_scope(report.candidate_scope)}")
    lines.append("")
    lines.append("Counts:")
    lines.append(
        f"  queries — base: {report.base_query_count} | "
        f"candidate: {report.candidate_query_count}"
    )
    lines.append(
        f"  warnings — base: {report.base_warning_count} | "
        f"candidate: {report.candidate_warning_count}"
    )
    lines.append(f"  matched ids: {len(report.matched_ids)}")
    lines.append(f"  added ids:   {len(report.added_ids)}")
    lines.append(f"  removed ids: {len(report.removed_ids)}")
    lines.append("")
    if report.added_ids:
        lines.append("Added query ids:")
        for qid in report.added_ids:
            lines.append(f"  + {qid}")
        lines.append("")
    if report.removed_ids:
        lines.append("Removed query ids:")
        for qid in report.removed_ids:
            lines.append(f"  - {qid}")
        lines.append("")

    regressions = report.regressions
    improvements = report.improvements
    lines.append(f"Top regressions ({len(regressions)}):")
    if not regressions:
        lines.append("  (none)")
    else:
        for case in regressions[:_TOP_LIST_CAP]:
            lines.append(_fmt_case(case))
        overflow = len(regressions) - _TOP_LIST_CAP
        if overflow > 0:
            lines.append(f"  ...and {overflow} more")
    lines.append("")
    lines.append(f"Top improvements ({len(improvements)}):")
    if not improvements:
        lines.append("  (none)")
    else:
        for case in improvements[:_TOP_LIST_CAP]:
            lines.append(_fmt_case(case))
        overflow = len(improvements) - _TOP_LIST_CAP
        if overflow > 0:
            lines.append(f"  ...and {overflow} more")
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in report.warnings:
            lines.append(f"  ! {warning}")
    return "\n".join(lines) + "\n"


def render_comparison_json(report: ComparisonReport) -> str:
    """Compact deterministic JSON for CI consumers."""
    payload = {
        "base_scope": report.base_scope,
        "candidate_scope": report.candidate_scope,
        "counts": {
            "base_query_count": report.base_query_count,
            "candidate_query_count": report.candidate_query_count,
            "base_warning_count": report.base_warning_count,
            "candidate_warning_count": report.candidate_warning_count,
            "matched": len(report.matched_ids),
            "added": len(report.added_ids),
            "removed": len(report.removed_ids),
        },
        "added_ids": list(report.added_ids),
        "removed_ids": list(report.removed_ids),
        "regressions": [
            _comparison_to_dict(c) for c in report.regressions
        ],
        "improvements": [
            _comparison_to_dict(c) for c in report.improvements
        ],
        "warnings": list(report.warnings),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _comparison_to_dict(c: QueryComparison) -> dict[str, Any]:
    return {
        "query_id": c.query_id,
        "question": c.question,
        "base": {
            "retrieved_count": c.base_retrieved,
            "evidence_count": c.base_evidence,
        },
        "candidate": {
            "retrieved_count": c.candidate_retrieved,
            "evidence_count": c.candidate_evidence,
        },
        "delta": {
            "retrieved_count": c.retrieved_delta,
            "evidence_count": c.evidence_delta,
            "enrichment_applied": c.enrichment_applied_delta,
        },
        "warning_status_change": c.warning_status_change,
        "change_flags": list(c.change_flags),
    }


def _fmt_scope(scope: Mapping[str, Any]) -> str:
    if not scope:
        return "(none)"
    parts = []
    for key in ("tenant_id", "project_id", "document_id", "snapshot_id"):
        value = scope.get(key)
        if value:
            parts.append(f"{key}={value}")
    return ", ".join(parts) or "(none)"


def _fmt_case(case: QueryComparison) -> str:
    flags = ", ".join(case.change_flags) or "none"
    qid = case.query_id or "(no id)"
    retrieved_delta = (
        case.retrieved_delta
        if case.retrieved_delta is not None else "—"
    )
    return f"  - {qid} | retrieved Δ={retrieved_delta} | flags: {flags}"


# ---- CLI ----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.compare_retrieval_broadening_reports",
        description=(
            "Compare two A/B reports produced by "
            "``j1.tools.evaluate_retrieval_broadening`` and print "
            "regressions + improvements + added/removed query ids."
        ),
    )
    parser.add_argument(
        "--base", required=True, type=Path,
        help="Path to the baseline report JSON.",
    )
    parser.add_argument(
        "--candidate", required=True, type=Path,
        help="Path to the new / candidate report JSON.",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format. Default is plain text.",
    )
    parser.add_argument(
        "--output", default=None, type=Path,
        help="Write the rendered comparison here; default stdout.",
    )
    return parser.parse_args(argv)


def _read_report(path: Path) -> dict | int:
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
    if not isinstance(payload, dict):
        sys.stderr.write(
            f"error: {path} top-level JSON is not an object\n",
        )
        return 2
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    base = _read_report(args.base)
    if isinstance(base, int):
        return base
    candidate = _read_report(args.candidate)
    if isinstance(candidate, int):
        return candidate
    report = compare_reports(base, candidate)
    text = (
        render_comparison_json(report)
        if args.format == "json"
        else format_comparison(report)
    )
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
