"""General-purpose retrieval reranker for the validation/manual-query
path.

The retriever returns a candidate set ordered by its own ranking
signal (BM25, vector similarity, or RAGAnything's internal score).
Operators reported that this ordering, when fed straight to the
LLM, can drop relevant evidence that's a few ranks deep — and that
the historical fix of "just raise topK" papers over the problem
instead of solving it. This module addresses the underlying
ranking quality by adding a second, evidence-aware pass before the
context budget is applied.

Pipeline shape:

    raw retrieval (top-K, K configurable for recall)
       ↓
    reranker (this module)
       ↓
    evidence selection by COVERAGE (this module)
       ↓
    LLM synthesizer

The reranker is general — no document names, no question text, no
domain values, no domain-specific logic. It composes a final
score from several explainable signals:

  * source_trust         — source-grounded > derived for fact
                           lookups; flipped for interpretive intents.
  * lexical_coverage     — fraction of query terms found in body.
  * phrase_match         — exact quoted-phrase hits.
  * numeric_unit         — number/unit overlap (boosted on
                           numeric_lookup intent).
  * structural           — table-row / key-value / bullet hits when
                           the query is field/value-shaped.
  * section_match        — query terms appear in the chunk's
                           section heading.
  * intent_compatibility — kind fits intent (e.g. document_map
                           preferred for summary queries).
  * interpretive_penalty — derived/inferred kinds penalised for
                           strictly factual lookups.

Selection is greedy-by-coverage: pick the top-scoring candidate
first, then pick subsequent candidates that cover query aspects
NOT yet covered. Caps at ``evidence_max_blocks`` AND the existing
character budget — same isolation contracts hold.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


_log = logging.getLogger("j1.validation.rerank")


# ---- Query intent classifier -------------------------------------


class QueryIntent(str, Enum):
    """Coarse intents detected from a natural-language query.

    Multiple intents can apply to one query (e.g. a numeric fact
    lookup is both ``FACT_LOOKUP`` and ``NUMERIC_LOOKUP``). The
    detector returns a frozenset.
    """

    FACT_LOOKUP = "fact_lookup"
    NUMERIC_LOOKUP = "numeric_lookup"
    TABLE_OR_FIELD_LOOKUP = "table_or_field_lookup"
    COMPOUND_LOOKUP = "compound_lookup"
    SECTION_LOOKUP = "section_lookup"
    INTERPRETIVE_QUERY = "interpretive_query"
    SUMMARY_QUERY = "summary_query"


_FACT_LOOKUP_HINTS: tuple[str, ...] = (
    "what is", "what are", "what does", "who is", "who are",
    "when is", "when was", "when does", "when did",
    "where is", "where are", "which", "identify", "list",
    "name the", "tell me",
)
_NUMERIC_LOOKUP_HINTS: tuple[str, ...] = (
    "value", "number", "threshold", "amount", "count",
    "date", "rate", "percentage", "percent",
    "dimension", "measurement", "duration", "size",
    "how many", "how much", "how long", "how far",
    "limit", "maximum", "minimum",
)
_COMPOUND_HINTS: tuple[str, ...] = (
    " and ", ", and ", "; ",
)
_TABLE_FIELD_HINTS: tuple[str, ...] = (
    "field", "row", "column", "table",
    "label", "key", "header",
)
_INTERPRETIVE_HINTS: tuple[str, ...] = (
    "why", "explain", "assess", "compare", "evaluate",
    "impact", "implication", "consequence",
    "recommendation", "risk", "advice", "should",
)
_SUMMARY_HINTS: tuple[str, ...] = (
    "summarize", "summary", "overview", "main point",
    "key point", "key information", "outline",
    "describe in general", "what is this about",
    # Covers "What is this document about?" / "What is this all
    # about?" — the substring trick keeps the matcher simple
    # without a regex.
    "what is this",
)
_SECTION_HINTS: tuple[str, ...] = (
    "section", "chapter", "paragraph", "subsection",
)

# Number-like tokens used to detect implicit numeric intent ("what
# was 20 May 2026?"). Catches digits + common date / unit patterns.
_NUMBER_RE = re.compile(r"\b\d[\d,\.]*\b")
# Quoted phrases the user explicitly wants matched verbatim.
_QUOTED_PHRASE_RE = re.compile(r'"([^"]+)"')


def detect_intents(query: str) -> frozenset[QueryIntent]:
    """Deterministic, lightweight intent classifier.

    Pure string matching on the lowercased query. No LLM round
    trip, no document/domain coupling. Multiple intents can fire
    for one query — the reranker treats them as additive signals.

    Falls back to ``FACT_LOOKUP`` when no hint matches so the
    reranker never operates with an empty intent set.
    """
    if not query:
        return frozenset({QueryIntent.FACT_LOOKUP})
    q = query.lower()
    intents: set[QueryIntent] = set()
    if any(h in q for h in _FACT_LOOKUP_HINTS):
        intents.add(QueryIntent.FACT_LOOKUP)
    if any(h in q for h in _NUMERIC_LOOKUP_HINTS) or _NUMBER_RE.search(query):
        intents.add(QueryIntent.NUMERIC_LOOKUP)
    # Compound: explicit conjunction OR multiple commas in the query.
    if (
        any(h in q for h in _COMPOUND_HINTS)
        or q.count(",") >= 2
    ):
        intents.add(QueryIntent.COMPOUND_LOOKUP)
    if any(h in q for h in _TABLE_FIELD_HINTS):
        intents.add(QueryIntent.TABLE_OR_FIELD_LOOKUP)
    if any(h in q for h in _INTERPRETIVE_HINTS):
        intents.add(QueryIntent.INTERPRETIVE_QUERY)
    if any(h in q for h in _SUMMARY_HINTS):
        intents.add(QueryIntent.SUMMARY_QUERY)
    if any(h in q for h in _SECTION_HINTS):
        intents.add(QueryIntent.SECTION_LOOKUP)
    if not intents:
        intents.add(QueryIntent.FACT_LOOKUP)
    return frozenset(intents)


# ---- Query-term extraction --------------------------------------


# Generic English stopwords. Kept minimal and lowercase — anything
# the user might genuinely want to search for (like "max", "min",
# "page") is NOT in this list.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "but", "by",
    "did", "do", "does", "for", "from",
    "had", "has", "have", "he", "her", "here", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "me", "my", "no", "not", "of", "on", "or",
    "she", "so", "some", "such", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "to",
    "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "whom", "why", "will", "with", "would",
    "you", "your",
})

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/.]*")


def extract_query_terms(query: str) -> list[str]:
    """Lowercased non-stopword tokens. Preserves identifiers,
    hyphenated terms, and tokens with dots / slashes (URLs, file
    paths, dotted identifiers like "v1.0")."""
    if not query:
        return []
    terms: list[str] = []
    for raw in _TOKEN_RE.findall(query):
        t = raw.lower()
        if len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        terms.append(t)
    return terms


def extract_query_phrases(query: str) -> list[str]:
    """Verbatim quoted phrases the user wants matched whole."""
    if not query:
        return []
    return [p.lower() for p in _QUOTED_PHRASE_RE.findall(query)]


def extract_query_numbers(query: str) -> list[str]:
    """Numeric tokens (digits, optionally with comma / dot
    separators). Returned as raw strings so callers can match
    "2026" against "May 2026" without parsing dates."""
    if not query:
        return []
    return _NUMBER_RE.findall(query)


# ---- Source-trust catalogue -------------------------------------


# Per-kind trust scores. Tuned so source-grounded text dominates
# derived/interpretive content for factual lookups — but every
# kind still has SOME positive weight so it stays in play as
# coverage filler when the high-trust slots are exhausted.
_SOURCE_TRUST_BY_KIND: dict[str, float] = {
    # High trust: raw / compiled source.
    "chunk": 5.0,
    "compiled.text": 4.0,
    "parsed_content_manifest": 3.0,
    # Medium trust: structured extractions of the source.
    "enriched.document_map": 2.5,
    "enriched.requirements": 2.0,
    "enriched.source_map": 1.5,
    # Lower trust: interpretive / inferred analyses.
    "enriched.risks": 1.0,
    "enriched.consistency_findings": 0.5,
    "enriched.confidence_assessment": 0.5,
}
_DEFAULT_SOURCE_TRUST = 1.0

_INTERPRETIVE_KINDS: frozenset[str] = frozenset({
    "enriched.consistency_findings",
    "enriched.confidence_assessment",
    "enriched.risks",
})
_SOURCE_GROUNDED_KINDS: frozenset[str] = frozenset({
    "chunk",
    "compiled.text",
    "parsed_content_manifest",
})


# ---- Score model + config ---------------------------------------


@dataclass(frozen=True)
class CandidateScore:
    """Decomposed score for one candidate. ``total`` is the sum;
    each component is kept on the dataclass so logs / tests can
    inspect why a candidate ranked where it did."""

    raw_score: float = 0.0
    source_trust: float = 0.0
    lexical_coverage: float = 0.0
    phrase_match: float = 0.0
    numeric_unit: float = 0.0
    structural: float = 0.0
    section_match: float = 0.0
    intent_compatibility: float = 0.0
    interpretive_penalty: float = 0.0
    duplicate_penalty: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.raw_score
            + self.source_trust
            + self.lexical_coverage
            + self.phrase_match
            + self.numeric_unit
            + self.structural
            + self.section_match
            + self.intent_compatibility
            - self.interpretive_penalty
            - self.duplicate_penalty
        )


@dataclass(frozen=True)
class RerankConfig:
    """Operator-tunable toggles for the reranker. Every signal can
    be disabled independently so the same code path serves both
    the production manual-query surface AND batch validation runs
    (where deterministic, single-signal scoring may be preferred)."""

    enabled: bool = True
    enable_source_trust: bool = True
    enable_lexical_coverage: bool = True
    enable_phrase_match: bool = True
    enable_numeric_unit: bool = True
    enable_structural: bool = True
    enable_section_match: bool = True
    enable_intent_compatibility: bool = True
    enable_interpretive_penalty: bool = True
    enable_coverage_selection: bool = True
    candidate_top_k: int = 20
    evidence_max_blocks: int = 5
    # Used by the coverage selector to detect near-duplicates.
    dedup_prefix_len: int = 200


# ---- Structural pattern detectors -------------------------------


# "Label: value" or "Label - value" lines. Catches both
# colon-terminated keys and dash-separated key/value pairs. We
# require the line to start with a Word / Capitalized word so we
# don't flag e.g. "due: 20" inside running prose.
_KV_LINE_RE = re.compile(
    r"^\s*[A-Z][A-Za-z0-9 _\-]{1,40}\s*[:\-]\s*\S",
    re.MULTILINE,
)
# Table-like content: pipe-separated rows.
_PIPE_TABLE_RE = re.compile(r"\|[^|\n]+\|[^|\n]+\|")
# Bullet / numbered list lines.
_BULLET_RE = re.compile(r"^\s*[-*•]\s+\S", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+[\.)]\s+\S", re.MULTILINE)


# ---- Scoring ----------------------------------------------------


def score_candidate(
    *,
    artifact_kind: str,
    body_text: str,
    raw_score: float,
    query_terms: list[str],
    query_phrases: list[str],
    query_numbers: list[str],
    intents: frozenset[QueryIntent],
    section: str | None = None,
    config: RerankConfig | None = None,
) -> CandidateScore:
    """Compose a per-candidate score from the configured signals.

    Pure function — no IO, no LLM calls. Deterministic per
    (candidate, query, intent) triple so the test suite can pin
    individual signals.
    """
    config = config or RerankConfig()
    kind = (artifact_kind or "").strip()
    body = body_text or ""
    body_l = body.lower()
    # Lowercased token set for fast lexical lookups.
    body_tokens = set(_TOKEN_RE.findall(body_l))

    source_trust = 0.0
    if config.enable_source_trust:
        source_trust = _SOURCE_TRUST_BY_KIND.get(
            kind, _DEFAULT_SOURCE_TRUST,
        )

    lexical_coverage = 0.0
    if config.enable_lexical_coverage and query_terms:
        matched = sum(1 for t in query_terms if t in body_tokens)
        # 0..3.0 scaled by fraction matched.
        lexical_coverage = (matched / len(query_terms)) * 3.0
        # Proximity bonus: if multiple query terms appear within a
        # 120-char window, add a small boost. Approximates "the
        # answer is together in the text" without a full
        # nearest-neighbour calculation.
        if matched >= 2:
            for t in query_terms:
                idx = body_l.find(t)
                if idx < 0:
                    continue
                # Count other query terms within the window.
                window = body_l[max(0, idx - 60): idx + 60 + len(t)]
                neighbours = sum(
                    1 for other in query_terms
                    if other != t and other in window
                )
                if neighbours:
                    lexical_coverage += 0.5
                    break

    phrase_match = 0.0
    if config.enable_phrase_match and query_phrases:
        phrase_match = sum(
            2.0 for p in query_phrases if p in body_l
        )

    numeric_unit = 0.0
    if config.enable_numeric_unit and query_numbers:
        matched_nums = sum(1 for n in query_numbers if n in body)
        numeric_unit = matched_nums * 1.5
        if QueryIntent.NUMERIC_LOOKUP in intents:
            # Boost when numeric intent is the dominant signal.
            numeric_unit *= 2.0
            # Extra boost if a number is co-located with a query
            # term — "rate of 5%" beats "5% (unrelated)".
            for n in query_numbers:
                idx = body.find(n)
                if idx < 0:
                    continue
                window = body_l[max(0, idx - 60): idx + 60 + len(n)]
                if any(t in window for t in query_terms):
                    numeric_unit += 1.0
                    break

    structural = 0.0
    if config.enable_structural and body:
        wants_structure = (
            QueryIntent.TABLE_OR_FIELD_LOOKUP in intents
            or QueryIntent.NUMERIC_LOOKUP in intents
        )
        if wants_structure:
            if _KV_LINE_RE.search(body):
                structural += 1.5
            if _PIPE_TABLE_RE.search(body):
                structural += 1.5
            if _BULLET_RE.search(body) or _NUMBERED_RE.search(body):
                structural += 0.5

    section_match = 0.0
    if config.enable_section_match and section and query_terms:
        section_l = section.lower()
        section_hits = sum(1 for t in query_terms if t in section_l)
        if section_hits:
            # Hits in a section heading are strong signals — the
            # author labelled the content this way.
            section_match = min(2.5, section_hits * 1.0)

    intent_compatibility = 0.0
    if config.enable_intent_compatibility:
        # Summary queries → prefer document_map / compiled.text.
        if QueryIntent.SUMMARY_QUERY in intents:
            if kind == "enriched.document_map":
                intent_compatibility += 2.0
            elif kind == "compiled.text":
                intent_compatibility += 1.0
        # Interpretive queries → derived artifacts get a small
        # bonus (offsets some of the penalty they take for
        # factual lookups).
        if QueryIntent.INTERPRETIVE_QUERY in intents:
            if kind in _INTERPRETIVE_KINDS:
                intent_compatibility += 1.5
        # Table/field queries → manifests + structured maps win.
        if QueryIntent.TABLE_OR_FIELD_LOOKUP in intents:
            if kind == "parsed_content_manifest":
                intent_compatibility += 1.5

    interpretive_penalty = 0.0
    if config.enable_interpretive_penalty:
        # Strictly-factual lookups: penalise interpretive/derived
        # artifacts so source-grounded text wins.
        is_strictly_factual = (
            (
                QueryIntent.FACT_LOOKUP in intents
                or QueryIntent.NUMERIC_LOOKUP in intents
                or QueryIntent.TABLE_OR_FIELD_LOOKUP in intents
            )
            and QueryIntent.INTERPRETIVE_QUERY not in intents
            and QueryIntent.SUMMARY_QUERY not in intents
        )
        if is_strictly_factual and kind in _INTERPRETIVE_KINDS:
            interpretive_penalty = 2.0

    return CandidateScore(
        raw_score=float(raw_score or 0.0),
        source_trust=source_trust,
        lexical_coverage=lexical_coverage,
        phrase_match=phrase_match,
        numeric_unit=numeric_unit,
        structural=structural,
        section_match=section_match,
        intent_compatibility=intent_compatibility,
        interpretive_penalty=interpretive_penalty,
    )


# ---- Coverage-based evidence selection --------------------------


@dataclass
class _ScoredCandidate:
    """Internal container for the selection loop."""
    index: int  # original retrieval position (for tiebreaks)
    body: str
    score: CandidateScore
    payload: Any  # opaque — the caller's hit/draft/whatever


def select_by_coverage(
    candidates: list[_ScoredCandidate],
    *,
    query_terms: list[str],
    max_blocks: int,
    budget_chars: int,
    dedup_prefix_len: int = 200,
) -> list[_ScoredCandidate]:
    """Greedy selection: top-scored candidate first, then iterate
    picking the candidate whose body covers the most still-
    uncovered query terms (tiebreak by score, then original
    retrieval order).

    Stops when:
      * ``max_blocks`` selected,
      * ``budget_chars`` exhausted,
      * no more candidates,
      * remaining candidates all add zero coverage AND nothing
        new to fill — protects against duplicate-ish blocks
        crowding out a genuinely-different complement candidate.
    """
    if not candidates:
        return []
    # Initial ordering: by total score desc, retrieval position
    # asc (stable tiebreak).
    sorted_pool = sorted(
        candidates,
        key=lambda c: (-c.score.total, c.index),
    )

    selected: list[_ScoredCandidate] = []
    covered_terms: set[str] = set()
    used_chars = 0
    seen_prefixes: set[str] = set()
    query_term_set = set(query_terms)

    def _terms_covered_by(body: str) -> set[str]:
        body_l = body.lower()
        body_tokens = set(_TOKEN_RE.findall(body_l))
        return {t for t in query_term_set if t in body_tokens}

    # Step 1: take the highest-scoring candidate as the seed (always).
    seed = sorted_pool[0]
    selected.append(seed)
    covered_terms |= _terms_covered_by(seed.body)
    seen_prefixes.add(seed.body[:dedup_prefix_len])
    used_chars += len(seed.body)
    sorted_pool = sorted_pool[1:]

    # Step 2: greedy coverage — pick the candidate that adds the
    # most uncovered terms; tiebreak by total score; final tiebreak
    # by original retrieval position. Reject duplicates.
    while sorted_pool and len(selected) < max_blocks and used_chars < budget_chars:
        best: _ScoredCandidate | None = None
        best_new = -1
        best_score = -float("inf")
        best_idx = -1
        for i, c in enumerate(sorted_pool):
            prefix = c.body[:dedup_prefix_len]
            if prefix in seen_prefixes:
                continue
            if used_chars + len(c.body) > budget_chars:
                # Don't drop it yet — a later iteration with a
                # shorter window might fit. But this candidate
                # can't be picked NOW.
                continue
            new_terms = _terms_covered_by(c.body) - covered_terms
            new_count = len(new_terms)
            if (
                new_count > best_new
                or (new_count == best_new and c.score.total > best_score)
            ):
                best = c
                best_new = new_count
                best_score = c.score.total
                best_idx = i
        if best is None:
            break
        # When the best remaining candidate adds zero NEW coverage
        # AND we already have at least one block, stop — adding
        # near-duplicates only burns budget. The synthesizer's
        # job is to ground claims, not to read 5 similar paragraphs.
        if best_new == 0 and len(selected) >= 1:
            break
        selected.append(best)
        covered_terms |= _terms_covered_by(best.body)
        seen_prefixes.add(best.body[:dedup_prefix_len])
        used_chars += len(best.body)
        sorted_pool.pop(best_idx)

    return selected


# ---- Public entry ------------------------------------------------


def rerank_and_select(
    *,
    bodies: list[tuple[Any, str]],  # (payload, body_text)
    raw_scores: list[float] | None,
    sections: list[str | None] | None,
    artifact_kinds: list[str],
    query: str,
    config: RerankConfig | None = None,
    intents: frozenset[QueryIntent] | None = None,
) -> tuple[list[Any], list[CandidateScore], frozenset[QueryIntent], list[str]]:
    """Score the candidates, then select by coverage. Returns the
    selected payloads (in order), their scores, the detected
    intents, and the extracted query terms — the caller uses the
    intents + terms for debug logging.

    ``bodies`` is a list of ``(payload, body_text)`` pairs. The
    payload is opaque to this module; the caller knows what it
    means (a retrieved hit, an EvidenceBlock, etc.). The body is
    the text used for scoring. Empty bodies are filtered out
    upstream — passing them through here is harmless (they just
    won't match any term).

    ``raw_scores`` / ``sections`` are aligned with ``bodies``.
    """
    config = config or RerankConfig()
    if intents is None:
        intents = detect_intents(query)
    query_terms = extract_query_terms(query)
    query_phrases = extract_query_phrases(query)
    query_numbers = extract_query_numbers(query)

    scored: list[_ScoredCandidate] = []
    for i, (payload, body) in enumerate(bodies):
        raw = (
            float(raw_scores[i]) if raw_scores and i < len(raw_scores)
            else 0.0
        )
        section = sections[i] if sections and i < len(sections) else None
        kind = artifact_kinds[i] if i < len(artifact_kinds) else ""
        score = score_candidate(
            artifact_kind=kind,
            body_text=body or "",
            raw_score=raw,
            query_terms=query_terms,
            query_phrases=query_phrases,
            query_numbers=query_numbers,
            intents=intents,
            section=section,
            config=config,
        )
        scored.append(_ScoredCandidate(
            index=i, body=body or "", score=score, payload=payload,
        ))

    if not config.enable_coverage_selection:
        # Pure-score path: sort by total descending and cap.
        scored.sort(key=lambda c: (-c.score.total, c.index))
        capped = scored[: config.evidence_max_blocks]
        return (
            [c.payload for c in capped],
            [c.score for c in capped],
            intents,
            query_terms,
        )

    # Coverage-based path. Budget enforced inside select_by_coverage.
    # ``budget_chars`` is intentionally very large here — the
    # caller's evidence builder applies the real prompt budget
    # downstream. Selection at this layer is for ranking + coverage
    # diversity, not budgeting.
    selected = select_by_coverage(
        scored,
        query_terms=query_terms,
        max_blocks=config.evidence_max_blocks,
        budget_chars=10 ** 9,
        dedup_prefix_len=config.dedup_prefix_len,
    )
    return (
        [c.payload for c in selected],
        [c.score for c in selected],
        intents,
        query_terms,
    )


__all__ = [
    "CandidateScore",
    "QueryIntent",
    "RerankConfig",
    "detect_intents",
    "extract_query_numbers",
    "extract_query_phrases",
    "extract_query_terms",
    "rerank_and_select",
    "score_candidate",
    "select_by_coverage",
]
