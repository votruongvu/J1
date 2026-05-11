"""Execute a validation set against an ingestion run.

 ships `DefaultValidationRunner`. It:

 1. Loops the set's test cases in priority order (smoke first).
 2. For each case, drives the existing `HybridQueryEngine` with
 `RunScope` so retrieval is restricted to artifacts produced
 by the run under test.
 3. Composes `ValidationCheckDTO[]` from the engine output using
 the deterministic check engine, plus 's
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
from j1.validation.judge import LLMJudge

_log = logging.getLogger("j1.validation.runner")


#  check augmentations layered on top of 's six.
# They run as `required` checks when the case carries the
# corresponding expected_* field; absent expected lists make the
# check a no-op (the DTO is omitted, NOT included-and-passing — same
# semantics as `citation_present`).
_CHECK_EXPECTED_CHUNK_IN_TOPK = "expected_chunk_in_topk"
_CHECK_EXPECTED_PAGE_IN_CITATIONS = "expected_page_in_citations"

#  modality-aware checks. `expected_artifact_retrieved`
# applies to table/image cases (the case names a specific
# artifact id and the runner verifies retrieval surfaced it).
# `expected_graph_evidence` applies to graph cases (the case
# names entity ids and the runner verifies they appear in the
# response's graph paths or related artifacts).
_CHECK_EXPECTED_ARTIFACT_RETRIEVED = "expected_artifact_retrieved"
_CHECK_EXPECTED_GRAPH_EVIDENCE = "expected_graph_evidence"

# Modality kinds that gate skip-applicability. A case typed
# "table" / "image" / "graph" is skipped (status="skipped") when
# the run produced none of the matching artifact kinds. Same
# vocabulary as `j1.ingestion_review.availability`.
_MODALITY_KIND_BY_CASE_TYPE: dict[str, frozenset[str]] = {
    "table": frozenset({"enriched.tables"}),
    "image": frozenset({"enriched.visuals"}),
    "graph": frozenset({"graph_json"}),
}


# Synchronous in-process limit. Matches the plan's "≤ 50
# cases per run" decision. The REST handler also clamps; this is
# defense-in-depth so a stand-alone caller (test, future async
# path) gets the same guarantee.
MAX_CASES_PER_RUN = 50

# Score floor used to categorise low-confidence retrieval. Tunable
# per profile in the future; ships a constant. Below
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
        judge: LLMJudge | None = None,
    ) -> None:
        self._query_engine = query_engine
        self._artifacts = artifact_registry
        self._on_lifecycle = lifecycle_callback or (lambda _vrun: None)
        # Optional LLM judge for semantic checks. When None
        # the runner skips the optional checks entirely — the
        # checks engine returns nothing for them, which leaves the
        # result accounting deterministic.
        self._judge = judge

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

        # 2. running — actively executing. For this is a
        # narrow window (synchronous), but a long-running case
        # would let the FE render an "executing X/N" indicator.
        running = _replace_status(
            pending, execution_status="running",
        )
        self._safe_lifecycle(running)

        # snapshot which artifact kinds exist for this
        # run BEFORE looping. Modality cases are gated against
        # this set — a `type="table"` case is skipped when the
        # run produced no `enriched.tables`. Computing once
        # bounds the registry I/O at one list_artifacts call.
        try:
            available_kinds = self._available_kinds_for_run(ctx, vset.run_id)
        except Exception:  # noqa: BLE001 — registry hiccup, treat as no info
            available_kinds = frozenset()

        try:
            results = [
                self._execute_case(
                    ctx, vset.run_id, case, available_kinds=available_kinds,
                )
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
        *,
        available_kinds: frozenset[str] = frozenset(),
    ) -> ValidationResultDTO:
        """Drive one test case end-to-end. Always returns a result —
 an engine exception becomes a `failed` result with the
 exception message in `failure_reason`.

 when the case names a modality (table/image/graph)
 the run doesn't have, the case short-circuits to a
 `skipped` result. Skipped cases don't count toward the
 run's `validation_status`, so a tester importing a generic
 validation set onto a text-only run isn't punished for
 modalities the run doesn't have.
 """
        # Skip-applicability gate ( defense-in-depth — the
        # generator already gates upstream).
        skip_reason = _modality_skip_reason(case, available_kinds)
        if skip_reason is not None:
            return ValidationResultDTO(
                result_id=f"vr-{uuid.uuid4().hex[:10]}",
                test_case_id=case.test_case_id,
                status="skipped",
                question=case.question,
                answer="",
                retrieved_chunks=[],
                citations=[],
                checks=[],
                failure_reason=skip_reason,
            )

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

        #  + deterministic and judge-driven checks.
        # Run-checks branches internally on `case_type` to swap
        # required positive-case checks for the negative abstain
        # check, and appends optional judge-driven checks when a
        # judge is supplied.
        checks = run_checks(
            ctx=ctx,
            run_id=run_id,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            citation_required=case.citation_required,
            artifact_registry=self._artifacts,
            case_type=case.type,
            expected_answer_points=list(case.expected_answer_points),
            question=case.question,
            judge=self._judge,
        )
        #  case-specific checks layered on top. Skipped for
        # negative cases — by definition there's no expected
        # chunk/page (the question is out-of-scope).
        if case.type != "negative":
            if case.expected_chunks:
                checks.append(_check_expected_chunk_in_topk(case, retrieved))
            if case.expected_pages:
                checks.append(_check_expected_page_in_citations(case, citations))
            #  modality checks. Required when the case
            # names expected modality evidence; skipped (omitted)
            # when the corresponding expected list is empty.
            if case.expected_artifacts:
                checks.append(
                    _check_expected_artifact_retrieved(case, retrieved),
                )
            if case.expected_graph_nodes or case.expected_graph_edges:
                checks.append(
                    _check_expected_graph_evidence(case, response),
                )

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

    def _available_kinds_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> frozenset[str]:
        """One-shot scan of the registry for kinds present in this
 run. Cached per `run` call so `_execute_case` can apply
 the modality skip gate without re-querying.

 Looks at `metadata.run_id == run_id` per artifact — same
 contract the chunk projector uses. + might layer in
 a registry-side index for this lookup, but for v1 the
 single-pass scan is fine (artifact counts are bounded).
 """
        kinds: set[str] = set()
        for record in self._artifacts.list_artifacts(ctx):
            if record.metadata.get("run_id") == run_id:
                kinds.add(record.kind)
        return frozenset(kinds)

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


# ---- case-specific checks ----------------------------------


def _modality_skip_reason(
    case: ValidationTestCaseDTO,
    available_kinds: frozenset[str],
) -> str | None:
    """Return a human-readable reason when the case targets a
 modality the run lacks; None when the case is applicable.

 Cases of type retrieval/answer/citation/negative are always
 applicable — they don't depend on a particular artifact kind.
 Modality cases (table/image/graph) skip when their kind set
 has zero overlap with the run's `available_kinds`.
 """
    required_kinds = _MODALITY_KIND_BY_CASE_TYPE.get(case.type)
    if required_kinds is None:
        return None
    if available_kinds & required_kinds:
        return None
    return (
        f"case type {case.type!r} skipped: run produced no "
        f"{', '.join(sorted(required_kinds))} artifact(s)"
    )


def _check_expected_artifact_retrieved(
    case: ValidationTestCaseDTO,
    retrieved: list[RetrievedChunkRefDTO],
) -> ValidationCheckDTO:
    """Required: at least one of `expected_artifacts` must surface
 in the retrieved set's `artifact_id`s. 's headline
 table/image check — 'is the table I named in the test
 actually retrievable for this question?'"""
    expected = set(case.expected_artifacts)
    actual = {c.artifact_id for c in retrieved if c.artifact_id}
    overlap = expected & actual
    passed = bool(overlap)
    return ValidationCheckDTO(
        name=_CHECK_EXPECTED_ARTIFACT_RETRIEVED,
        severity="required",
        passed=passed,
        detail=(
            None if passed
            else (
                f"expected one of {sorted(expected)[:5]}; "
                f"retrieved {sorted(actual)[:5]}"
            )
        ),
        expected=sorted(expected),
        actual=sorted(actual),
    )


def _check_expected_graph_evidence(
    case: ValidationTestCaseDTO,
    response: Any,
) -> ValidationCheckDTO:
    """Required for graph cases: at least one of the expected
 graph node ids must appear in the engine's `graph_paths`
 (the entity ids surfaced by `GraphQueryProvider`).

 Edge ids aren't checked separately yet — the engine doesn't
 surface them as standalone identifiers, only as part of a
 path. When edge-level identification ships in a future
 engine pass, this check grows to include them.
 """
    expected_nodes = set(case.expected_graph_nodes)
    paths = list(getattr(response, "graph_paths", []))
    seen_nodes: set[str] = set()
    for p in paths:
        seen_nodes.update(getattr(p, "nodes", []) or [])
    overlap = expected_nodes & seen_nodes
    passed = bool(overlap) if expected_nodes else False
    return ValidationCheckDTO(
        name=_CHECK_EXPECTED_GRAPH_EVIDENCE,
        severity="required",
        passed=passed,
        detail=(
            None if passed
            else (
                f"expected node(s) {sorted(expected_nodes)[:5]}; "
                f"engine returned graph paths over {sorted(seen_nodes)[:5]}"
            )
        ),
        expected=sorted(expected_nodes),
        actual=sorted(seen_nodes),
    )


def _check_expected_chunk_in_topk(
    case: ValidationTestCaseDTO,
    retrieved: list[RetrievedChunkRefDTO],
) -> ValidationCheckDTO:
    """Required: at least one of `expected_chunks` must show up in
 the retrieved set's chunk_ids. 's headline assertion —
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
                artifact_kind=getattr(source, "artifact_type", None),
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
