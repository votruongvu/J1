"""Phase 6 — Memory-aware query A/B evaluation harness.

Runs a fixed query set against the same target scope twice — once
with the Knowledge Memory query path disabled (baseline) and once
with it enabled (memory-aware variant) — and emits structured
JSON + Markdown reports comparing answer shape, evidence/citation
counts, memory diagnostics, and latency.

Scope (hard contract):

* **Read-only.** The harness never mutates artifacts, promotes
  snapshots, or writes audit rows. The runner the harness calls
  goes through the existing validation surface; production gates
  (snapshot scope, document filtering) remain in force.
* **No default flag flips.** The harness toggles
  ``J1_QUERY_KNOWLEDGE_MEMORY_ENABLED`` /
  ``J1_QUERY_EXPANSION_ENABLED`` per-query via context managers
  that restore prior state — even on exception. Defaults stay
  default-off.
* **No new LLM calls.** Each query runs once per mode; the
  underlying validation service's normal answer synthesis path
  is the only LLM call exercised.
* **Quality verdicts are data, not test failures.** ``main()``
  returns 0 even when the comparison shows mixed quality so CI
  can collect reports across many runs without flaking.

The harness is structured as a library + thin CLI:

* :class:`MemoryQueryEvaluator` does the comparison work.
* ``load_memory_query_fixture(path)`` parses YAML / JSON fixtures.
* ``write_markdown_report(report, path)`` renders the comparison
  for humans.
* ``compute_recommendation(report)`` collapses the report into
  one of the pinned recommendation strings.
* ``main(argv)`` provides the production CLI; tests inject a stub
  runner directly into the evaluator.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


_log = logging.getLogger("j1.tools.evaluate_memory_query")


__all__ = [
    "MemoryQueryEvalQuery",
    "MemoryQueryEvalOutcome",
    "MemoryQueryEvalResult",
    "MemoryQueryEvalReport",
    "MemoryQueryEvaluator",
    "RunnerCallable",
    "RECOMMENDATION_VALUES",
    "compute_recommendation",
    "load_memory_query_fixture",
    "render_markdown_report",
    "main",
]


# Recommendation vocabulary — pinned strings. The CLI + the
# Markdown writer emit these verbatim so downstream dashboards can
# pattern-match without parsing prose.
RECOMMENDATION_KEEP_DISABLED = "keep_disabled"
RECOMMENDATION_ENABLE_DEV_ONLY = "enable_in_dev_only"
RECOMMENDATION_ENABLE_PREVIEW = "enable_in_preview"
RECOMMENDATION_ENABLE_DOCUMENT_SCOPE = (
    "enable_by_default_for_document_scope"
)
RECOMMENDATION_ENABLE_PROJECT_SCOPE = (
    "enable_by_default_for_project_scope"
)
RECOMMENDATION_NEEDS_MORE_DATA = "needs_more_data"

RECOMMENDATION_VALUES: tuple[str, ...] = (
    RECOMMENDATION_KEEP_DISABLED,
    RECOMMENDATION_ENABLE_DEV_ONLY,
    RECOMMENDATION_ENABLE_PREVIEW,
    RECOMMENDATION_ENABLE_DOCUMENT_SCOPE,
    RECOMMENDATION_ENABLE_PROJECT_SCOPE,
    RECOMMENDATION_NEEDS_MORE_DATA,
)


# Env keys we toggle per-query. Imported lazily so this module
# stays free of orchestrator-internal imports for the tests that
# only exercise pure logic.
ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED = "J1_QUERY_KNOWLEDGE_MEMORY_ENABLED"
ENV_QUERY_EXPANSION_ENABLED = "J1_QUERY_EXPANSION_ENABLED"


# ---- Fixture types -----------------------------------------------


@dataclass(frozen=True)
class MemoryQueryEvalQuery:
    """One fixture entry. ``scope`` is the operator-facing scope
    name — passed through to the runner verbatim, which decides
    how to map it to the validation surface's API."""

    id: str
    question: str
    scope: str = "project_active"
    document_id: str | None = None
    expected_terms: tuple[str, ...] = ()
    expected_artifact_types: tuple[str, ...] = ()
    category: str | None = None
    notes: str = ""

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, index: int,
    ) -> "MemoryQueryEvalQuery":
        question = payload.get("question") or ""
        if not question:
            raise ValueError(
                f"fixture entry {index} missing 'question'"
            )
        return cls(
            id=str(payload.get("id") or f"q{index}"),
            question=str(question),
            scope=str(payload.get("scope") or "project_active"),
            document_id=_str_or_none(payload.get("document_id")),
            expected_terms=_tuple_of_str(payload.get("expected_terms")),
            expected_artifact_types=_tuple_of_str(
                payload.get("expected_artifact_types"),
            ),
            category=_str_or_none(payload.get("category")),
            notes=str(payload.get("notes") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "scope": self.scope,
            "document_id": self.document_id,
            "expected_terms": list(self.expected_terms),
            "expected_artifact_types": list(self.expected_artifact_types),
            "category": self.category,
            "notes": self.notes,
        }


# Runner signature: takes a query + memory-enabled flag, returns a
# response Mapping with the wire-shape the harness reads. Test
# stubs return hand-built dicts; the production runner wraps
# ``IngestionValidationService.run_document_test_query`` /
# ``run_project_query`` and projects the typed DTO into a Mapping.
RunnerCallable = Callable[
    [MemoryQueryEvalQuery, bool], Mapping[str, Any],
]


# ---- Outcome / result / report ----------------------------------


@dataclass
class MemoryQueryEvalOutcome:
    """One-mode capture for one query."""

    answer: str
    answer_present: bool
    citation_count: int | None
    retrieved_count: int | None
    evidence_count: int | None
    duration_ms: int | None
    # Knowledge Memory diagnostics — verbatim from
    # ``debug.orchestrator_trace.knowledge_memory``. ``None`` when
    # the trace block is absent (legacy / disabled-feature mode).
    knowledge_memory: dict[str, Any] | None
    # Quality-proxy fields derived from ``expected_terms`` /
    # ``expected_artifact_types`` on the fixture entry.
    expected_terms_present: tuple[str, ...] = ()
    expected_terms_missing: tuple[str, ...] = ()
    expected_artifact_types_present: tuple[str, ...] = ()
    expected_artifact_types_missing: tuple[str, ...] = ()
    # The cited memory entries — a list of memory-id strings the
    # answer's evidence pool attributes to memory-guided source
    # refs. Should be EMPTY by Phase 5B contract (memory entries
    # are never cited directly; only their resolved source refs
    # appear as evidence with ``evidence_origin=memory_guided_source_ref``).
    cited_memory_entries: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "answer_present": self.answer_present,
            "citation_count": self.citation_count,
            "retrieved_count": self.retrieved_count,
            "evidence_count": self.evidence_count,
            "duration_ms": self.duration_ms,
            "knowledge_memory": (
                dict(self.knowledge_memory)
                if self.knowledge_memory is not None else None
            ),
            "expected_terms_present": list(self.expected_terms_present),
            "expected_terms_missing": list(self.expected_terms_missing),
            "expected_artifact_types_present": list(
                self.expected_artifact_types_present,
            ),
            "expected_artifact_types_missing": list(
                self.expected_artifact_types_missing,
            ),
            "cited_memory_entries": list(self.cited_memory_entries),
        }


@dataclass
class MemoryQueryEvalResult:
    """Per-query baseline + variant + diff."""

    query: MemoryQueryEvalQuery
    baseline: MemoryQueryEvalOutcome
    memory_aware: MemoryQueryEvalOutcome
    delta: dict[str, Any]
    verdict: str
    safety_violations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "baseline": self.baseline.to_dict(),
            "memory_aware": self.memory_aware.to_dict(),
            "delta": dict(self.delta),
            "verdict": self.verdict,
            "safety_violations": list(self.safety_violations),
        }


# Verdict vocabulary — keyed off the delta + safety checks.
VERDICT_IMPROVED = "improved"
VERDICT_UNCHANGED = "unchanged"
VERDICT_WORSENED = "worsened"
VERDICT_SAFETY_VIOLATION = "safety_violation"


@dataclass
class MemoryQueryEvalReport:
    """Whole-run report: per-query results + aggregate summary +
    a final recommendation string."""

    generated_at: str
    scope: dict[str, Any]
    config: dict[str, Any]
    summary: dict[str, Any]
    results: list[MemoryQueryEvalResult]
    recommendation: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "scope": dict(self.scope),
            "config": dict(self.config),
            "summary": dict(self.summary),
            "results": [r.to_dict() for r in self.results],
            "recommendation": self.recommendation,
            "warnings": list(self.warnings),
        }


# ---- Evaluator ---------------------------------------------------


class MemoryQueryEvaluator:
    """A/B harness orchestrator.

    Construction takes a ``runner`` callable + a scope dict the
    report carries verbatim. The harness toggles the memory + query
    expansion env flags around each runner call; if the runner has
    captured the flag values at construction time, the runner is
    responsible for re-reading per-call (which the validation
    service does via ``load_knowledge_memory_query_settings``).

    ``evaluate`` runs every query twice, captures diagnostics, and
    returns a fully-populated :class:`MemoryQueryEvalReport`."""

    def __init__(
        self,
        *,
        runner: RunnerCallable,
        scope: Mapping[str, Any] | None = None,
        now: Callable[[], datetime] | None = None,
        strict: bool = False,
    ) -> None:
        self._runner = runner
        self._scope = dict(scope or {})
        self._now = now or (
            lambda: datetime.now(timezone.utc)
        )
        # ``strict`` causes safety violations to surface as test
        # failures via ``main()`` — non-zero exit. Default is
        # off so report generation never breaks CI.
        self._strict = bool(strict)

    def evaluate(
        self, queries: Iterable[MemoryQueryEvalQuery],
    ) -> MemoryQueryEvalReport:
        results: list[MemoryQueryEvalResult] = []
        warnings: list[str] = []
        for query in queries:
            try:
                baseline = self._run_one(
                    query, memory_enabled=False,
                    warnings=warnings,
                )
                memory_aware = self._run_one(
                    query, memory_enabled=True,
                    warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001 — never abort the batch
                warnings.append(
                    f"query {query.id!r} raised: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            delta = _compute_delta(baseline, memory_aware)
            violations = _safety_violations(memory_aware, delta)
            verdict = _classify_verdict(
                query=query,
                baseline=baseline,
                memory_aware=memory_aware,
                delta=delta,
                safety_violations=violations,
            )
            results.append(MemoryQueryEvalResult(
                query=query,
                baseline=baseline,
                memory_aware=memory_aware,
                delta=delta,
                verdict=verdict,
                safety_violations=tuple(violations),
            ))
        summary = _summarize(results)
        recommendation = compute_recommendation_for_summary(summary, results)
        return MemoryQueryEvalReport(
            generated_at=self._now().isoformat(),
            scope=self._scope,
            config={
                "baseline": {
                    "knowledge_memory_enabled": False,
                    "query_expansion_enabled": False,
                },
                "memory_aware": {
                    "knowledge_memory_enabled": True,
                    "query_expansion_enabled": True,
                },
                "strict": self._strict,
            },
            summary=summary,
            results=results,
            recommendation=recommendation,
            warnings=warnings,
        )

    def _run_one(
        self,
        query: MemoryQueryEvalQuery,
        *,
        memory_enabled: bool,
        warnings: list[str],
    ) -> MemoryQueryEvalOutcome:
        with _memory_env(memory_enabled):
            start = time.perf_counter()
            try:
                response = self._runner(query, memory_enabled)
            except Exception:  # noqa: BLE001 — propagate to caller
                # Caller's broad try/except in ``evaluate`` records
                # the warning + skips the query. We re-raise to let
                # that path own the warning shape.
                raise
            elapsed_ms = int(
                (time.perf_counter() - start) * 1000,
            )
        return _capture_outcome(
            response,
            elapsed_ms=elapsed_ms,
            query=query,
            warnings=warnings,
            mode=("memory_aware" if memory_enabled else "baseline"),
        )


# ---- Outcome capture --------------------------------------------


def _capture_outcome(
    response: Mapping[str, Any],
    *,
    elapsed_ms: int,
    query: MemoryQueryEvalQuery,
    warnings: list[str],
    mode: str,
) -> MemoryQueryEvalOutcome:
    """Project the runner's Mapping response into a typed outcome.

    The runner's Mapping mirrors ``ManualTestQueryResponseDTO``'s
    snake_case wire shape; missing fields surface as ``None`` plus
    a warning. The harness never raises on a shape mismatch — the
    spec is "exit non-zero only for harness errors, not because
    quality is mixed"."""
    answer = str(response.get("answer") or "")
    citations = response.get("citations")
    if isinstance(citations, list):
        citation_count: int | None = len(citations)
    elif citations is None:
        citation_count = None
        warnings.append(
            f"query {query.id!r} ({mode}): missing citations"
        )
    else:
        citation_count = None

    retrieved = response.get("retrieved_chunks")
    if isinstance(retrieved, list):
        retrieved_count: int | None = len(retrieved)
    elif retrieved is None:
        retrieved_count = None
        warnings.append(
            f"query {query.id!r} ({mode}): missing retrieved_chunks"
        )
    else:
        retrieved_count = None

    evidence = response.get("evidence_sent_to_llm")
    if isinstance(evidence, list):
        evidence_count: int | None = len(evidence)
    else:
        evidence_count = None

    debug = response.get("debug") or {}
    if not isinstance(debug, Mapping):
        debug = {}
    knowledge_memory = _knowledge_memory_from_debug(debug)
    if mode == "memory_aware" and knowledge_memory is None:
        warnings.append(
            f"query {query.id!r} ({mode}): no knowledge_memory "
            "trace block in response (memory provider may not be "
            "wired)"
        )

    # Quality-proxy projections — expected_terms / artifact_types.
    answer_lower = answer.lower()
    expected_terms_present = tuple(
        t for t in query.expected_terms
        if t and t.lower() in answer_lower
    )
    expected_terms_missing = tuple(
        t for t in query.expected_terms
        if t and t.lower() not in answer_lower
    )
    retrieved_kinds = _retrieved_artifact_kinds(retrieved)
    expected_artifact_types_present = tuple(
        kind for kind in query.expected_artifact_types
        if kind and kind in retrieved_kinds
    )
    expected_artifact_types_missing = tuple(
        kind for kind in query.expected_artifact_types
        if kind and kind not in retrieved_kinds
    )

    cited_memory_entries = _cited_memory_entries_from_response(
        response,
    )

    return MemoryQueryEvalOutcome(
        answer=answer,
        answer_present=bool(answer.strip()),
        citation_count=citation_count,
        retrieved_count=retrieved_count,
        evidence_count=evidence_count,
        duration_ms=elapsed_ms,
        knowledge_memory=knowledge_memory,
        expected_terms_present=expected_terms_present,
        expected_terms_missing=expected_terms_missing,
        expected_artifact_types_present=expected_artifact_types_present,
        expected_artifact_types_missing=expected_artifact_types_missing,
        cited_memory_entries=cited_memory_entries,
    )


def _knowledge_memory_from_debug(
    debug: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Extract ``debug.orchestrator_trace.knowledge_memory``.

    Mirrors the FE's ``knowledgeMemoryTraceFrom`` helper —
    permissive on missing / malformed shapes."""
    trace = debug.get("orchestrator_trace")
    if not isinstance(trace, Mapping):
        return None
    km = trace.get("knowledge_memory")
    if not isinstance(km, Mapping):
        return None
    return dict(km)


def _retrieved_artifact_kinds(
    retrieved: Any,
) -> set[str]:
    """Pull the ``artifact_kind`` set across retrieved chunks. Used
    to evaluate ``expected_artifact_types`` against the actual
    retrieval pool."""
    if not isinstance(retrieved, list):
        return set()
    out: set[str] = set()
    for entry in retrieved:
        if isinstance(entry, Mapping):
            kind = entry.get("artifact_kind") or entry.get("artifactKind")
        else:
            kind = getattr(entry, "artifact_kind", None) or getattr(
                entry, "artifactKind", None,
            )
        if kind:
            out.add(str(kind))
    return out


def _cited_memory_entries_from_response(
    response: Mapping[str, Any],
) -> tuple[str, ...]:
    """Find any citation that points at a memory entry directly
    (Phase 5B contract violation).

    A citation is "memory-direct" when its ``artifact_kind`` starts
    with ``knowledge_memory``. The Phase 5B resolver emits
    candidates routed as ``ARTIFACT_LOOKUP`` with the SOURCE
    artifact's kind, never the memory artifact's; so a citation
    with ``artifact_kind=knowledge_memory.*`` indicates a bug or a
    bypass.
    """
    citations = response.get("citations")
    if not isinstance(citations, list):
        return ()
    out: list[str] = []
    for c in citations:
        if not isinstance(c, Mapping):
            continue
        kind = str(
            c.get("artifact_type") or c.get("artifactType")
            or c.get("artifact_kind") or "",
        )
        if kind.startswith("knowledge_memory"):
            out.append(str(c.get("artifact_id") or c.get("artifactId") or ""))
    return tuple(out)


# ---- Delta + safety + verdict -----------------------------------


def _subtract(a: Any, b: Any) -> int | None:
    if a is None or b is None:
        return None
    return int(a) - int(b)


def _compute_delta(
    baseline: MemoryQueryEvalOutcome,
    memory: MemoryQueryEvalOutcome,
) -> dict[str, Any]:
    """Per-query baseline → memory diff."""
    return {
        "citation_count": _subtract(
            memory.citation_count, baseline.citation_count,
        ),
        "retrieved_count": _subtract(
            memory.retrieved_count, baseline.retrieved_count,
        ),
        "evidence_count": _subtract(
            memory.evidence_count, baseline.evidence_count,
        ),
        "duration_ms": _subtract(
            memory.duration_ms, baseline.duration_ms,
        ),
        "expected_terms_gained": list(
            set(memory.expected_terms_present)
            - set(baseline.expected_terms_present)
        ),
        "expected_terms_lost": list(
            set(baseline.expected_terms_present)
            - set(memory.expected_terms_present)
        ),
        "expected_artifact_types_gained": list(
            set(memory.expected_artifact_types_present)
            - set(baseline.expected_artifact_types_present)
        ),
        "expected_artifact_types_lost": list(
            set(baseline.expected_artifact_types_present)
            - set(memory.expected_artifact_types_present)
        ),
        # Memory-only diagnostics — populated only on memory_aware
        # outcomes; pass-through here for the report row.
        "memory_status": (
            memory.knowledge_memory.get("status")
            if memory.knowledge_memory else None
        ),
        "memory_injected_evidence_count": (
            memory.knowledge_memory.get("injected_evidence_count")
            if memory.knowledge_memory else None
        ),
        "memory_applied_expansion_terms": (
            list(
                memory.knowledge_memory.get("applied_expansion_terms")
                or [],
            )
            if memory.knowledge_memory else []
        ),
    }


# Safety violation codes — stable wire strings.
SAFETY_DIRECT_MEMORY_CITATION = "direct_memory_citation"
SAFETY_FEWER_CITATIONS_FOR_DOMAIN_QUERY = (
    "fewer_citations_for_domain_query"
)
SAFETY_STALE_SOURCE_EVIDENCE = "stale_source_evidence"
SAFETY_LATENCY_REGRESSION = "latency_regression"
SAFETY_MEMORY_PROVIDER_FAILURE = "memory_provider_failure"


_DOMAIN_INDICATOR_CATEGORIES = frozenset({
    "risk", "requirement", "validation_check", "inspection_finding",
    "ncr", "rfi", "action_item", "boq", "table",
})


# Latency regression threshold — Phase 6 spec's "no more than
# 20-30% average latency increase unless quality lift is clear".
# We pin 30% per-query as the safety threshold; the recommendation
# layer separately considers the aggregate.
_LATENCY_REGRESSION_RATIO = 0.30


def _safety_violations(
    memory: MemoryQueryEvalOutcome,
    delta: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    # 1. Direct memory citations — Phase 5B contract violation.
    if memory.cited_memory_entries:
        out.append(SAFETY_DIRECT_MEMORY_CITATION)
    # 2. Memory provider failed (status=failed).
    km = memory.knowledge_memory or {}
    if str(km.get("status") or "") == "failed":
        out.append(SAFETY_MEMORY_PROVIDER_FAILURE)
    # 3. Latency regression — per-query 30% threshold on absolute
    # duration. Requires both timings present and a meaningful
    # baseline duration (>0).
    if (
        memory.duration_ms is not None
        and delta.get("duration_ms") is not None
    ):
        baseline_ms = memory.duration_ms - delta["duration_ms"]
        if baseline_ms > 0 and (
            delta["duration_ms"] / baseline_ms
        ) > _LATENCY_REGRESSION_RATIO:
            out.append(SAFETY_LATENCY_REGRESSION)
    return out


def _classify_verdict(
    *,
    query: MemoryQueryEvalQuery,
    baseline: MemoryQueryEvalOutcome,
    memory_aware: MemoryQueryEvalOutcome,
    delta: dict[str, Any],
    safety_violations: list[str],
) -> str:
    if safety_violations:
        return VERDICT_SAFETY_VIOLATION
    citation_delta = delta.get("citation_count")
    gained = delta.get("expected_terms_gained") or []
    lost = delta.get("expected_terms_lost") or []
    types_gained = delta.get("expected_artifact_types_gained") or []
    types_lost = delta.get("expected_artifact_types_lost") or []
    # Domain-relevant: fewer citations is worsening.
    if (
        query.category in _DOMAIN_INDICATOR_CATEGORIES
        and isinstance(citation_delta, int)
        and citation_delta < 0
    ):
        return VERDICT_WORSENED
    if lost or types_lost:
        return VERDICT_WORSENED
    if gained or types_gained:
        return VERDICT_IMPROVED
    if isinstance(citation_delta, int) and citation_delta > 0:
        return VERDICT_IMPROVED
    if isinstance(citation_delta, int) and citation_delta < 0:
        return VERDICT_WORSENED
    return VERDICT_UNCHANGED


# ---- Summary ----------------------------------------------------


def _summarize(
    results: list[MemoryQueryEvalResult],
) -> dict[str, Any]:
    total = len(results)
    if not total:
        return _empty_summary()
    improved = sum(
        1 for r in results if r.verdict == VERDICT_IMPROVED
    )
    unchanged = sum(
        1 for r in results if r.verdict == VERDICT_UNCHANGED
    )
    worsened = sum(
        1 for r in results if r.verdict == VERDICT_WORSENED
    )
    safety = sum(
        1 for r in results if r.verdict == VERDICT_SAFETY_VIOLATION
    )
    memory_used = sum(
        1 for r in results
        if r.memory_aware.knowledge_memory
        and r.memory_aware.knowledge_memory.get("status") == "used"
    )
    memory_unavailable = sum(
        1 for r in results
        if r.memory_aware.knowledge_memory
        and r.memory_aware.knowledge_memory.get("status")
        in ("not_available", "loaded_no_match", "disabled")
    )
    avg_baseline_latency = _avg(
        r.baseline.duration_ms for r in results
    )
    avg_memory_latency = _avg(
        r.memory_aware.duration_ms for r in results
    )
    avg_baseline_citations = _avg(
        r.baseline.citation_count for r in results
    )
    avg_memory_citations = _avg(
        r.memory_aware.citation_count for r in results
    )
    avg_injected_evidence = _avg(
        (r.memory_aware.knowledge_memory or {}).get(
            "injected_evidence_count",
        )
        for r in results
    )
    warnings_frequency = _warnings_frequency(results)
    safety_violations_frequency = _violations_frequency(results)
    return {
        "total_queries": total,
        "queries_improved": improved,
        "queries_unchanged": unchanged,
        "queries_worsened": worsened,
        "queries_with_safety_violation": safety,
        "queries_with_memory_used": memory_used,
        "queries_with_memory_unavailable": memory_unavailable,
        "avg_baseline_latency_ms": avg_baseline_latency,
        "avg_memory_latency_ms": avg_memory_latency,
        "avg_baseline_citation_count": avg_baseline_citations,
        "avg_memory_citation_count": avg_memory_citations,
        "avg_injected_evidence_count": avg_injected_evidence,
        "memory_warnings_frequency": warnings_frequency,
        "safety_violations_frequency": safety_violations_frequency,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "total_queries": 0,
        "queries_improved": 0,
        "queries_unchanged": 0,
        "queries_worsened": 0,
        "queries_with_safety_violation": 0,
        "queries_with_memory_used": 0,
        "queries_with_memory_unavailable": 0,
        "avg_baseline_latency_ms": None,
        "avg_memory_latency_ms": None,
        "avg_baseline_citation_count": None,
        "avg_memory_citation_count": None,
        "avg_injected_evidence_count": None,
        "memory_warnings_frequency": {},
        "safety_violations_frequency": {},
    }


def _avg(values: Iterable[Any]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _warnings_frequency(
    results: list[MemoryQueryEvalResult],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        km = r.memory_aware.knowledge_memory or {}
        all_warnings = list(km.get("warnings") or [])
        all_warnings.extend(
            list(km.get("source_ref_resolution_warnings") or []),
        )
        for w in all_warnings:
            out[w] = out.get(w, 0) + 1
    return dict(sorted(out.items()))


def _violations_frequency(
    results: list[MemoryQueryEvalResult],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        for v in r.safety_violations:
            out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# ---- Recommendation engine --------------------------------------


def compute_recommendation_for_summary(
    summary: Mapping[str, Any],
    results: list[MemoryQueryEvalResult],
) -> str:
    """Collapse the summary + per-query results into one of the
    pinned recommendation strings.

    Decision logic (kept simple + explainable):

      * No queries → ``needs_more_data``.
      * Any safety violation → ``keep_disabled``.
      * No queries used memory at all → ``needs_more_data``.
      * >25% queries worsened → ``keep_disabled``.
      * <25% queries used memory → ``needs_more_data``.
      * Latency regression > 30% on aggregate → ``enable_in_dev_only``.
      * Mix of improved + unchanged, no worsened, no safety
        violations → recommend based on dominant scope:
        - all results scoped ``project_active`` → enable for project
        - all results scoped ``document_active`` → enable for document
        - mixed → ``enable_in_preview``.
      * Otherwise → ``enable_in_dev_only``.
    """
    total = summary.get("total_queries") or 0
    if not total:
        return RECOMMENDATION_NEEDS_MORE_DATA
    if (summary.get("queries_with_safety_violation") or 0) > 0:
        return RECOMMENDATION_KEEP_DISABLED
    memory_used = summary.get("queries_with_memory_used") or 0
    if memory_used == 0:
        return RECOMMENDATION_NEEDS_MORE_DATA
    worsened = summary.get("queries_worsened") or 0
    if worsened / total > 0.25:
        return RECOMMENDATION_KEEP_DISABLED
    if memory_used / total < 0.25:
        return RECOMMENDATION_NEEDS_MORE_DATA
    baseline_lat = summary.get("avg_baseline_latency_ms")
    memory_lat = summary.get("avg_memory_latency_ms")
    if (
        isinstance(baseline_lat, (int, float))
        and isinstance(memory_lat, (int, float))
        and baseline_lat > 0
        and memory_lat / baseline_lat > 1 + _LATENCY_REGRESSION_RATIO
    ):
        return RECOMMENDATION_ENABLE_DEV_ONLY
    improved = summary.get("queries_improved") or 0
    if improved == 0:
        # Memory used but no quality lift visible — preview at most.
        return RECOMMENDATION_ENABLE_PREVIEW
    # Pick scope-targeted recommendation when results are
    # scope-homogeneous.
    scopes = {r.query.scope for r in results}
    if scopes == {"project_active"}:
        return RECOMMENDATION_ENABLE_PROJECT_SCOPE
    if scopes == {"document_active"}:
        return RECOMMENDATION_ENABLE_DOCUMENT_SCOPE
    return RECOMMENDATION_ENABLE_PREVIEW


def compute_recommendation(
    report: MemoryQueryEvalReport,
) -> str:
    """Public wrapper — accepts a full report and re-runs the
    recommendation engine. Used by callers that hold a report dict
    and want to recompute the field (e.g. tests verifying the
    string is stable across serialisation roundtrip)."""
    return compute_recommendation_for_summary(
        report.summary, report.results,
    )


# ---- Env toggle context manager ---------------------------------


@contextmanager
def _memory_env(memory_enabled: bool):
    """Set the memory + expansion env flags for the duration of the
    block. Restores prior values (or absence) on exit, even on
    exception — the harness must never leave the process env in
    an unexpected state.

    The two flags move together for the memory-aware mode: the
    Phase 5A expansion merge only fires when
    ``J1_QUERY_EXPANSION_ENABLED=true`` AND memory is on, so the
    A/B has to flip both."""
    keys = (
        ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
        ENV_QUERY_EXPANSION_ENABLED,
    )
    prior: dict[str, str | None] = {}
    for k in keys:
        prior[k] = os.environ.get(k) if k in os.environ else None
    try:
        for k in keys:
            os.environ[k] = "true" if memory_enabled else "false"
        yield
    finally:
        for k, prev in prior.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


# ---- Fixture loader ---------------------------------------------


def load_memory_query_fixture(
    path: Path,
) -> list[MemoryQueryEvalQuery]:
    """Parse a YAML or JSON fixture file.

    Accepted shapes:

      * YAML / JSON with a top-level ``queries: [...]`` list.
      * JSONL — one JSON object per line.

    Each entry must carry ``question``. Malformed entries raise
    ``ValueError`` with a precise message so the harness fails fast
    on bad input rather than silently dropping queries."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    suffix = path.suffix.lower()
    payload: Any
    if suffix in {".yaml", ".yml"}:
        import yaml  # type: ignore[import-untyped]
        payload = yaml.safe_load(raw)
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # JSONL fallback.
            items: list[Mapping[str, Any]] = []
            for lineno, line in enumerate(raw.splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"fixture file is not valid JSON or JSONL "
                        f"(line {lineno}): {exc}"
                    ) from exc
                if not isinstance(entry, Mapping):
                    raise ValueError(
                        f"fixture JSONL line {lineno} is not a JSON object"
                    )
                items.append(entry)
            return [
                MemoryQueryEvalQuery.from_payload(it, index=i + 1)
                for i, it in enumerate(items)
            ]
    if not isinstance(payload, Mapping):
        raise ValueError(
            "fixture must contain a mapping with a top-level "
            "'queries' list"
        )
    items_field = payload.get("queries")
    if not isinstance(items_field, list):
        raise ValueError(
            "fixture mapping must contain a 'queries' list"
        )
    return [
        MemoryQueryEvalQuery.from_payload(it, index=i + 1)
        for i, it in enumerate(items_field)
        if isinstance(it, Mapping)
    ]


# ---- Markdown report writer -------------------------------------


def render_markdown_report(
    report: MemoryQueryEvalReport,
) -> str:
    """Render a human-readable Markdown summary of the report.

    Layout:

      # Memory Query Evaluation Report
      Generated: <ISO>
      Scope: <project_id> / <document_id?>
      Recommendation: **<recommendation>**

      ## Summary
      <table of summary counts>

      ## Warnings frequency
      <ordered list>

      ## Per-query results
      <one section per query with deltas>
    """
    lines: list[str] = []
    lines.append("# Memory Query Evaluation Report")
    lines.append("")
    lines.append(f"Generated: `{report.generated_at}`")
    scope_parts = []
    for k, v in report.scope.items():
        if v is None or v == "":
            continue
        scope_parts.append(f"{k}=`{v}`")
    if scope_parts:
        lines.append(f"Scope: {', '.join(scope_parts)}")
    lines.append(f"Recommendation: **{report.recommendation}**")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    summary = report.summary
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for key, label in [
        ("total_queries", "Total queries"),
        ("queries_improved", "Improved"),
        ("queries_unchanged", "Unchanged"),
        ("queries_worsened", "Worsened"),
        ("queries_with_safety_violation", "Safety violations"),
        ("queries_with_memory_used", "Memory used"),
        ("queries_with_memory_unavailable", "Memory unavailable"),
        ("avg_baseline_latency_ms", "Baseline avg latency (ms)"),
        ("avg_memory_latency_ms", "Memory-aware avg latency (ms)"),
        ("avg_baseline_citation_count", "Baseline avg citations"),
        ("avg_memory_citation_count", "Memory-aware avg citations"),
        ("avg_injected_evidence_count", "Avg injected evidence"),
    ]:
        value = summary.get(key)
        lines.append(f"| {label} | {value if value is not None else '—'} |")
    lines.append("")

    if summary.get("memory_warnings_frequency"):
        lines.append("## Memory warnings frequency")
        lines.append("")
        for w, count in summary["memory_warnings_frequency"].items():
            lines.append(f"- `{w}`: {count}")
        lines.append("")

    if summary.get("safety_violations_frequency"):
        lines.append("## Safety violations frequency")
        lines.append("")
        for v, count in summary["safety_violations_frequency"].items():
            lines.append(f"- `{v}`: {count}")
        lines.append("")

    lines.append("## Per-query results")
    lines.append("")
    for r in report.results:
        q = r.query
        lines.append(f"### `{q.id}` — {q.question}")
        lines.append("")
        lines.append(f"- Scope: `{q.scope}`"
                     + (f", document `{q.document_id}`" if q.document_id else ""))
        if q.category:
            lines.append(f"- Category: `{q.category}`")
        lines.append(f"- Verdict: **{r.verdict}**")
        if r.safety_violations:
            lines.append(
                "- Safety violations: "
                + ", ".join(f"`{v}`" for v in r.safety_violations),
            )
        lines.append("")
        lines.append("| Field | Baseline | Memory-aware | Delta |")
        lines.append("|---|---|---|---|")
        for field_key, label in [
            ("citation_count", "Citations"),
            ("retrieved_count", "Retrieved"),
            ("evidence_count", "Evidence sent to LLM"),
            ("duration_ms", "Duration (ms)"),
        ]:
            b_val = getattr(r.baseline, field_key)
            m_val = getattr(r.memory_aware, field_key)
            d_val = r.delta.get(field_key)
            lines.append(
                f"| {label} | {_fmt(b_val)} | {_fmt(m_val)} "
                f"| {_fmt(d_val)} |",
            )
        if q.expected_terms:
            lines.append("")
            lines.append("Expected terms (baseline → memory):")
            lines.append("")
            lines.append(
                f"- Baseline present: "
                + ", ".join(r.baseline.expected_terms_present) or "(none)",
            )
            lines.append(
                f"- Memory present: "
                + ", ".join(r.memory_aware.expected_terms_present) or "(none)",
            )
        km = r.memory_aware.knowledge_memory or {}
        if km:
            lines.append("")
            lines.append("Memory diagnostics:")
            lines.append("")
            lines.append(f"- Status: `{km.get('status', '—')}`")
            lines.append(f"- Scope: `{km.get('scope', '—')}`")
            if km.get("selected_entry_count"):
                lines.append(
                    f"- Selected entries: {km['selected_entry_count']}",
                )
            if km.get("applied_expansion_terms"):
                lines.append(
                    "- Applied expansion terms: "
                    + ", ".join(km["applied_expansion_terms"]),
                )
            if km.get("injected_evidence_count"):
                lines.append(
                    f"- Injected evidence: {km['injected_evidence_count']}",
                )
            if km.get("warnings"):
                lines.append(
                    "- Warnings: " + ", ".join(km["warnings"]),
                )
        if q.notes:
            lines.append("")
            lines.append(f"Notes: {q.notes}")
        lines.append("")

    if report.warnings:
        lines.append("## Harness warnings")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


# ---- CLI --------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="j1.tools.evaluate_memory_query",
        description=(
            "Run a fixed query set twice (baseline vs memory-aware)"
            " and emit JSON + Markdown reports comparing answer "
            "quality, evidence/citation counts, memory diagnostics,"
            " and latency."
        ),
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--document-id", default=None)
    parser.add_argument(
        "--fixture", required=True, type=Path,
        help="YAML or JSON fixture file (see module docstring).",
    )
    parser.add_argument(
        "--output-dir", default=None, type=Path,
        help=(
            "Directory to write `memory_query_eval_report.json` "
            "+ `memory_query_eval_report.md`. When omitted the "
            "JSON report is printed to stdout."
        ),
    )
    parser.add_argument(
        "--tenant-id", default=None,
        help=(
            "Tenant id for scope. Falls back to "
            "``J1_DEFAULT_TENANT_ID`` env if unset."
        ),
    )
    parser.add_argument(
        "--strict", action="store_true",
        help=(
            "Exit non-zero when any query trips a safety violation."
            " Default is off (recommended) — quality verdicts are "
            "report data, not test failures."
        ),
    )
    return parser.parse_args(argv)


def _build_runner(args: argparse.Namespace) -> RunnerCallable:
    """Production runner — wraps the validation service. Lazy-
    imports so test-only callers can use the evaluator class
    directly without the bootstrap layer."""
    from j1.projects.context import ProjectContext
    from j1.validation.dtos import (
        ManualTestQueryRequest,
        QueryScopeDTO,
    )

    tenant_id = (
        args.tenant_id
        or os.environ.get("J1_DEFAULT_TENANT_ID")
        or ""
    )
    if not tenant_id:
        raise SystemExit(
            "tenant id required: pass --tenant-id or set "
            "J1_DEFAULT_TENANT_ID"
        )

    try:
        from deploy.dev._wiring import (
            build_validation_service_for_tool,
        )
    except ImportError as exc:
        raise SystemExit(
            "CLI runner requires deploy.dev._wiring."
            f"build_validation_service_for_tool: {exc}"
        ) from exc

    service = build_validation_service_for_tool(
        tenant_id=tenant_id, project_id=args.project_id,
    )
    ctx = ProjectContext(tenant_id=tenant_id, project_id=args.project_id)

    def _runner(
        query: MemoryQueryEvalQuery, memory_enabled: bool,
    ) -> Mapping[str, Any]:
        del memory_enabled  # the env flags drive the orchestrator
        scope_type = query.scope
        document_id = query.document_id or args.document_id
        if scope_type == "document_active" and document_id:
            scope_dto = QueryScopeDTO(
                type="document_active", document_id=document_id,
            )
            response = service.run_document_test_query(
                ctx, document_id,
                ManualTestQueryRequest(
                    question=query.question, scope=scope_dto,
                ),
            )
        else:
            scope_dto = QueryScopeDTO(type="project_active")
            response = service.run_project_query(
                ctx,
                ManualTestQueryRequest(
                    question=query.question, scope=scope_dto,
                ),
            )
        return _response_to_mapping(response)

    return _runner


def _response_to_mapping(response: Any) -> Mapping[str, Any]:
    """Project the validation-surface response into the Mapping
    the evaluator reads."""
    return {
        "answer": getattr(response, "answer", "") or "",
        "citations": [
            _to_mapping(c) for c in (
                getattr(response, "citations", None) or []
            )
        ],
        "retrieved_chunks": [
            _to_mapping(c) for c in (
                getattr(response, "retrieved_chunks", None) or []
            )
        ],
        "evidence_sent_to_llm": [
            _to_mapping(b) for b in (
                getattr(response, "evidence_sent_to_llm", None) or []
            )
        ],
        "debug": dict(getattr(response, "debug", None) or {}),
    }


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    # Dataclass-ish: pull public attributes.
    return {
        k: getattr(value, k)
        for k in dir(value)
        if not k.startswith("_") and not callable(getattr(value, k))
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=os.environ.get("J1_LOG_LEVEL", "INFO"))
    queries = load_memory_query_fixture(args.fixture)
    runner = _build_runner(args)
    evaluator = MemoryQueryEvaluator(
        runner=runner,
        scope={
            "project_id": args.project_id,
            "document_id": args.document_id,
            "tenant_id": args.tenant_id,
        },
        strict=args.strict,
    )
    report = evaluator.evaluate(queries)
    json_payload = json.dumps(
        report.to_dict(), indent=2, ensure_ascii=False,
    )
    md_payload = render_markdown_report(report)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "memory_query_eval_report.json"
        md_path = args.output_dir / "memory_query_eval_report.md"
        json_path.write_text(json_payload + "\n", encoding="utf-8")
        md_path.write_text(md_payload + "\n", encoding="utf-8")
        _log.info(
            "wrote memory-query reports to %s, %s", json_path, md_path,
        )
    else:
        print(json_payload)
    # Strict-mode exit: non-zero only on safety violations. Mixed
    # quality (improved + unchanged + worsened without violations)
    # always exits 0 so report-generation runs don't break CI.
    if args.strict and report.summary.get(
        "queries_with_safety_violation", 0,
    ):
        return 2
    return 0


# ---- Helpers ---------------------------------------------------


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return ()


if __name__ == "__main__":
    raise SystemExit(main())
