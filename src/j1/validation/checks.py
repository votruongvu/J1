"""Deterministic check engine for Phase 1 manual test queries.

Six required checks today, all source-server-derived metadata only:
no LLM judge, no semantic comparison, no abstain regex (negative
validation is deferred per the implementation plan). Optional /
warning-severity checks are reserved for later phases.

Each check is a small pure function that takes the response context
and returns a `ValidationCheckDTO`. The engine runs them in order
and aggregates the result via `_aggregate_status`.
"""

from __future__ import annotations

from dataclasses import dataclass

from j1.artifacts.registry import ArtifactNotFoundError, ArtifactRegistry
from j1.projects.context import ProjectContext
from j1.validation.dtos import (
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationStatus,
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
) -> list[ValidationCheckDTO]:
    """Run the Phase-1 deterministic check suite in fixed order.

    Order matters for operator readability — answer presence first,
    retrieval next, then run-scope checks, then ownership defense.
    Conditional checks (`citation_present`) are appended only when
    applicable so the FE doesn't render confusing "skipped" badges
    for checks that never ran.
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
    checks: list[ValidationCheckDTO] = [
        _check_answer_non_empty(check_ctx),
        _check_retrieved_chunks_present(check_ctx),
    ]
    citation_check = _check_citation_present(check_ctx)
    if citation_check is not None:
        checks.append(citation_check)
    checks.append(_check_retrieved_chunks_belong_to_run(check_ctx))
    checks.append(_check_citations_belong_to_run(check_ctx))
    checks.append(_check_no_cross_tenant_or_cross_project_leak(check_ctx))
    return checks


def aggregate_status(checks: list[ValidationCheckDTO]) -> ValidationStatus:
    """Roll up per-check outcomes into the single `validationStatus`
    field on the response.

    Phase 1 ships only `required` checks, so the aggregation reduces
    to "any required failed → failed; else passed". The
    `passed_with_warnings` branch exists for forward-compat with
    Phase 3's optional / judge checks. `inconclusive` is reserved
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
