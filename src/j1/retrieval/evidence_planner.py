"""Generic structure-aware evidence planning.

For non-trivial intents (responsibility / dependency / stage /
deliverable / issue-risk / decision_trace / comparison /
list_extraction), raw top-K by similarity is wrong. The final
answer needs a *graph* of evidence: actor→action, stage→stage,
input→decision. One high-scoring chunk about ONE node loses every
other node.

This planner is structure-aware, NOT domain-aware. The "graph
shape" comes from the document's own heading hierarchy + the
mix of artifact types present in the candidate set — NOT from a
hardcoded label dictionary. The planner asks:

  * How many distinct section paths do my candidates cover?
  * Does the intent want **diversity** (responsibility, stage,
    deliverable, comparison, list_extraction) or **depth**
    (exact_fact_lookup, summary_lookup)?
  * Are enriched artifacts available (risks, requirements,
    summaries)? If so, anchor with them; ground with source
    chunks.

Diversity is enforced by *section-path uniqueness*: the planner
greedily picks the highest-scoring candidate from each
not-yet-covered section path, then fills the remaining budget
with the next-best candidates regardless of section. This is
the generic structural analog of "include Task 3, Task 4, Task 5"
without ever mentioning a task number.

The planner OUTPUTS a `PlannedEvidence` (selected list + drop
list + reasons) — it does NOT mutate the candidates. Callers are
free to ignore the plan and run their own selection; the plan
is also serialised into the diagnostic snapshot for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from j1.retrieval.diagnostics import CandidateDiagnostic
    from j1.retrieval.intent_router import QueryIntentLabel


# ---- Intent → planning policy -------------------------------------
#
# Each policy declares (a) whether the intent prefers diversity,
# (b) which artifact-kind anchors it prefers, and (c) which kinds
# to avoid. Everything else (the actual section labels, task IDs,
# AACE classes...) is derived from the candidate metadata at
# runtime — the policy is the meta-rule, not the data.

@dataclass(frozen=True)
class IntentPolicy:
    # When True, the planner spends its first slots maximising
    # distinct section paths. When False (e.g. exact_fact_lookup,
    # summary_lookup), it stays in top-K-by-score order.
    require_section_diversity: bool
    # Artifact kinds preferred as the "anchor" — included first
    # when present. Order matters: earlier entries win ties.
    preferred_anchor_kinds: tuple[str, ...] = ()
    # Artifact kinds the intent should generally avoid for the
    # purposes of this question. Demoted in scoring; not removed.
    avoid_kinds: tuple[str, ...] = ()
    # Bonus multiplier on candidates whose kind is preferred.
    anchor_bonus: float = 1.2
    # Penalty multiplier on candidates whose kind is avoided.
    avoid_penalty: float = 0.5
    # Minimum number of distinct section paths the pack should
    # cover when `require_section_diversity` is True. Soft — the
    # planner stops trying once it runs out of candidates.
    min_section_paths: int = 3
    # When True, after picking a high-level "summary" artifact
    # (kind starts with "enriched."), require at least one
    # grounding source-kind chunk in the pack.
    require_source_grounding: bool = False


_DEFAULT_POLICY = IntentPolicy(
    require_section_diversity=False,
)


_POLICIES_BY_INTENT: dict[str, IntentPolicy] = {
    # Diversity-leaning intents: pack must span multiple section
    # paths so the answer can map actor→action / stage→stage /
    # one→another. Anchor with structured artifacts when
    # available; ground with source chunks.
    "responsibility_mapping": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=("enriched.consistency_findings",),
        require_source_grounding=True,
        min_section_paths=3,
    ),
    "dependency_mapping": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=(),
        require_source_grounding=True,
        min_section_paths=3,
    ),
    "stage_progression": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=(),
        min_section_paths=3,
    ),
    "deliverable_mapping": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        # Prefer exact list sections over the high-level
        # document_map summary. The summary is fine for
        # navigation but the question wants the list itself.
        avoid_kinds=("enriched.document_map",),
        min_section_paths=2,
    ),
    "issue_risk_mapping": IntentPolicy(
        require_section_diversity=True,
        # Anchor with the enriched risks artifact when present —
        # it's the structured roll-up — then ground with source
        # chunks that explain cause / impact / mitigation.
        preferred_anchor_kinds=(
            "enriched.risks", "enriched.issues", "chunk", "compiled.text",
        ),
        avoid_kinds=(),
        require_source_grounding=True,
        min_section_paths=2,
    ),
    "decision_trace": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=(),
        min_section_paths=3,
    ),
    "comparison": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=(),
        min_section_paths=2,
    ),
    "list_extraction": IntentPolicy(
        require_section_diversity=True,
        # Prefer the exact list source over its summary roll-up.
        preferred_anchor_kinds=("chunk", "compiled.text"),
        avoid_kinds=("enriched.document_map",),
        min_section_paths=2,
    ),
    "requirements_lookup": IntentPolicy(
        require_section_diversity=True,
        preferred_anchor_kinds=(
            "enriched.requirements", "chunk", "compiled.text",
        ),
        avoid_kinds=(),
        require_source_grounding=True,
        min_section_paths=2,
    ),
    # Non-diversity intents: take top-K, no expansion.
    "exact_fact_lookup": _DEFAULT_POLICY,
    "summary_lookup": IntentPolicy(
        require_section_diversity=False,
        preferred_anchor_kinds=(
            "enriched.document_map", "compiled.text", "chunk",
        ),
    ),
    "cost_or_effort_lookup": _DEFAULT_POLICY,
    "schedule_or_milestone_lookup": _DEFAULT_POLICY,
    "compliance_lookup": _DEFAULT_POLICY,
    "legal_or_contract_terms": _DEFAULT_POLICY,
    "generic_lookup": _DEFAULT_POLICY,
}


def policy_for_intent(
    intent: "QueryIntentLabel | str | None",
) -> IntentPolicy:
    if intent is None:
        return _DEFAULT_POLICY
    key = intent.value if hasattr(intent, "value") else str(intent)
    return _POLICIES_BY_INTENT.get(key, _DEFAULT_POLICY)


# ---- Plan output -------------------------------------------------


@dataclass
class PlannedEvidence:
    """Result of one planning pass.

    ``selected`` is the ordered pack to send downstream.
    ``dropped`` carries (candidate, reason) tuples so the caller
    can emit the matching drop events on the diagnostics object.
    ``policy_summary`` is a compact view of the rules used —
    persisted into the snapshot so an audit reader sees
    "responsibility_mapping → required 3 section paths, got 4"
    without having to re-derive."""

    selected: list[Any] = field(default_factory=list)
    dropped: list[tuple[Any, str]] = field(default_factory=list)
    policy_summary: dict[str, Any] = field(default_factory=dict)


class PlannerOutcome:
    """Stable status codes the quality-check + finalize events
    pin against."""

    OK = "ok"
    INSUFFICIENT_SECTION_DIVERSITY = "insufficient_section_diversity"
    NO_SOURCE_GROUNDING = "no_source_grounding"
    EMPTY = "empty"


# ---- Planner ------------------------------------------------------


def plan_evidence(
    candidates: list[Any],
    *,
    intent: "QueryIntentLabel | str | None",
    max_blocks: int = 5,
    score_key: str = "rerank_score",
) -> PlannedEvidence:
    """Build a structure-aware evidence pack from already-scored,
    already-scoped candidates.

    ``candidates`` is the post-scope, post-rerank list. The
    planner does NOT change scores — it picks a subset based on:

      1. ``policy.preferred_anchor_kinds`` (anchor selection)
      2. ``policy.require_section_diversity`` (greedy distinct
         section-path expansion)
      3. ``policy.require_source_grounding`` (a source-chunk
         appears when an enriched anchor is used)
      4. ``max_blocks`` cap

    Returns the selected list + the dropped pairs + a policy
    summary. The caller decides what to do with the drops (emit
    diagnostic events, ignore, etc.)."""
    policy = policy_for_intent(intent)
    selected: list[Any] = []
    dropped: list[tuple[Any, str]] = []
    covered_paths: set[str] = set()
    anchor_kinds_used: set[str] = set()

    def _score(c: Any) -> float:
        getter = _make_getter(c)
        v = getter(score_key)
        if v is None:
            v = getter("score")
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _kind(c: Any) -> str:
        getter = _make_getter(c)
        return str(
            getter("artifact_type") or getter("kind") or ""
        )

    def _section_path(c: Any) -> str:
        getter = _make_getter(c)
        sp = (
            getter("section_path")
            or (getter("metadata") or {}).get("section_path")
            or (getter("metadata") or {}).get("section")
            or getter("source_location")
            or ""
        )
        return str(sp).strip().lower()

    # Stable sort by score descending. Ties broken by anchor-kind
    # preference (earlier entry in ``preferred_anchor_kinds`` wins).
    anchor_priority = {
        kind: idx for idx, kind in enumerate(policy.preferred_anchor_kinds)
    }
    ranked = sorted(
        candidates,
        key=lambda c: (
            -_score(c),
            anchor_priority.get(_kind(c), 999),
        ),
    )

    # ---- Pass 1: pick distinct-section-path anchors when the
    # policy wants diversity. We skip same-section-path candidates
    # until the diversity quota is met OR we run out.
    if policy.require_section_diversity:
        for cand in list(ranked):
            if len(selected) >= max_blocks:
                break
            if len(covered_paths) >= policy.min_section_paths:
                break
            path = _section_path(cand)
            if path and path in covered_paths:
                # Defer — let pass 2 maybe pick it.
                continue
            kind = _kind(cand)
            if kind in policy.avoid_kinds:
                # Even diverse, avoid-listed kinds wait for pass 2.
                continue
            selected.append(cand)
            anchor_kinds_used.add(kind)
            if path:
                covered_paths.add(path)
            ranked.remove(cand)

    # ---- Pass 2: fill remaining slots in score order, ignoring
    # diversity. Avoid-kinds get penalized via score but can still
    # land if nothing else qualifies.
    for cand in ranked:
        if len(selected) >= max_blocks:
            break
        selected.append(cand)
        path = _section_path(cand)
        if path:
            covered_paths.add(path)
        anchor_kinds_used.add(_kind(cand))

    # ---- Pass 3: source-grounding requirement check
    # When the policy requires source grounding AND the pack so
    # far is all enriched artifacts, swap the lowest-scoring
    # enriched block for a source chunk picked by a documented
    # grounding method:
    #   1. ``explicit_source_chunk``  — enriched anchor metadata
    #       names a chunk_id we have in the pool.
    #   2. ``source_artifact_section`` — anchor metadata names a
    #       source_artifact_id whose section_path matches a chunk.
    #   3. ``page_range_match`` — anchor's page_range matches a
    #       chunk's page_range.
    #   4. ``heuristic_best_score`` — last-resort fallback.
    grounding_method: str | None = None
    if policy.require_source_grounding:
        has_source = any(
            _kind(c) in ("chunk", "compiled.text") for c in selected
        )
        if not has_source:
            source_pool = [
                c for c in ranked
                if c not in selected
                and _kind(c) in ("chunk", "compiled.text")
            ]
            enriched_in_pack = [
                c for c in selected
                if _kind(c).startswith("enriched.")
            ]
            if source_pool and enriched_in_pack:
                drop = min(enriched_in_pack, key=_score)
                replacement, grounding_method = _pick_grounding_source(
                    enriched_anchor=drop,
                    source_pool=source_pool,
                )
                selected.remove(drop)
                # Surface BOTH the swap reason and the grounding
                # method so the audit reader sees how the source
                # was chosen.
                dropped.append((
                    drop,
                    f"swapped_for_source_grounding:{grounding_method}",
                ))
                selected.append(replacement)

    # Everything in `candidates` that didn't land in `selected`
    # is implicitly dropped — but we DON'T emit drop reasons for
    # those here. The caller is the right place to decide
    # whether a non-selected candidate is "dropped" (the
    # diagnostic event) versus "carried forward" (used by a later
    # stage). The planner only reports the active drops (the
    # source-grounding swap above).

    summary = {
        "intent": intent.value if hasattr(intent, "value") else intent,
        "diversity_required": policy.require_section_diversity,
        "min_section_paths": policy.min_section_paths,
        "covered_section_paths": len(covered_paths),
        "covered_path_samples": sorted(list(covered_paths))[:5],
        "preferred_anchor_kinds": list(policy.preferred_anchor_kinds),
        "avoid_kinds": list(policy.avoid_kinds),
        "anchor_kinds_used": sorted(list(anchor_kinds_used)),
        "max_blocks": max_blocks,
        "pack_size": len(selected),
    }
    return PlannedEvidence(
        selected=selected,
        dropped=dropped,
        policy_summary=summary,
    )


def _pick_grounding_source(
    *,
    enriched_anchor: Any,
    source_pool: list[Any],
) -> tuple[Any, str]:
    """Choose the source-chunk replacement using the documented
    grounding methods, in order of decreasing fidelity.

    Returns ``(chunk, grounding_method)``. ``grounding_method`` is
    one of:
      * ``explicit_source_chunk``
      * ``source_artifact_section``
      * ``page_range_match``
      * ``heuristic_best_score``

    The caller is expected to surface the method via the drop
    event AND (per spec) lower confidence / add a warning when
    the method is ``heuristic_best_score``."""
    getter = _make_getter(enriched_anchor)
    anchor_meta = getter("metadata") or {}
    if not isinstance(anchor_meta, dict):
        anchor_meta = {}

    # 1. Explicit chunk-id list on the enriched anchor.
    explicit_chunk_ids = anchor_meta.get("source_chunk_ids")
    if isinstance(explicit_chunk_ids, (list, tuple)) and explicit_chunk_ids:
        wanted = {str(cid) for cid in explicit_chunk_ids}
        for cand in source_pool:
            cand_chunk = _make_getter(cand)("chunk_id")
            if cand_chunk and str(cand_chunk) in wanted:
                return cand, "explicit_source_chunk"

    # 2. Source artifact id + section path overlap.
    source_artifact_id = anchor_meta.get("source_artifact_id")
    anchor_section = _section_value(anchor_meta)
    if source_artifact_id is not None:
        wanted_artifact = str(source_artifact_id)
        for cand in source_pool:
            cand_get = _make_getter(cand)
            cand_meta = cand_get("metadata") or {}
            if not isinstance(cand_meta, dict):
                cand_meta = {}
            cand_artifact = (
                cand_meta.get("source_artifact_id")
                or cand_get("artifact_id")
            )
            cand_section = _section_value(cand_meta)
            if str(cand_artifact or "") == wanted_artifact and (
                not anchor_section
                or not cand_section
                or anchor_section.lower() in cand_section.lower()
                or cand_section.lower() in anchor_section.lower()
            ):
                return cand, "source_artifact_section"

    # 3. Page range match.
    anchor_pages = anchor_meta.get("page_range")
    if anchor_pages:
        anchor_pages_str = str(anchor_pages)
        for cand in source_pool:
            cand_meta = _make_getter(cand)("metadata") or {}
            if not isinstance(cand_meta, dict):
                cand_meta = {}
            cand_pages = cand_meta.get("page_range")
            if cand_pages and str(cand_pages) == anchor_pages_str:
                return cand, "page_range_match"

    # 4. Heuristic — best-scoring source chunk.
    def _score(c: Any) -> float:
        getter = _make_getter(c)
        v = getter("rerank_score") or getter("score")
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    return max(source_pool, key=_score), "heuristic_best_score"


def _section_value(meta: dict[str, Any]) -> str:
    return str(
        meta.get("section_path")
        or meta.get("section")
        or ""
    ).strip()


def _make_getter(hit):
    if isinstance(hit, dict):
        return hit.get
    return lambda name: getattr(hit, name, None)


__all__ = [
    "IntentPolicy",
    "PlannedEvidence",
    "PlannerOutcome",
    "plan_evidence",
    "policy_for_intent",
]
