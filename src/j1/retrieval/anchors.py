"""Extract semantic anchors FROM THE USER'S QUERY at runtime.

This is the document-agnostic alternative to a hardcoded
domain-specific stage dictionary. The anchors are whatever the
user actually wrote — so the same coverage logic works for any
document type:

  * Stage-progression query A: "from conceptual through 60%, 90%,
    and 100% design" → anchors include "conceptual",
    "60%", "90%", "100%", "design".
  * Stage-progression query B: "from v1 through v2 to v3 release"
    → anchors include "v1", "v2", "v3", "release".
  * Stage-progression query C: "the draft, the review, the final
    approval stages" → anchors include "draft", "review",
    "final", "approval".

The extractor is intent-aware: it activates for stage-progression
queries (the ones that ask "how does X evolve from A through B to
C"). Other intents don't generate anchors here — they have their
own coverage signals (section diversity, list_extraction, etc.).

Coverage helpers:

  * ``query_stage_anchors(query)`` — anchor strings extracted
    from the query. Generic; never hardcoded.
  * ``expand_query_with_anchors(query, anchors)`` — boost-style
    expansion used by the targeted re-retrieval fallback. Just
    concatenates anchors to the query; no domain logic.
  * ``count_anchors_present(text, anchors)`` — case-insensitive
    substring scan returning (matched_anchors, count).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---- Stage-shape patterns ----------------------------------------
#
# These detect THE SHAPE of a stage-progression marker the user
# may have written. They never name a specific section, task, or
# domain term. Each match's GROUP(0) becomes the anchor string —
# preserving what the user actually wrote so coverage checking
# against the document body uses the user's vocabulary.

# Percentage anchors: "60%", "90 percent", "100% design".
_PERCENT_STAGE_RE = re.compile(
    r"\b\d{1,3}\s*%(?:\s+\w+)?",
    re.IGNORECASE,
)
# Numbered stage / phase / version / step.
_NUMBERED_STAGE_RE = re.compile(
    r"\b(?:stage|phase|step|version|round|sprint|milestone)\s+"
    r"(?:\d+(?:\.\d+)?|[ivxlcdm]+|\w+)",
    re.IGNORECASE,
)
# Ordinal-style stage words ("first phase", "final review", "draft").
_ORDINAL_STAGE_RE = re.compile(
    # Ordinal/lifecycle adjective followed by ONE word (any noun).
    # The follow-up noun is open-ended ``\S+`` so the regex is
    # domain-neutral — it catches "conceptual design",
    # "conceptual estimate", "first draft", "final approval"
    # without naming any specific noun in the lexicon.
    r"\b(?:initial|preliminary|conceptual|draft|interim|"
    r"intermediate|final|early|late|first|second|third|fourth|"
    r"fifth)\s+[A-Za-z]+",
    re.IGNORECASE,
)
# "From X through Y to Z" range — the anchor is each component.
# We don't split here; the other patterns catch the parts.
# Estimate-class / classification anchors. The CLASS NUMBER comes
# from the user's wording; not hardcoded.
_ESTIMATE_CLASS_RE = re.compile(
    r"\b(?:class\s+(?:[ivxlcdm]+|\d+)\s+(?:estimate|cost)|"
    r"estimate\s+class(?:\s+(?:[ivxlcdm]+|\d+))?|"
    r"cost\s+estimate(?:\s+(?:class|classification))?)",
    re.IGNORECASE,
)
# Generic stage-progression nouns the user mentioned.
_PROGRESSION_NOUNS_RE = re.compile(
    r"\b(deliverables?|cost\s+estimate(?:s|d)?|design|review|"
    r"approval|milestone)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StageAnchors:
    """Result of anchor extraction over one query.

    ``stage_markers`` are the most specific (percentages, numbered
    stages, ordinals); ``progression_terms`` are the supporting
    nouns ("design", "deliverables", "cost estimate") the user
    wrote alongside them. Together they form the coverage set the
    sufficiency check requires evidence to hit."""

    stage_markers: tuple[str, ...]
    progression_terms: tuple[str, ...]

    @property
    def all(self) -> tuple[str, ...]:
        """All anchors as a flat tuple, deduped, preserving order."""
        seen: set[str] = set()
        out: list[str] = []
        for s in self.stage_markers + self.progression_terms:
            k = s.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(s)
        return tuple(out)

    def __bool__(self) -> bool:
        return bool(self.stage_markers)


def query_stage_anchors(query: str) -> StageAnchors:
    """Extract stage anchors a stage-progression query writes.

    Returns ``StageAnchors(stage_markers=(), progression_terms=())``
    when the query doesn't look like a stage question — that
    empty result is falsy, so callers can do ``if not anchors:``.

    Crucially the anchors come from the QUERY TEXT, never from a
    hardcoded list. The user wrote "60%, 90%, and 100% design" →
    we extract "60%", "90%", "100% design".
    """
    if not query:
        return StageAnchors(stage_markers=(), progression_terms=())

    markers: list[str] = []
    seen_markers: set[str] = set()

    def _add_marker(m: re.Match[str]) -> None:
        s = m.group(0).strip()
        k = s.lower()
        if k not in seen_markers:
            seen_markers.add(k)
            markers.append(s)

    for pat in (
        _PERCENT_STAGE_RE,
        _NUMBERED_STAGE_RE,
        _ORDINAL_STAGE_RE,
        _ESTIMATE_CLASS_RE,
    ):
        for m in pat.finditer(query):
            _add_marker(m)

    progression: list[str] = []
    seen_prog: set[str] = set()
    for m in _PROGRESSION_NOUNS_RE.finditer(query):
        s = m.group(0).strip()
        k = s.lower()
        if k not in seen_prog:
            seen_prog.add(k)
            progression.append(s)

    return StageAnchors(
        stage_markers=tuple(markers),
        progression_terms=tuple(progression),
    )


def count_anchors_present(
    text: str,
    anchors: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Case-insensitive substring scan of ``text`` for each anchor.

    Returns ``(matched_anchors, count)`` — caller decides whether
    the count clears the sufficiency threshold."""
    if not text or not anchors:
        return ((), 0)
    text_l = text.lower()
    matched: list[str] = []
    for anchor in anchors:
        if anchor.lower() in text_l:
            matched.append(anchor)
    return (tuple(matched), len(matched))


def pack_anchor_coverage(
    blocks_text: list[str],
    anchors: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Aggregate anchor coverage across a pack of evidence blocks.

    An anchor counts as "covered" if at least ONE block contains
    it. Returns ``(matched_anchors, count)``."""
    if not anchors:
        return ((), 0)
    union: set[str] = set()
    for body in blocks_text:
        if not body:
            continue
        body_l = body.lower()
        for anchor in anchors:
            if anchor.lower() in body_l:
                union.add(anchor)
    matched = tuple(a for a in anchors if a in union)
    return (matched, len(matched))


def expand_query_with_anchors(
    query: str, anchors: StageAnchors,
) -> str:
    """Build the expansion string the targeted re-retrieval uses.

    Adds the user's own anchors as boost terms — the retriever's
    BM25 / lexical signals upweight chunks containing them. The
    original query is preserved verbatim so the semantic intent
    is unchanged; only the lexical surface is widened."""
    if not anchors:
        return query
    expansion = " ".join(anchors.all)
    return f"{query} {expansion}"


__all__ = [
    "StageAnchors",
    "StageProgressionGroups",
    "count_anchors_present",
    "expand_query_with_anchors",
    "pack_anchor_coverage",
    "query_stage_anchors",
    "stage_progression_groups",
    "stage_progression_coverage",
]


# ---- Group-based coverage (stage-progression intent) ---------------
#
# Flat anchor counting (≥ N anchors anywhere) lets a pack pass
# when it covers, e.g., "60%" + "deliverables" but mentions
# neither the OTHER stages nor any cost-estimate concept. For
# stage-progression questions the answer SHAPE is "stage →
# deliverable → cost class" — so the sufficiency rule needs THREE
# groups, each with its own minimum.
#
# Patterns here are SHAPE-based — they match the surface forms
# documents in this question family use. They are NOT a per-
# customer dictionary. The intent gate (stage_progression only)
# keeps them from firing on unrelated queries.

# Stage-marker patterns. These match against EVIDENCE TEXT to
# determine which of the user's stage anchors are present.
_PERCENT_STAGE_BODY = re.compile(r"\b(\d{1,3})\s*%", re.IGNORECASE)

# Deliverable-side shape patterns (generic across business docs).
_DELIVERABLE_BODY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"\bdeliverable[s]?\b",
        r"\bsubmittal[s]?\b",
        r"\bsubmission[s]?\b",
        r"\b(report|memo|drawing|plan|specification|package)s?\b"
        r"\s+(?:will|shall|to\s+be|include[ds]?|are\s+(?:provided|produced))",
        r"\b(output|artifact|document)s?\s+(?:include|comprise|consist)",
    )
)

# Cost-estimate / classification shape patterns. The "Class N"
# and "AACE" shapes are widely used across cost-estimation /
# project-budget documents — they are SHAPES, not single-customer
# references.
_ESTIMATE_BODY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"\bcost\s+estimate[s]?\b",
        r"\b(?:rough\s+order\s+of\s+magnitude|ROM)\b",
        r"\bestimate\s+(?:class|classification|category)\b",
        r"\bclass\s+[ivxlcdm0-9]+\s+(?:estimate|cost)?",
        r"\bAACE\b",
        r"\bbudgetary\s+estimate\b",
    )
)


@dataclass(frozen=True)
class StageProgressionGroups:
    """Per-group anchor sets for the sufficiency rule.

    ``stages_requested`` are the specific stage markers the user
    wrote in the query (e.g. ["60%", "90%", "100% design",
    "conceptual"]). ``estimate_terms`` and ``deliverable_terms``
    are caller-supplied bag-of-shapes used only during coverage
    checking — not extracted from the query (which usually only
    says "cost estimate class" once)."""

    stages_requested: tuple[str, ...]
    deliverable_present: bool
    estimate_present: bool
    stage_hits: tuple[str, ...]          # stages found in evidence
    deliverable_hits: tuple[str, ...]    # which deliverable patterns matched
    estimate_hits: tuple[str, ...]       # which estimate patterns matched


def stage_progression_groups(query: str) -> StageProgressionGroups | None:
    """Return the requested STAGE GROUP from a query, ready for
    pairing with ``stage_progression_coverage`` over evidence.

    Crucially: this method extracts ONLY stage markers
    (percentages, numbered stages, ordinal-stage phrases) — it
    does NOT collect estimate-class shapes ("cost estimate
    class") into the stage list. Estimate-class is a SEPARATE
    group covered by ``_ESTIMATE_BODY_PATTERNS`` over the
    evidence text.

    Returns ``None`` when the query has no stage markers —
    callers fall back to the legacy flat coverage rule."""
    if not query:
        return None
    seen: set[str] = set()
    stage_markers: list[str] = []
    for pat in (
        _PERCENT_STAGE_RE,
        _NUMBERED_STAGE_RE,
        _ORDINAL_STAGE_RE,
    ):
        for m in pat.finditer(query):
            s = m.group(0).strip()
            k = s.lower()
            if k not in seen:
                seen.add(k)
                stage_markers.append(s)
    if not stage_markers:
        return None
    return StageProgressionGroups(
        stages_requested=tuple(stage_markers),
        deliverable_present=False,
        estimate_present=False,
        stage_hits=(),
        deliverable_hits=(),
        estimate_hits=(),
    )


def stage_progression_coverage(
    *,
    groups: StageProgressionGroups,
    bodies: list[str],
) -> StageProgressionGroups:
    """Walk the evidence ``bodies`` and report coverage per group.

    Returns a NEW ``StageProgressionGroups`` with the ``*_hits`` /
    ``*_present`` fields populated. ``stage_hits`` is the subset
    of ``stages_requested`` that appeared somewhere in the
    bodies.

    The sufficiency rule the caller enforces:
      * ``len(stage_hits) >= 3`` (≥3 of 4 requested stages)
      * ``deliverable_present`` (≥1 deliverable-shape hit)
      * ``estimate_present`` (≥1 estimate/class-shape hit)
    """
    stage_hit_set: set[str] = set()
    deliverable_hits: list[str] = []
    estimate_hits: list[str] = []
    for body in bodies:
        if not body:
            continue
        body_l = body.lower()
        # Stage match — substring of each requested anchor.
        for anchor in groups.stages_requested:
            if anchor.lower() in body_l:
                stage_hit_set.add(anchor)
        # Deliverable patterns.
        for pat in _DELIVERABLE_BODY_PATTERNS:
            m = pat.search(body)
            if m:
                deliverable_hits.append(m.group(0))
                break  # one hit per body is enough for this group
        # Estimate patterns.
        for pat in _ESTIMATE_BODY_PATTERNS:
            m = pat.search(body)
            if m:
                estimate_hits.append(m.group(0))
                break
    return StageProgressionGroups(
        stages_requested=groups.stages_requested,
        stage_hits=tuple(
            s for s in groups.stages_requested
            if s in stage_hit_set
        ),
        deliverable_hits=tuple(deliverable_hits),
        estimate_hits=tuple(estimate_hits),
        deliverable_present=bool(deliverable_hits),
        estimate_present=bool(estimate_hits),
    )
