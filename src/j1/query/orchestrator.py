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


# ---- Public request / result ---------------------------------


@dataclass(frozen=True)
class OrchestratorRequest:
    """Everything ``SmartQueryOrchestrator.run`` needs.

    ``profile`` is optional — None → generic mode. ``eligible_run_ids``
    is the strict scoping set (post-refactor eligibility gate);
    callers that don't pre-compute it pass None and the BM25
    adapter's resolver fills in.
    """

    ctx: ProjectContext
    question: str
    scope: QueryScope = field(default_factory=default_scope)
    profile: DomainProfile | None = None
    document_id: str | None = None
    run_id: str | None = None
    eligible_run_ids: frozenset[str] | None = None


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
            document_id=request.document_id,
            run_id=request.run_id,
        )
        records = self._routes.run_all(
            plan.retrieval_jobs, route_ctx,
        )
        trace = trace.with_routes(records)
        all_cands = trace.all_candidates

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
