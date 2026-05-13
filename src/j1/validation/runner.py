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
    EvidenceBlockDTO,
    LLMTraceDTO,
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
from j1.validation.evidence import build_evidence_blocks
from j1.validation.judge import LLMJudge
from j1.validation.synthesis import AnswerSynthesizer
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation.runner")


# ---- Unmistakable live-path markers -------------------------------
#
# Two structured logs that fire at the exact entry and exit points
# of the live retrieval-quality pipeline. Operators chasing
# "did the new code path run for this query?" search the audit log
# for these stable event names — separate from the planner's per-
# stage ``j1.retrieval.*`` events so they're easy to grep even when
# the broader retrieval stream is voluminous.
EVENT_LIVE_PATH_ENTERED = "j1.retrieval.live_path.entered"
EVENT_LIVE_PATH_EVIDENCE_SENT = "j1.retrieval.live_path.evidence_sent"


def _emit_live_path_entered(
    *,
    audit,
    ctx,
    endpoint: str,
    handler: str,
    run_id: str | None,
    document_id: str | None,
    query: str,
    retrieval_mode: str,
) -> None:
    """Always-safe live-path entry log. Best-effort: any failure is
    logged at WARNING and ignored."""
    if audit is None:
        # Still emit a Python log line so a developer tailing the
        # service log can see the path was hit even without audit
        # wiring.
        _log.info(
            "%s endpoint=%s handler=%s run_id=%s document_id=%s "
            "retrieval_mode=%s query_chars=%d",
            EVENT_LIVE_PATH_ENTERED, endpoint, handler,
            run_id, document_id, retrieval_mode,
            len(query or ""),
        )
        return
    try:
        audit.record(
            ctx,
            actor="system",
            action=EVENT_LIVE_PATH_ENTERED,
            target_kind="retrieval_query",
            target_id=run_id or "no-run",
            payload={
                "endpoint": endpoint,
                "handler": handler,
                "run_id": run_id,
                "document_id": document_id,
                "retrieval_mode": retrieval_mode,
                "query_chars": len(query or ""),
            },
        )
    except Exception:  # noqa: BLE001
        _log.warning(
            "live_path.entered audit emit failed", exc_info=True,
        )


def _emit_live_path_evidence_sent(
    *,
    audit,
    ctx,
    endpoint: str,
    handler: str,
    run_id: str | None,
    document_id: str | None,
    evidence,
    snapshot,
) -> None:
    """Always-safe pre-LLM marker. Carries:
      * evidence ids + artifact_types + section_paths
      * intent (from snapshot)
      * planner_used flag (True iff a structured intent fired)
      * fallback_triggered (from snapshot)
    """
    finalized = getattr(snapshot, "finalized_summary", {}) or {}
    intent = getattr(snapshot, "intent", None)
    structured_intents = frozenset({
        "responsibility_mapping", "dependency_mapping",
        "stage_progression", "deliverable_mapping",
        "issue_risk_mapping", "decision_trace",
        "list_extraction",
    })
    planner_used = intent in structured_intents
    payload = {
        "endpoint": endpoint,
        "handler": handler,
        "run_id": run_id,
        "document_id": document_id,
        "intent": intent,
        "planner_used": planner_used,
        "fallback_triggered": finalized.get("fallback_triggered"),
        "fallback_succeeded": finalized.get("fallback_succeeded"),
        "evidence": [
            {
                "artifact_id": b.artifact_id,
                "artifact_type": b.artifact_type,
                "section_path": (
                    getattr(b, "section", None)
                    or getattr(b, "source_location", None)
                ),
            }
            for b in (evidence or [])
        ],
        "evidence_count": len(evidence or []),
        "check_failures": finalized.get("check_failures") or [],
        "check_failures_before_fallback": (
            finalized.get("check_failures_before_fallback") or []
        ),
    }
    if audit is None:
        _log.info(
            "%s endpoint=%s handler=%s run_id=%s intent=%s "
            "planner_used=%s fallback_triggered=%s evidence_count=%d",
            EVENT_LIVE_PATH_EVIDENCE_SENT, endpoint, handler,
            run_id, intent, planner_used,
            finalized.get("fallback_triggered"),
            len(evidence or []),
        )
        return
    try:
        audit.record(
            ctx,
            actor="system",
            action=EVENT_LIVE_PATH_EVIDENCE_SENT,
            target_kind="retrieval_query",
            target_id=run_id or "no-run",
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        _log.warning(
            "live_path.evidence_sent audit emit failed", exc_info=True,
        )


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
        answer_synthesizer: AnswerSynthesizer | None = None,
        synthesize_answers: bool = True,
        workspace: WorkspaceResolver | None = None,
        audit: "Any | None" = None,
        smart_query_orchestrator: "Any | None" = None,
    ) -> None:
        self._query_engine = query_engine
        self._artifacts = artifact_registry
        # SmartQueryOrchestrator — when wired, every per-case
        # execution flows through the orchestrator pipeline (intent
        # classifier → routes → evidence pack → sufficiency gate →
        # synth → citation binder → quality gate). The legacy
        # ``run_checks`` / ``aggregate_status`` path is bypassed
        # entirely. Case-specific checks
        # (``expected_chunk_in_topk`` / ``expected_page`` /
        # ``expected_artifact`` / graph evidence) still run on top
        # of the orchestrator's retrieved set so existing batch
        # validation sets keep their semantics.
        self._smart_query_orchestrator = smart_query_orchestrator
        # Workspace is needed to load real artifact body text for
        # the synthesizer. When None (legacy callers / tests), the
        # runner falls back to the engine's preview text — same
        # broken behaviour as before, but at least non-crashing.
        # Production wiring MUST pass it.
        self._workspace = workspace
        # Audit recorder for the retrieval-quality diagnostic event
        # stream. When None, the runner still uses the planner-
        # driven path but stops short of emitting
        # ``j1.retrieval.*`` events. Production wiring SHOULD pass
        # this so the validation tab's audit timeline carries the
        # new events.
        self._audit = audit
        self._on_lifecycle = lifecycle_callback or (lambda _vrun: None)
        # Optional LLM judge for semantic checks. When None
        # the runner skips the optional checks entirely — the
        # checks engine returns nothing for them, which leaves the
        # result accounting deterministic.
        self._judge = judge
        # Optional LLM answer synthesizer. When wired AND
        # `synthesize_answers=True`, the runner replaces the
        # provider's raw composed answer (e.g. "Knowledge results
        # for: <q>\n- title: preview…") with a grounded LLM answer
        # before persisting the result. When None or opt-out, the
        # runner returns the engine's raw answer — preserving the
        # deterministic-replay path for CI / regression runs.
        self._synthesizer = answer_synthesizer
        self._synthesize_answers = synthesize_answers

    def run(
        self,
        ctx: ProjectContext,
        vset: ValidationSetDTO,
        *,
        actor: str = "system",
        active_document_id: str | None = None,
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
                    ctx, vset.run_id, case,
                    available_kinds=available_kinds,
                    active_document_id=active_document_id,
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
        active_document_id: str | None = None,
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
        # Unmistakable live-path entry marker. Operators looking
        # for "did the new retrieval pipeline run for this query?"
        # search the audit log for this event. Fires before any
        # short-circuit (skip / engine error) so a missing event
        # means the case never reached this handler at all.
        _emit_live_path_entered(
            audit=self._audit, ctx=ctx,
            endpoint="validation.set.run",
            handler="DefaultValidationRunner._execute_case",
            run_id=run_id,
            document_id=active_document_id,
            query=case.question,
            retrieval_mode="planner_first" if active_document_id else "legacy",
        )

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

        # New SmartQueryOrchestrator branch — replaces the legacy
        # query_engine + run_checks + aggregate_status path. The
        # orchestrator owns refusal detection, evidence sufficiency,
        # and citation binding via explicit gates. Case-specific
        # expected_*-checks still layer on top.
        if self._smart_query_orchestrator is not None:
            return self._execute_case_via_orchestrator(
                ctx=ctx, run_id=run_id, case=case,
                active_document_id=active_document_id,
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
        # ---- Targeted re-retrieval for stage-progression queries ----
        # When the user's query contains stage-progression markers
        # (percentages / "conceptual" / "final" / etc.) AND the
        # first pass didn't surface chunks that mention them, run
        # ONE expanded query — boosting the user's own anchors so
        # BM25 / lexical signals upweight chunks containing those
        # terms. Bounded: max one retry, no recursion. ANCHORS
        # COME FROM THE QUERY (never a hardcoded domain
        # dictionary).
        retrieved, citations = self._maybe_retry_with_anchor_expansion(
            ctx=ctx, run_id=run_id, case=case,
            retrieved=retrieved, citations=citations,
            initial_top_k=top_k,
        )
        # Populate citation previews from real artifact body text
        # (chunk NDJSON, compiled.text files, document_map prose).
        # The groundedness judge consumes ``citation.preview`` to
        # compare against the answer's claims; without a non-empty
        # preview the judge has nothing to verify against and
        # over-flags every claim as "unsupported" — the false
        # positive operators saw on otherwise-grounded answers.
        # Mirrors the synthesizer's body-loading via
        # ``build_evidence_blocks``; safe no-op when workspace
        # isn't wired.
        citations = self._enrich_citations_with_preview(
            ctx=ctx, retrieved=retrieved, citations=citations,
        )

        # Optional LLM synthesis on top of the engine's raw answer.
        # Positive cases benefit most — the engine returns a
        # debug-style "Knowledge results for: <q>\n- title: …"
        # string that's useless for graded answer-coverage checks.
        # Negative cases skip synthesis (the engine's abstention
        # text is already what the runner wants to grade).
        synthesized_answer, llm_trace = self._maybe_synthesize_for_case(
            ctx=ctx,
            case=case,
            retrieved=retrieved,
            run_id=run_id,
            active_document_id=active_document_id,
        )
        final_answer = synthesized_answer or response.answer

        #  + deterministic and judge-driven checks.
        # Run-checks branches internally on `case_type` to swap
        # required positive-case checks for the negative abstain
        # check, and appends optional judge-driven checks when a
        # judge is supplied.
        checks = run_checks(
            ctx=ctx,
            run_id=run_id,
            answer=final_answer,
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
            answer=final_answer,
            retrieved_chunks=retrieved,
            citations=citations,
            checks=checks,
            failure_reason=failure_reason,
            raw_answer=response.answer if synthesized_answer else None,
            llm=llm_trace,
        )

    def _execute_case_via_orchestrator(
        self,
        *,
        ctx: ProjectContext,
        run_id: str,
        case: ValidationTestCaseDTO,
        active_document_id: str | None,
    ) -> ValidationResultDTO:
        """Per-case execution via SmartQueryOrchestrator.

        Replaces the legacy ``query_engine.query`` + ``run_checks``
        + ``aggregate_status`` chain. The orchestrator owns answer
        quality decisions; the runner still layers on case-specific
        ``expected_*`` checks (chunks / pages / artifacts / graph)
        that lock test-set authoring intent.

        Negative cases get special handling: the legacy
        ``negative_answer_abstains`` check passes when the answer
        IS a refusal. We map that by inverting the orchestrator's
        ``answer_not_refusal`` gate."""
        from j1.query.orchestrator import OrchestratorRequest
        from j1.query.scope import RunScope as _RunScope
        from j1.validation.service import (
            _checks_from_gate_results,
            _retrieved_chunks_from_trace,
            _citations_from_orchestrator,
            _validation_status_from_final,
        )

        try:
            result = self._smart_query_orchestrator.run(OrchestratorRequest(
                ctx=ctx,
                question=case.question,
                scope=_RunScope(run_id=run_id),
                run_id=run_id,
                document_id=active_document_id,
            ))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "validation case %s orchestrator failure: %s",
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
                        name="orchestrator_invocation",
                        severity="required",
                        passed=False,
                        detail=f"orchestrator raised: {exc}",
                    ),
                ],
                failure_reason=f"Orchestrator error: {exc}",
            )

        retrieved = _retrieved_chunks_from_trace(result.trace)
        citations_dicts = _citations_from_orchestrator(result)
        # Convert citation dicts → ValidationCitationDTO so the
        # existing case-specific checks (which take
        # ``list[ValidationCitationDTO]``) keep working.
        citations: list[ValidationCitationDTO] = [
            ValidationCitationDTO(
                artifact_id=c["artifactId"],
                artifact_type=c["artifactType"],
                source_document_id=c.get("sourceDocumentId"),
                source_location=c.get("sourceLocation"),
                chunk_id=c.get("chunkId"),
                run_id=c.get("runId"),
            )
            for c in citations_dicts
        ]
        # Same body-preview enrichment as the legacy path — case-
        # specific checks and judges need real text to verify
        # against, not empty previews.
        citations = self._enrich_citations_with_preview(
            ctx=ctx, retrieved=retrieved, citations=citations,
        )
        # Map orchestrator gate results → ValidationCheckDTOs.
        checks = list(_checks_from_gate_results(result.gate_results))

        # Negative-case semantics: an abstaining answer IS the
        # ideal outcome on a negative test. The orchestrator's
        # ``answer_not_refusal`` gate fires opposite to what we
        # want — invert it for negative cases by appending an
        # explicit ``negative_answer_abstains`` check the way
        # batch validation has always graded them.
        if case.type == "negative":
            from j1.validation.checks import _is_abstain_response
            abstained = _is_abstain_response(result.answer or "")
            checks = [
                c for c in checks
                if c.name != "answer_not_refusal"
            ]
            checks.append(ValidationCheckDTO(
                name="negative_answer_abstains",
                severity="required",
                passed=abstained,
                detail=(
                    None if abstained
                    else "negative case expected an abstention "
                         "but the synthesizer produced an answer"
                ),
                expected="abstain / refusal / empty",
                actual=(result.answer or "")[:120],
            ))

        # Case-specific checks (orthogonal to synthesis).
        if case.type != "negative":
            if case.expected_chunks:
                checks.append(
                    _check_expected_chunk_in_topk(case, retrieved),
                )
            if case.expected_pages:
                checks.append(
                    _check_expected_page_in_citations(case, citations),
                )
            if case.expected_artifacts:
                checks.append(
                    _check_expected_artifact_retrieved(case, retrieved),
                )
            if case.expected_graph_nodes or case.expected_graph_edges:
                # The graph-evidence check expects a ``QueryResponse``-
                # shaped object with ``graph_paths``. Construct a
                # minimal stand-in from the trace.
                checks.append(
                    _check_expected_graph_evidence(
                        case, _OrchestratorGraphView(result.trace),
                    ),
                )

        # Composite verdict — propagate the orchestrator's
        # final_status verdict UNLESS a case-specific check fails.
        validation_status = _validation_status_from_final(
            result.final_status,
        )
        # Any case-specific check failure flips status to failed
        # (matches the legacy aggregate_status rule).
        for c in checks:
            if (
                c.severity == "required"
                and not c.passed
                and not c.skipped
            ):
                validation_status = "failed"
                break
        result_status = _result_status_from_validation_status(
            validation_status,
        )
        failure_reason = (
            _failure_reason_from_checks(checks)
            if result_status == "failed" else None
        )
        return ValidationResultDTO(
            result_id=f"vr-{uuid.uuid4().hex[:10]}",
            test_case_id=case.test_case_id,
            status=result_status,
            question=case.question,
            answer=result.answer or "",
            retrieved_chunks=retrieved,
            citations=citations,
            checks=checks,
            failure_reason=failure_reason,
            raw_answer=None,
            llm=None,
        )

    def _maybe_retry_with_anchor_expansion(
        self,
        *,
        ctx: ProjectContext,
        run_id: str,
        case,
        retrieved: list,
        citations: list,
        initial_top_k: int,
    ) -> tuple[list, list]:
        """One-pass targeted re-retrieval for stage-progression
        queries that didn't surface anchor-bearing chunks.

        The mechanism:

          1. Extract stage anchors from the case question
             (``query_stage_anchors``). Empty result → no retry.
          2. Scan the FIRST PASS retrieved set's previews for
             anchor coverage. If ≥2 anchors are already present,
             no retry needed.
          3. Build the expanded query
             (``expand_query_with_anchors``) — original query +
             user's own anchors as boost terms.
          4. Run ONE more ``query_engine.query`` with a larger
             ``max_results`` so the re-ranker has more raw recall.
          5. Merge the new candidates onto the original (dedupe
             by artifact_id), preserving the original ordering
             plus the new additions.

        No-op (returns ``(retrieved, citations)`` unchanged) when:
          * the query has no stage anchors,
          * the first pass already covers ≥ 2 anchors,
          * the engine raises on the retry,
          * the merge yields no new candidates.

        Strict bounding: this method is called once per case; it
        does not loop. Audit consumers can see the retry by
        comparing retrieved counts before/after."""
        try:
            from j1.retrieval.anchors import (
                expand_query_with_anchors, pack_anchor_coverage,
                query_stage_anchors,
            )
            from j1.query.models import QueryRequest, QueryMode
            from j1.query.scope import RunScope
        except Exception:  # noqa: BLE001
            return retrieved, citations

        anchors = query_stage_anchors(case.question)
        if not anchors:
            return retrieved, citations

        # First-pass anchor coverage. Use the retrieval preview
        # text — it's the only body field present on
        # ``RetrievedChunkRefDTO`` without an artifact lookup.
        previews = [
            (getattr(r, "preview", None) or "") for r in retrieved
        ]
        _matched, covered = pack_anchor_coverage(
            previews, anchors.all,
        )
        if covered >= 2:
            return retrieved, citations  # adequate

        # Re-query with the user's own anchors appended.
        expanded = expand_query_with_anchors(case.question, anchors)
        # Bumped max_results so the re-ranker has more candidates
        # to work with. Capped to keep the call cheap.
        retry_top_k = max(initial_top_k * 2, 20)
        try:
            retry_response = self._query_engine.query(
                ctx,
                QueryRequest(
                    question=expanded,
                    mode=QueryMode.AUTO,
                    max_results=retry_top_k,
                    scope=RunScope(run_id=run_id),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "anchor-expanded retry failed for case %s: %s",
                getattr(case, "test_case_id", "?"), exc,
            )
            return retrieved, citations

        retry_retrieved = _retrieved_chunks_from_response(retry_response)
        retry_citations = _citations_from_response(retry_response)
        if not retry_retrieved:
            return retrieved, citations

        # Merge: first-pass first (preserves original ordering),
        # then any new candidates from the retry. Dedupe on
        # artifact_id + chunk_id pair.
        seen = {
            (r.artifact_id, getattr(r, "chunk_id", None))
            for r in retrieved
        }
        merged = list(retrieved)
        for r in retry_retrieved:
            key = (r.artifact_id, getattr(r, "chunk_id", None))
            if key not in seen:
                merged.append(r)
                seen.add(key)

        merged_citations = list(citations)
        cit_seen = {
            getattr(c, "artifact_id", None) for c in citations
        }
        for c in retry_citations:
            if getattr(c, "artifact_id", None) not in cit_seen:
                merged_citations.append(c)
                cit_seen.add(getattr(c, "artifact_id", None))

        # Audit-friendly log line so an operator inspecting the
        # case can see the retry fired + how many new candidates
        # it added.
        _log.info(
            "j1.retrieval.anchor_retry case=%s anchors=%s "
            "first_pass_covered=%d first_pass_count=%d "
            "retry_added=%d",
            getattr(case, "test_case_id", "?"),
            list(anchors.all),
            covered, len(retrieved),
            len(merged) - len(retrieved),
        )
        return merged, merged_citations

    def _maybe_synthesize_for_case(
        self,
        *,
        ctx: ProjectContext,
        case: ValidationTestCaseDTO,
        retrieved: list[RetrievedChunkRefDTO],
        run_id: str | None = None,
        active_document_id: str | None = None,
    ) -> tuple[str | None, LLMTraceDTO | None]:
        """Run the LLM synthesizer for this case when wired + enabled.

 Returns `(synthesized_answer, trace)`. Both `None` when:
   * the runner wasn't constructed with a synthesizer
   * `synthesize_answers=False` was set on the runner
   * the case type is `negative` (the runner needs the engine's
     abstention text verbatim for the abstain check to grade)
   * no chunks were retrieved (no evidence to ground on)

 The synthesizer's internal failure modes (LLM raise, empty
 answer) surface on the trace via `error`; the caller falls
 back to the raw engine answer in that case.
 """
        if self._synthesizer is None or not self._synthesize_answers:
            return (None, None)
        if case.type == "negative":
            return (None, None)
        if not retrieved:
            return (None, None)

        # CRITICAL: load REAL body text for each retrieved chunk
        # before sending to the synthesizer. Earlier this path used
        # ``r.preview`` (which is just the artifact title like
        # "compiled.text/b7e57…") as evidence text — so the LLM
        # saw a list of titles, not content, and correctly replied
        # "Not in the retrieved evidence." That was the failure
        # mode the latest validation report flagged on every
        # retrieval/smoke case despite citations including chunk +
        # compiled.text. The manual-query path already uses
        # ``build_evidence_blocks`` for real text; we use the same
        # helper here so both paths share one evidence pipeline.
        #
        # ``build_evidence_blocks`` requires a workspace +
        # path-resolver. When the runner wasn't wired with a
        # workspace (legacy test fixtures, deployments that
        # predate this fix), fall back to the title-only evidence —
        # same broken behaviour as before, with a WARNING logged
        # so operators can spot the misconfiguration.
        evidence: list[EvidenceBlockDTO]
        if self._workspace is not None:
            from pathlib import Path, PurePosixPath

            def _resolver(record):
                location = record.location
                parts = PurePosixPath(location).parts
                if len(parts) < 2:
                    return Path(location)
                area_name, *rest = parts
                area = WorkspaceArea(area_name)
                return self._workspace.area(  # type: ignore[union-attr]
                    ctx, area,
                ).joinpath(*rest)

            # Build the retrieval-quality diagnostic collector for
            # THIS case. Carries the active scope (doc+run) so
            # ``build_evidence_blocks`` enables the planner-first
            # path, boilerplate demotion, source grounding, and
            # quality_check / fallback pipeline. ``audit`` may be
            # None — events still record in-memory, just no audit
            # emit. ``query`` from the case so the intent router
            # can classify.
            from j1.retrieval.diagnostics import RetrievalDiagnostics
            diag = RetrievalDiagnostics(
                audit=self._audit, ctx=ctx,
                run_id=run_id, document_id=active_document_id,
                query=case.question,
            )
            evidence = build_evidence_blocks(
                ctx=ctx,
                retrieved=retrieved,
                artifact_registry=self._artifacts,
                path_resolver=_resolver,
                query=case.question,
                active_document_id=active_document_id,
                active_run_id=run_id,
                diagnostics=diag,
            )
            # Unmistakable pre-LLM marker so an operator inspecting
            # the audit log knows EXACTLY what evidence reached
            # the synthesizer for this case.
            _emit_live_path_evidence_sent(
                audit=self._audit, ctx=ctx,
                endpoint="validation.set.run",
                handler="DefaultValidationRunner._maybe_synthesize_for_case",
                run_id=run_id,
                document_id=active_document_id,
                evidence=evidence,
                snapshot=diag.snapshot(),
            )
        else:
            _log.warning(
                "validation runner: no workspace wired — synthesizer "
                "will see artifact titles instead of body text, which "
                "causes false 'Not in the retrieved evidence' "
                "fallbacks. Pass workspace=... to DefaultValidationRunner."
            )
            evidence = [
                EvidenceBlockDTO(
                    artifact_id=r.artifact_id,
                    artifact_type=r.artifact_kind or "chunk",
                    text=r.preview or "",
                    chunk_id=r.chunk_id,
                    score=r.score,
                )
                for r in retrieved
                if (r.preview or "").strip()
            ]
        if not evidence:
            return (None, None)
        result = self._synthesizer.synthesize(
            question=case.question,
            evidence=evidence,
        )
        trace = LLMTraceDTO(
            called=True,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            error=result.error,
        )
        return (result.answer, trace)

    def _enrich_citations_with_preview(
        self,
        *,
        ctx: ProjectContext,
        retrieved: list[RetrievedChunkRefDTO],
        citations: list[ValidationCitationDTO],
    ) -> list[ValidationCitationDTO]:
        """Fill in ``ValidationCitationDTO.preview`` with the real
        chunk body text via the shared evidence builder.

        Why: the groundedness judge LLM compares answer claims
        against citation previews. Empty previews mean the judge
        has only ``[N] artifact_id @ location`` lines to verify
        against — it can't, so it flags everything as unsupported.
        The same body the synthesizer sees (chunk NDJSON,
        compiled.text leading window, document_map prose) is what
        the judge needs.

        No-op when ``workspace`` isn't wired or ``retrieved`` is
        empty. Per-citation match-up by ``artifact_id`` — handles
        the case where retrieved hits and citations are in
        different orders (or have different lengths after
        deduplication / filtering).
        """
        if self._workspace is None or not retrieved or not citations:
            return citations

        from pathlib import Path, PurePosixPath

        def _resolver(record):
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(  # type: ignore[union-attr]
                ctx, area,
            ).joinpath(*rest)

        try:
            evidence_blocks = build_evidence_blocks(
                ctx=ctx,
                retrieved=retrieved,
                artifact_registry=self._artifacts,
                path_resolver=_resolver,
            )
        except Exception:  # noqa: BLE001 — judge degrades gracefully
            _log.warning(
                "validation runner: failed to load citation body text "
                "for groundedness check; judge will see lineage-only "
                "citations and may over-flag claims.",
                exc_info=True,
            )
            return citations

        # Map artifact_id → first non-empty body. Multiple chunks
        # from the same artifact pick the first one — the judge
        # only needs a representative sample to verify claims.
        body_by_artifact: dict[str, str] = {}
        for block in evidence_blocks:
            if block.artifact_id in body_by_artifact:
                continue
            text = (block.text or "").strip()
            if text:
                body_by_artifact[block.artifact_id] = text

        if not body_by_artifact:
            return citations

        # Project the enriched body onto each citation matching by
        # artifact_id. Use dataclass-replace so we don't mutate the
        # frozen DTOs.
        from dataclasses import replace
        enriched: list[ValidationCitationDTO] = []
        for citation in citations:
            body = body_by_artifact.get(citation.artifact_id)
            if body and not (citation.preview or "").strip():
                enriched.append(replace(citation, preview=body))
            else:
                enriched.append(citation)
        return enriched

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


class _OrchestratorGraphView:
    """Adapter so ``_check_expected_graph_evidence`` can read graph
    evidence from a ``QueryTrace`` without changing its signature.

    The check expects an object with ``graph_paths`` exposing a list
    of node-id-bearing objects. We project the trace's candidates
    of kind ``graph_json`` (and graph-shaped extras) into that
    shape. When the orchestrator wasn't asked for graph routes, the
    list is empty — same as a legacy engine that didn't return
    graph paths."""

    def __init__(self, trace: Any) -> None:
        self._trace = trace

    @property
    def graph_paths(self) -> list:
        # Each candidate of a graph-related kind contributes its
        # artifact_id as a "node id"; this is a coarse mapping but
        # matches what the legacy check actually consumes (it only
        # reads node ids out of paths).
        out: list[_GraphPath] = []
        for c in getattr(self._trace, "all_candidates", ()):
            if "graph" in (c.artifact_kind or "").lower():
                out.append(_GraphPath(nodes=[c.artifact_id]))
        return out


class _GraphPath:
    """Minimal stand-in for ``j1.query.models.GraphPath`` —
    ``nodes`` is the only attribute the graph check reads."""

    def __init__(self, nodes: list[str]) -> None:
        self.nodes = nodes


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
