"""SmartQueryOrchestrator — the public entrypoint for the new
query layer.

The orchestrator wires the components in fixed order:

  1. Classify intent → ``QueryPlan``.
  2. Dispatch retrieval routes → ``RouteExecutionRecord``s +
     ``EvidenceCandidate``s.
  3. Build the evidence pack → ``EvidencePack``.
  4. Sufficiency gate. Fail-fast here means NO LLM call.
  5. Synthesize → ``SynthesisOutput``.
  6. Bind citations → cited subset of selected.
  7. Quality gate. ``passed`` only when every required gate passed.
  8. Return ``QueryResult`` + a fully-populated ``QueryTrace``.

Every stage feeds into the trace, so the manual-test endpoint can
render the full picture without re-running anything.

Public API:

  * ``OrchestratorRequest`` — what callers hand in.
  * ``OrchestratorResult`` — what they get back: answer, citations,
    final status, plus the trace.
  * ``SmartQueryOrchestrator.run(request)`` — sync entrypoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping

from j1.projects.context import ProjectContext
from j1.query.answer_quality import (
    AnswerQualityGate,
    QueryFinalStatus,
)
from j1.query.answer_synthesizer import (
    AnswerSynthesizer,
    LLMCallable,
)
from j1.query.citation_binder import CitationBinder
from j1.query.domain_profile import DomainProfile, GENERIC_PROFILE
from j1.query.evidence_builder import (
    EvidenceBuilderConfig,
    EvidencePackBuilder,
)
from j1.query.evidence_sufficiency import (
    EvidenceSufficiencyGate,
    first_failure_reason,
)
from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.query_plan import EvidenceBlock, GateResult
from j1.query.query_trace import QueryTrace
from j1.query.retrieval_routes import (
    RetrievalRoute,
    RetrievalRouteKind,
    RouteContext,
    RouteRunner,
)
from j1.query.scope import QueryScope, default_scope


def _collect_snapshot_ids(
    records: tuple, *, route_kind: str,
) -> tuple[str, ...]:
    """Pull the ``snapshot_id`` values stamped in candidate ``extra``
    metadata for a given route kind. Empty when the route didn't run
    or didn't stamp the field — both reasons are operator-visible in
    the routes_executed section of the trace."""
    seen: set[str] = set()
    for rec in records:
        rec_kind = getattr(rec.route, "value", None) or str(rec.route)
        if rec_kind != route_kind:
            continue
        for cand in rec.candidates:
            sid = (cand.extra or {}).get("snapshot_id")
            if sid:
                seen.add(str(sid))
    return tuple(sorted(seen))


def _any_global_workspace(records: tuple) -> bool:
    """True if any RAGAnything candidate reported a working_dir that
    looks unscoped (no ``/snapshots/`` segment). Heuristic — surfaced
    in the trace so operators can spot a regression that re-introduces
    global fallback. Strict enforcement lives in the bridge."""
    for rec in records:
        for cand in rec.candidates:
            wd = (cand.extra or {}).get("raganything_working_dir")
            if wd and "/snapshots/" not in str(wd):
                return True
    return False


# ---- Public request / result ---------------------------------


@dataclass(frozen=True)
class OrchestratorRequest:
    """Everything ``SmartQueryOrchestrator.run`` needs.

    ``profile`` is optional — None → generic mode. ``eligible_run_ids``
    is the legacy scoping set (run-keyed FTS / validation diagnostic
    paths). ``eligible_snapshot_ids`` is the Phase 9 visibility key;
    every retrieval adapter that consults persisted knowledge MUST
    filter by it.

    Callers that don't pre-compute eligibility pass ``None`` for
    both — the adapters' resolver callbacks fill in.
    """

    ctx: ProjectContext
    question: str
    scope: QueryScope = field(default_factory=default_scope)
    profile: DomainProfile | None = None
    document_id: str | None = None
    run_id: str | None = None
    eligible_run_ids: frozenset[str] | None = None
    eligible_snapshot_ids: frozenset[str] | None = None
    # Pre-resolved ``(document_id, snapshot_id)`` allowlist. When the
    # caller already knows the exact pairs to query (e.g. the
    # validation service translating ``snapshot_explicit`` against
    # the snapshot store), pass them here so the per-pair fan-out
    # adapters (RAGAnything) bypass scope-driven eligibility — which
    # only sees ACTIVE snapshots and would refuse a candidate that
    # hasn't been promoted yet.
    eligible_snapshot_pairs: frozenset[tuple[str, str]] | None = None


@dataclass(frozen=True)
class OrchestratorResult:
    """Public result shape. ``trace`` is the full record for the
    manual-test view; ``answer`` / ``citations`` / ``final_status``
    are the shorthand most callers actually read."""

    answer: str
    final_status: str
    citations: tuple[EvidenceBlock, ...]
    gate_results: tuple[GateResult, ...]
    trace: QueryTrace
    message: str | None = None


# ---- Orchestrator -------------------------------------------


class SmartQueryOrchestrator:
    """Pulls intent classifier + routes + builder + gates + synth +
    binder together. Construct once per worker; ``run`` is
    thread-safe (each call is a value-only pipeline)."""

    def __init__(
        self,
        *,
        classifier: QueryIntentClassifier,
        route_runner: RouteRunner,
        builder: EvidencePackBuilder,
        sufficiency: EvidenceSufficiencyGate,
        synthesizer: AnswerSynthesizer,
        binder: CitationBinder,
        quality: AnswerQualityGate,
    ) -> None:
        self._classifier = classifier
        self._routes = route_runner
        self._builder = builder
        self._sufficiency = sufficiency
        self._synth = synthesizer
        self._binder = binder
        self._quality = quality

    # ---- Construction helper ---------------------------------

    @classmethod
    def from_components(
        cls,
        *,
        routes: Mapping[RetrievalRouteKind, RetrievalRoute],
        llm: LLMCallable,
        builder_config: EvidenceBuilderConfig | None = None,
    ) -> "SmartQueryOrchestrator":
        return cls(
            classifier=QueryIntentClassifier(),
            route_runner=RouteRunner(routes),
            builder=EvidencePackBuilder(config=builder_config),
            sufficiency=EvidenceSufficiencyGate(),
            synthesizer=AnswerSynthesizer(llm=llm),
            binder=CitationBinder(),
            quality=AnswerQualityGate(),
        )

    # ---- Run ------------------------------------------------

    def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        started = time.perf_counter()
        profile = request.profile or GENERIC_PROFILE

        # 1. Classify.
        plan = self._classifier.classify(
            request.question, profile=profile,
        )
        trace = QueryTrace.empty_with_plan(request.question, plan)

        # 2. Retrieval routes.
        route_ctx = RouteContext(
            ctx=request.ctx,
            scope=request.scope,
            eligible_run_ids=request.eligible_run_ids,
            eligible_snapshot_ids=request.eligible_snapshot_ids,
            eligible_snapshot_pairs=request.eligible_snapshot_pairs,
            document_id=request.document_id,
            run_id=request.run_id,
        )
        records = self._routes.run_all(
            plan.retrieval_jobs, route_ctx,
        )
        trace = trace.with_routes(records)
        all_cands = trace.all_candidates
        # Stamp snapshot-scope diagnostics so the trace proves BM25 +
        # RAGAnything used the same eligibility boundary. Empty
        # eligibility set is a valid answer (no attached documents);
        # the trace surface shows it explicitly.
        trace = trace.with_snapshot_scope(
            eligible_snapshot_ids=tuple(sorted(
                request.eligible_snapshot_ids or ()
            )),
            queried_raganything_snapshot_ids=_collect_snapshot_ids(
                records, route_kind="raganything",
            ),
            bm25_allowed_snapshot_ids=_collect_snapshot_ids(
                records, route_kind="bm25",
            ),
            used_global_workspace=_any_global_workspace(records),
        )

        # 3. Evidence pack.
        scope_run_id = request.run_id
        pack = self._builder.build(
            plan, all_cands,
            scope_run_id=scope_run_id, profile=profile,
        )
        trace = trace.with_pack(pack)

        # 4. Sufficiency gate.
        suf_results, suf_status = self._sufficiency.check(
            plan, pack, total_candidates=len(all_cands),
        )
        if suf_status != "ok":
            # Skip synthesis. Final status mirrors the sufficiency
            # status — both ``retrieval_insufficient`` and
            # ``evidence_insufficient`` are FAILED (with the precise
            # status string preserved for the trace).
            trace = trace.with_gates(suf_results, suf_status)
            duration_ms = int((time.perf_counter() - started) * 1000)
            trace = trace.with_duration(duration_ms)
            return OrchestratorResult(
                answer="",
                final_status=suf_status,
                citations=(),
                gate_results=suf_results,
                trace=trace,
                message=first_failure_reason(suf_results),
            )

        # 5. Synthesis.
        output = self._synth.synthesize(
            plan, pack.blocks, profile=profile,
        )
        trace = trace.with_llm_evidence(pack.blocks)

        # 6. Bind citations.
        cited = self._binder.bind(pack.blocks, output)
        trace = trace.with_answer(output.answer, cited)

        # 7. Quality gate.
        quality_results, final_status = self._quality.check(
            plan, output, cited=cited, selected=pack.blocks,
        )
        all_results = suf_results + quality_results
        trace = trace.with_gates(all_results, final_status.value)
        duration_ms = int((time.perf_counter() - started) * 1000)
        trace = trace.with_duration(duration_ms)

        # 8. Compose result.
        message: str | None = None
        if final_status != QueryFinalStatus.PASSED:
            message = first_failure_reason(all_results)
        return OrchestratorResult(
            answer=output.answer,
            final_status=final_status.value,
            citations=cited,
            gate_results=all_results,
            trace=trace,
            message=message,
        )


__all__ = [
    "OrchestratorRequest",
    "OrchestratorResult",
    "SmartQueryOrchestrator",
]
