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
    # Snapshot scope diagnostics — populated by the orchestrator /
    # routes so operators can verify that BM25 + RAGAnything queried
    # the same eligible boundary. Empty when the trace was built by
    # a legacy caller; the manual view treats empty as "unknown".
    eligible_snapshot_ids: tuple[str, ...] = ()
    queried_raganything_snapshot_ids: tuple[str, ...] = ()
    bm25_allowed_snapshot_ids: tuple[str, ...] = ()
    used_global_workspace: bool = False
    # Augmentation diagnostics — populated when a
    # ``DomainQueryAugmentationProvider`` was wired into the
    # orchestrator. When the provider is absent or returns
    # ``source="disabled"``, every augmentation field below stays
    # empty / False / zero.
    augmentation_source: str = ""  # "domain_pack" / "disabled" / ""
    augmentation_terms: tuple[str, ...] = ()
    augmentation_aliases: tuple[tuple[str, str], ...] = ()
    augmentation_expansions: tuple[str, ...] = ()
    # ``applied_to_retrieval`` flips True when
    # ``J1_QUERY_EXPANSION_ENABLED=true`` AND at least one expansion
    # variant was generated. When True the retrieval counts below
    # are populated; when False they stay at 0.
    augmentation_applied_to_retrieval: bool = False
    augmentation_retrieval_counts: tuple[int, int, int] = (0, 0, 0)
    # Distribution of dedup-key hits: ``(original_only, expanded_only,
    # both)``. Computed BEFORE dedup but using dedup-keyed identity
    # so operators see how each provenance class contributed.
    augmentation_distribution: tuple[int, int, int] = (0, 0, 0)

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

    def with_snapshot_scope(
        self,
        *,
        eligible_snapshot_ids: tuple[str, ...] = (),
        queried_raganything_snapshot_ids: tuple[str, ...] = (),
        bm25_allowed_snapshot_ids: tuple[str, ...] = (),
        used_global_workspace: bool = False,
    ) -> "QueryTrace":
        """Stamp snapshot-scope diagnostics. Called by the
        orchestrator after route execution so operators can verify
        BM25 + RAGAnything used the same eligibility boundary."""
        return _replace(
            self,
            eligible_snapshot_ids=eligible_snapshot_ids,
            queried_raganything_snapshot_ids=queried_raganything_snapshot_ids,
            bm25_allowed_snapshot_ids=bm25_allowed_snapshot_ids,
            used_global_workspace=used_global_workspace,
        )

    def with_augmentation(
        self,
        *,
        source: str = "",
        terms: tuple[str, ...] = (),
        aliases: tuple[tuple[str, str], ...] = (),
        expansions: tuple[str, ...] = (),
        applied_to_retrieval: bool = False,
    ) -> "QueryTrace":
        """Stamp augmentation diagnostics on the trace. Pure data —
        ``applied_to_retrieval`` is the truthful "did retrieval get
        broader inputs?" flag. When False, the broadening fields
        below (``augmentation_retrieval_counts`` /
        ``augmentation_distribution``) stay at their zero
        defaults."""
        return _replace(
            self,
            augmentation_source=source,
            augmentation_terms=terms,
            augmentation_aliases=aliases,
            augmentation_expansions=expansions,
            augmentation_applied_to_retrieval=applied_to_retrieval,
        )

    def with_augmentation_retrieval_stats(
        self,
        *,
        original_count: int,
        expanded_count: int,
        deduplicated_total: int,
        distribution: dict[str, int],
    ) -> "QueryTrace":
        """Stamp the retrieval-side proof that expansion was actually
        consumed: how many raw candidates came from the original
        query, how many from variants, what the dedup'd total is,
        and how the dedup'd identities split across provenance
        classes (``original_only`` / ``expanded_only`` / ``both``).
        Diagnostic-only — does not affect retrieval / synthesis."""
        return _replace(
            self,
            augmentation_retrieval_counts=(
                original_count,
                expanded_count,
                deduplicated_total,
            ),
            augmentation_distribution=(
                int(distribution.get("original_only", 0)),
                int(distribution.get("expanded_only", 0)),
                int(distribution.get("both", 0)),
            ),
        )

    def with_deduped_candidates(
        self, candidates: tuple,
    ) -> "QueryTrace":
        """Replace ``all_candidates`` with the post-dedup set.
        ``routes_executed`` is intentionally untouched so the per-
        route raw rows stay visible in the manual-test view."""
        return _replace(self, all_candidates=tuple(candidates))

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
            "snapshot_scope": {
                "eligible_snapshot_ids": list(self.eligible_snapshot_ids),
                "queried_raganything_snapshot_ids": list(
                    self.queried_raganything_snapshot_ids,
                ),
                "bm25_allowed_snapshot_ids": list(
                    self.bm25_allowed_snapshot_ids,
                ),
                "used_global_workspace": self.used_global_workspace,
            },
            "augmentation": {
                "source": self.augmentation_source,
                "terms": list(self.augmentation_terms),
                "aliases": [list(p) for p in self.augmentation_aliases],
                "expansions": list(self.augmentation_expansions),
                "applied_to_retrieval": (
                    self.augmentation_applied_to_retrieval
                ),
                "retrieval_counts": {
                    "original": self.augmentation_retrieval_counts[0],
                    "expanded": self.augmentation_retrieval_counts[1],
                    "deduplicated_total": (
                        self.augmentation_retrieval_counts[2]
                    ),
                },
                "final_evidence_distribution": {
                    "original_only": self.augmentation_distribution[0],
                    "expanded_only": self.augmentation_distribution[1],
                    "both": self.augmentation_distribution[2],
                },
            },
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
