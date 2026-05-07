"""Execute a validation set against an ingestion run.

Phase 2 ships `DefaultValidationRunner`. It:

  1. Loops the set's test cases in priority order (smoke first).
  2. For each case, drives the existing `HybridQueryEngine` with
     `RunScope` so retrieval is restricted to artifacts produced
     by the run under test.
  3. Composes `ValidationCheckDTO[]` from the engine output using
     the Phase 1 deterministic check engine, plus Phase 2's
     case-specific checks (expected chunks, expected pages).
  4. Aggregates per-case statuses + a coverage breakdown into a
     `ValidationSummaryDTO`.
  5. Emits the lifecycle states to a callback (`pending` →
     `running` → `completed`/`failed`/`cancelled`) so the service
     can persist each transition without the runner having to know
     about the store.

Synchronous in-process execution. Hard cap on test-case count
enforced upstream — the runner trusts what it gets.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from j1.artifacts.registry import ArtifactRegistry
from j1.projects.context import ProjectContext
from j1.query.engine import HybridQueryEngine
from j1.query.models import QueryMode, QueryRequest
from j1.query.scope import RunScope
from j1.validation.checks import aggregate_status, run_checks
from j1.validation.dtos import (
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationCoverageDTO,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
    ValidationStatus,
    ValidationSummaryDTO,
    ValidationTestCaseDTO,
)

_log = logging.getLogger("j1.validation.runner")


# Phase 2 check augmentations layered on top of Phase 1's six.
# They run as `required` checks when the case carries the
# corresponding expected_* field; absent expected lists make the
# check a no-op (the DTO is omitted, NOT included-and-passing — same
# semantics as `citation_present`).
_CHECK_EXPECTED_CHUNK_IN_TOPK = "expected_chunk_in_topk"
_CHECK_EXPECTED_PAGE_IN_CITATIONS = "expected_page_in_citations"


# Synchronous in-process limit. Matches the Phase 2 plan's "≤ 50
# cases per run" decision. The REST handler also clamps; this is
# defense-in-depth so a stand-alone caller (test, future async
# path) gets the same guarantee.
MAX_CASES_PER_RUN = 50

# Score floor used to categorise low-confidence retrieval. Tunable
# per profile in a future phase; Phase 2 ships a constant. Below
# this BM25-rank-derived floor we still pass `retrieved_chunks_present`
# but flag it as a soft signal in `recommended_action`.
_LOW_CONFIDENCE_SCORE_FLOOR = 0.0  # placeholder — engine doesn't surface scores yet

# Cap on the preview length surfaced on result rows. Mirrors the
# chunk projector for visual consistency on the FE.
_PREVIEW_MAX_CHARS = 240


class DefaultValidationRunner:
    """Drives one validation set to completion.

    Constructor takes the engine + artifact registry directly — the
    runner stays decoupled from REST, the store, and the audit log.
    The service layer composes those.

    `lifecycle_callback` is the seam for persistence: the runner
    calls it before/after running so the caller can upsert the
    pending / running / completed snapshot. Signature:
    `(vrun: ValidationRunDTO) -> None`. Default no-op so unit
    tests don't have to wire a store.
    """

    def __init__(
        self,
        *,
        query_engine: HybridQueryEngine,
        artifact_registry: ArtifactRegistry,
        lifecycle_callback: Callable[[ValidationRunDTO], None] | None = None,
    ) -> None:
        self._query_engine = query_engine
        self._artifacts = artifact_registry
        self._on_lifecycle = lifecycle_callback or (lambda _vrun: None)

    def run(
        self,
        ctx: ProjectContext,
        vset: ValidationSetDTO,
        *,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Execute every case in the set and return the terminal
        snapshot. Callers persist as they see fit — the lifecycle
        callback fires three times so a JSONL store can append the
        pending/running/completed snapshots atomically."""
        validation_run_id = f"vrun-{uuid.uuid4().hex[:12]}"
        started_at = _iso_now()

        # 1. pending — the set has been accepted, execution hasn't
        # started yet. Surfaces in the FE timeline immediately.
        pending = ValidationRunDTO(
            validation_run_id=validation_run_id,
            validation_set_id=vset.validation_set_id,
            run_id=vset.run_id,
            execution_status="pending",
            validation_status="inconclusive",
            started_at=started_at,
            completed_at=None,
            actor=actor,
            summary=ValidationSummaryDTO(),
            results=[],
        )
        self._safe_lifecycle(pending)

        # 2. running — actively executing. For Phase 2 this is a
        # narrow window (synchronous), but a long-running case
        # would let the FE render an "executing X/N" indicator.
        running = _replace_status(
            pending, execution_status="running",
        )
        self._safe_lifecycle(running)

        try:
            results = [
                self._execute_case(ctx, vset.run_id, case)
                for case in self._ordered_cases(vset.test_cases)
            ]
        except Exception as exc:  # noqa: BLE001 — surface as failed run
            failed = _replace_status(
                running,
                execution_status="failed",
                validation_status="inconclusive",
                completed_at=_iso_now(),
                failure_message=str(exc),
            )
            self._safe_lifecycle(failed)
            return failed

        summary = _build_summary(vset, results)
        completed = ValidationRunDTO(
            validation_run_id=validation_run_id,
            validation_set_id=vset.validation_set_id,
            run_id=vset.run_id,
            execution_status="completed",
            # The split: execution_status reports "the runner job
            # finished," validation_status reports "the document
            # passed the test cases." A run can be (completed, failed)
            # — that's the canonical "ran fine but didn't pass" case.
            validation_status=_aggregate_validation_status(results),
            started_at=started_at,
            completed_at=_iso_now(),
            actor=actor,
            summary=summary,
            results=results,
        )
        self._safe_lifecycle(completed)
        return completed

    # ---- Per-case execution --------------------------------------------

    def _execute_case(
        self,
        ctx: ProjectContext,
        run_id: str,
        case: ValidationTestCaseDTO,
    ) -> ValidationResultDTO:
        """Drive one test case end-to-end. Always returns a result —
        an engine exception becomes a `failed` result with the
        exception message in `failure_reason`."""
        try:
            top_k = max(10, len(case.expected_chunks) * 2)
            response = self._query_engine.query(
                ctx,
                QueryRequest(
                    question=case.question,
                    mode=QueryMode.AUTO,
                    max_results=top_k,
                    scope=RunScope(run_id=run_id),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "validation case %s engine failure: %s",
                case.test_case_id, exc,
            )
            return ValidationResultDTO(
                result_id=f"vr-{uuid.uuid4().hex[:10]}",
                test_case_id=case.test_case_id,
                status="failed",
                question=case.question,
                answer="",
                retrieved_chunks=[],
                citations=[],
                checks=[
                    ValidationCheckDTO(
                        name="engine_invocation",
                        severity="required",
                        passed=False,
                        detail=f"engine raised: {exc}",
                    ),
                ],
                failure_reason=f"Engine error: {exc}",
            )

        retrieved = _retrieved_chunks_from_response(response)
        citations = _citations_from_response(response)

        # Phase 1 deterministic checks (six).
        checks = run_checks(
            ctx=ctx,
            run_id=run_id,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            citation_required=case.citation_required,
            artifact_registry=self._artifacts,
        )
        # Phase 2 case-specific checks layered on top.
        if case.expected_chunks:
            checks.append(_check_expected_chunk_in_topk(case, retrieved))
        if case.expected_pages:
            checks.append(_check_expected_page_in_citations(case, citations))

        validation_status = aggregate_status(checks)
        result_status = _result_status_from_validation_status(validation_status)
        failure_reason = _failure_reason_from_checks(checks) if result_status == "failed" else None

        return ValidationResultDTO(
            result_id=f"vr-{uuid.uuid4().hex[:10]}",
            test_case_id=case.test_case_id,
            status=result_status,
            question=case.question,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            checks=checks,
            failure_reason=failure_reason,
        )

    # ---- Internals -----------------------------------------------------

    def _safe_lifecycle(self, vrun: ValidationRunDTO) -> None:
        """Lifecycle callback failures must not fail the run. The
        runner is the source of truth; persistence is best-effort."""
        try:
            self._on_lifecycle(vrun)
        except Exception:  # noqa: BLE001
            _log.debug(
                "lifecycle callback raised for vrun=%s",
                vrun.validation_run_id,
                exc_info=True,
            )

    @staticmethod
    def _ordered_cases(
        cases: list[ValidationTestCaseDTO],
    ) -> list[ValidationTestCaseDTO]:
        """Smoke first, then normal, then deep. Within a priority
        bucket, preserve the generator's original order — that's
        the document-section order, useful for visual scanning."""
        priority_rank = {"smoke": 0, "normal": 1, "deep": 2}
        return sorted(
            cases,
            key=lambda c: (priority_rank.get(c.priority, 99),),
        )


# ---- Phase 2 case-specific checks ----------------------------------


def _check_expected_chunk_in_topk(
    case: ValidationTestCaseDTO,
    retrieved: list[RetrievedChunkRefDTO],
) -> ValidationCheckDTO:
    """Required: at least one of `expected_chunks` must show up in
    the retrieved set's chunk_ids. Phase 2's headline assertion —
    'is the chunk we cited as ground-truth retrievable?'"""
    expected = set(case.expected_chunks)
    actual = {c.chunk_id for c in retrieved if c.chunk_id}
    overlap = expected & actual
    passed = bool(overlap)
    return ValidationCheckDTO(
        name=_CHECK_EXPECTED_CHUNK_IN_TOPK,
        severity="required",
        passed=passed,
        detail=(
            None if passed
            else (
                f"expected one of {sorted(expected)[:5]}; "
                f"got {sorted(actual)[:5]}"
            )
        ),
        expected=sorted(expected),
        actual=sorted(actual),
    )


def _check_expected_page_in_citations(
    case: ValidationTestCaseDTO,
    citations: list[ValidationCitationDTO],
) -> ValidationCheckDTO:
    """Required: at least one citation's `source_location` must
    overlap the case's `expected_pages`. Page locations come from
    the indexer's `source_location` column verbatim — we accept
    any string match (e.g. 'p.3', 'page-3', '3') since producers
    don't yet share a single page-format convention.
    """
    expected = {str(p) for p in case.expected_pages}
    actual = {
        str(c.source_location) for c in citations
        if c.source_location is not None
    }
    overlap = {
        actual_loc for actual_loc in actual
        if any(exp in actual_loc for exp in expected)
    }
    passed = bool(overlap)
    return ValidationCheckDTO(
        name=_CHECK_EXPECTED_PAGE_IN_CITATIONS,
        severity="required",
        passed=passed,
        detail=(
            None if passed
            else (
                f"expected one of pages {sorted(expected)}; "
                f"citation locations {sorted(actual)[:5]}"
            )
        ),
        expected=sorted(expected),
        actual=sorted(actual),
    )


# ---- Result/summary aggregation ------------------------------------


def _aggregate_validation_status(
    results: list[ValidationResultDTO],
) -> ValidationStatus:
    """Roll the per-case `status` field up into the run's
    `validation_status`. Strict precedence: any failed → failed;
    any warning → passed_with_warnings; otherwise passed.
    `skipped` doesn't affect the run-level status (that's the
    contract — a skipped modality check shouldn't gate the verdict)."""
    if not results:
        return "inconclusive"
    if any(r.status == "failed" for r in results):
        return "failed"
    if any(r.status == "warning" for r in results):
        return "passed_with_warnings"
    return "passed"


def _result_status_from_validation_status(
    status: ValidationStatus,
) -> str:
    """Map the per-case validation_status onto the per-result
    `status` field's narrower vocabulary. The pass/warning/fail
    triplet is what shows up in `summary.passed/warning/failed`,
    which is why `inconclusive` collapses to `failed` here — we
    don't want it counted in any other bucket."""
    if status == "passed":
        return "passed"
    if status == "passed_with_warnings":
        return "warning"
    return "failed"


def _failure_reason_from_checks(checks: list[ValidationCheckDTO]) -> str | None:
    """First failed required check's detail. Surfaces on the
    Result Detail drawer as the headline 'why did this fail?'
    string — testers shouldn't have to scan all checks to find
    the cause."""
    for c in checks:
        if not c.passed and c.severity == "required":
            return c.detail or f"check {c.name!r} failed"
    return None


def _build_summary(
    vset: ValidationSetDTO,
    results: list[ValidationResultDTO],
) -> ValidationSummaryDTO:
    """Compose the run-level summary. Counters reconcile to `total`;
    `main_issues` surfaces up to three failure detail strings to
    drive the Knowledge Readiness card's "what broke?" copy.
    `recommended_action` is a human-readable string the FE renders
    as the card subtitle."""
    total = len(results)
    counts = {"passed": 0, "warning": 0, "failed": 0, "skipped": 0}
    issues: list[str] = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "failed" and r.failure_reason and len(issues) < 3:
            issues.append(r.failure_reason)

    coverage = ValidationCoverageDTO(
        by_type=_count_by_field(vset.test_cases, "type"),
        by_priority=_count_by_field(vset.test_cases, "priority"),
    )

    if counts["failed"]:
        recommended = "block release until resolved"
    elif counts["warning"]:
        recommended = "review warnings"
    elif total == 0:
        recommended = "no test cases to evaluate"
    else:
        recommended = "ready"

    return ValidationSummaryDTO(
        total=total,
        passed=counts["passed"],
        warning=counts["warning"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        coverage=coverage,
        main_issues=issues,
        recommended_action=recommended,
    )


def _count_by_field(
    cases: list[ValidationTestCaseDTO], field_name: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        key = str(getattr(case, field_name, "") or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---- Engine response → DTO helpers ---------------------------------


def _retrieved_chunks_from_response(response: Any) -> list[RetrievedChunkRefDTO]:
    out: list[RetrievedChunkRefDTO] = []
    for source in getattr(response, "sources", []):
        title = str(getattr(source, "title", "") or "")
        out.append(
            RetrievedChunkRefDTO(
                artifact_id=source.artifact_id,
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
                document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                score=0.0,
                preview=title[:_PREVIEW_MAX_CHARS],
            )
        )
    return out


def _citations_from_response(response: Any) -> list[ValidationCitationDTO]:
    out: list[ValidationCitationDTO] = []
    for source in getattr(response, "sources", []):
        out.append(
            ValidationCitationDTO(
                artifact_id=source.artifact_id,
                artifact_type=source.artifact_type,
                source_document_id=getattr(source, "source_document_id", None),
                source_location=getattr(source, "source_location", None),
                chunk_id=getattr(source, "chunk_id", None),
                run_id=getattr(source, "run_id", None),
            )
        )
    return out


# ---- Misc ---------------------------------------------------------


def _replace_status(
    vrun: ValidationRunDTO,
    *,
    execution_status: str | None = None,
    validation_status: str | None = None,
    completed_at: str | None = None,
    failure_message: str | None = None,
) -> ValidationRunDTO:
    """Return a new `ValidationRunDTO` with the named fields swapped.
    Avoids `dataclasses.replace` so callers don't import `dataclasses`
    just to mutate one field."""
    return ValidationRunDTO(
        validation_run_id=vrun.validation_run_id,
        validation_set_id=vrun.validation_set_id,
        run_id=vrun.run_id,
        execution_status=execution_status if execution_status is not None else vrun.execution_status,  # type: ignore[arg-type]
        validation_status=validation_status if validation_status is not None else vrun.validation_status,  # type: ignore[arg-type]
        started_at=vrun.started_at,
        completed_at=completed_at if completed_at is not None else vrun.completed_at,
        actor=vrun.actor,
        summary=vrun.summary,
        results=vrun.results,
        failure_message=failure_message if failure_message is not None else vrun.failure_message,
        metadata=vrun.metadata,
    )


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
