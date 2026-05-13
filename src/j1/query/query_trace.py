"""QueryTrace — the structured record of one query end-to-end.

The trace IS the raw manual-test view. Every piece of state the
orchestrator generates lands here, so an operator reading the trace
can answer:

  * What did I ask?
  * What did the planner make of it?
  * Which routes ran, with what queries?
  * Which candidates came back from each route?
  * Which ones did the builder keep / drop, and why?
  * Which evidence groups got covered, which didn't?
  * What was actually sent to the LLM?
  * What did the LLM say?
  * Which citations bind to the evidence?
  * Which gates passed / failed?
  * What's the final status?

Nothing here is for production answer rendering — that's the public
``QueryResponse``. The trace is the operator surface and the test-
oracle surface: regression tests assert on its shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from j1.query.query_plan import (
    DroppedCandidate,
    EvidenceBlock,
    EvidenceCandidate,
    EvidencePack,
    GateResult,
    QueryPlan,
    RetrievalRouteKind,
)


@dataclass(frozen=True)
class RouteExecutionRecord:
    """One row in the trace's "routes executed" table. Captures input
    + output + timing so an operator can diagnose "why did BM25
    return nothing" without re-running the route.

    ``error`` is populated when the route raised — the orchestrator
    treats route errors as soft (one route failure doesn't kill the
    query) so the trace must carry the failure explicitly."""

    route: RetrievalRouteKind
    query: str
    label: str
    duration_ms: int
    candidates: tuple[EvidenceCandidate, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.value,
            "query": self.query,
            "label": self.label,
            "duration_ms": self.duration_ms,
            "candidates": [c.to_dict() for c in self.candidates],
            "error": self.error,
        }


@dataclass(frozen=True)
class QueryTrace:
    """The full end-to-end record. ``final_status`` is the answer
    quality gate's verdict; ``answer`` is the synthesizer's output
    (or empty when a gate failed before synthesis); ``citations`` is
    the binder's output — always a subset of selected evidence."""

    question: str
    normalized_question: str
    plan: QueryPlan
    routes_executed: tuple[RouteExecutionRecord, ...]
    all_candidates: tuple[EvidenceCandidate, ...]
    selected: tuple[EvidenceBlock, ...]
    dropped: tuple[DroppedCandidate, ...]
    groups_covered: tuple[str, ...]
    groups_missing: tuple[str, ...]
    llm_evidence: tuple[EvidenceBlock, ...]
    answer: str
    citations: tuple[EvidenceBlock, ...]
    gate_results: tuple[GateResult, ...]
    final_status: str
    # Total wall-clock for the whole orchestrator call. Surfaced for
    # the manual view's perf column; not used by gates.
    duration_ms: int = 0

    @classmethod
    def empty_with_plan(cls, question: str, plan: QueryPlan) -> "QueryTrace":
        """Construct a trace shell containing only the plan. Useful
        for the early-exit paths (no candidates → sufficiency fails)
        so the manual view always has *something* to render."""
        return cls(
            question=question,
            normalized_question=plan.normalized_question,
            plan=plan,
            routes_executed=(),
            all_candidates=(),
            selected=(),
            dropped=(),
            groups_covered=(),
            groups_missing=tuple(g.name for g in plan.required_groups),
            llm_evidence=(),
            answer="",
            citations=(),
            gate_results=(),
            final_status="pending",
        )

    def with_routes(
        self, routes: tuple[RouteExecutionRecord, ...],
    ) -> "QueryTrace":
        """Return a copy with routes_executed populated. Trace
        construction is incremental and value-based — every stage
        rebuilds the trace from the previous one, never mutates."""
        all_candidates: list[EvidenceCandidate] = []
        for r in routes:
            all_candidates.extend(r.candidates)
        return _replace(
            self,
            routes_executed=routes,
            all_candidates=tuple(all_candidates),
        )

    def with_pack(self, pack: EvidencePack) -> "QueryTrace":
        return _replace(
            self,
            selected=pack.blocks,
            dropped=pack.dropped,
            groups_covered=pack.groups_covered,
            groups_missing=pack.groups_missing,
        )

    def with_llm_evidence(
        self, blocks: tuple[EvidenceBlock, ...],
    ) -> "QueryTrace":
        return _replace(self, llm_evidence=blocks)

    def with_answer(
        self,
        answer: str,
        citations: tuple[EvidenceBlock, ...],
    ) -> "QueryTrace":
        return _replace(self, answer=answer, citations=citations)

    def with_gates(
        self,
        gates: tuple[GateResult, ...],
        final_status: str,
    ) -> "QueryTrace":
        return _replace(
            self, gate_results=gates, final_status=final_status,
        )

    def with_duration(self, duration_ms: int) -> "QueryTrace":
        return _replace(self, duration_ms=duration_ms)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly shape rendered by the manual-test endpoint
        verbatim. Keys are stable; new ones land at the end so
        operators consuming the JSON keep working."""
        return {
            "question": self.question,
            "normalized_question": self.normalized_question,
            "plan": self.plan.to_dict(),
            "routes_executed": [
                r.to_dict() for r in self.routes_executed
            ],
            "all_candidates": [
                c.to_dict() for c in self.all_candidates
            ],
            "selected": [b.to_dict() for b in self.selected],
            "dropped": [d.to_dict() for d in self.dropped],
            "groups_covered": list(self.groups_covered),
            "groups_missing": list(self.groups_missing),
            "llm_evidence": [b.to_dict() for b in self.llm_evidence],
            "answer": self.answer,
            "citations": [b.to_dict() for b in self.citations],
            "gate_results": [g.to_dict() for g in self.gate_results],
            "final_status": self.final_status,
            "duration_ms": self.duration_ms,
        }


def _replace(trace: QueryTrace, **kwargs: Any) -> QueryTrace:
    """Tiny shim around ``dataclasses.replace`` so the methods above
    stay readable. Local helper — dataclasses.replace works fine but
    the import noise in every method gets in the way."""
    import dataclasses
    return dataclasses.replace(trace, **kwargs)


__all__ = [
    "QueryTrace",
    "RouteExecutionRecord",
]
