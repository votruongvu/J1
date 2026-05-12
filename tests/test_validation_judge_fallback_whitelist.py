"""Tests for the groundedness / fabrication fallback-phrase
whitelist in ``j1.validation.judge``.

When the synthesizer correctly abstains ("Not in the retrieved
evidence.", "Not enough information…", etc.), there are NO factual
claims for the grounding judge to attack. Earlier the judge round-
tripped to the LLM with the fallback phrase as the "answer" and
the LLM duly flagged the abstention itself as an unsupported claim
— producing false-positive moderate-severity warnings on otherwise-
honest responses.

These tests pin the short-circuit so a future refactor doesn't drop
the whitelist back into the LLM-call path.
"""

from __future__ import annotations

from typing import Any

from j1.validation.judge import (
    DefaultLLMJudge,
    FabricationJudgement,
    GroundingJudgement,
    _is_fallback_answer,
)


class _SpyClient:
    """Stub LLM client that records whether it was called. The
    short-circuit must not invoke it."""

    def __init__(self):
        self.calls = 0

    def extract(self, prompt, schema):  # noqa: ARG002
        self.calls += 1
        return {"unsupported_claims": [], "fabricated_claims": []}


def test_is_fallback_answer_canonical_phrase():
    assert _is_fallback_answer("Not in the retrieved evidence.")
    assert _is_fallback_answer("NOT IN THE RETRIEVED EVIDENCE")
    assert _is_fallback_answer("  Not\n in the\tretrieved evidence  ")


def test_is_fallback_answer_variant_phrases():
    """Several phrasings the LLM emits in practice. Substring +
    whitespace-normalised + case-insensitive match all hit."""
    assert _is_fallback_answer("Not enough information is provided.")
    assert _is_fallback_answer("The evidence does not mention the topic.")
    assert _is_fallback_answer(
        "The retrieved evidence does not contain that detail."
    )
    assert _is_fallback_answer("Insufficient information.")
    assert _is_fallback_answer("I don't have enough context to answer.")


def test_is_fallback_answer_real_answer_not_fallback():
    """A grounded answer is NOT a fallback. The whitelist must not
    short-circuit on every answer that mentions evidence."""
    assert not _is_fallback_answer(
        "The proposal due date is 20 May 2026 [1]."
    )
    assert not _is_fallback_answer("Yes — see evidence block [2].")
    assert not _is_fallback_answer("")
    assert not _is_fallback_answer("   ")


def test_grounding_short_circuits_on_fallback():
    """``judge_answer_grounded`` returns an empty GroundingJudgement
    without invoking the LLM when the answer is a fallback phrase."""
    client = _SpyClient()
    judge = DefaultLLMJudge(text_client=client)
    citations: list[dict[str, Any]] = [
        {"artifact_id": "a-1", "source_location": "p/1", "preview": "..."},
    ]

    judgement = judge.judge_answer_grounded(
        question="What is the proposal due date?",
        answer="Not in the retrieved evidence.",
        citations=citations,
    )

    assert isinstance(judgement, GroundingJudgement)
    assert judgement.unsupported_claims == []
    assert judgement.rationale is not None
    assert "fallback phrase" in judgement.rationale
    assert client.calls == 0  # no LLM round-trip


def test_fabrication_short_circuits_on_fallback():
    """``judge_negative_abstain`` short-circuits the same way — a
    fallback answer IS the correct abstain, with no fabricated
    claims."""
    client = _SpyClient()
    judge = DefaultLLMJudge(text_client=client)

    judgement = judge.judge_negative_abstain(
        question="What is the founder's birthday?",
        answer="Not enough information was provided.",
        citations=[],
    )

    assert isinstance(judgement, FabricationJudgement)
    assert judgement.fabricated_claims == []
    assert client.calls == 0


def test_grounding_still_calls_llm_for_real_answers():
    """The whitelist is targeted — real answers must still flow into
    the LLM round-trip so the judge can actually do its job."""
    client = _SpyClient()
    judge = DefaultLLMJudge(text_client=client)

    judge.judge_answer_grounded(
        question="What is the proposal due date?",
        answer="The proposal due date is 20 May 2026 [1].",
        citations=[
            {
                "artifact_id": "c-1",
                "source_location": "chunks/c-1",
                "preview": "The proposal due date is 20 May 2026.",
            },
        ],
    )

    assert client.calls == 1
