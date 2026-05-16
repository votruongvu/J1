"""Phase 5A — merge Knowledge Memory expansion terms with the
existing query-augmentation expansions.

The function returns a deterministic, deduplicated, capped term
list the orchestrator hands to ``_build_expansion_jobs`` as the
``variants`` arg. Memory-side provenance is preserved so the
trace can show which terms came from memory versus from the
existing domain-pack augmentation.

Hard contract:

  * **Pure function.** No side effects, no IO, no LLM. Tests
    drive it with plain tuples.
  * **Case-insensitive dedup.** Two terms that differ only in
    case collapse to the FIRST occurrence preserving the
    original casing — important so retrieval gets the
    operator-readable form rather than a normalised lowercase.
  * **Stable ordering.** Augmentation terms come first (they're
    the established source), memory terms are appended in
    selection order. The orchestrator's per-job variant cap
    (``_MAX_EXPANSION_VARIANTS_PER_JOB`` = 4) decides which of
    these the routes actually see — so the merger biases toward
    the existing pipeline by listing augmentation terms first.
  * **Filtering** drops empty / too-short / common stopword /
    too-long terms so retrieval doesn't get junk variants.
  * **Capping** truncates the MEMORY pool only; augmentation
    expansions pass through unchanged (they're already
    capped + deduped by the augmentation layer).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


# Common stopwords expanded with retrieval-noise terms. Same set
# the provider's tokeniser uses + a few synonyms operators
# typically type as filler. Lowercase only.
_FILTER_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "this",
    "that", "with", "from", "into", "have", "has", "had", "will",
    "would", "should", "could", "can", "but", "not", "any", "all",
    "some", "what", "where", "when", "why", "how", "which", "who",
    "whom", "whose", "they", "them", "their", "its", "our", "your",
    "his", "her", "him", "she", "about", "such", "yes", "yet",
    "also", "very", "more", "less", "only", "than", "then", "too",
})


# Minimum + maximum lengths for an acceptable expansion term.
# Too-short terms produce noisy variant explosion; too-long terms
# typically encode whole sentences and don't behave like
# retrieval anchors. Pinned constants — operator-facing tuning
# happens via the cap setting.
_MIN_TERM_LEN = 3
_MAX_TERM_LEN = 80


@dataclass(frozen=True)
class MemoryExpansionMergeResult:
    """Output of ``merge_memory_expansion_terms``.

    Fields:
      * `final_terms` — full deduped capped list passed to
        ``_build_expansion_jobs``. Includes both augmentation
        and memory contributions, augmentation first.
      * `applied_memory_terms` — memory-only terms that survived
        filtering + dedup against augmentation + cap. Used by
        the trace's `applied_expansion_terms` field.
      * `truncated` — True iff at least one memory term was
        dropped to honour `max_memory_terms`. Surfaced as
        `expansion_terms_truncated=true` on the trace.
      * `applied` — True iff at least one memory term contributed
        to `final_terms`. Drives the trace's
        `expansion_terms_applied` flag.
    """

    final_terms: tuple[str, ...]
    applied_memory_terms: tuple[str, ...]
    truncated: bool
    applied: bool


def merge_memory_expansion_terms(
    *,
    augmentation_terms: Iterable[str],
    memory_terms: Iterable[str],
    max_memory_terms: int,
) -> MemoryExpansionMergeResult:
    """Merge memory expansion terms into the existing augmentation
    expansions. Augmentation terms pass through unchanged
    (already deduped + capped upstream); memory terms are
    filtered, deduped against the augmentation set + each other,
    and capped at ``max_memory_terms`` before being appended.

    Returns a `MemoryExpansionMergeResult`. Always returns a
    valid result — empty inputs produce empty outputs without
    raising."""
    if max_memory_terms < 0:
        max_memory_terms = 0

    # Build the augmentation-side baseline. We preserve insertion
    # order + original casing while building a case-folded
    # membership set for dedup.
    final: list[str] = []
    seen_lower: set[str] = set()
    for term in augmentation_terms:
        if not isinstance(term, str):
            continue
        normalised = term.strip()
        if not normalised:
            continue
        key = normalised.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        final.append(normalised)

    # Filter + dedup memory terms against the baseline.
    applied: list[str] = []
    truncated = False
    for raw in memory_terms:
        if max_memory_terms == 0:
            # Cap of 0 means "no memory contribution"; any
            # remaining unique terms count as truncation so the
            # trace surfaces the intent rather than silently
            # absorbing the cap.
            truncated = True
            break
        if not isinstance(raw, str):
            continue
        term = raw.strip()
        if not _is_acceptable_term(term):
            continue
        key = term.lower()
        if key in seen_lower:
            continue
        if len(applied) >= max_memory_terms:
            truncated = True
            break
        seen_lower.add(key)
        applied.append(term)
        final.append(term)

    # If we exhausted the loop without hitting the cap but there
    # were more memory terms than we applied (e.g. all filtered
    # out), we don't claim truncation — the operator's terms
    # just didn't qualify.
    return MemoryExpansionMergeResult(
        final_terms=tuple(final),
        applied_memory_terms=tuple(applied),
        truncated=truncated,
        applied=bool(applied),
    )


def _is_acceptable_term(term: str) -> bool:
    """Filter empty / too-short / stopword / too-long terms.
    Mirrors the conservative posture the provider's tokeniser
    uses — but applies AFTER selection, so the same term that
    looked like a useful expansion at selection time can still be
    rejected here if it's noisy as a retrieval variant (e.g. a
    standalone "shall" from a requirement title)."""
    if not term:
        return False
    if len(term) < _MIN_TERM_LEN:
        return False
    if len(term) > _MAX_TERM_LEN:
        return False
    if term.lower() in _FILTER_STOPWORDS:
        return False
    return True
