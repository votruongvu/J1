"""Regression tests for the grounding prompt's explicit instruction
to NOT extract claims from the question section.

Bug fixed:
   The groundedness judge's LLM occasionally extracted the
   Question text itself as an "unsupported claim" in the answer.
   The judge prompt sent ``Question: X / Answer: Y / Citations: Z``
   and the LLM treated everything as candidate claim material.
   Operators saw warnings like "first unsupported claim: '<the
   original question text>'".

   The new prompt explicitly says: extract claims ONLY from the
   Answer section; do NOT list the question (verbatim or
   paraphrased); do NOT list answer preambles like "Knowledge
   results:" as claims; lineage-only citations cap claim
   severity at `low`.
"""

from __future__ import annotations

from j1.validation.judge import _GROUNDING_PROMPT


def test_prompt_explicitly_excludes_question_claims():
    """The prompt must contain the explicit exclusion clause —
    pins the intent so a future rewrite doesn't accidentally
    drop the guardrail."""
    p = _GROUNDING_PROMPT
    assert "ONLY" in p and "Answer" in p
    # Either the literal "Question" mention with NOT, or the
    # negated extraction phrase.
    assert "Question" in p
    assert "Do NOT list the question" in p


def test_prompt_excludes_answer_preamble_claims():
    """The prompt must mark answer preambles (formatting headers)
    as not-claims. Without this, "Knowledge results:" or
    "Graph relationships:" gets flagged as an unsupported claim."""
    p = _GROUNDING_PROMPT
    assert "preamble" in p.lower() or "heading" in p.lower()
    assert "Knowledge results:" in p
    assert "Graph relationships:" in p


def test_prompt_capping_severity_for_lineage_only_citations():
    """When a citation has no body excerpt (lineage-only), the
    prompt caps claim severity at ``low`` so an unverifiable claim
    doesn't fail the optional check. Earlier the judge over-flagged
    such cases as moderate-severity because it had nothing to
    compare against."""
    p = _GROUNDING_PROMPT
    assert "lineage" in p.lower()
    # Both vocabulary anchors so a future prompt edit must rewrite
    # the rule explicitly.
    assert "`low`" in p
    assert "evidence-of-existence" in p


def test_prompt_still_returns_empty_for_grounded_answers():
    """Pinning the contract that no claims = empty list."""
    p = _GROUNDING_PROMPT
    assert "empty list" in p.lower()
