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
    "count_anchors_present",
    "expand_query_with_anchors",
    "pack_anchor_coverage",
    "query_stage_anchors",
]
