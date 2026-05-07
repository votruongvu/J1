"""Unit tests for `DefaultLLMJudge`.

Tests the judge in isolation with a stub LLM client so we can
exercise the prompt-building, schema-validation, and graceful-
failure paths without burning real LLM calls.

Cross-check tests (judge → runner → service) live in
test_validation_runner.py and test_validation_checks_phase3.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from j1.validation import (
    CoverageJudgement,
    DefaultLLMJudge,
    FabricationJudgement,
    GroundingJudgement,
)
from j1.validation.judge import coverage_threshold


class _StubLLM:
    """Records every extract() call + returns canned responses.

    Tests vary `responses` (a list — popped in order) to simulate
    different LLM behaviours."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or []
        self._raise = raise_on_call

    def extract(self, prompt: str, schema: dict):
        self.calls.append((prompt, schema))
        if self._raise:
            raise RuntimeError("simulated LLM failure")
        if not self._responses:
            return ({}, object())
        return (self._responses.pop(0), object())


# ---- judge_answer_covers_points -----------------------------------


def test_coverage_judgement_round_trips_canned_response():
    """The stub LLM returns the schema-valid response; the judge
    marshals it into a typed `CoverageJudgement`."""
    stub = _StubLLM(
        responses=[
            {
                "points": [
                    {"text": "p1", "covered": True, "rationale": "obvious"},
                    {"text": "p2", "covered": False},
                    {"text": "p3", "covered": True},
                ],
                "rationale": "judge thinks 2/3 covered",
            },
        ],
    )
    judge = DefaultLLMJudge(text_client=stub)

    result = judge.judge_answer_covers_points(
        question="What's X?", answer="X is the foo of Y.",
        expected_points=["p1", "p2", "p3"],
    )

    assert result is not None
    assert len(result.points) == 3
    assert result.points[0].covered is True
    assert result.points[1].covered is False
    # Coverage ratio: 2/3 ≈ 0.67 — below the 0.8 threshold.
    assert result.coverage_ratio == pytest.approx(2 / 3)
    assert result.coverage_ratio < coverage_threshold()


def test_coverage_returns_none_for_empty_points():
    """No expected points → no judge call. Saves a round trip and
    keeps the runner-side conditional honest."""
    stub = _StubLLM()
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_covers_points(
        question="Q", answer="A", expected_points=[],
    )
    assert result is None
    assert stub.calls == []


def test_coverage_returns_none_for_empty_answer():
    stub = _StubLLM()
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_covers_points(
        question="Q", answer="   ", expected_points=["p1"],
    )
    assert result is None
    assert stub.calls == []


def test_coverage_returns_none_on_llm_failure():
    """Critical: when the LLM raises, the judge returns None, not
    a "coverage = 0" judgement. The runner branches on None to
    OMIT the optional check rather than count silence as failed."""
    stub = _StubLLM(raise_on_call=True)
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_covers_points(
        question="Q", answer="A", expected_points=["p1"],
    )
    assert result is None


def test_coverage_returns_none_on_malformed_response():
    """LLM returned valid JSON but not the expected shape (missing
    `points` key, or wrong type). Same OMIT-the-check signal."""
    stub = _StubLLM(responses=[{"wrong_shape": True}])
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_covers_points(
        question="Q", answer="A", expected_points=["p1"],
    )
    assert result is None


def test_coverage_drops_invalid_point_entries():
    """A judge that returns extra noise (non-dict items) must not
    crash. The judge filters and returns the usable subset."""
    stub = _StubLLM(
        responses=[
            {
                "points": [
                    "not a dict",  # drop
                    {"text": "good", "covered": True},  # keep
                    None,  # drop
                ],
            },
        ],
    )
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_covers_points(
        question="Q", answer="A", expected_points=["x"],
    )
    assert result is not None
    assert len(result.points) == 1


# ---- judge_answer_grounded ----------------------------------------


def test_grounding_returns_typed_claims():
    stub = _StubLLM(
        responses=[
            {
                "unsupported_claims": [
                    {
                        "text": "Bitcoin trades at $100k.",
                        "severity": "high",
                        "rationale": "not in citations",
                    },
                    {"text": "filler", "severity": "low"},
                ],
            },
        ],
    )
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_grounded(
        question="Q", answer="X is Y. Bitcoin trades at $100k.",
        citations=[],
    )
    assert result is not None
    assert len(result.unsupported_claims) == 2
    # has_significant_issues: any moderate-or-higher → True.
    assert result.has_significant_issues() is True


def test_grounding_low_severity_does_not_count_as_significant():
    """The contract: low-severity flags are tolerated. Locked here
    so a future judge prompt change can't silently start failing
    the grounding check on hedging language."""
    stub = _StubLLM(
        responses=[
            {
                "unsupported_claims": [
                    {"text": "filler", "severity": "low"},
                ],
            },
        ],
    )
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_grounded(
        question="Q", answer="X.", citations=[],
    )
    assert result is not None
    assert result.has_significant_issues() is False


def test_grounding_returns_none_for_empty_answer():
    stub = _StubLLM()
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_answer_grounded(
        question="Q", answer="", citations=[],
    )
    assert result is None
    assert stub.calls == []


def test_grounding_returns_none_on_llm_failure():
    stub = _StubLLM(raise_on_call=True)
    judge = DefaultLLMJudge(text_client=stub)
    assert (
        judge.judge_answer_grounded(question="Q", answer="A", citations=[])
        is None
    )


# ---- judge_negative_abstain ---------------------------------------


def test_fabrication_returns_typed_claims():
    stub = _StubLLM(
        responses=[
            {
                "fabricated_claims": [
                    {"text": "Mars has cities.", "severity": "high"},
                ],
            },
        ],
    )
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_negative_abstain(
        question="What's the capital of Mars?",
        answer="The capital of Mars is Olympus City.",
        citations=[],
    )
    assert result is not None
    assert result.has_fabrication() is True


def test_fabrication_clean_abstain_passes():
    stub = _StubLLM(responses=[{"fabricated_claims": []}])
    judge = DefaultLLMJudge(text_client=stub)
    result = judge.judge_negative_abstain(
        question="Off-topic?", answer="I don't know.", citations=[],
    )
    assert result is not None
    assert result.has_fabrication() is False


def test_judge_with_no_llm_client_returns_none_everywhere():
    """Belt-and-braces: every judge method must collapse to None
    when no LLM is configured. The runner relies on this for the
    `judge=None` ergonomics."""
    judge = DefaultLLMJudge(text_client=None)
    assert (
        judge.judge_answer_covers_points(
            question="Q", answer="A", expected_points=["p"],
        )
        is None
    )
    assert (
        judge.judge_answer_grounded(question="Q", answer="A", citations=[])
        is None
    )
    assert (
        judge.judge_negative_abstain(
            question="Q", answer="A", citations=[],
        )
        is None
    )


# ---- coverage_ratio property --------------------------------------


def test_coverage_ratio_matches_covered_total():
    """Property unit — independent of the judge call path."""
    j = CoverageJudgement(
        points=[
            CoverageJudgement.Point(text="x", covered=True),
            CoverageJudgement.Point(text="y", covered=False),
            CoverageJudgement.Point(text="z", covered=True),
            CoverageJudgement.Point(text="w", covered=True),
        ],
    )
    assert j.coverage_ratio == pytest.approx(0.75)


def test_coverage_ratio_empty_points_is_one():
    """Vacuous truth — no points to cover means full coverage. The
    runner doesn't trigger the check on empty point lists, but
    locking the property keeps reasoning local."""
    assert CoverageJudgement(points=[]).coverage_ratio == 1.0
