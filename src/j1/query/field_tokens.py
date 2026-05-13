"""Shared field-token tokenizer used by the answer-quality gate and
the synthesizer fallback.

A "requested field" is a noun phrase the intent classifier pulled
out of the question (e.g. "modules involved", "cost estimate
class", "scope of work"). When we check whether the answer
covers the requested field, we tokenize it and require the
content tokens — not the literal phrase — to appear, so common
filler like "involved" or "of" doesn't punish a substantively
correct answer.

The tokenizer is intentionally tiny and domain-neutral: it lives
in its own module so both ``answer_quality`` (the gate) and
``answer_synthesizer`` (the native-answer fallback) can read it
without creating an import cycle.
"""

from __future__ import annotations

import re


FIELD_STOPWORDS: frozenset[str] = frozenset({
    # Articles / prepositions / conjunctions.
    "a", "an", "the",
    "of", "for", "to", "from", "in", "on", "at", "by", "with",
    "and", "or", "vs", "vs.",
    # Linking verbs.
    "is", "are", "was", "were", "be", "being", "been",
    # Generic filler classifiers sometimes pull into a field
    # ("modules involved", "associated risk", "applicable section").
    "involved", "involve", "involves",
    "associated", "related", "applicable",
    # Quantifiers (rarely meaningful on their own as field tokens).
    "any", "all", "each", "every", "some",
})


def field_tokens(field: str) -> list[str]:
    """Split a requested-field label into content tokens.

    Empty list means "no content signal" — callers should treat
    that as "field is satisfied" rather than spuriously failing
    on classifier noise."""
    tokens: list[str] = []
    for raw in re.split(r"[\s\-_/]+", (field or "").lower()):
        t = re.sub(r"[^a-z0-9]+", "", raw)
        if not t or t in FIELD_STOPWORDS or len(t) <= 1:
            continue
        tokens.append(t)
    return tokens


__all__ = ["FIELD_STOPWORDS", "field_tokens"]
