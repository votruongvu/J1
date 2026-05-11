"""Phase 3 check-engine tests: negative deterministic + judge-driven
optional checks.

Phase 1 deterministic check tests live in `test_validation_checks.py`.
These tests focus on:

  * `negative_answer_abstains` — required, regex-based.
  * `answer_covers_expected_points` — optional, judge-driven.
  * `answer_grounded_in_citations` — optional, judge-driven.
  * `negative_no_fabrication` — optional, judge-driven.
  * Aggregate behaviour: optional fails downgrade to
    `passed_with_warnings`; required negative fails are `failed`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.validation import (
    CoverageJudgement,
    FabricationJudgement,
    GroundingJudgement,
)
from j1.validation.checks import _is_abstain_response, run_checks
from j1.validation.dtos import RetrievedChunkRefDTO, ValidationCitationDTO


# ---- Helpers --------------------------------------------------------


def _chunk(
    *, chunk_id: str = "c-1", run_id: str = "run-1",
) -> RetrievedChunkRefDTO:
    return RetrievedChunkRefDTO(
        artifact_id="art-1",
        chunk_id=chunk_id,
        run_id=run_id,
        document_id="doc-1",
        source_location="p.1",
        score=0.5,
        preview="…",
    )


def _citation(*, run_id: str = "run-1") -> ValidationCitationDTO:
    return ValidationCitationDTO(
        artifact_id="art-1",
        artifact_type="chunk",
        source_document_id="doc-1",
        source_location="p.1",
        chunk_id="c-1",
        run_id=run_id,
    )


def _stage(workspace, ctx, artifact_registry, *, artifact_id: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(
        ArtifactRecord(
            artifact_id=artifact_id,
            project=ctx,
            kind="chunk",
            location=f"compiled/{artifact_id}.json",
            content_hash=f"sha256:{artifact_id}",
            byte_size=10,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now,
            updated_at=now,
            source_document_ids=[],
            source_artifact_ids=[],
            metadata={},
        )
    )


# ---- _is_abstain_response (regex unit) ------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "I don't know.",
        "I do not know the answer.",
        "I cannot find that in the document.",
        "I can't answer this from the provided context.",
        "There is not enough information to answer.",
        "Insufficient information.",
        "Unable to answer based on the document.",
        "No information about this topic.",
        "The document does not contain that.",
        "The document doesn't mention this.",
        "Cannot determine from the given content.",
        "",  # empty answer = honest abstain
        "   \n  ",  # whitespace-only = honest abstain
    ],
)
def test_abstain_regex_matches_expected_phrases(answer):
    """Lock the abstain pattern set. False negatives (missing a real
    abstain) are bad — the regex must catch typical LLM refusal
    phrases."""
    assert _is_abstain_response(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "The proposal is due 20 May 2026.",
        "Yes, the answer is X.",
        "Bitcoin trades at $100k.",  # confident wrong answer != abstain
        "We have all the information needed.",
    ],
)
def test_abstain_regex_does_not_match_substantive_answers(answer):
    """False positives (treating a real answer as an abstain) are
    worse than false negatives — locked here too."""
    assert _is_abstain_response(answer) is False


# ---- negative_answer_abstains (required) ---------------------------


def test_negative_case_with_abstain_passes(workspace, ctx, artifact_registry):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="I don't know.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="negative",
    )
    abstain = next(c for c in checks if c.name == "negative_answer_abstains")
    assert abstain.passed is True
    # Critical: the negative-case path must NOT include the
    # positive-case checks (answer_non_empty, retrieved_chunks_present).
    names = {c.name for c in checks}
    assert "answer_non_empty" not in names
    assert "retrieved_chunks_present" not in names


def test_negative_case_with_substantive_answer_fails(
    workspace, ctx, artifact_registry,
):
    """The fail mode for a negative case: the engine confidently
    answered an out-of-scope question. Required failure → run is
    `failed`."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="Bitcoin closed at $100,000 today.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="negative",
    )
    abstain = next(c for c in checks if c.name == "negative_answer_abstains")
    assert abstain.passed is False
    assert "Bitcoin" in (abstain.actual or "")  # surfaces in detail/actual


# ---- answer_covers_expected_points (optional, judge-driven) -------


class _CoverageJudge:
    """Stub LLM judge that returns canned coverage results."""

    def __init__(self, response):
        self.calls: list[tuple[str, str, list[str]]] = []
        self._response = response

    def judge_answer_covers_points(self, *, question, answer, expected_points):
        self.calls.append((question, answer, list(expected_points)))
        return self._response

    def judge_answer_grounded(self, **kwargs):
        return None  # not used in coverage tests

    def judge_negative_abstain(self, **kwargs):
        return None


def test_coverage_check_passes_at_or_above_threshold(
    workspace, ctx, artifact_registry,
):
    """≥80% coverage = pass. Stub judge returns 4/5 covered."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _CoverageJudge(
        CoverageJudgement(
            points=[
                CoverageJudgement.Point(text=f"p{i}", covered=(i < 4))
                for i in range(5)
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="A real answer.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="answer",
        expected_answer_points=["p0", "p1", "p2", "p3", "p4"],
        question="What is X?",
        judge=judge,
    )
    cov = next(c for c in checks if c.name == "answer_covers_expected_points")
    assert cov.severity == "optional"
    assert cov.passed is True


def test_coverage_check_fails_below_threshold(workspace, ctx, artifact_registry):
    """3/5 = 0.6, below threshold. Optional fail → warning, not fail."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _CoverageJudge(
        CoverageJudgement(
            points=[
                CoverageJudgement.Point(text=f"p{i}", covered=(i < 3))
                for i in range(5)
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="A real answer.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="answer",
        expected_answer_points=["p0", "p1", "p2", "p3", "p4"],
        question="What is X?",
        judge=judge,
    )
    cov = next(c for c in checks if c.name == "answer_covers_expected_points")
    assert cov.passed is False
    assert cov.severity == "optional"  # warning, NOT fail


def test_coverage_check_skipped_when_no_expected_points(
    workspace, ctx, artifact_registry,
):
    """No expected_answer_points → check OMITTED (not present-but-
    passing). Same convention as Phase 1's citation_present."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _CoverageJudge(CoverageJudgement(points=[]))
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="A real answer.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        expected_answer_points=[],
        judge=judge,
    )
    assert all(c.name != "answer_covers_expected_points" for c in checks)
    # And the judge wasn't called.
    assert judge.calls == []


def test_coverage_check_skipped_when_judge_returns_none(
    workspace, ctx, artifact_registry,
):
    """Judge unavailable / failure → check OMITTED. Critical: a
    silent judge must NEVER count as a pass; that would make
    judge availability indistinguishable from a real verdict."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _CoverageJudge(None)
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="A real answer.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="answer",
        expected_answer_points=["x"],
        question="?",
        judge=judge,
    )
    assert all(c.name != "answer_covers_expected_points" for c in checks)


# ---- answer_grounded_in_citations (optional, judge-driven) --------


class _GroundingJudge:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def judge_answer_covers_points(self, **kwargs):
        return None

    def judge_answer_grounded(self, **kwargs):
        self.calls += 1
        return self._response

    def judge_negative_abstain(self, **kwargs):
        return None


def test_grounding_check_passes_when_no_unsupported_claims(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _GroundingJudge(GroundingJudgement(unsupported_claims=[]))
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="A grounded answer.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        question="?",
        judge=judge,
    )
    g = next(c for c in checks if c.name == "answer_grounded_in_citations")
    assert g.passed is True
    assert g.severity == "optional"


def test_grounding_check_fails_on_moderate_unsupported_claim(
    workspace, ctx, artifact_registry,
):
    """Moderate-or-higher severity → fail. Low severity (hedging,
    filler) is tolerated; that's the contract for a fallible judge."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _GroundingJudge(
        GroundingJudgement(
            unsupported_claims=[
                GroundingJudgement.Claim(
                    text="Bitcoin trades at $100k.",
                    severity="moderate",
                ),
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="X is Y. Bitcoin trades at $100k.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        question="?",
        judge=judge,
    )
    g = next(c for c in checks if c.name == "answer_grounded_in_citations")
    assert g.passed is False
    # `severity=optional` keeps this a warning, never a hard fail.
    assert g.severity == "optional"


def test_grounding_check_tolerates_low_severity_claims(
    workspace, ctx, artifact_registry,
):
    """Low-severity flags don't fail the check — common knowledge
    filler is allowed even if technically not in citations."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _GroundingJudge(
        GroundingJudgement(
            unsupported_claims=[
                GroundingJudgement.Claim(text="Water is wet.", severity="low"),
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="X is Y.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        question="?",
        judge=judge,
    )
    g = next(c for c in checks if c.name == "answer_grounded_in_citations")
    assert g.passed is True


def test_grounding_check_skipped_for_empty_answer(
    workspace, ctx, artifact_registry,
):
    """Empty answer → nothing to ground. The negative-case abstain
    check covers blank-answer accounting; double-checking here would
    confuse the result."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _GroundingJudge(GroundingJudgement(unsupported_claims=[]))
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="",
        retrieved_chunks=[],
        citations=[],
        citation_required=False,
        artifact_registry=artifact_registry,
        question="?",
        judge=judge,
    )
    assert all(c.name != "answer_grounded_in_citations" for c in checks)
    assert judge.calls == 0  # judge never consulted


# ---- negative_no_fabrication (optional, judge-driven) ------------


class _FabricationJudge:
    def __init__(self, response):
        self._response = response

    def judge_answer_covers_points(self, **kwargs):
        return None

    def judge_answer_grounded(self, **kwargs):
        return None

    def judge_negative_abstain(self, **kwargs):
        return self._response


def test_negative_no_fabrication_passes_for_clean_abstain(
    workspace, ctx, artifact_registry,
):
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _FabricationJudge(FabricationJudgement(fabricated_claims=[]))
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="I don't know.",
        retrieved_chunks=[],
        citations=[],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="negative",
        question="off topic?",
        judge=judge,
    )
    fab = next(c for c in checks if c.name == "negative_no_fabrication")
    assert fab.passed is True
    assert fab.severity == "optional"


def test_negative_no_fabrication_fails_on_concrete_fabrication(
    workspace, ctx, artifact_registry,
):
    """Hybrid mode: the answer abstained (or claims to) but the
    judge sees concrete fabricated facts. The deterministic
    abstain check passes (the regex matched); the optional
    fabrication check fails → run becomes passed_with_warnings."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _FabricationJudge(
        FabricationJudgement(
            fabricated_claims=[
                FabricationJudgement.Claim(
                    text="Bitcoin trades at $100k.", severity="high",
                ),
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        # Reads as an abstain regex-wise but contains a fabrication.
        answer="I don't know, but Bitcoin trades at $100k.",
        retrieved_chunks=[],
        citations=[],
        citation_required=False,
        artifact_registry=artifact_registry,
        case_type="negative",
        question="off topic?",
        judge=judge,
    )
    fab = next(c for c in checks if c.name == "negative_no_fabrication")
    assert fab.passed is False
    abstain = next(c for c in checks if c.name == "negative_answer_abstains")
    # The deterministic abstain check still passes because the
    # answer matched the regex — split between deterministic
    # required + optional judge surfaces both signals honestly.
    assert abstain.passed is True


# ---- Aggregate-status interaction ---------------------------------


def test_phase_3_optional_fail_yields_passed_with_warnings(
    workspace, ctx, artifact_registry,
):
    """Forward-compat regression: the Phase 1 aggregator's
    `passed_with_warnings` rule is reachable in Phase 3 reality, not
    just unit tests. A judge-driven optional fail with all
    Phase 1/2 required checks passing must downgrade the run."""
    _stage(workspace, ctx, artifact_registry, artifact_id="art-1")
    judge = _GroundingJudge(
        GroundingJudgement(
            unsupported_claims=[
                GroundingJudgement.Claim(text="X", severity="moderate"),
            ],
        ),
    )
    checks = run_checks(
        ctx=ctx, run_id="run-1",
        answer="X is Y.",
        retrieved_chunks=[_chunk()],
        citations=[_citation()],
        citation_required=False,
        artifact_registry=artifact_registry,
        question="?",
        judge=judge,
    )
    # Required checks all pass; one optional check fails →
    # aggregate is passed_with_warnings.
    from j1.validation.checks import aggregate_status
    assert aggregate_status(checks) == "passed_with_warnings"
