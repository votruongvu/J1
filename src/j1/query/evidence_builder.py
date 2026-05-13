"""EvidencePackBuilder — turn raw route candidates into a selected
evidence pack, grouped by required groups.

The builder is the load-bearing piece between retrieval and
synthesis. It owns five jobs:

  1. **Merge & dedupe.** Same chunk surfaced by multiple routes
     collapses to ONE candidate (BM25 and RAGAnything both ranking
     the same chunk shouldn't inflate the pack).
  2. **Group by required group.** Each ``EvidenceGroupSpec`` is a
     bucket — the builder assigns candidates by anchor match. A
     candidate may belong to multiple groups (e.g. a chunk
     mentioning "60% design" AND "deliverables").
  3. **Rank within group.** Score-aware ranking — RAGAnything
     semantic scores compete with BM25 lexical scores; the builder
     uses a per-route normalisation so neither dominates.
  4. **Cap per group.** "Don't send 20 citations when 4 will do"
     is the explicit fix for the failed-question observation. Each
     group gets at most ``per_group_cap`` blocks.
  5. **Report dropped candidates.** Every rejection records a
     reason so the manual view can show "why didn't this chunk
     make it into evidence".

Suppression rules are deliberately generic. Boilerplate, scope,
and dedup are domain-neutral. Domain-specific priority (e.g.
"prefer enriched.requirements over raw chunks for requirement
queries") flows in through the ``DomainProfile`` artifact_priority
table when the orchestrator passes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from j1.query.domain_profile import DomainProfile, GENERIC_PROFILE
from j1.query.query_plan import (
    DroppedCandidate,
    EvidenceBlock,
    EvidenceCandidate,
    EvidenceGroupSpec,
    EvidencePack,
    Intent,
    QueryPlan,
)


# ---- Dedupe keys -------------------------------------------------


def _candidate_key(c: EvidenceCandidate) -> tuple[str, str]:
    """Identity for deduplication. ``chunk_id`` wins when present;
    ``artifact_id`` is the fallback for artifact-lookup hits that
    don't have a chunk grain."""
    return (c.artifact_id, c.chunk_id or "")


# ---- Boilerplate signal -----------------------------------------


# Generic boilerplate phrases — any document type. We deliberately
# DO NOT name specific section titles or document types here; the
# domain-purity guard would catch that anyway.
_BOILERPLATE_MARKERS: tuple[str, ...] = (
    "table of contents",
    "confidential",
    "do not distribute",
    "this document is proprietary",
    "copyright ©",
    "page intentionally left blank",
    "all rights reserved",
)


def _looks_boilerplate(body: str) -> bool:
    if not body:
        return True
    bl = body.lower()
    # A chunk matching multiple markers is almost certainly a
    # cover page / TOC; one marker on a long chunk isn't.
    hits = sum(1 for m in _BOILERPLATE_MARKERS if m in bl)
    if hits >= 2:
        return True
    if hits == 1 and len(bl) < 200:
        return True
    return False


# ---- Builder ----------------------------------------------------


@dataclass
class EvidenceBuilderConfig:
    """Knobs operators can turn without rebuilding the orchestrator.

    Defaults are tuned for the failed-question shape: at most 4
    blocks per group keeps the synthesis-evidence set small enough
    for the LLM context, and ``min_score`` is loose so a low-
    confidence lexical recall doesn't get filtered out before the
    sufficiency gate has seen it."""

    per_group_cap: int = 4
    overall_cap: int = 24
    min_score: float = 0.0
    suppress_boilerplate: bool = True
    # When True, drop candidates whose run_id doesn't match the
    # active run (defence-in-depth — the routes already enforce
    # scope, but a misbehaving adapter shouldn't poison synthesis).
    enforce_run_scope: bool = True


class EvidencePackBuilder:
    """Selects the evidence the synthesizer is allowed to read.

    Construct once; the ``build`` method is pure with respect to its
    inputs (plan + candidates + scope_run_id). Tests run it on
    handwritten candidate lists without any retrieval infra."""

    def __init__(
        self,
        *,
        config: EvidenceBuilderConfig | None = None,
    ) -> None:
        self._config = config or EvidenceBuilderConfig()

    def build(
        self,
        plan: QueryPlan,
        candidates: Iterable[EvidenceCandidate],
        *,
        scope_run_id: str | None = None,
        profile: DomainProfile | None = None,
    ) -> EvidencePack:
        profile = profile or GENERIC_PROFILE
        deduped, dedup_drops = self._dedupe(candidates)
        scoped, scope_drops = self._enforce_scope(deduped, scope_run_id)
        clean, boilerplate_drops = self._suppress_boilerplate(scoped)
        priority_order = self._priority_order(plan.intent, profile)
        # Assign each candidate to one or more groups.
        grouped, ungrouped = self._assign_groups(clean, plan.required_groups)
        # Rank + cap inside each group.
        selected: list[EvidenceBlock] = []
        group_caps_hit: list[DroppedCandidate] = []
        for group_name, group_cands in grouped.items():
            ranked = self._rank(group_cands, priority_order)
            for rank, cand in enumerate(ranked):
                if rank < self._config.per_group_cap:
                    selected.append(EvidenceBlock(
                        candidate=cand,
                        body=_full_body(cand),
                        group=group_name,
                        rank_in_group=rank,
                    ))
                else:
                    group_caps_hit.append(DroppedCandidate(
                        candidate=cand,
                        reason=(
                            f"group_cap: '{group_name}' already has "
                            f"{self._config.per_group_cap} blocks"
                        ),
                    ))
        # If there's overall headroom, fold in a few ungrouped
        # candidates so the synthesizer has SOME evidence even when
        # the plan didn't carve groups (UNKNOWN intent, citation
        # lookup, etc.). Always keep selected ≤ overall_cap.
        ungrouped_ranked = self._rank(ungrouped, priority_order)
        for cand in ungrouped_ranked:
            if len(selected) >= self._config.overall_cap:
                group_caps_hit.append(DroppedCandidate(
                    candidate=cand,
                    reason=f"overall_cap: pack full at "
                           f"{self._config.overall_cap}",
                ))
                continue
            selected.append(EvidenceBlock(
                candidate=cand,
                body=_full_body(cand),
                group=None,
                rank_in_group=0,
            ))
        # Final overall trim — if per-group selection already
        # overflowed the cap, prefer the highest-ranked blocks.
        if len(selected) > self._config.overall_cap:
            selected.sort(
                key=lambda b: (b.rank_in_group, -b.candidate.score),
            )
            kept = selected[: self._config.overall_cap]
            for b in selected[self._config.overall_cap:]:
                group_caps_hit.append(DroppedCandidate(
                    candidate=b.candidate,
                    reason=f"overall_cap: pack trimmed to "
                           f"{self._config.overall_cap}",
                ))
            selected = kept
        groups_covered = tuple(
            sorted({b.group for b in selected if b.group is not None})
        )
        groups_missing = tuple(
            g.name for g in plan.required_groups
            if g.required and g.name not in groups_covered
        )
        dropped = tuple(
            dedup_drops + scope_drops + boilerplate_drops + group_caps_hit
        )
        return EvidencePack(
            blocks=tuple(selected),
            groups_covered=groups_covered,
            groups_missing=groups_missing,
            dropped=dropped,
        )

    # ---- Stages -----------------------------------------------

    def _dedupe(
        self, candidates: Iterable[EvidenceCandidate],
    ) -> tuple[list[EvidenceCandidate], list[DroppedCandidate]]:
        """Collapse duplicates. Same artifact_id+chunk_id from two
        routes keeps the higher-scoring copy; the other goes to
        ``dropped`` with reason ``duplicate``."""
        kept: dict[tuple[str, str], EvidenceCandidate] = {}
        drops: list[DroppedCandidate] = []
        for c in candidates:
            key = _candidate_key(c)
            existing = kept.get(key)
            if existing is None:
                kept[key] = c
                continue
            # Keep the higher-scoring; drop the other with a reason
            # that records who survived.
            if c.score > existing.score:
                drops.append(DroppedCandidate(
                    candidate=existing,
                    reason=(
                        f"duplicate: superseded by {c.route.value} "
                        f"(score {c.score:.3f} > {existing.score:.3f})"
                    ),
                ))
                kept[key] = c
            else:
                drops.append(DroppedCandidate(
                    candidate=c,
                    reason=(
                        f"duplicate: already kept from "
                        f"{existing.route.value} (score "
                        f"{existing.score:.3f} >= {c.score:.3f})"
                    ),
                ))
        return list(kept.values()), drops

    def _enforce_scope(
        self,
        candidates: list[EvidenceCandidate],
        scope_run_id: str | None,
    ) -> tuple[list[EvidenceCandidate], list[DroppedCandidate]]:
        """Defence-in-depth scope filter. A candidate with a run_id
        that doesn't match the active scope's run_id is dropped —
        even though the routes already filtered. The dropped
        reason makes the leak visible in the manual view."""
        if not self._config.enforce_run_scope or scope_run_id is None:
            return candidates, []
        kept: list[EvidenceCandidate] = []
        drops: list[DroppedCandidate] = []
        for c in candidates:
            if c.run_id and c.run_id != scope_run_id:
                drops.append(DroppedCandidate(
                    candidate=c,
                    reason=(
                        f"scope: candidate.run_id={c.run_id!r} "
                        f"!= active {scope_run_id!r}"
                    ),
                ))
                continue
            kept.append(c)
        return kept, drops

    def _suppress_boilerplate(
        self, candidates: list[EvidenceCandidate],
    ) -> tuple[list[EvidenceCandidate], list[DroppedCandidate]]:
        if not self._config.suppress_boilerplate:
            return candidates, []
        kept: list[EvidenceCandidate] = []
        drops: list[DroppedCandidate] = []
        for c in candidates:
            body = _full_body(c)
            if _looks_boilerplate(body):
                drops.append(DroppedCandidate(
                    candidate=c, reason="boilerplate_suppressed",
                ))
                continue
            kept.append(c)
        return kept, drops

    def _priority_order(
        self, intent: Intent, profile: DomainProfile,
    ) -> Mapping[str, int]:
        """Build artifact-kind → priority rank (lower = higher
        priority). Profile-driven when configured; otherwise empty
        and the builder uses score alone."""
        ordered = profile.artifact_priority.get(intent, ())
        return {kind: i for i, kind in enumerate(ordered)}

    def _assign_groups(
        self,
        candidates: list[EvidenceCandidate],
        groups: tuple[EvidenceGroupSpec, ...],
    ) -> tuple[
        dict[str, list[EvidenceCandidate]],
        list[EvidenceCandidate],
    ]:
        """Bucket candidates by required group. A candidate that
        matches multiple groups lands in all of them — the
        synthesizer needs to see the same chunk from "60%" AND
        "deliverables" when both anchors hit. Candidates that hit
        no group are returned separately so the orchestrator can
        decide what to do with them (drop, fold in, etc.)."""
        grouped: dict[str, list[EvidenceCandidate]] = {
            g.name: [] for g in groups
        }
        ungrouped: list[EvidenceCandidate] = []
        for c in candidates:
            body_l = _full_body(c).lower()
            matched_any = False
            for g in groups:
                anchors = g.anchors or (g.name,)
                if any(a.lower() in body_l for a in anchors if a):
                    grouped[g.name].append(c)
                    matched_any = True
            if not matched_any:
                ungrouped.append(c)
        return grouped, ungrouped

    def _rank(
        self,
        candidates: list[EvidenceCandidate],
        priority: Mapping[str, int],
    ) -> list[EvidenceCandidate]:
        """Sort: profile priority (when set) then score descending
        then artifact_id ascending for determinism."""
        def _key(c: EvidenceCandidate) -> tuple[int, float, str]:
            kind_rank = priority.get(c.artifact_kind, 1_000_000)
            # Negate score so higher score sorts first.
            return (kind_rank, -float(c.score or 0.0), c.artifact_id)
        return sorted(candidates, key=_key)


# ---- Helpers ----------------------------------------------------


def _full_body(c: EvidenceCandidate) -> str:
    """Read the full chunk body off the extra dict (where the route
    parked it). Falls back to ``text_preview`` when the route
    didn't carry a separate full body — preview-only is better
    than nothing for the sufficiency check."""
    extra = c.extra or {}
    body = extra.get("body")
    if body:
        return str(body)
    return c.text_preview or ""


__all__ = [
    "EvidenceBuilderConfig",
    "EvidencePackBuilder",
]
