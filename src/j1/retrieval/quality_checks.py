"""Pre-LLM evidence-pack quality checks.

After the planner builds the pack and before the synthesizer
sees it, we run a SHORT list of generic guards. Failures don't
mean the user gets an error — the caller decides what to do.
The recommended flow:

  1. Build pack.
  2. ``check_pack(pack, ...)`` → ``EvidenceCheckResult``.
  3. If ``result.ok`` → send to LLM.
     If not, the caller runs ONE fallback retrieval pass with
     adjusted parameters (e.g. relaxed boilerplate demotion,
     wider candidate pool, alternative scope).
  4. Re-check.
  5. Still failing → return ``insufficient_evidence`` state with
     ``result.failures`` in the response so the FE shows the
     reason instead of synthesising a confident but groundless
     answer.

Checks shipped (all generic):

  evidence_pack_non_empty
      The pack has ≥ 1 candidate.
  evidence_belongs_to_active_scope
      No candidate's owning doc/run is outside the active scope.
      (Belt-and-braces — the scope filter SHOULD have caught
      these already; this check covers any path that built a
      pack without going through ``enforce_active_scope``.)
  no_unrelated_document_evidence
      All non-None document IDs in the pack are the same.
      Independent of "active scope" — fires when a pack mixes
      documents even if the active scope was None.
  no_boilerplate_unless_intent_allows
      No pack candidate matches a boilerplate pattern unless the
      intent is in ``_BOILERPLATE_OK_INTENTS``.
  section_diversity_for_structured_intents
      For diversity-requiring intents, the pack covers at least
      ``min_section_paths`` distinct section paths.
  source_grounding_for_enriched_anchored_packs
      When the pack contains ≥1 enriched artifact, it must
      also contain ≥1 source chunk (chunk / compiled.text).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from j1.retrieval.boilerplate import is_boilerplate_chunk
from j1.retrieval.evidence_planner import policy_for_intent


# Intents where boilerplate-in-the-pack is the right answer.
_BOILERPLATE_OK_INTENTS: frozenset[str] = frozenset({
    "legal_or_contract_terms",
    "compliance_lookup",
})


# Intents where a single-section pack is acceptable — diversity
# becomes a warning, never a hard failure.
_DIVERSITY_SOFT_INTENTS: frozenset[str] = frozenset({
    "list_extraction",
    "exact_fact_lookup",
    "summary_lookup",
    "generic_lookup",
})


@dataclass
class EvidenceCheckResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def check_pack(
    pack: Iterable[Any],
    *,
    intent: Any | None,
    active_document_id: str | None,
    active_run_id: str | None,
    stage_anchors: tuple[str, ...] | None = None,
    min_anchor_coverage: int = 2,
    stage_groups: "Any | None" = None,
    min_stages_covered: int = 3,
) -> EvidenceCheckResult:
    pack_list = list(pack)
    failures: list[str] = []
    details: dict[str, Any] = {
        "pack_size": len(pack_list),
        "intent": _intent_str(intent),
    }

    # 1. non-empty
    if not pack_list:
        failures.append("evidence_pack_non_empty")
        details["pack_size"] = 0
        return EvidenceCheckResult(
            ok=False, failures=failures, details=details,
        )

    # 2. belongs_to_active_scope (defensive)
    if active_document_id is not None:
        out_of_scope = [
            _candidate_summary(c)
            for c in pack_list
            if not _belongs_to_scope(
                c, active_document_id, active_run_id,
            )
        ]
        if out_of_scope:
            failures.append("evidence_belongs_to_active_scope")
            details["out_of_scope_samples"] = out_of_scope[:3]

    # 3. no_unrelated_document_evidence
    doc_ids = {
        d for d in (_owning_document_id(c) for c in pack_list)
        if d is not None
    }
    if len(doc_ids) > 1:
        failures.append("no_unrelated_document_evidence")
        details["document_ids_present"] = sorted(doc_ids)

    # 4. no_boilerplate_unless_intent_allows
    intent_str = _intent_str(intent)
    if intent_str not in _BOILERPLATE_OK_INTENTS:
        boilerplate_hits = [
            _candidate_summary(c)
            for c in pack_list
            if _is_boilerplate(c)
        ]
        if boilerplate_hits:
            failures.append("no_boilerplate_unless_intent_allows")
            details["boilerplate_samples"] = boilerplate_hits[:3]

    # 5. section_diversity_for_structured_intents
    # Intent-aware: not every "diversity-leaning" intent should
    # hard-fail on single-section packs. The spec carves out two
    # exceptions:
    #
    #   * ``list_extraction`` — one exact list section is often
    #     the right answer (an enumeration belongs in ONE place
    #     in the document). Diversity below the policy minimum
    #     is a SOFT WARNING here, not a hard fail.
    #   * ``exact_fact_lookup`` / ``generic_lookup`` /
    #     ``summary_lookup`` — diversity is irrelevant; never
    #     fails on these.
    #
    # Mapping intents (responsibility / dependency / stage /
    # deliverable / issue_risk / decision_trace) still hard-fail
    # below 2 distinct paths because their answer SHAPE is a
    # graph that needs ≥ 2 nodes.
    policy = policy_for_intent(intent)
    if policy.require_section_diversity:
        section_paths = {
            sp.lower()
            for sp in (_section_path(c) for c in pack_list)
            if sp
        }
        details["distinct_section_paths"] = len(section_paths)
        details["min_section_paths"] = policy.min_section_paths
        if len(section_paths) < policy.min_section_paths:
            warnings = details.setdefault("warnings", [])
            warnings.append(
                "section_diversity_for_structured_intents",
            )
        if (
            intent_str not in _DIVERSITY_SOFT_INTENTS
            and len(section_paths) < 2
        ):
            failures.append(
                "section_diversity_for_structured_intents",
            )

    # 7. evidence_anchor_coverage_for_stage_progression
    # Group-based rule: stage-progression questions get
    # FAIL → need ≥3 of the requested stage anchors AND ≥1
    # deliverable-shape match AND ≥1 estimate/class-shape match.
    # When the caller supplies ``stage_anchors`` (legacy code
    # path) but doesn't supply ``stage_groups``, we keep the
    # original flat-count rule for backward compatibility.
    if stage_groups is not None and stage_groups.stages_requested:
        from j1.retrieval.anchors import stage_progression_coverage
        bodies = []
        for c in pack_list:
            txt = (
                _candidate_text(c)
                or _section_path(c)
                or ""
            )
            if txt:
                bodies.append(txt)
        coverage = stage_progression_coverage(
            groups=stage_groups, bodies=bodies,
        )
        details["stage_groups"] = {
            "requested": list(coverage.stages_requested),
            "stage_hits": list(coverage.stage_hits),
            "stages_covered": len(coverage.stage_hits),
            "stages_required": min_stages_covered,
            "deliverable_present": coverage.deliverable_present,
            "estimate_present": coverage.estimate_present,
            "deliverable_hits": list(coverage.deliverable_hits[:3]),
            "estimate_hits": list(coverage.estimate_hits[:3]),
        }
        stages_ok = len(coverage.stage_hits) >= min_stages_covered
        if not (
            stages_ok
            and coverage.deliverable_present
            and coverage.estimate_present
        ):
            failures.append(
                "evidence_anchor_coverage_for_stage_progression",
            )
    elif stage_anchors:
        from j1.retrieval.anchors import pack_anchor_coverage
        bodies = []
        for c in pack_list:
            txt = (
                _candidate_text(c)
                or _section_path(c)
                or ""
            )
            if txt:
                bodies.append(txt)
        matched, covered = pack_anchor_coverage(
            bodies, tuple(stage_anchors),
        )
        details["stage_anchors"] = list(stage_anchors)
        details["stage_anchors_matched"] = list(matched)
        details["stage_anchors_covered"] = covered
        details["stage_anchors_required"] = min_anchor_coverage
        if covered < min_anchor_coverage:
            failures.append(
                "evidence_anchor_coverage_for_stage_progression",
            )

    # 6. source_grounding_for_enriched_anchored_packs
    if policy.require_source_grounding:
        has_enriched = any(
            (_kind(c) or "").startswith("enriched.")
            for c in pack_list
        )
        has_source = any(
            _kind(c) in ("chunk", "compiled.text")
            for c in pack_list
        )
        if has_enriched and not has_source:
            failures.append(
                "source_grounding_for_enriched_anchored_packs",
            )

    return EvidenceCheckResult(
        ok=not failures, failures=failures, details=details,
    )


# ---- Helpers -----------------------------------------------------


def _intent_str(intent: Any) -> str | None:
    if intent is None:
        return None
    return intent.value if hasattr(intent, "value") else str(intent)


def _make_getter(hit):
    if isinstance(hit, dict):
        return hit.get
    return lambda name: getattr(hit, name, None)


def _kind(c: Any) -> str | None:
    getter = _make_getter(c)
    return getter("artifact_type") or getter("kind")


def _section_path(c: Any) -> str:
    getter = _make_getter(c)
    return str(
        getter("section_path")
        or (getter("metadata") or {}).get("section_path")
        or (getter("metadata") or {}).get("section")
        or getter("source_location")
        or ""
    ).strip()


def _owning_document_id(c: Any) -> str | None:
    getter = _make_getter(c)
    meta = getter("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    return (
        meta.get("source_document_id")
        or meta.get("document_id")
        or getter("source_document_id")
        or getter("document_id")
    )


def _owning_run_id(c: Any) -> str | None:
    getter = _make_getter(c)
    meta = getter("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    return (
        meta.get("source_run_id")
        or meta.get("run_id")
        or getter("source_run_id")
        or getter("run_id")
    )


def _belongs_to_scope(
    c: Any,
    active_doc: str | None,
    active_run: str | None,
) -> bool:
    """Defensive scope check used after evidence packing.

    Returns True when:
      * the candidate's owning doc/run ids match the active scope,
        OR
      * the candidate has NO owning doc/run id at all — meaning we
        can't judge from here. Strict scope filtering (which DOES
        reject candidates with no scope metadata) runs BEFORE
        evidence packing, and the projection step from
        ``RetrievedChunkRefDTO`` → ``EvidenceBlockDTO`` drops the
        metadata dict on purpose. Treating the missing-id case as
        "out of scope" would produce a false ``check_pack`` failure
        every time the pack reached the synthesizer through the
        normal path.

    Mismatch (non-None and != active) still returns False so a real
    cross-document leak still surfaces if one ever made it past the
    scope filter."""
    if active_doc is not None:
        d = _owning_document_id(c)
        if d is not None and d != active_doc:
            return False
    if active_run is not None:
        r = _owning_run_id(c)
        if r is not None and r != active_run:
            return False
    return True


def _is_boilerplate(c: Any) -> bool:
    getter = _make_getter(c)
    meta = getter("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    section_path = (
        getter("section_path")
        or meta.get("section_path")
        or getter("source_location")
    )
    heading = meta.get("heading") or getter("title")
    body_preview = (
        getter("body_preview")
        or getter("text")
        or getter("extracted_text")
        or meta.get("body_preview")
    )
    return is_boilerplate_chunk(
        section_path=str(section_path) if section_path else None,
        heading=str(heading) if heading else None,
        body_preview=str(body_preview) if body_preview else None,
    ) is not None


def _candidate_text(c: Any) -> str | None:
    """Best-effort body-text extractor. Tries fields in order:
    ``text`` (EvidenceBlockDTO) → ``body`` → ``preview`` →
    ``extracted_text``. Returns None when nothing is available."""
    getter = _make_getter(c)
    for k in ("text", "body", "preview", "extracted_text"):
        v = getter(k)
        if v:
            return str(v)
    return None


def _candidate_summary(c: Any) -> dict[str, Any]:
    getter = _make_getter(c)
    return {
        "artifact_id": getter("artifact_id"),
        "artifact_type": _kind(c),
        "document_id": _owning_document_id(c),
        "section_path": _section_path(c) or None,
    }


__all__ = [
    "EvidenceCheckResult",
    "check_pack",
]
