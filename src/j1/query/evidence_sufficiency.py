"""EvidenceSufficiencyGate — the "is this evidence good enough?"
check that fires BEFORE the synthesizer call.

The legacy validation was the load-bearing bug: it sent whatever
retrieved → asked the LLM → marked the run Passed regardless of
whether the LLM had real evidence to work with. The result was
the failed-question observation: 20 unrelated citations, an
abstaining answer, and a green check mark.

The sufficiency gate solves this by reading the plan's
``SufficiencyPolicy`` and asserting the pack hits its thresholds
BEFORE any LLM call. When the gate fails, the orchestrator returns
``evidence_insufficient`` or ``retrieval_insufficient`` — never
``passed``.

Returns a single ``GateResult`` per check so the trace shows one
row per assertion, with the reason for failures."""

from __future__ import annotations

from typing import Iterable

from j1.query.query_plan import (
    EvidencePack,
    GateResult,
    QueryPlan,
)


# Stable gate-result names. Log consumers / UI surfaces match
# these strings; new gates land here without renaming the old ones.
GATE_RETRIEVAL_NONEMPTY = "retrieval_nonempty"
GATE_REQUIRED_GROUPS = "required_groups_covered"
GATE_MIN_TOTAL_BLOCKS = "min_total_blocks"


class EvidenceSufficiencyGate:
    """Pure decision component. ``check(plan, pack, total_candidates)``
    returns the tuple of gate results plus a derived status string —
    ``ok``, ``retrieval_insufficient``, or ``evidence_insufficient``.

    The status meanings:

      * **retrieval_insufficient** — zero candidates came back from
        any route. The query couldn't even start. Operators see
        this when the active scope has no indexed content for the
        question.
      * **evidence_insufficient** — candidates were retrieved but
        the policy's group / block thresholds weren't hit. The
        orchestrator MUST NOT call the LLM in this state.
      * **ok** — sufficient evidence; proceed to synthesis.
    """

    def check(
        self,
        plan: QueryPlan,
        pack: EvidencePack,
        *,
        total_candidates: int,
    ) -> tuple[tuple[GateResult, ...], str]:
        results: list[GateResult] = []

        # --- Gate 1: retrieval produced any candidates ----------
        had_any = total_candidates > 0
        nonempty_required = plan.sufficiency.fail_when_no_candidates
        if not had_any and nonempty_required:
            results.append(GateResult(
                name=GATE_RETRIEVAL_NONEMPTY,
                passed=False,
                severity="required",
                reason=(
                    "retrieval returned zero candidates; the active "
                    "scope may have no indexed content for this "
                    "question"
                ),
                detail={"total_candidates": total_candidates},
            ))
            return tuple(results), "retrieval_insufficient"
        results.append(GateResult(
            name=GATE_RETRIEVAL_NONEMPTY,
            passed=True,
            severity="required",
            detail={"total_candidates": total_candidates},
        ))

        # --- Gate 2: required-group coverage --------------------
        required_groups = [
            g for g in plan.required_groups if g.required
        ]
        covered = set(pack.groups_covered)
        groups_required = len(required_groups)
        groups_covered_count = sum(
            1 for g in required_groups if g.name in covered
        )
        min_groups = plan.sufficiency.min_required_groups
        passed_groups = groups_covered_count >= min_groups
        results.append(GateResult(
            name=GATE_REQUIRED_GROUPS,
            passed=passed_groups,
            severity="required",
            reason=(
                None if passed_groups else
                f"{groups_covered_count} of {groups_required} required "
                f"groups have evidence; threshold is {min_groups}. "
                f"missing: {list(pack.groups_missing)}"
            ),
            detail={
                "groups_required": groups_required,
                "groups_covered": groups_covered_count,
                "missing": list(pack.groups_missing),
                "threshold": min_groups,
            },
        ))

        # --- Gate 3: min total blocks ---------------------------
        block_count = len(pack.blocks)
        min_blocks = plan.sufficiency.min_total_blocks
        passed_blocks = block_count >= min_blocks
        results.append(GateResult(
            name=GATE_MIN_TOTAL_BLOCKS,
            passed=passed_blocks,
            severity="required",
            reason=(
                None if passed_blocks else
                f"pack has {block_count} blocks; threshold is {min_blocks}"
            ),
            detail={
                "blocks": block_count,
                "threshold": min_blocks,
            },
        ))

        # Composite status.
        if not passed_groups or not passed_blocks:
            return tuple(results), "evidence_insufficient"
        return tuple(results), "ok"


def first_failure_reason(
    results: Iterable[GateResult],
) -> str | None:
    """Return the reason on the first failing required gate, or
    ``None`` when everything passed. Used by the orchestrator to
    populate the final ``QueryResult.message`` so a caller sees the
    same first-failure reason the trace shows."""
    for r in results:
        if r.severity == "required" and not r.passed:
            return r.reason
    return None


__all__ = [
    "EvidenceSufficiencyGate",
    "GATE_MIN_TOTAL_BLOCKS",
    "GATE_REQUIRED_GROUPS",
    "GATE_RETRIEVAL_NONEMPTY",
    "first_failure_reason",
]
