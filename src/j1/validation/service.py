"""IngestionValidationService — read/write surface for validation.

synchronous manual test query (`run_manual_test_query`).
generate / list / get validation sets, run validation,
list / get validation runs.

All methods enforce run ownership via `_load_run` (raises
`ReviewNotFound` → REST 404 on cross-tenant / cross-project access).

The service is constructed from already-built dependencies (no
container / no facade) so tests wire it from `tmp_path` fixtures
the same way `IngestionResultReviewService` is wired.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from j1.intake.registry import SourceRegistry

from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.domains.models import DomainValidationGuidance
from j1.domains.registry import DomainRegistry
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.ingestion_review.projectors.chunks import ChunkProjector, _ChunkRecord
from j1.processing.results import ARTIFACT_KIND_CHUNK
from j1.projects.context import ProjectContext
from j1.query.engine import HybridQueryEngine
from j1.query.models import QueryMode, QueryRequest
from j1.query.scope import ActiveScope, QueryScope, RunScope
from j1.runs.models import IngestionRun
from j1.runs.store import IngestionRunStore
# Legacy ``aggregate_status`` / ``run_checks`` removed — validation
# flows through ``SmartQueryOrchestrator``.
from j1.validation.dtos import (
    EvidenceBlockDTO,
    LLMTraceDTO,
    ManualTestQueryRequest,
    ManualTestQueryResponseDTO,
    NativeDebugQueryResponseDTO,
    RetrievedChunkRefDTO,
    ValidationCheckDTO,
    ValidationCitationDTO,
    ValidationResultDTO,
    ValidationRunDTO,
    ValidationSetDTO,
)
from j1.validation.evidence import build_evidence_blocks
from j1.validation.generator import (
    DefaultTestCaseGenerator,
    GenerationOptions,
)
from j1.validation.judge import LLMJudge
from j1.validation.synthesis import AnswerSynthesizer
from j1.validation.runner import (
    DefaultValidationRunner,
    MAX_CASES_PER_RUN,
)
from j1.validation.store import ValidationRunStore, ValidationSetStore
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.validation")

_ACTION_MANUAL_QUERY = "j1.validation.manual_query.completed"
_ACTION_SET_GENERATED = "j1.validation.set_generated"
_ACTION_RUN_COMPLETED = "j1.validation.run_completed"
_ACTION_VERDICT_RECORDED = "j1.validation.verdict_recorded"
_TARGET_KIND_RUN = "ingestion_run"
_TARGET_KIND_VALIDATION_SET = "validation_set"
_TARGET_KIND_VALIDATION_RUN = "validation_run"
_TARGET_KIND_VALIDATION_RESULT = "validation_result"

# Allowed tester verdict values. Keeping this constant local to the
# service makes the validation tighter than just trusting the DTO's
# Literal type — the REST layer can re-use it for input validation.
_VALID_VERDICTS: frozenset[str] = frozenset({"pass", "warning", "fail"})

# Hard cap on `top_k` — 's manual query is synchronous and we
# don't want a tester accidentally requesting 10k results and blocking
# the worker. The REST layer also clamps via Pydantic but the service
# enforces too so stand-alone callers (tests, future async paths) get
# the same guarantee.
_TOP_K_HARD_CAP = 50

# Default candidate-retrieval breadth for validation queries.
# Decoupled from the FE/request ``top_k`` (which was historically
# both the recall ceiling AND the user-visible cap). Per the
# retrieval audit, raw FTS ``LIMIT`` set to a small request top_k
# starved the reranker — relevant chunks below rank N were never
# in the candidate pool, so no downstream rerank or coverage
# selection could recover them. Production now retrieves up to
# this many candidates from the engine, then the reranker
# (``j1.validation.rerank``) picks the final blocks. Operators
# tune via constructor kwargs.
_DEFAULT_VALIDATION_CANDIDATE_TOP_K = 20

# Default cap on final evidence blocks sent to the LLM. The
# rerank + coverage-selection layer enforces this — tighter than
# the candidate pool above. Aligned with
# ``rerank.RerankConfig.evidence_max_blocks``.
_DEFAULT_VALIDATION_EVIDENCE_MAX_BLOCKS = 5

# Query-engine mode catalogue.
#
# RATIONALE (post-audit + role-clarification): the prior catalogue
# had ``rag_native_primary``, which despite its name **always**
# ran BM25 alongside native to provide citations AND let BM25
# drive the answer text when native failed. The role-clarification
# refactor pins BM25 to an AUXILIARY DATA-QUALITY role only —
# BM25 may surface chunks/citations/metadata-quality diagnostics
# but MUST NOT participate in the answer-generation path unless
# an explicit fallback mode is selected.
#
# Canonical engines:
#
#   * ``lightrag_native``
#         Pure LightRAG ``aquery`` for the answer. No BM25 in any
#         role. The new DEFAULT.
#
#   * ``lightrag_native_with_quality_evidence``
#         LightRAG ``aquery`` for the answer; BM25 runs in
#         parallel ONLY to populate the data-quality / evidence
#         inspection section (auxiliary). BM25's text never
#         drives the final answer. ``bm25_participated_in_answer``
#         stays ``false``. ``bm25_purpose`` is
#         ``"data_quality_evidence_inspection"``.
#
#   * ``bm25_quality_debug``
#         BM25-only lexical retrieval — explicitly NOT a
#         user-facing answer engine. Used to inspect whether
#         indexed chunks/artifacts carry the expected
#         (run_id, document_id, artifact_id) metadata and whether
#         the lexical content is searchable at all. The
#         ``bm25_participated_in_answer`` flag is ``true`` here
#         because the engine is intentionally "BM25 produced
#         this text"; the engine name + the
#         ``"lexical_debug_answer"`` purpose tell operators not
#         to treat it as a real answer.
#
#   * ``hybrid_ab``
#         BM25 is the stable answer + native runs for
#         observability comparison. Operators who want to A/B
#         the two paths use this. ``bm25_participated_in_answer``
#         is ``true``; ``bm25_purpose`` is
#         ``"observability_answer"`` so it can't be confused
#         with the production native-driven flow.
#
# Fallback is a FLAG (``enable_bm25_fallback``), NOT an engine.
# When set, native-driven engines may use BM25 as the answer if
# native fails. The response records this with
# ``bm25_participated_in_answer=true`` +
# ``bm25_purpose="fallback_answer"`` so it's obvious in audit.
#
# LEGACY ALIASES — every prior name still works as input but is
# mapped to the new canonical vocabulary on construction:
#     bm25_primary                       → bm25_quality_debug
#     bm25_debug                         → bm25_quality_debug
#     rag_native_primary                 → lightrag_native_with_quality_evidence
#     lightrag_native_with_bm25_evidence → lightrag_native_with_quality_evidence
QUERY_ENGINE_LIGHTRAG_NATIVE = "lightrag_native"
QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE = (
    "lightrag_native_with_quality_evidence"
)
QUERY_ENGINE_BM25_QUALITY_DEBUG = "bm25_quality_debug"
QUERY_ENGINE_HYBRID_AB = "hybrid_ab"

_VALID_QUERY_ENGINES: frozenset[str] = frozenset({
    QUERY_ENGINE_LIGHTRAG_NATIVE,
    QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    QUERY_ENGINE_BM25_QUALITY_DEBUG,
    QUERY_ENGINE_HYBRID_AB,
})

# Legacy → canonical alias map. Two generations of names map
# through: V1 (bm25_primary / rag_native_primary) and V2
# (bm25_debug / lightrag_native_with_bm25_evidence).
_QUERY_ENGINE_ALIASES: dict[str, str] = {
    "bm25_primary": QUERY_ENGINE_BM25_QUALITY_DEBUG,
    "bm25_debug": QUERY_ENGINE_BM25_QUALITY_DEBUG,
    "rag_native_primary": QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
    "lightrag_native_with_bm25_evidence": (
        QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
    ),
}

# Backward-compat constant aliases for existing imports / test
# assertions. All point at the new canonical strings.
QUERY_ENGINE_BM25_DEBUG = QUERY_ENGINE_BM25_QUALITY_DEBUG
QUERY_ENGINE_LIGHTRAG_WITH_BM25_EVIDENCE = (
    QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
)
QUERY_PROVIDER_MODE_BM25 = QUERY_ENGINE_BM25_QUALITY_DEBUG
QUERY_PROVIDER_MODE_NATIVE = QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
QUERY_PROVIDER_MODE_HYBRID_AB = QUERY_ENGINE_HYBRID_AB
_VALID_QUERY_PROVIDER_MODES: frozenset[str] = _VALID_QUERY_ENGINES

# BM25 purpose vocabulary stamped on every debug payload.
# ``null`` means BM25 did not run. The other values record
# exactly WHY BM25 ran so an auditor can answer "did BM25 affect
# this answer?" at a glance:
BM25_PURPOSE_DATA_QUALITY = "data_quality_evidence_inspection"
BM25_PURPOSE_FALLBACK_ANSWER = "fallback_answer"
BM25_PURPOSE_LEXICAL_DEBUG = "lexical_debug_answer"
BM25_PURPOSE_OBSERVABILITY = "observability_answer"

# Default native-query timeout (seconds). Bounds the
# ``rag.aquery`` call so a stuck vendor request can't hang a
# validation HTTP handler. The persistent-loop helper raises
# ``concurrent.futures.TimeoutError`` past this deadline and the
# dispatcher catches it as a fallback trigger.
_DEFAULT_NATIVE_QUERY_TIMEOUT_SECONDS = 30.0


# Preview-length cap for the ``bm25_answer_preview`` /
# ``native_answer_preview`` debug fields. The FE renders these
# as one-line summaries in the Final Answer / debug panels; 240
# matches the existing ``_PREVIEW_MAX_CHARS`` on retrieved-chunk
# previews so all preview widths look consistent.
_ANSWER_PREVIEW_CAP = 240

# Mirror of the runner's preview cap. Used by the orchestrator →
# DTO projection helpers below to keep retrieved-chunk preview
# widths visually consistent with the runner output.
_PREVIEW_MAX_CHARS = 240


def _answer_preview(answer: str | None) -> str:
    """Truncate an answer to a stable preview length. Used by the
    debug payload so operators can compare native vs BM25
    outputs side-by-side without scrolling the full response."""
    if not answer:
        return ""
    text = str(answer).strip()
    if len(text) <= _ANSWER_PREVIEW_CAP:
        return text
    return text[:_ANSWER_PREVIEW_CAP].rstrip() + "…"


class IngestionValidationService:
    """Validation surface — manual queries + generated
 sets and runs.

 Verdicts / human overrides / async execution arrive in later
 phases; the constructor accepts the relevant dependencies as
 Optional so a only deployment can still wire just the
 manual-query path.
 """

    def __init__(
        self,
        *,
        run_store: IngestionRunStore,
        artifact_registry: ArtifactRegistry,
        query_engine: HybridQueryEngine,
        audit: AuditRecorder | None = None,
        workspace: WorkspaceResolver | None = None,
        validation_set_store: ValidationSetStore | None = None,
        validation_run_store: ValidationRunStore | None = None,
        test_case_generator: DefaultTestCaseGenerator | None = None,
        judge: LLMJudge | None = None,
        answer_synthesizer: AnswerSynthesizer | None = None,
        domain_registry: DomainRegistry | None = None,
        source_registry: "SourceRegistry | None" = None,
        validation_candidate_top_k: int = _DEFAULT_VALIDATION_CANDIDATE_TOP_K,
        validation_evidence_max_blocks: int = (
            _DEFAULT_VALIDATION_EVIDENCE_MAX_BLOCKS
        ),
        # Native-query plumbing. All optional so legacy callers
        # / tests don't have to change. When
        # ``native_query_provider`` is ``None`` OR the engine is
        # ``bm25_debug``, the service skips the native path
        # entirely.
        native_query_provider: Any | None = None,
        # ``query_engine_mode`` is the canonical knob;
        # ``query_provider_mode`` remains accepted as a legacy alias
        # when callers haven't migrated yet. The constructor
        # normalises both, then alias-maps legacy strings
        # (``bm25_primary`` / ``rag_native_primary``) into the new
        # vocabulary. DEFAULT IS NOW ``lightrag_native`` — per the
        # audit, the production query path should hit LightRAG
        # native first and **not** silently couple in BM25.
        # (``query_engine_mode`` rather than ``query_engine``
        # because the dependency-injected ``HybridQueryEngine``
        # already owns that kwarg name above.)
        query_engine_mode: str | None = None,
        query_provider_mode: str | None = None,
        native_query_timeout_seconds: float = (
            _DEFAULT_NATIVE_QUERY_TIMEOUT_SECONDS
        ),
        # New canonical flags. Both default to False so the
        # native path stays pure unless an operator explicitly
        # opts in to BM25 involvement.
        enable_bm25_evidence: bool = False,
        enable_bm25_fallback: bool = False,
        # Legacy back-compat: the previous flag name. When
        # supplied (truthy), it sets ``enable_bm25_fallback``.
        # The default ``None`` means "respect the new flag."
        native_query_fallback_to_bm25: bool | None = None,
        # SmartQueryOrchestrator (j1.query.orchestrator). When wired,
        # ``run_manual_test_query`` delegates to the new pipeline:
        # intent classification → multi-route retrieval → grouped
        # evidence pack → sufficiency gate → synthesis → citation
        # binder → answer-quality gate. The legacy aggregate_status
        # / refusal-regex path is bypassed entirely. Optional so
        # tests (and deployments that haven't migrated yet) keep
        # working unchanged. Type ``object`` here to avoid an import
        # cycle — the service only reads ``.run(OrchestratorRequest)``.
        smart_query_orchestrator: object | None = None,
    ) -> None:
        self._run_store = run_store
        self._artifacts = artifact_registry
        self._query_engine = query_engine
        self._audit = audit
        self._workspace = workspace
        self._set_store = validation_set_store
        self._run_store_v = validation_run_store
        self._generator = test_case_generator
        # Optional LLM judge for semantic checks. The runner
        # picks this up when it's configured; when None, optional
        # checks are simply omitted.
        self._judge = judge
        # Optional LLM answer synthesizer for the manual-query path.
        # When None, manual queries fall back to retrieval-only mode
        # and the response reports `llm.called=False`. Batch validation
        # runs do not consult this — they must stay deterministic.
        self._synthesizer = answer_synthesizer
        # Optional domain registry. When wired, the set generator
        # asks for the run's domain pack and threads the pack's
        # `validation_guidance` into the LLM prompt as a TESTING-LENS
        # rubric (never as factual evidence). None = generation
        # always runs in generic mode.
        self._domain_registry = domain_registry
        # Optional source registry. When wired, manual-query
        # requests with `validation_scope="active"` resolve to the
        # document's currently-promoted run via `ActiveScope`. When
        # None, "active" silently falls back to "run" — the
        # spec-compliant default for deployments that haven't
        # adopted the document-centric flow.
        self._source_registry = source_registry
        # SmartQueryOrchestrator (lazy attribute — accessed only on
        # the manual-query path). Stored as ``object | None`` to
        # avoid a static import dependency from the validation
        # module onto ``j1.query``.
        self._smart_query_orchestrator = smart_query_orchestrator
        # Decoupled candidate vs final-evidence sizing. The
        # request's ``top_k`` controls the user-visible "how many
        # retrievals does the FE show" knob; the service
        # independently retrieves ``candidate_top_k`` rows from
        # the engine so the reranker has enough breadth to pick
        # from. Final evidence is capped at
        # ``evidence_max_blocks`` regardless of either input.
        #
        # TODO: a follow-up change should evaluate routing the
        # graph + knowledge queries through RAGAnything's native
        # ``aquery`` (currently NOT wired into HybridQueryEngine,
        # per the retrieval audit). For now this minimal change
        # only fixes the candidate-starvation issue inside the
        # existing J1 BM25 retriever.
        self._validation_candidate_top_k = max(
            1, min(int(validation_candidate_top_k), _TOP_K_HARD_CAP),
        )
        self._validation_evidence_max_blocks = max(
            1, int(validation_evidence_max_blocks),
        )

        # Native-query dispatch state. The engine string is
        # validated here once so a misconfigured env var becomes
        # an obvious server-side log instead of a silent
        # fallthrough. Engines that name native-query are
        # demoted to ``bm25_debug`` if the native provider isn't
        # wired — operators get the warning, and the path stays
        # predictable.
        self._native_query_provider = native_query_provider
        # Prefer the new canonical kwarg; fall back to legacy.
        raw_engine = (query_engine_mode or query_provider_mode or "").strip()
        # Empty string → new default (pure LightRAG native).
        if not raw_engine:
            engine = QUERY_ENGINE_LIGHTRAG_NATIVE
        else:
            # Apply legacy alias before validation so old env
            # strings (``bm25_primary`` / ``rag_native_primary``)
            # land on the new canonical vocabulary.
            engine = _QUERY_ENGINE_ALIASES.get(raw_engine, raw_engine)
            if engine != raw_engine:
                _log.debug(
                    "query_engine alias %r → %r", raw_engine, engine,
                )
        if engine not in _VALID_QUERY_ENGINES:
            _log.warning(
                "unknown query_engine=%r; falling back to %r",
                engine, QUERY_ENGINE_LIGHTRAG_NATIVE,
            )
            engine = QUERY_ENGINE_LIGHTRAG_NATIVE
        # If the operator asked for a native-driven engine but
        # didn't wire a native provider, demote to
        # ``bm25_quality_debug`` with a clear warning. Preserves
        # the "things still work" contract for deployments that
        # haven't installed RAGAnything.
        needs_native = engine in {
            QUERY_ENGINE_LIGHTRAG_NATIVE,
            QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
            QUERY_ENGINE_HYBRID_AB,
        }
        if needs_native and native_query_provider is None:
            _log.warning(
                "query_engine=%r requested but no native_query_provider "
                "wired; falling back to %r",
                engine, QUERY_ENGINE_BM25_QUALITY_DEBUG,
            )
            engine = QUERY_ENGINE_BM25_QUALITY_DEBUG
        self._query_engine_mode = engine
        # Backward-compat attribute: prior tests / call sites read
        # ``_query_provider_mode``. Same value as the new
        # ``_query_engine_mode``.
        self._query_provider_mode = engine
        self._native_query_timeout_seconds = max(
            1.0, float(native_query_timeout_seconds),
        )
        # Fallback flag resolution:
        #   * legacy ``native_query_fallback_to_bm25`` when supplied
        #     (truthy True/False) wins for backward compat;
        #   * otherwise use the canonical ``enable_bm25_fallback``.
        if native_query_fallback_to_bm25 is not None:
            self._enable_bm25_fallback = bool(
                native_query_fallback_to_bm25,
            )
        else:
            self._enable_bm25_fallback = bool(enable_bm25_fallback)
        self._enable_bm25_evidence = bool(enable_bm25_evidence)
        # Convenience: if the operator selected pure
        # ``lightrag_native`` AND set ``enable_bm25_evidence=True``,
        # promote to the explicit "with evidence" engine. Avoids
        # the trap where "I want native answer but also need
        # citations" silently behaves as pure native.
        if (
            self._query_engine_mode == QUERY_ENGINE_LIGHTRAG_NATIVE
            and self._enable_bm25_evidence
        ):
            _log.debug(
                "enable_bm25_evidence=True with engine=%r → promoting "
                "to %r",
                self._query_engine_mode,
                QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
            )
            self._query_engine_mode = (
                QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
            )
            self._query_provider_mode = self._query_engine_mode

    def run_manual_test_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        request: ManualTestQueryRequest,
        *,
        actor: str = "system",
    ) -> ManualTestQueryResponseDTO:
        """Execute a single tester question against this run.

 synchronous. Calls `HybridQueryEngine.query` with
 `RunScope(run_id)` so retrieval is restricted to artifacts
 produced by this run. Builds deterministic check results
 from the engine output.

 Raises `ReviewNotFound` (→ 404 at REST) when the run doesn't
 exist in `(ctx.tenant_id, ctx.project_id)`. Cross-tenant /
 cross-project access produces an identical 404 — existence
 is never leakable.
 """
        run = self._load_run(ctx, run_id)
        request_id = f"tq-{uuid.uuid4().hex[:12]}"
        if self._smart_query_orchestrator is None:
            raise RuntimeError(
                "IngestionValidationService.run_manual_test_query "
                "requires a SmartQueryOrchestrator. The legacy "
                "HybridQueryEngine + run_checks path was removed."
            )
        return self._run_manual_query_via_orchestrator(
            ctx=ctx, run=run, request=request,
            request_id=request_id, actor=actor,
        )


    def run_native_debug_query(
        self,
        ctx: ProjectContext,
        run_id: str,
        question: str,
        *,
        actor: str = "system",
    ) -> NativeDebugQueryResponseDTO:
        """Direct LightRAG-native diagnostic call. **No BM25**, no
        reranking, no coverage selection — pure ``rag.aquery``
        against this run's workspace.

        Use this when the regular ``test-query`` endpoint isn't
        enough to isolate whether retrieval problems originate in
        native indexing or in BM25 / evidence-building layers. The
        response surfaces the resolved workspace path so the
        operator can visually confirm "yes, the call hit the
        per-run directory I expected" without inferring it from
        debug logs.

        ``actor`` is recorded in the audit row alongside the
        request_id for traceability.

        Raises ``ReviewNotFound`` (→ 404) when the run doesn't
        exist in ``(ctx.tenant_id, ctx.project_id)``.
        """
        run = self._load_run(ctx, run_id)
        request_id = f"nd-{uuid.uuid4().hex[:12]}"

        tenant = getattr(ctx, "tenant_id", None) or ""
        project = getattr(ctx, "project_id", None) or ""
        workspace_id = (
            f"{tenant}/{project}/{run.document_id}/{run.run_id}"
            if tenant and project and run.document_id and run.run_id
            else ""
        )
        workspace_path: str | None = None
        if self._native_query_provider is not None:
            try:
                workspace_path = (
                    self._native_query_provider.workspace_path_for(
                        ctx, run.document_id, run.run_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — debug only
                _log.debug(
                    "workspace_path_for failed for run=%s: %s",
                    run.run_id, exc,
                )

        provider_wired = self._native_query_provider is not None
        if not provider_wired:
            # Honest failure shape — no native call attempted.
            return NativeDebugQueryResponseDTO(
                request_id=request_id,
                run_id=run.run_id,
                document_id=run.document_id,
                question=question,
                answer="",
                workspace_path=workspace_path,
                workspace_id=workspace_id,
                native_query_used=False,
                native_query_failed_reason="native_provider_not_wired",
                native_latency_ms=0,
                provider_wired=False,
            )

        # Synthesize a minimal QueryRequest so the existing
        # ``_run_native_query`` helper can be reused. ``mode`` /
        # ``scope`` are not consulted by the native path — the
        # provider keys off ``ctx`` + ``run_id`` + ``document_id``
        # only.
        query_request = QueryRequest(
            question=question,
            mode="hybrid",
            max_results=self._validation_candidate_top_k,
            scope=RunScope(run_id=run.run_id),
        )
        native_answer, native_latency, native_error = self._run_native_query(
            ctx=ctx, run=run, query_request=query_request,
        )

        # Audit the call so native-debug usage is observable in
        # the same row format the manual-query path uses. The
        # action tag is distinct so dashboards can isolate
        # diagnostic traffic from regular validation traffic.
        try:
            self._audit.record(
                actor=actor,
                action="j1.validation.native_debug_query.completed",
                target_kind=_TARGET_KIND_RUN,
                target_id=run.run_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                metadata={
                    "request_id": request_id,
                    "document_id": run.document_id,
                    "workspace_id": workspace_id,
                    "native_query_used": native_answer is not None,
                    "native_query_failed_reason": native_error,
                    "native_latency_ms": native_latency,
                },
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break
            _log.warning(
                "native-debug audit record failed run=%s: %s",
                run.run_id, exc,
            )

        return NativeDebugQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            document_id=run.document_id,
            question=question,
            answer=native_answer or "",
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            native_query_used=native_answer is not None,
            native_query_failed_reason=native_error,
            native_latency_ms=native_latency,
            provider_wired=True,
        )

    # ---- New orchestrator-based manual query path ------------

    def _run_manual_query_via_orchestrator(
        self,
        *,
        ctx: ProjectContext,
        run: "IngestionRun",
        request: ManualTestQueryRequest,
        request_id: str,
        actor: str,
    ) -> ManualTestQueryResponseDTO:
        """Drive one manual test query through the SmartQueryOrchestrator
        and project the result into the legacy
        ``ManualTestQueryResponseDTO`` shape.

        The new pipeline owns:
          * intent classification + retrieval planning
          * multi-route retrieval (RAGAnything / BM25 / artifact)
          * grouped evidence pack + per-group caps + scope filter
          * sufficiency gate (refuses to call the LLM on a thin pack)
          * synthesis (shape-specific prompt, only selected evidence)
          * citation binding (cited ⊆ selected)
          * answer-quality gate (no length shortcut, no aggregate
            override letting refusals pass)

        The frontend reads the same DTO fields it always has —
        ``validation_status`` comes from the orchestrator's
        ``final_status`` instead of ``aggregate_status``, and
        ``checks[]`` is a flattened view of the gate results so the
        Validation tab keeps rendering one row per check.

        The full trace JSON lands on ``debug['orchestrator_trace']``
        so operators can dig in without needing the separate
        ``/dev/query-trace`` endpoint.
        """
        # Lazy imports — keep ``j1.query`` out of this module's top.
        from j1.query.orchestrator import OrchestratorRequest

        engine_scope = self._resolve_query_scope(
            ctx=ctx, run=run, validation_scope=request.validation_scope,
        )
        result = self._smart_query_orchestrator.run(OrchestratorRequest(
            ctx=ctx,
            question=request.question,
            scope=engine_scope,
            run_id=run.run_id,
            document_id=run.document_id,
        ))

        retrieved_chunks = _retrieved_chunks_from_trace(result.trace)
        citations_list = _citations_from_orchestrator(result)
        checks = _checks_from_gate_results(result.gate_results)
        validation_status = _validation_status_from_final(
            result.final_status,
        )
        evidence_sent_to_llm = _evidence_blocks_from_trace(result.trace)
        evidence_flags = _evidence_flags_from_trace(result.trace)
        llm_trace = LLMTraceDTO(
            called=bool(result.trace.llm_evidence),
            provider="smart_query_orchestrator",
            model="composite",
            error=(
                None if result.final_status == "passed"
                else result.message
            ),
        )
        debug: dict[str, Any] = {
            "query_engine": "smart_query_orchestrator",
            "orchestrator_final_status": result.final_status,
            "orchestrator_message": result.message,
            "orchestrator_trace": result.trace.to_dict(),
        }
        self._audit_manual_query(
            ctx=ctx, run=run, request_id=request_id, request=request,
            validation_status=validation_status,
            retrieved_count=len(retrieved_chunks),
            citation_count=len(citations_list),
            actor=actor,
        )
        return ManualTestQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            question=request.question,
            answer=result.answer or "",
            mode_used="smart_query_orchestrator",
            retrieved_chunks=retrieved_chunks,
            citations=citations_list,
            checks=checks,
            validation_status=validation_status,
            evidence_flags=evidence_flags,
            raw_response=None,
            synthesized_answer=result.answer or None,
            llm=llm_trace,
            evidence_sent_to_llm=evidence_sent_to_llm,
            debug=debug,
        )

    def _resolve_query_scope(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        validation_scope: str,
    ) -> "QueryScope":
        """Map the request's `validation_scope` literal to a concrete
        `QueryScope` for the engine.

        * ``"run"``    — the existing default. RunScope(this run id).
        * ``"active"`` — ActiveScope(document_id), resolved against
          the source registry to the document's currently-promoted
          run. Falls back to RunScope(this run id) when no source
          registry is wired (legacy deployments).

        Centralised here so the engine layer stays scope-agnostic
        and the existing RunScope filter does all the heavy lifting
        downstream.
        """
        if validation_scope == "active" and self._source_registry is not None:
            from j1.query.active_scope import resolve_to_concrete_scope
            active = ActiveScope(document_id=run.document_id)
            return resolve_to_concrete_scope(
                active, registry=self._source_registry, ctx=ctx,
            )
        # Default / fallback path. `"run"` (or `"active"` without a
        # registry wired) → scope to this specific run id.
        return RunScope(run_id=run.run_id)

    # ---- Query dispatcher (BM25 / native / hybrid_ab) ------------------

    def _run_native_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        query_request: "QueryRequest",
    ) -> tuple[str | None, int, str | None]:
        """Best-effort native ``aquery`` call.

        Returns ``(native_answer, latency_ms, error)``:
          * ``native_answer`` is the raw prose LightRAG produced,
            or ``None`` when the call failed / timed out / the
            provider was unwired.
          * ``latency_ms`` is the wall-clock duration even on
            failure (so debug telemetry reports useful numbers).
          * ``error`` is a short reason string when the call
            didn't yield an answer; ``None`` on success.

        Per-run workspace isolation is enforced by the bridge —
        ``RAGAnythingQueryProvider`` threads ``run_id`` +
        ``document_id`` into the request so the LightRAG
        ``working_dir`` lands at the per-run scoped path
        (``{workdir}/runs/{tenant}/{project}/{doc}/{run}/``).
        """
        if self._native_query_provider is None:
            return (None, 0, "native_provider_not_wired")
        # Lazy import — only required when native mode is wired.
        from j1.processing.results import ResultStatus

        started = time.monotonic()
        try:
            # NOTE: the provider's own try/except converts most
            # vendor failures to a FAILED ``QueryResult`` rather
            # than raising. Timeout propagates as
            # ``concurrent.futures.TimeoutError`` from the
            # persistent loop helper; the bridge re-raises it.
            # Both shapes are handled below.
            from j1.providers.raganything._persistent_loop import (
                get_persistent_loop,
            )
            # The provider's ``query`` is synchronous from our
            # POV (it dispatches onto the persistent loop
            # internally). We still need a wall-clock timeout so
            # a stuck call doesn't hang the validation handler.
            # The provider already handles the loop dispatch; we
            # bound it via a thread-future wrapper.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
            ) as pool:
                future = pool.submit(
                    self._native_query_provider.query,
                    ctx,
                    query_request.question,
                    max_results=query_request.max_results,
                    document_id=run.document_id,
                    run_id=run.run_id,
                )
                try:
                    result = future.result(
                        timeout=self._native_query_timeout_seconds,
                    )
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    latency_ms = int(
                        (time.monotonic() - started) * 1000,
                    )
                    reason = (
                        f"native_query_timeout_after_"
                        f"{self._native_query_timeout_seconds}s"
                    )
                    _log.warning(
                        "native_query failed run_id=%s document_id=%s "
                        "latency_ms=%d reason=%s",
                        run.run_id, run.document_id, latency_ms, reason,
                    )
                    return (None, latency_ms, reason)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            reason = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "native_query failed run_id=%s document_id=%s "
                "latency_ms=%d reason=%s",
                run.run_id, run.document_id, latency_ms, reason,
                exc_info=True,
            )
            return (None, latency_ms, reason)

        latency_ms = int((time.monotonic() - started) * 1000)
        # ``RAGAnythingQueryProvider.query`` returns a ``QueryResult``
        # (not the ``QueryResponse`` the rest of the pipeline uses).
        # We only need its ``answer`` for the synthesized-answer
        # override; sources are augmented from BM25 separately.
        if getattr(result, "status", None) != ResultStatus.SUCCEEDED:
            reason = str(
                getattr(result, "error", None) or "native_query_failed",
            )
            _log.warning(
                "native_query failed run_id=%s document_id=%s "
                "latency_ms=%d reason=%s",
                run.run_id, run.document_id, latency_ms, reason,
            )
            return (None, latency_ms, reason)
        answer = (getattr(result, "answer", "") or "").strip()
        if not answer:
            _log.warning(
                "native_query returned empty answer run_id=%s "
                "document_id=%s latency_ms=%d",
                run.run_id, run.document_id, latency_ms,
            )
            return (None, latency_ms, "native_query_empty_answer")
        return (answer, latency_ms, None)

    def generate_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        *,
        max_cases: int = 25,
        citation_required: bool = False,
        force: bool = False,
        actor: str = "system",
    ) -> ValidationSetDTO:
        """Generate a fresh validation set from this run's chunks.

 Idempotent on `(run_id, generator_version, artifacts_hash)`:
 when an existing set in the store has a matching hash and
 `force=False`, the existing record is returned unchanged.
 Set `force=True` to bypass the cache (e.g. after editing the
 prompt or chunk content).

 Raises `ReviewNotFound` if the run isn't visible in the
 caller's `(tenant, project)`.
 Raises `RuntimeError` when dependencies aren't wired
 (set store / generator) — same shape as 's missing-
 deps degradation.
 """
        if self._set_store is None or self._generator is None or self._workspace is None:
            raise RuntimeError(
                "validation set generation not configured "
                "(pass validation_set_store, test_case_generator, "
                "workspace to IngestionValidationService)"
            )
        max_cases = max(1, min(max_cases, MAX_CASES_PER_RUN))

        run = self._load_run(ctx, run_id)
        chunks = self._project_run_chunks(ctx, run)
        # Build the evidence blocks the generator will hand to the
        # LLM. Sourced from the same `_ChunkRecord` list we already
        # have — no extra IO. Falls back to empty list when the run
        # produced no chunks (generator then ships only smoke +
        # domain-driven negatives).
        evidence_blocks = _evidence_blocks_from_chunks(chunks)
        # Look up the run's domain pack (if any). Quiet on every
        # failure — generation still works in generic mode when no
        # domain pack is wired or the planning_result.json is missing.
        domain_id, domain_guidance = self._resolve_domain_for_run(
            ctx, run,
        )
        # gather modality artifacts the generator can
        # author cases against. Single registry scan; the partition
        # below is O(n) over the run's artifact list.
        tables, visuals, graphs = self._modality_artifacts_for_run(
            ctx, run.run_id,
        )
        # Enriched-stage artifacts (document_map / summary) carry
        # the structured context (entities, sections, doc purpose)
        # the question generator mines for high-signal question
        # seeds. Optional: when the run skipped enrichment the
        # generator falls back to chunk-only context.
        enriched = self._enriched_artifacts_for_run(ctx, run.run_id)
        # Final ingestion report carries doc title / page count /
        # compile summary. Optional: legacy runs don't have one.
        final_report = self._final_report_for_run(ctx, run.run_id)

        # Generate first so we can compute the artifacts hash off
        # the sampled chunks. Cheap — no LLM call yet on the empty
        # path; the real LLM cost is in the ONE whole-document call
        # the generator makes inside.
        vset = self._generator.generate(
            run_id=run.run_id,
            document_ids=_document_ids(run),
            chunks=chunks,
            options=GenerationOptions(
                max_cases=max_cases,
                citation_required=citation_required,
            ),
            actor=actor,
            table_artifacts=tables,
            visual_artifacts=visuals,
            graph_artifacts=graphs,
            evidence_blocks=evidence_blocks,
            domain_guidance=domain_guidance,
            domain_id=domain_id,
            enriched_artifacts=enriched,
            final_report=final_report,
        )

        # Idempotency: scan existing sets for a hash match. Force
        # bypasses the cache.
        if not force:
            existing = self._find_existing_set(ctx, run.run_id, vset.artifacts_content_hash)
            if existing is not None:
                _log.debug(
                    "reusing existing validation set %s (hash match)",
                    existing.validation_set_id,
                )
                return existing

        self._set_store.upsert(ctx, vset)
        self._audit_set_generated(ctx, run, vset, actor)
        return vset

    def list_validation_sets(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationSetDTO]:
        """List sets for a run, most-recent-first. Empty list when
 the run exists but no sets have been generated."""
        if self._set_store is None:
            return []
        # Run-ownership check first — cross-tenant access raises 404
        # rather than returning an empty list (which would leak
        # existence: missing run vs. no-sets-yet should not be
        # distinguishable).
        self._load_run(ctx, run_id)
        return self._set_store.list_for_run(ctx, run_id)

    def get_validation_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
    ) -> ValidationSetDTO:
        """Fetch one set by id. Raises `ReviewNotFound` for missing /
 cross-tenant / set-belongs-to-different-run."""
        if self._set_store is None:
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vset = self._set_store.get(ctx, validation_set_id)
        if vset is None or vset.run_id != run_id:
            # Identical message regardless of cause — existence is
            # not probeable across runs.
            raise ReviewNotFound(
                f"validation set {validation_set_id!r} not found"
            )
        return vset

    # ---- validation runs ----------------------------------------

    def run_validation(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_set_id: str,
        *,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Execute a validation set. Synchronous — blocks
 until every case has run. Persists three lifecycle snapshots
 (pending → running → terminal) via the run store.

 Raises `ReviewNotFound` for unknown / cross-tenant set or run.
 Raises `RuntimeError` when the dependencies aren't
 wired."""
        if self._run_store_v is None:
            raise RuntimeError(
                "validation run execution not configured "
                "(pass validation_run_store to IngestionValidationService)"
            )
        # Both ownership gates first — `_load_run` then a set-scope
        # check. Cross-tenant probing for a known set under a wrong
        # project must still 404.
        vset = self.get_validation_set(ctx, run_id, validation_set_id)

        runner = DefaultValidationRunner(
            smart_query_orchestrator=self._smart_query_orchestrator,
            query_engine=self._query_engine,
            artifact_registry=self._artifacts,
            lifecycle_callback=lambda v: self._run_store_v.upsert(ctx, v),  # type: ignore[union-attr]
            judge=self._judge,
            # Reuse the manual-query synthesizer here so batch
            # validation runs also get grounded LLM answers instead
            # of the engine's raw "Knowledge results for: …" debug
            # strings. None-safe — runner falls back to the raw
            # engine answer when no synthesizer is wired.
            answer_synthesizer=self._synthesizer,
            # Workspace is REQUIRED for the synthesizer to read
            # real artifact body text (chunk NDJSON, compiled.text
            # files, document_map JSON). Without it the runner
            # falls back to artifact-title-only evidence and the
            # LLM correctly says "Not in the retrieved evidence."
            # since titles don't answer questions.
            workspace=self._workspace,
            # Audit recorder for the retrieval-quality diagnostic
            # event stream (``j1.retrieval.*``). Wired here so each
            # set-execution case lights up the planner / scope /
            # boilerplate / fallback audit trail.
            audit=self._audit,
        )
        # Look up the document_id from the run record so the
        # runner can enforce active-document scope on every case's
        # evidence build. ``run_store`` is wired in production;
        # absent in some tests — the runner gracefully falls back
        # to the legacy unscoped path when document_id is None.
        active_document_id: str | None = None
        try:
            run_record = self._run_store.get(ctx, run_id)
            active_document_id = getattr(
                run_record, "document_id", None,
            )
        except Exception:  # noqa: BLE001 — defensive
            active_document_id = None
        vrun = runner.run(
            ctx, vset, actor=actor,
            active_document_id=active_document_id,
        )
        self._audit_run_completed(ctx, run_id, vrun, actor)
        return vrun

    def list_validation_runs(
        self, ctx: ProjectContext, run_id: str,
    ) -> list[ValidationRunDTO]:
        if self._run_store_v is None:
            return []
        self._load_run(ctx, run_id)
        return self._run_store_v.list_for_run(ctx, run_id)

    def get_validation_run(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
    ) -> ValidationRunDTO:
        if self._run_store_v is None:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        self._load_run(ctx, run_id)
        vrun = self._run_store_v.get(ctx, validation_run_id)
        if vrun is None or vrun.run_id != run_id:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        return vrun

    # ---- tester verdict ---------------------------------------

    def record_tester_verdict(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
        result_id: str,
        *,
        verdict: str,
        notes: str | None = None,
        actor: str = "system",
    ) -> ValidationRunDTO:
        """Record a human override on a single validation result.

 Tester verdict is INDEPENDENT of the automated
 `validation_status` — the deterministic checks stay
 reproducible, and the human verdict layers on top. The FE
 renders both side-by-side; downstream tooling can treat
 whichever it prefers as authoritative.

 Persists by upserting the parent `ValidationRunDTO` with
 the verdict-augmented result swapped in. JSONL latest-wins
 means subsequent reads see the updated record.

 Raises `ReviewNotFound` for missing run / cross-tenant /
 cross-run / unknown result. Raises `ValueError` for an
 invalid verdict string (REST layer translates to 422 via
 Pydantic; this guards stand-alone callers).
 """
        if self._run_store_v is None:
            raise ReviewNotFound(
                f"validation run {validation_run_id!r} not found"
            )
        if verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"invalid tester verdict {verdict!r}; expected one of "
                f"{sorted(_VALID_VERDICTS)}"
            )
        # Run-ownership gates: load_run for tenant/project, then the
        # vrun-belongs-to-this-run check via get_validation_run.
        vrun = self.get_validation_run(ctx, run_id, validation_run_id)

        # Find + replace the result. List comprehension over results
        # keeps the rest of the run snapshot untouched — only the
        # verdict + notes change.
        new_results: list[ValidationResultDTO] = []
        found = False
        for r in vrun.results:
            if r.result_id == result_id:
                found = True
                new_results.append(
                    _replace_verdict(r, verdict=verdict, notes=notes),
                )
            else:
                new_results.append(r)
        if not found:
            raise ReviewNotFound(
                f"validation result {result_id!r} not found in run "
                f"{validation_run_id!r}"
            )

        updated = _replace_run_results(vrun, results=new_results)
        self._run_store_v.upsert(ctx, updated)
        self._audit_verdict_recorded(
            ctx=ctx,
            run_id=run_id,
            vrun=updated,
            result_id=result_id,
            verdict=verdict,
            actor=actor,
        )
        return updated

    # ---- export validation report -----------------------------

    def export_validation_run_report(
        self,
        ctx: ProjectContext,
        run_id: str,
        validation_run_id: str,
        *,
        format: str = "markdown",
    ) -> tuple[str, str]:
        """Compose a tester-friendly report from a terminal
 validation run.

 Returns `(content, media_type)` so the REST layer can set
 the right `Content-Type` header without re-deriving from
 the format. Two formats ship in v1:

 * `markdown` — narrative summary + per-case section. The
 default — copy-pastes cleanly into PR descriptions,
 release notes, etc.
 * `json` — projection of the same data; downstream
 automation should prefer the typed REST endpoints
 (`GET /validation-runs/{id}`) but JSON-export is here
 for parity with markdown.
 """
        vrun = self.get_validation_run(ctx, run_id, validation_run_id)
        fmt = (format or "markdown").lower()
        if fmt == "markdown" or fmt == "md":
            return _render_markdown_report(vrun), "text/markdown"
        if fmt == "json":
            import json
            from j1._serialization import to_jsonable
            return (
                json.dumps(to_jsonable(vrun), indent=2),
                "application/json",
            )
        raise ValueError(
            f"unsupported report format {format!r}; expected 'markdown' or 'json'"
        )

    def purge_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> dict[str, int]:
        """Cascade-delete every validation set + run that references
 `run_id`. Used by the hard-delete (purge) orchestration in
 the REST layer so a purged ingestion run doesn't leave
 dangling validation history pointing at a missing run.

 Best-effort across both stores — a failure on one doesn't
 abort the other. Returns a count report:
 `{sets_removed: int, runs_removed: int}`."""
        sets_removed = 0
        runs_removed = 0
        if self._set_store is not None:
            purge = getattr(self._set_store, "purge_for_run", None)
            if callable(purge):
                try:
                    sets_removed = int(purge(ctx, run_id) or 0)
                except Exception:  # noqa: BLE001 — best-effort cascade
                    sets_removed = 0
        if self._run_store_v is not None:
            purge = getattr(self._run_store_v, "purge_for_run", None)
            if callable(purge):
                try:
                    runs_removed = int(purge(ctx, run_id) or 0)
                except Exception:  # noqa: BLE001 — best-effort cascade
                    runs_removed = 0
        return {
            "sets_removed": sets_removed,
            "runs_removed": runs_removed,
        }

    # ---- helpers (private) -------------------------------------

    def _project_run_chunks(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> list[_ChunkRecord]:
        """Use the existing `ChunkProjector` to flatten the run's
 chunk artifacts into a list of `_ChunkRecord`. Reuses the
 same `path_resolver` pattern the review service uses so
 the two surfaces see identical chunk text."""
        if self._workspace is None:
            return []
        # Resolve only chunk-kind artifacts that belong to this run.
        # + artifact tagging means we read directly from the
        # registry by run_id; 's lineage fallback is preserved
        # in `_resolve_run_artifacts` (we don't need that here yet).
        artifacts = [
            a for a in self._artifacts.list_artifacts(ctx)
            if a.kind == ARTIFACT_KIND_CHUNK and a.metadata.get("run_id") == run.run_id
        ]
        # Closure binds `ctx` so the projector can resolve paths
        # without knowing about the workspace.
        def _resolver(record):
            from pathlib import PurePosixPath
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                from pathlib import Path
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(ctx, area).joinpath(*rest)  # type: ignore[union-attr]

        projector = ChunkProjector(path_resolver=_resolver)
        return projector.project_records(artifacts)

    def _modality_artifacts_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> tuple[list, list, list]:
        """Partition the run's artifacts into the three modality
 buckets (tables / visuals / graph). One pass over the
 registry; returns the three lists in fixed order so the
 generator's call site stays unambiguous.

 keeps the kind taxonomy in lockstep with
 `j1.ingestion_review.availability` — table/image/graph
 gating uses identical kind strings everywhere.
 """
        tables: list = []
        visuals: list = []
        graphs: list = []
        for record in self._artifacts.list_artifacts(ctx):
            if record.metadata.get("run_id") != run_id:
                continue
            if record.kind == "enriched.tables":
                tables.append(record)
            elif record.kind == "enriched.visuals":
                visuals.append(record)
            elif record.kind == ARTIFACT_KIND_GRAPH_JSON:
                graphs.append(record)
        return tables, visuals, graphs

    def _enriched_artifacts_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> list:
        """Collect the enrichment-stage artifacts that carry
        structured context for question generation
        (``enriched.document_map``, ``enriched.summary``).

        These are NOT modality artifacts — they're the rich
        denormalised views that the question-context builder
        mines for entities, sections, and a doc purpose. The
        generator gracefully degrades when none are registered
        (chunk-only context still produces document-specific
        questions; just fewer of them)."""
        out: list = []
        for record in self._artifacts.list_artifacts(ctx):
            if record.metadata.get("run_id") != run_id:
                continue
            if record.kind in {
                "enriched.document_map", "enriched.summary",
            }:
                out.append(record)
        return out

    def _final_report_for_run(
        self, ctx: ProjectContext, run_id: str,
    ) -> dict[str, Any] | None:
        """Decode the ``final_ingestion_report`` artifact (when the
        run produced one) into a plain dict the generator's
        context builder consumes.

        Returns ``None`` quietly on any failure path: the artifact
        may not exist (legacy runs), may be unreadable, or its
        body may not be JSON. Question generation degrades to
        chunk-only context — never raises."""
        from j1.processing.results import (
            ARTIFACT_KIND_FINAL_INGESTION_REPORT,
        )
        candidates = [
            r for r in self._artifacts.list_artifacts(ctx)
            if r.kind == ARTIFACT_KIND_FINAL_INGESTION_REPORT
            and r.metadata.get("run_id") == run_id
        ]
        if not candidates:
            return None
        # Newest wins when multiple exist (regenerated runs).
        candidates.sort(key=lambda r: r.updated_at, reverse=True)
        artifact = candidates[0]
        if self._workspace is None:
            return None
        try:
            from pathlib import PurePosixPath
            from j1.workspace.layout import WorkspaceArea as _WA
            import json as _json
            parts = PurePosixPath(artifact.location).parts
            if len(parts) < 2:
                return None
            area_name, *rest = parts
            area = _WA(area_name)
            path = self._workspace.area(ctx, area).joinpath(*rest)
            return _json.loads(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.debug(
                "final_ingestion_report decode failed for run=%s: %s",
                run_id, exc,
            )
            return None

    def _resolve_domain_for_run(
        self, ctx: ProjectContext, run: IngestionRun,
    ) -> tuple[str | None, DomainValidationGuidance | None]:
        """Look up the run's domain pack via the planning result.

 Returns `(None, None)` quietly on every failure path (no
 registry wired, planning_result missing, pack id unknown,
 pack has no validation block). Domain awareness is opt-in —
 generation runs in generic mode when anything is missing."""
        if self._domain_registry is None or self._workspace is None:
            return (None, None)
        try:
            planning_path = self._workspace.area(
                ctx, WorkspaceArea.PLANNING,
            ) / run.run_id / "planning_result.json"
        except Exception:  # noqa: BLE001 — workspace may not have planning
            return (None, None)
        if not planning_path.is_file():
            return (None, None)
        try:
            import json
            data = json.loads(planning_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — malformed JSON shouldn't 500
            return (None, None)
        dctx = data.get("domain_context") or {}
        domain_id = dctx.get("selected_domain")
        if not isinstance(domain_id, str) or not domain_id:
            return (None, None)
        pack = self._domain_registry.get(domain_id)
        if pack is None:
            return (domain_id, None)
        guidance = getattr(pack, "validation_guidance", None)
        if guidance is None or not guidance.enabled:
            return (domain_id, None)
        return (domain_id, guidance)

    def _find_existing_set(
        self,
        ctx: ProjectContext,
        run_id: str,
        artifacts_content_hash: str | None,
    ) -> ValidationSetDTO | None:
        """Idempotency lookup — returns the most-recent set whose
 `artifacts_content_hash` matches. None when no match (caller
 proceeds to upsert the freshly generated set)."""
        if self._set_store is None or not artifacts_content_hash:
            return None
        for existing in self._set_store.list_for_run(ctx, run_id):
            if existing.artifacts_content_hash == artifacts_content_hash:
                return existing
        return None

    def _audit_set_generated(
        self,
        ctx: ProjectContext,
        run: IngestionRun,
        vset: ValidationSetDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_SET_GENERATED,
                target_kind=_TARGET_KIND_VALIDATION_SET,
                target_id=vset.validation_set_id,
                correlation_id=run.run_id,
                payload={
                    "validationSetId": vset.validation_set_id,
                    "runId": run.run_id,
                    "caseCount": len(vset.test_cases),
                    "source": vset.source,
                    "generatorVersion": vset.generator_version,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for set generation", exc_info=True)

    def _audit_verdict_recorded(
        self,
        *,
        ctx: ProjectContext,
        run_id: str,
        vrun: ValidationRunDTO,
        result_id: str,
        verdict: str,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_VERDICT_RECORDED,
                target_kind=_TARGET_KIND_VALIDATION_RESULT,
                target_id=result_id,
                correlation_id=run_id,
                payload={
                    "validationRunId": vrun.validation_run_id,
                    "validationSetId": vrun.validation_set_id,
                    "runId": run_id,
                    "resultId": result_id,
                    "verdict": verdict,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for verdict recording", exc_info=True)

    def _audit_run_completed(
        self,
        ctx: ProjectContext,
        run_id: str,
        vrun: ValidationRunDTO,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_RUN_COMPLETED,
                target_kind=_TARGET_KIND_VALIDATION_RUN,
                target_id=vrun.validation_run_id,
                correlation_id=run_id,
                payload={
                    "validationRunId": vrun.validation_run_id,
                    "validationSetId": vrun.validation_set_id,
                    "runId": run_id,
                    "executionStatus": vrun.execution_status,
                    "validationStatus": vrun.validation_status,
                    "total": vrun.summary.total,
                    "passed": vrun.summary.passed,
                    "failed": vrun.summary.failed,
                },
            )
        except Exception:  # noqa: BLE001
            _log.warning("audit write failed for run completion", exc_info=True)

    # ---- Internals -----------------------------------------------------

    def _load_run(self, ctx: ProjectContext, run_id: str) -> IngestionRun:
        """Run-ownership gate.

 Same shape and behaviour as `IngestionResultReviewService._load_run`:
 identical message on missing-vs-cross-tenant so existence is
 not probeable. Returning the typed `ReviewNotFound` lets the
 REST layer share the existing exception handler.
 """
        run = self._run_store.get(ctx, run_id)
        if run is None:
            raise ReviewNotFound(f"ingestion run {run_id!r} not found")
        return run

    def _audit_manual_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        request_id: str,
        request: ManualTestQueryRequest,
        validation_status: str,
        retrieved_count: int,
        citation_count: int,
        actor: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                ctx,
                actor=actor,
                action=_ACTION_MANUAL_QUERY,
                target_kind=_TARGET_KIND_RUN,
                target_id=run.run_id,
                correlation_id=run.run_id,
                payload={
                    "requestId": request_id,
                    "question": request.question,
                    "mode": request.mode,
                    "topK": request.top_k,
                    "citationRequired": request.citation_required,
                    "validationStatus": validation_status,
                    "retrievedCount": retrieved_count,
                    "citationCount": citation_count,
                },
            )
        except Exception:  # noqa: BLE001
            # Telemetry never fails the user-facing call.
            _log.warning("audit write failed for manual test query", exc_info=True)


# ---- Module-level helpers (easy to unit-test) --------------------------


def _evidence_blocks_from_chunks(
    chunks: list,
) -> list[EvidenceBlockDTO]:
    """Project the run's chunks into evidence blocks for the
    generator's LLM call.

    Used by ``generate_validation_set`` only — the manual-query and
    batch-validation paths now flow through SmartQueryOrchestrator
    which builds evidence blocks itself. Kept here so the test-
    case generator's prompt stays grounded against real chunk
    bodies.

    Empty chunks (body-less, e.g. a structural index entry) are
    dropped so the LLM doesn't see ``[3] `` with nothing after it.
    """
    blocks: list[EvidenceBlockDTO] = []
    for chunk in chunks:
        body = (chunk.body or "").strip()
        if not body:
            continue
        artifact_id = (
            getattr(chunk, "source_artifact_id", None)
            or chunk.chunk_id or ""
        )
        if not artifact_id:
            continue
        blocks.append(EvidenceBlockDTO(
            artifact_id=artifact_id,
            artifact_type="chunk",
            text=body,
            chunk_id=chunk.chunk_id,
            score=0.0,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section=chunk.section,
        ))
    return blocks


def _document_ids(run: IngestionRun) -> list[str]:
    """Best-effort recovery of the run's target documents.

 Mirrors the helper in `j1.ingestion_review.service` so the
 validation set carries the same document_ids list the rest of
 the review surface surfaces. Inlined rather than imported to
 avoid coupling validation to review's private internals.
 """
    raw = run.metadata.get("target_document_ids")
    if isinstance(raw, list) and raw:
        seen: list[str] = []
        for entry in raw:
            text = str(entry)
            if text and text not in seen:
                seen.append(text)
        if run.document_id and run.document_id not in seen:
            seen.append(run.document_id)
        return seen
    return [run.document_id] if run.document_id else []


def _coerce_mode(raw: str) -> QueryMode:
    """Tolerantly map a request-supplied mode string to a `QueryMode`.

 Unknown values fall back to AUTO so a tester typo can't turn into
 a 500. The REST layer additionally validates upstream, but the
 service is the source of truth for the final dispatch.
 """
    try:
        return QueryMode(raw)
    except ValueError:
        return QueryMode.AUTO


def _replace_verdict(
    result: ValidationResultDTO,
    *,
    verdict: str,
    notes: str | None,
) -> ValidationResultDTO:
    """Return a new result DTO with `tester_verdict` + `tester_notes`
 swapped. Avoids `dataclasses.replace` so callers don't have to
 import dataclasses just to mutate two fields. Frozen dataclasses
 can't be edited in place — this is the supported pattern."""
    return ValidationResultDTO(
        result_id=result.result_id,
        test_case_id=result.test_case_id,
        status=result.status,
        question=result.question,
        answer=result.answer,
        retrieved_chunks=list(result.retrieved_chunks),
        citations=list(result.citations),
        checks=list(result.checks),
        judge_notes=result.judge_notes,
        failure_reason=result.failure_reason,
        tester_verdict=verdict,  # type: ignore[arg-type]
        tester_notes=notes,
    )


def _replace_run_results(
    vrun: ValidationRunDTO,
    *,
    results: list[ValidationResultDTO],
) -> ValidationRunDTO:
    """Return a new run DTO with `results` swapped. Same field-by-
 field copy pattern as `_replace_verdict`."""
    return ValidationRunDTO(
        validation_run_id=vrun.validation_run_id,
        validation_set_id=vrun.validation_set_id,
        run_id=vrun.run_id,
        execution_status=vrun.execution_status,
        validation_status=vrun.validation_status,
        started_at=vrun.started_at,
        completed_at=vrun.completed_at,
        actor=vrun.actor,
        summary=vrun.summary,
        results=results,
        failure_message=vrun.failure_message,
        metadata=dict(vrun.metadata),
    )


def _render_markdown_report(vrun: ValidationRunDTO) -> str:
    """Compose a Markdown validation report for one terminal run.

 Sections (in order):
 1. Header — run id, set id, status, timestamps.
 2. Summary — counters + recommendation + main issues.
 3. Coverage — by-type / by-priority counts.
 4. Per-case results — question, status, tester verdict,
 answer, citations, checks (failed first).

 Render rules:
 * `executionStatus` and `validationStatus` are surfaced
 side-by-side; the split is the operator's main signal.
 * Tester verdicts (when set) appear next to the auto status
 as `auto: failed → tester: pass` so the override is
 explicit.
 * Failed cases bubble to the top of the per-case list (the
 thing testers want to act on).
 * Long content is hard-wrapped to ~120 cols where reasonable;
 we don't actually re-wrap user-provided text — that's the
 producer's responsibility.
 """
    lines: list[str] = []
    lines.append(f"# Validation Report — {vrun.validation_run_id}")
    lines.append("")
    lines.append(
        f"- **Ingestion run:** `{vrun.run_id}`  ·  "
        f"**Validation set:** `{vrun.validation_set_id}`"
    )
    lines.append(
        f"- **Execution status:** `{vrun.execution_status}`  ·  "
        f"**Validation status:** `{vrun.validation_status}`"
    )
    lines.append(
        f"- **Started:** {vrun.started_at}  ·  "
        f"**Completed:** {vrun.completed_at or '—'}"
    )
    lines.append(f"- **Actor:** {vrun.actor}")
    if vrun.failure_message:
        lines.append(f"- **Failure message:** {vrun.failure_message}")
    lines.append("")

    # Summary counts.
    s = vrun.summary
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- Total: **{s.total}**  ·  "
        f"Passed: **{s.passed}**  ·  Warning: **{s.warning}**  ·  "
        f"Failed: **{s.failed}**  ·  Skipped: **{s.skipped}**"
    )
    if s.recommended_action:
        lines.append(f"- **Recommendation:** {s.recommended_action}")
    if s.main_issues:
        lines.append("- **Main issues:**")
        for issue in s.main_issues:
            lines.append(f"    - {issue}")
    lines.append("")

    # Coverage.
    cov = s.coverage
    if cov.by_type or cov.by_priority:
        lines.append("## Coverage")
        lines.append("")
        if cov.by_type:
            lines.append("### By type")
            for k, v in sorted(cov.by_type.items()):
                lines.append(f"- `{k}`: {v}")
            lines.append("")
        if cov.by_priority:
            lines.append("### By priority")
            for k, v in sorted(cov.by_priority.items()):
                lines.append(f"- `{k}`: {v}")
            lines.append("")

    # Per-case results — failed first, then warning, then passed,
    # then skipped. Within each bucket: original execution order so
    # smoke-priority shows before the rest.
    lines.append("## Results")
    lines.append("")
    bucket_order = {
        "failed": 0, "warning": 1, "passed": 2, "skipped": 3,
    }
    sorted_results = sorted(
        enumerate(vrun.results),
        key=lambda pair: (bucket_order.get(pair[1].status, 99), pair[0]),
    )
    for _, r in sorted_results:
        lines.extend(_render_result_section(r))

    return "\n".join(lines).rstrip() + "\n"


def _render_result_section(r: ValidationResultDTO) -> list[str]:
    """Per-case Markdown block. Used by `_render_markdown_report`."""
    lines: list[str] = []
    status_marker = {
        "passed": "✓",
        "warning": "⚠",
        "failed": "✗",
        "skipped": "⊝",
    }.get(r.status, "?")
    title = f"{status_marker} {r.test_case_id} — `{r.status}`"
    if r.tester_verdict and r.tester_verdict != r.status:
        # Make the override explicit when it disagrees with auto.
        title += f" (tester: `{r.tester_verdict}`)"
    elif r.tester_verdict:
        title += f" · tester: `{r.tester_verdict}`"
    lines.append(f"### {title}")
    lines.append("")
    lines.append(f"**Question:** {r.question}")
    lines.append("")
    if r.failure_reason:
        lines.append(f"**Failure reason:** {r.failure_reason}")
        lines.append("")
    if r.answer:
        # Indent the answer as a quote block so multi-line answers
        # don't break the heading hierarchy.
        for ln in r.answer.splitlines() or [""]:
            lines.append(f"> {ln}")
        lines.append("")
    if r.tester_notes:
        lines.append(f"**Tester notes:** {r.tester_notes}")
        lines.append("")
    if r.checks:
        lines.append("**Checks:**")
        for c in r.checks:
            mark = "✓" if c.passed else "✗"
            sev = c.severity
            line = f"- {mark} `{c.name}` ({sev})"
            if c.detail:
                line += f" — {c.detail}"
            lines.append(line)
        lines.append("")
    if r.citations:
        lines.append("**Citations:**")
        for c in r.citations:
            piece = f"`{c.artifact_type}` · `{c.artifact_id}`"
            if c.chunk_id:
                piece += f" · chunk `{c.chunk_id}`"
            if c.source_location:
                piece += f" · {c.source_location}"
            lines.append(f"- {piece}")
        lines.append("")
    return lines


def _retrieved_chunks_from_trace(trace: Any) -> list[RetrievedChunkRefDTO]:
    """Project ``QueryTrace.all_candidates`` into the public chunk-ref
    DTO. All candidates that came back from any route surface here —
    the frontend's "retrieved" list shows what retrieval surfaced,
    independent of what synthesis used."""
    out: list[RetrievedChunkRefDTO] = []
    seen: set[tuple[str, str | None]] = set()
    for cand in getattr(trace, "all_candidates", ()):
        key = (cand.artifact_id, cand.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(RetrievedChunkRefDTO(
            artifact_id=cand.artifact_id,
            chunk_id=cand.chunk_id,
            run_id=cand.run_id,
            document_id=cand.document_id,
            source_location=(
                (cand.extra or {}).get("section_path")
            ),
            score=float(cand.score or 0.0),
            preview=(cand.text_preview or "")[:_PREVIEW_MAX_CHARS],
            artifact_kind=cand.artifact_kind,
        ))
    return out


def _citations_from_orchestrator(result: Any) -> list[dict[str, Any]]:
    """Project the orchestrator's ``citations`` (cited subset of
    selected) into the wire-shape citation dicts. The contract: this
    list is STRICTLY the blocks the LLM cited — never the broader
    retrieved set, never the full selected pack. Closes the legacy
    "20 citations when only 4 were used" gap."""
    out: list[dict[str, Any]] = []
    for block in getattr(result, "citations", ()):
        cand = block.candidate
        out.append({
            "artifactId": cand.artifact_id,
            "artifactType": cand.artifact_kind,
            "sourceDocumentId": cand.document_id,
            "sourceLocation": (cand.extra or {}).get("section_path"),
            "chunkId": cand.chunk_id,
            "runId": cand.run_id,
        })
    return out


def _checks_from_gate_results(
    gate_results: tuple,
) -> list[ValidationCheckDTO]:
    """Translate orchestrator ``GateResult``s into the legacy
    ``ValidationCheckDTO`` list. The frontend's Validation tab
    iterates this list one row per check — same wire shape, new
    source of truth.

    ``severity`` is mapped: orchestrator's "required" stays "required";
    "advisory" maps to "optional" so the legacy UI keeps rendering
    correctly. Skipped gates (advisory with no failure) come through
    as ``skipped=True``."""
    out: list[ValidationCheckDTO] = []
    for g in gate_results:
        severity = "required" if g.severity == "required" else "optional"
        is_skipped = (
            g.severity == "advisory"
            and bool(g.detail.get("skipped"))
        )
        out.append(ValidationCheckDTO(
            name=g.name,
            severity=severity,
            passed=bool(g.passed) and not is_skipped,
            detail=g.reason,
            expected=None,
            actual=g.detail if g.detail else None,
            skipped=is_skipped,
            skipped_reason=(
                "gate skipped for this intent / plan policy"
                if is_skipped else None
            ),
        ))
    return out


def _validation_status_from_final(final_status: str):
    """Map ``QueryFinalStatus`` strings into the legacy
    ``ValidationStatus`` literal.

    The orchestrator's vocabulary is narrower (passed / failed /
    evidence_insufficient / retrieval_insufficient). The legacy
    vocabulary has passed / passed_with_warnings / failed /
    inconclusive. Mapping:

      * ``passed``                  → ``passed``
      * ``failed``                  → ``failed``
      * ``evidence_insufficient``   → ``failed`` (the gate fired
                                       BEFORE synthesis — that's a
                                       failure for the validation
                                       surface, not a soft warning)
      * ``retrieval_insufficient``  → ``inconclusive`` (no
                                       candidates at all — typically
                                       a configuration / scope issue)
      * anything else               → ``inconclusive``
    """
    if final_status == "passed":
        return "passed"
    if final_status == "failed":
        return "failed"
    if final_status == "evidence_insufficient":
        return "failed"
    if final_status == "retrieval_insufficient":
        return "inconclusive"
    return "inconclusive"


def _evidence_blocks_from_trace(trace: Any) -> list[EvidenceBlockDTO]:
    """Project the orchestrator's ``llm_evidence`` (the blocks the
    synthesizer actually saw) into the DTO. When the sufficiency
    gate failed before synthesis, ``llm_evidence`` is empty — that's
    correct: nothing was sent to the LLM."""
    out: list[EvidenceBlockDTO] = []
    for block in getattr(trace, "llm_evidence", ()):
        cand = block.candidate
        out.append(EvidenceBlockDTO(
            artifact_id=cand.artifact_id,
            artifact_type=cand.artifact_kind,
            text=(block.body or cand.text_preview or "")[:4000],
            chunk_id=cand.chunk_id,
            score=float(cand.score or 0.0),
            section=(cand.extra or {}).get("section_path"),
            source_location=(cand.extra or {}).get("section_path"),
        ))
    return out


def _evidence_flags_from_trace(trace: Any) -> dict[str, bool]:
    """Modality flags the FE renders as Graph/Tables/Images chips.
    Derived from the retrieved-candidate kinds + trace metadata."""
    kinds = {
        c.artifact_kind for c in getattr(trace, "all_candidates", ())
    }
    return {
        "graphUsed": "graph_json" in kinds or any(
            "graph" in (k or "") for k in kinds
        ),
        "tablesUsed": "enriched.tables" in kinds,
        "imagesUsed": "enriched.visuals" in kinds,
    }
