"""Deterministic check engine for the validation feature.

six required deterministic checks (server-derived metadata
only, no LLM judging).
two new check families:

 * **Negative test deterministic check** —
 `negative_answer_abstains` (required) — for case type=`negative`,
 the answer must match a regex pattern of "I don't know" /
 "insufficient information" / similar OR be empty.
 * **Optional semantic checks** (judge-driven, severity=optional):
 - `answer_covers_expected_points` — when `expected_answer_points`
 is non-empty AND a judge is configured.
 - `answer_grounded_in_citations` — when there's an answer and a
 judge is configured.
 - `negative_no_fabrication` — for negative cases, when a judge
 is configured.

Optional checks are EVER warning-severity. A judge that flips its
mind between runs would create flapping outcomes — required failures
must stay reproducible. The judge is "witness, not validator."

Each check is a small pure function that takes the response context
and returns a `ValidationCheckDTO`. The engine runs them in order
and aggregates the result via `_aggregate_status`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from j1.artifacts.registry import ArtifactNotFoundError, ArtifactRegistry
from j1.projects.context import ProjectContext
from j1.validation.dtos import (
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationStatus,
)
from j1.validation.judge import (
    LLMJudge,
    coverage_threshold,
)


# ---- Check evaluation context ------------------------------------------


@dataclass(frozen=True)
class _CheckContext:
    """Bundle every input the deterministic checks need.

 Kept in one struct so `run_checks` doesn't grow a 10-arg
 signature, and so unit tests can construct a context directly
 without going through the full service.
 """

    ctx: ProjectContext
    run_id: str
    answer: str
    retrieved_chunks: list[RetrievedChunkRefDTO]
    citations: list[ValidationCitationDTO]
    citation_required: bool
    artifact_registry: ArtifactRegistry


# ---- abstain regex ---------------------------------------------
#
# Matches phrase-level signals that the answer admits it doesn't know /
# can't answer / has insufficient information. Case-insensitive across
# whitespace boundaries. Tuned for the kind of language an LLM uses
# when politely declining: "I don't know", "the document doesn't
# contain", "not enough information", "cannot determine", etc.
#
# Deliberately conservative — false negatives (missing an abstain that
# WAS there) are preferable to false positives. An LLM that says "Yes,
# 20 May 2026" should never look like an abstain.

_ABSTAIN_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "I don't know" / "I do not know"
        r"\bi\s+(do\s+not|don'?t)\s+know\b",
        # "I cannot" / "I can not" / "I can't"
        r"\bi\s+(cannot|can\s+not|can'?t)\b",
        r"\bnot\s+enough\s+information\b",
        r"\binsufficient\s+information\b",
        r"\bunable\s+to\s+answer\b",
        r"\bno\s+information\b",
        r"\bnot\s+(present|mentioned|covered|specified|provided|in\s+the\s+document)\b",
        r"\bthe\s+document\s+(does\s+not|doesn'?t)\b",
        r"\b(cannot|can\s+not|can'?t)\s+determine\b",
        r"\bunable\s+to\s+determine\b",
        # "cannot find" / "can't find" — captures "I cannot find that"
        r"\b(cannot|can\s+not|can'?t)\s+find\b",
    )
)


def _is_abstain_response(answer: str) -> bool:
    """True when the answer reads as a refusal / abstention.

 Empty / whitespace-only answers also count as abstaining — a
 blank response from a knowledge-grounded engine on an out-of-
 scope question is the right behaviour."""
    body = (answer or "").strip()
    if not body:
        return True
    return any(p.search(body) for p in _ABSTAIN_PATTERNS)


# ---- Individual checks --------------------------------------------------


def _check_answer_non_empty(ctx_: _CheckContext) -> ValidationCheckDTO:
    body = (ctx_.answer or "").strip()
    passed = len(body) >= 5
    return ValidationCheckDTO(
        name="answer_non_empty",
        severity="required",
        passed=passed,
        detail=None if passed else "answer was empty or trivially short",
        expected="answer with >= 5 non-whitespace chars",
        actual=f"len={len(body)}",
    )


def _check_retrieved_chunks_present(ctx_: _CheckContext) -> ValidationCheckDTO:
    count = len(ctx_.retrieved_chunks)
    passed = count >= 1
    return ValidationCheckDTO(
        name="retrieved_chunks_present",
        severity="required",
        passed=passed,
        detail=None if passed else "no chunks/artifacts matched the question",
        expected=">= 1 retrieved chunk",
        actual=count,
    )


def _check_citation_present(ctx_: _CheckContext) -> ValidationCheckDTO | None:
    """Conditional check.

 Returns None (skipped — not added to `checks[]`) when the caller
 didn't request citation enforcement. Returning the check from
 `run_checks` only when the request demands it keeps the
 `passed_with_warnings`/`failed` aggregation honest: a check that
 isn't in the list can't fail.
 """
    if not ctx_.citation_required:
        return None
    count = len(ctx_.citations)
    passed = count >= 1
    return ValidationCheckDTO(
        name="citation_present",
        severity="required",
        passed=passed,
        detail=None if passed else "answer has no citations despite citationRequired=true",
        expected=">= 1 citation",
        actual=count,
    )


def _check_retrieved_chunks_belong_to_run(
    ctx_: _CheckContext,
) -> ValidationCheckDTO:
    """Server-derived `run_id` on every retrieved chunk must match
 the request's run scope. Anything else means the FTS scope
 filter leaked, the indexer mis-tagged a row, or the producer
 forgot to set `metadata.run_id` on the artifact.

 Skipped (passed=True with detail) when there are no retrieved
 chunks — there's nothing to check, and `retrieved_chunks_present`
 will already have failed in that case so we don't double-count.
 """
    if not ctx_.retrieved_chunks:
        return ValidationCheckDTO(
            name="retrieved_chunks_belong_to_run",
            severity="required",
            passed=True,
            detail="no retrieved chunks to check (covered by retrieved_chunks_present)",
            expected=ctx_.run_id,
            actual=None,
        )
    mismatched = [c for c in ctx_.retrieved_chunks if c.run_id != ctx_.run_id]
    passed = not mismatched
    return ValidationCheckDTO(
        name="retrieved_chunks_belong_to_run",
        severity="required",
        passed=passed,
        detail=(
            None
            if passed
            else (
                f"{len(mismatched)} retrieved chunks had a different run_id; "
                f"first offender: artifact_id={mismatched[0].artifact_id} "
                f"run_id={mismatched[0].run_id!r}"
            )
        ),
        expected=ctx_.run_id,
        actual=[c.run_id for c in ctx_.retrieved_chunks],
    )


def _check_citations_belong_to_run(
    ctx_: _CheckContext,
) -> ValidationCheckDTO:
    """Same shape as the chunks check, applied to citations. A
 citation with `run_id is None` counts as a fail — every citation
 that survived the FTS run-scope filter MUST carry the run id."""
    if not ctx_.citations:
        return ValidationCheckDTO(
            name="citations_belong_to_run",
            severity="required",
            passed=True,
            detail="no citations to check",
            expected=ctx_.run_id,
            actual=None,
        )
    mismatched = [c for c in ctx_.citations if c.run_id != ctx_.run_id]
    passed = not mismatched
    return ValidationCheckDTO(
        name="citations_belong_to_run",
        severity="required",
        passed=passed,
        detail=(
            None
            if passed
            else (
                f"{len(mismatched)} citations had a different run_id; "
                f"first offender: artifact_id={mismatched[0].artifact_id} "
                f"run_id={mismatched[0].run_id!r}"
            )
        ),
        expected=ctx_.run_id,
        actual=[c.run_id for c in ctx_.citations],
    )


def _check_no_cross_tenant_or_cross_project_leak(
    ctx_: _CheckContext,
) -> ValidationCheckDTO:
    """Defense in depth: every cited artifact must resolve in the
 caller's `(tenant, project)` via the registry. The `run_id`
 filter alone protects against same-project cross-run leaks; this
 check covers the (would-be-bug) case where the indexer somehow
 surfaced an artifact whose registry ownership is elsewhere.

 `ArtifactNotFoundError` from the registry is treated as a fail
 — if a citation references an artifact we can't load under this
 project, something is wrong even if the run_id matches by
 coincidence.
 """
    offenders: list[str] = []
    for citation in ctx_.citations:
        try:
            ctx_.artifact_registry.get(ctx_.ctx, citation.artifact_id)
        except ArtifactNotFoundError:
            offenders.append(citation.artifact_id)
    passed = not offenders
    return ValidationCheckDTO(
        name="no_cross_tenant_or_cross_project_leak",
        severity="required",
        passed=passed,
        detail=(
            None
            if passed
            else (
                f"{len(offenders)} cited artifacts not resolvable in "
                f"({ctx_.ctx.tenant_id!r}, {ctx_.ctx.project_id!r}): "
                f"{offenders[:3]}"
            )
        ),
        expected="all citations resolve in the caller's project",
        actual=offenders,
    )


# ---- negative-test deterministic check -------------------------


def _check_negative_answer_abstains(ctx_: _CheckContext) -> ValidationCheckDTO:
    """Required for `case_type=negative`: the answer must read as a
 refusal / "I don't know" / similar. Empty answers also count.
 Honest abstention is the entire point of a negative case."""
    abstained = _is_abstain_response(ctx_.answer)
    return ValidationCheckDTO(
        name="negative_answer_abstains",
        severity="required",
        passed=abstained,
        detail=(
            None if abstained
            else (
                "negative case expected an abstain / 'I don't know' "
                "response; got a substantive answer instead"
            )
        ),
        expected="abstain phrase or empty answer",
        actual=(ctx_.answer or "")[:200],
    )


# ---- optional judge-driven checks -----------------------------


def _check_answer_covers_expected_points(
    ctx_: _CheckContext,
    *,
    judge: LLMJudge,
    question: str,
    expected_points: list[str],
) -> ValidationCheckDTO | None:
    """Optional: the answer must semantically cover ≥80% of the
 expected answer points. Below the threshold is a warning, NOT a
 failure (the judge is fallible — required failures must stay
 deterministic).

 Returns None when the judge couldn't render an opinion (LLM
 unavailable, malformed response, etc.) — we omit the check
 rather than count silence as a pass."""
    judgement = judge.judge_answer_covers_points(
        question=question, answer=ctx_.answer,
        expected_points=list(expected_points),
    )
    if judgement is None:
        return None
    threshold = coverage_threshold()
    ratio = judgement.coverage_ratio
    passed = ratio >= threshold
    covered = sum(1 for p in judgement.points if p.covered)
    total = len(judgement.points)
    return ValidationCheckDTO(
        name="answer_covers_expected_points",
        severity="optional",
        passed=passed,
        detail=(
            None if passed
            else (
                f"covered {covered}/{total} expected points "
                f"(ratio={ratio:.2f}, threshold={threshold:.2f})"
            )
        ),
        expected=f"coverage_ratio >= {threshold}",
        actual={
            "coverage_ratio": round(ratio, 4),
            "covered": covered,
            "total": total,
            "missing": [
                p.text for p in judgement.points if not p.covered
            ][:5],
        },
    )


def _check_answer_grounded_in_citations(
    ctx_: _CheckContext,
    *,
    judge: LLMJudge,
    question: str,
) -> ValidationCheckDTO | None:
    """Optional: the answer must rely on the citations. The judge
 flags any unsupported claims; severity≥moderate counts as a fail.
 Low-severity flags (filler, hedging) are tolerated.

 Skipped when the answer is empty (nothing to ground) — for the
 abstain case `negative_answer_abstains` already covers it."""
    if not (ctx_.answer or "").strip():
        return None
    judgement = judge.judge_answer_grounded(
        question=question,
        answer=ctx_.answer,
        citations=[_citation_to_dict(c) for c in ctx_.citations],
    )
    if judgement is None:
        return None
    has_issues = judgement.has_significant_issues()
    return ValidationCheckDTO(
        name="answer_grounded_in_citations",
        severity="optional",
        passed=not has_issues,
        detail=(
            None if not has_issues
            else (
                f"{len(judgement.unsupported_claims)} unsupported "
                f"claim(s) flagged; first: "
                f"{judgement.unsupported_claims[0].text[:200]!r}"
            )
        ),
        expected="no moderate-or-higher unsupported claims",
        actual=[
            {"text": c.text, "severity": c.severity}
            for c in judgement.unsupported_claims
        ][:5],
    )


def _check_negative_no_fabrication(
    ctx_: _CheckContext,
    *,
    judge: LLMJudge,
    question: str,
) -> ValidationCheckDTO | None:
    """Optional: for negative cases, even an abstaining answer
 shouldn't fabricate facts. The judge looks at the answer + any
 citations and flags concrete fabrications.

 Distinct from `answer_grounded_in_citations` because the
 fabrication check accepts an empty citation list (the question
 is OUT of scope; honest abstention with no citations is the
 target). Severity threshold matches the grounding check."""
    judgement = judge.judge_negative_abstain(
        question=question,
        answer=ctx_.answer,
        citations=[_citation_to_dict(c) for c in ctx_.citations],
    )
    if judgement is None:
        return None
    has_issues = judgement.has_fabrication()
    return ValidationCheckDTO(
        name="negative_no_fabrication",
        severity="optional",
        passed=not has_issues,
        detail=(
            None if not has_issues
            else (
                f"{len(judgement.fabricated_claims)} fabricated "
                f"claim(s) flagged; first: "
                f"{judgement.fabricated_claims[0].text[:200]!r}"
            )
        ),
        expected="no moderate-or-higher fabricated claims",
        actual=[
            {"text": c.text, "severity": c.severity}
            for c in judgement.fabricated_claims
        ][:5],
    )


def _citation_to_dict(c: ValidationCitationDTO) -> dict:
    """Compact dict for the judge prompt. Mirrors the wire shape so
 the judge sees the same fields the FE renders."""
    return {
        "artifact_id": c.artifact_id,
        "artifact_type": c.artifact_type,
        "source_document_id": c.source_document_id,
        "source_location": c.source_location,
        "chunk_id": c.chunk_id,
        "run_id": c.run_id,
        # `preview` not on the DTO today — leave it absent so the
        # judge renderer surfaces "lineage only" lines.
    }


# ---- Engine -------------------------------------------------------------


def run_checks(
    *,
    ctx: ProjectContext,
    run_id: str,
    answer: str,
    retrieved_chunks: list[RetrievedChunkRefDTO],
    citations: list[ValidationCitationDTO],
    citation_required: bool,
    artifact_registry: ArtifactRegistry,
    case_type: str | None = None,
    expected_answer_points: list[str] | None = None,
    question: str | None = None,
    judge: LLMJudge | None = None,
) -> list[ValidationCheckDTO]:
    """Run the deterministic check suite + optional judge checks.

 Order matters for operator readability — answer presence first,
 retrieval next, then run-scope checks, then ownership defense,
 then negative/judge optional checks at the tail.

 Per-case-type branching:
 * `case_type="negative"` swaps the answer-non-empty +
 retrieved-chunks-present required checks for
 `negative_answer_abstains` (required) +
 `negative_no_fabrication` (optional, judge-driven).
 * Any other case (or `case_type=None` for the manual query
 path) runs the / positive-case suite.

 Optional judge checks are appended ONLY when a judge is
 supplied AND its preconditions hold (e.g. `expected_answer_points`
 non-empty for the coverage check). Conditional checks are
 OMITTED rather than included-and-passing — that keeps the
 `_aggregate_status` rule honest: a check that wasn't run can't
 flip the validation status by accident.
 """
    check_ctx = _CheckContext(
        ctx=ctx,
        run_id=run_id,
        answer=answer,
        retrieved_chunks=retrieved_chunks,
        citations=citations,
        citation_required=citation_required,
        artifact_registry=artifact_registry,
    )
    checks: list[ValidationCheckDTO] = []

    if case_type == "negative":
        # Negative test: an empty answer is the IDEAL outcome,
        # retrieval may legitimately return nothing relevant. Skip
        # the positive-case required checks for those two
        # dimensions. Ownership checks always run.
        checks.append(_check_negative_answer_abstains(check_ctx))
    else:
        checks.append(_check_answer_non_empty(check_ctx))
        checks.append(_check_retrieved_chunks_present(check_ctx))
        citation_check = _check_citation_present(check_ctx)
        if citation_check is not None:
            checks.append(citation_check)

    checks.append(_check_retrieved_chunks_belong_to_run(check_ctx))
    checks.append(_check_citations_belong_to_run(check_ctx))
    checks.append(_check_no_cross_tenant_or_cross_project_leak(check_ctx))

    # Optional judge checks (severity=optional → at worst downgrade
    # to passed_with_warnings). All judge calls happen via the
    # `LLMJudge` Protocol so tests can inject a stub.
    if judge is not None:
        if case_type == "negative":
            fab_check = _check_negative_no_fabrication(
                check_ctx, judge=judge, question=question or "",
            )
            if fab_check is not None:
                checks.append(fab_check)
        else:
            if expected_answer_points:
                cov_check = _check_answer_covers_expected_points(
                    check_ctx,
                    judge=judge,
                    question=question or "",
                    expected_points=expected_answer_points,
                )
                if cov_check is not None:
                    checks.append(cov_check)
            grounded_check = _check_answer_grounded_in_citations(
                check_ctx, judge=judge, question=question or "",
            )
            if grounded_check is not None:
                checks.append(grounded_check)

    return checks


def aggregate_status(checks: list[ValidationCheckDTO]) -> ValidationStatus:
    """Roll up per-check outcomes into the single `validationStatus`
 field on the response.

 ships only `required` checks, so the aggregation reduces
 to "any required failed → failed; else passed". The
 `passed_with_warnings` branch exists for forward-compat with
 's optional / judge checks. `inconclusive` is reserved
 for catastrophic engine failures (the service layer sets it
 directly when an exception bubbles out of the query call).
 """
    has_required_fail = any(
        not c.passed and c.severity == "required" for c in checks
    )
    if has_required_fail:
        return "failed"
    has_optional_fail = any(
        not c.passed and c.severity == "optional" for c in checks
    )
    if has_optional_fail:
        return "passed_with_warnings"
    return "passed"
