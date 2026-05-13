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
from j1.validation.checks import aggregate_status, run_checks
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


def _rewrite_check_as_skipped(
    check: ValidationCheckDTO, *, skipped_reason: str,
) -> ValidationCheckDTO:
    """Return a copy of ``check`` flipped to the ``skipped`` state.

    Used by the service when an upstream dispatcher outcome (e.g.
    native unavailable + no fallback) makes a check N/A even
    though the check itself can't see the dispatcher state. Lets
    the Checks panel render a neutral skipped row instead of a
    red ✗ for a deliberate dispatcher outcome.
    """
    from dataclasses import replace
    return replace(
        check,
        skipped=True,
        skipped_reason=skipped_reason,
        passed=False,
    )


@dataclass(frozen=True)
class _DispatchResult:
    """Outcome of one ``_dispatch_query`` call.

    ``response`` is always the ``QueryResponse`` the rest of the
    validation pipeline operates on (citations, run-scope
    checks, evidence). ``native_answer``, when present, is used
    by ``run_manual_test_query`` to override the synthesized
    answer; ``None`` means "use the local synthesizer's output."
    ``debug_extras`` carries mode-specific telemetry that gets
    merged into the manual-query debug payload.

    ``answer_provider`` records which provider produced the
    final user-visible answer text:
      * ``"native"``              — native LightRAG answer.
      * ``"bm25"``                — BM25 + local LLM synthesizer
                                    (only valid in the
                                    ``bm25_quality_debug`` /
                                    ``hybrid_ab`` engines and
                                    on the explicit fallback
                                    path).
      * ``"bm25_fallback"``       — native attempted but failed;
                                    BM25 was used as the
                                    fallback because the
                                    operator opted in.
      * ``"native_unavailable"``  — native failed AND fallback is
                                    off. Answer surface is empty;
                                    BM25 (if it ran) is still
                                    available as auxiliary
                                    evidence.

    ``suppress_synthesis`` tells ``run_manual_test_query`` to
    NOT invoke the local LLM synthesizer. Used by engines /
    paths where BM25 evidence might exist for inspection but
    must NOT be allowed to drive the answer text — preserving
    the post-clarification rule that BM25 is auxiliary-only
    unless an explicit fallback / debug engine is selected.
    """

    response: Any  # j1.query.models.QueryResponse — typed Any to
                   # avoid coupling the dataclass to the optional
                   # import surface.
    native_answer: str | None
    native_latency_ms: int | None
    debug_extras: dict[str, Any]
    answer_provider: str = "bm25"
    suppress_synthesis: bool = False

# Preview length for retrieved-chunk excerpts on the response. Mirrors
# the chunk projector's value so the UI layer renders consistent
# preview lengths across the Validation tab and the Chunks tab.
_PREVIEW_MAX_CHARS = 240


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
        # Reserve the request id up-front so the value the FE sees in
        # the response also lands in the audit log on the same row.
        request_id = f"tq-{uuid.uuid4().hex[:12]}"

        # New pipeline branch: when a SmartQueryOrchestrator is
        # wired, delegate to the new flow. The legacy
        # aggregate_status / refusal-regex path is bypassed
        # entirely — the orchestrator's AnswerQualityGate owns
        # final_status. The legacy code below stays in place for
        # tests / deployments that haven't migrated yet, but
        # production should land here.
        if self._smart_query_orchestrator is not None:
            return self._run_manual_query_via_orchestrator(
                ctx=ctx, run=run, request=request,
                request_id=request_id, actor=actor,
            )

        requested_top_k = max(1, min(request.top_k, _TOP_K_HARD_CAP))
        # Decouple raw FTS candidate breadth from the FE-requested
        # value. The engine is asked for at least
        # ``candidate_top_k`` rows so the downstream reranker
        # (``j1.validation.rerank``) has enough candidates to
        # cover the question's aspects. If the FE / caller asked
        # for MORE than the configured floor, honour that — they
        # want broader output. Final evidence is still capped
        # downstream at ``evidence_max_blocks``.
        candidate_top_k = max(requested_top_k, self._validation_candidate_top_k)
        candidate_top_k = min(candidate_top_k, _TOP_K_HARD_CAP)
        mode = _coerce_mode(request.mode)

        # Resolve the validation scope (spec section 9). Default
        # `"run"` keeps the existing behaviour — RunScope(this.run_id)
        # — so legacy callers see no change. `"active"` redirects
        # to the document's currently-promoted run, which can
        # differ from `run_id` after a successful reindex. When the
        # document has no active run (detached/removed/never
        # ingested), the resolver returns a sentinel that matches
        # zero artifacts, which is the correct "nothing to
        # validate" answer.
        engine_scope = self._resolve_query_scope(
            ctx=ctx, run=run, validation_scope=request.validation_scope,
        )

        query_request = QueryRequest(
            question=request.question,
            mode=mode,
            max_results=candidate_top_k,
            scope=engine_scope,
        )

        try:
            dispatch = self._dispatch_query(
                ctx=ctx, run=run, query_request=query_request,
            )
            response = dispatch.response
        except Exception as exc:  # noqa: BLE001
            # Engine failures must not 500 — surface them as a structured
            # `inconclusive` response so the FE can render an actionable
            # message instead of a transport error.
            _log.warning(
                "validation manual query engine failure run_id=%s: %s",
                run.run_id, exc,
            )
            return _inconclusive_response(
                request_id=request_id,
                run_id=run.run_id,
                question=request.question,
                error=str(exc),
            )

        retrieved = _retrieved_chunks_from_response(response)
        citations = _citations_from_response(response)

        # ``chunks_expected`` controls whether the
        # ``retrieved_chunks_present`` REQUIRED check fires as a
        # hard check or as a "skipped" placeholder. The pure-native
        # engine doesn't surface chunks by design, so an empty
        # retrieval there is the correct outcome — not a failure.
        # Likewise when the with-quality-evidence engine's native
        # call succeeded the chunks are AUXILIARY evidence, not
        # the primary answer source, so an empty list shouldn't
        # flip the overall verdict to "failed" — we let the
        # check skip and the absence surface via the
        # ``data_quality_evidence`` section instead.
        chunks_expected = self._is_chunks_expected(dispatch)

        checks = run_checks(
            ctx=ctx,
            run_id=run.run_id,
            answer=response.answer,
            retrieved_chunks=retrieved,
            citations=citations,
            citation_required=request.citation_required,
            artifact_registry=self._artifacts,
            chunks_expected=chunks_expected,
        )
        validation_status = aggregate_status(checks)

        # When native returned no answer AND the operator chose NOT
        # to fall back to BM25, the response is "inconclusive" —
        # the validation tab has nothing to verify, not a content
        # failure. Without this override:
        #   * the empty answer trips ``answer_non_empty`` →
        #     "failed", suggesting the engine produced a bad
        #     answer when in fact it intentionally produced none.
        # We also rewrite that single check to ``skipped`` so the
        # Checks panel doesn't show a red ✗ next to a row whose
        # absence was a deliberate dispatcher outcome.
        if dispatch.answer_provider == "native_unavailable":
            checks = [
                _rewrite_check_as_skipped(
                    c,
                    skipped_reason=(
                        "engine intentionally produced no answer "
                        "(native unavailable, BM25 fallback off)"
                    ),
                ) if c.name == "answer_non_empty" else c
                for c in checks
            ]
            validation_status = "inconclusive"

        evidence_flags = {
            "graphUsed": bool(response.graph_paths),
            "tablesUsed": _has_artifact_kind(retrieved, "enriched.tables"),
            "imagesUsed": _has_artifact_kind(retrieved, "enriched.visuals"),
        }

        raw_response = (
            _engine_response_to_raw(response)
            if request.include_raw
            else None
        )

        # Phase-1 retrieval-quality diagnostics. One per query;
        # carries the audit trail through scope filter / intent
        # router / boilerplate demotion / quality checks. The
        # snapshot is also surfaced on the response as
        # ``retrieval_trace`` so the FE can render the per-
        # candidate decisions.
        from j1.retrieval.diagnostics import RetrievalDiagnostics
        from j1.validation.runner import (
            _emit_live_path_entered, _emit_live_path_evidence_sent,
        )
        # Unmistakable live-path entry marker — manual-query side.
        _emit_live_path_entered(
            audit=self._audit, ctx=ctx,
            endpoint="POST /ingestion-runs/{run_id}/test-query",
            handler="IngestionValidationService.run_manual_test_query",
            run_id=run.run_id,
            document_id=run.document_id,
            query=request.question,
            retrieval_mode=(
                "planner_first" if run.document_id else "legacy"
            ),
        )
        retrieval_diag = RetrievalDiagnostics(
            audit=self._audit,
            ctx=ctx,
            run_id=run.run_id,
            document_id=run.document_id,
            query=request.question,
        )
        evidence_blocks = self._build_evidence_blocks_for_run(
            ctx=ctx,
            request=request,
            retrieved=retrieved,
            response=response,
            active_document_id=run.document_id,
            active_run_id=run.run_id,
            diagnostics=retrieval_diag,
        )
        _emit_live_path_evidence_sent(
            audit=self._audit, ctx=ctx,
            endpoint="POST /ingestion-runs/{run_id}/test-query",
            handler="IngestionValidationService.run_manual_test_query",
            run_id=run.run_id,
            document_id=run.document_id,
            evidence=evidence_blocks,
            snapshot=retrieval_diag.snapshot(),
        )
        # Synthesis is suppressed by the dispatcher when BM25
        # evidence may exist (for inspection) but must NOT be
        # allowed to drive the answer text — the post-clarification
        # rule that BM25 is auxiliary-only outside the explicit
        # ``bm25_quality_debug`` / fallback paths. In that case we
        # also drop the evidence blocks from ``evidence_sent_to_llm``
        # so the FE doesn't render them as "the LLM saw this when
        # answering" (the LLM didn't run).
        if dispatch.suppress_synthesis:
            synthesized_answer = None
            # Emit a STRUCTURED skipped trace rather than ``None``
            # so the FE renders an accurate reason. Previously
            # ``llm_trace=None`` flowed through to the FE as
            # "LLM synthesis is off — flip the toggle" — wrong,
            # because the operator HAD the toggle on.
            llm_trace = LLMTraceDTO(
                called=False,
                error="synthesis_skipped_native_unavailable",
            )
            evidence_blocks = []
        else:
            synthesized_answer, llm_trace = self._maybe_synthesize_answer(
                request=request,
                evidence=evidence_blocks,
            )

        # When the native LightRAG ``aquery`` produced its own
        # prose answer, we prefer that over the local
        # synthesizer's answer — it was generated from the full
        # graph + vector context LightRAG has, whereas the local
        # synthesizer only saw the BM25-augmented evidence
        # blocks. The local synthesizer's trace is preserved in
        # ``llm`` so operators can still see what it would have
        # said for comparison. Citation augmentation is recorded
        # in the debug payload.
        if dispatch.native_answer:
            synthesized_answer = dispatch.native_answer
            if llm_trace is None:
                llm_trace = LLMTraceDTO(
                    called=True,
                    provider="raganything",
                    model="native",
                    latency_ms=dispatch.native_latency_ms,
                    prompt_tokens=None,
                    completion_tokens=None,
                    error=None,
                )

        self._audit_manual_query(
            ctx=ctx,
            run=run,
            request_id=request_id,
            request=request,
            validation_status=validation_status,
            retrieved_count=len(retrieved),
            citation_count=len(citations),
            actor=actor,
        )

        debug = _build_manual_query_debug(
            retrieved=retrieved,
            evidence_blocks=evidence_blocks,
            synthesized_answer=synthesized_answer,
            llm_trace=llm_trace,
            requested_top_k=requested_top_k,
            candidate_top_k_used=candidate_top_k,
            evidence_max_blocks=self._validation_evidence_max_blocks,
            scope_run_id=run.run_id,
            question=request.question,
        )
        # Merge the dispatcher's mode-specific debug. ``debug_extras``
        # carries query_provider_mode / native_query_used /
        # bm25_query_used / native_query_failed_reason /
        # native_latency_ms / bm25_latency_ms /
        # citation_augmentation_used / fallback_used /
        # bm25_answer_preview / native_answer_preview /
        # answer_provider — populated by ``_dispatch_query``.
        debug.update(dispatch.debug_extras)
        # Provider-role surface. ``evidence_provider`` and
        # ``citation_provider`` are constants today — BM25 is the
        # only path that produces structured citations / evidence
        # blocks. They're stamped explicitly so debug consumers
        # don't have to infer "where did this come from" from
        # other fields.
        debug["answer_provider"] = dispatch.answer_provider
        debug["evidence_provider"] = "bm25"
        debug["citation_provider"] = "bm25"
        # Canonical metadata surface (audit follow-up). Older
        # keys are retained above for one release while callers
        # migrate; the new names are the ones the FE / external
        # debug consumers should standardise on.
        self._stamp_canonical_metadata(
            debug=debug,
            ctx=ctx,
            run=run,
            dispatch=dispatch,
            retrieved=retrieved,
            request=request,
            llm_trace=llm_trace,
            synthesized_answer=synthesized_answer,
            requested_top_k=requested_top_k,
            candidate_top_k_used=candidate_top_k,
            chunks_expected=chunks_expected,
        )
        # Operator-readable retrieval breakdown. Helps confirm
        # whether the empty-result outcome was "engine never asked
        # BM25" (pure native engine, ``bm25_query_used=False``) vs
        # "BM25 asked but returned nothing" (broken index / wrong
        # workspace). The ``engine`` + ``answer_provider`` +
        # ``bm25_query_used`` + ``native_query_failed_reason``
        # fields self-document the line so an operator reading
        # the log can answer "what just happened?" without
        # opening the response payload.
        _log.info(
            "manual_query retrieval: run_id=%s engine=%s "
            "answer_provider=%s bm25_query_used=%s "
            "native_query_used=%s native_failed_reason=%s "
            "requested_top_k=%d candidate_top_k_used=%d "
            "fts_returned=%d evidence_max=%d selected_evidence=%d",
            run.run_id,
            self._query_engine_mode,
            dispatch.answer_provider,
            dispatch.debug_extras.get("bm25_query_used"),
            dispatch.debug_extras.get("native_query_used"),
            dispatch.debug_extras.get("native_query_failed_reason"),
            requested_top_k, candidate_top_k,
            len(retrieved), self._validation_evidence_max_blocks,
            len(evidence_blocks),
        )
        return ManualTestQueryResponseDTO(
            request_id=request_id,
            run_id=run.run_id,
            question=request.question,
            answer=response.answer,
            mode_used=response.mode_used,
            retrieved_chunks=retrieved,
            citations=[_citation_to_dict(c) for c in citations],
            checks=checks,
            validation_status=validation_status,
            evidence_flags=evidence_flags,
            raw_response=raw_response,
            synthesized_answer=synthesized_answer,
            llm=llm_trace,
            evidence_sent_to_llm=evidence_blocks,
            debug=debug,
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

    def _dispatch_query(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        query_request: "QueryRequest",
    ) -> "_DispatchResult":
        """Route the manual-query request through the configured
        provider mode.

        Returns a ``_DispatchResult`` carrying:

          * ``response``           — the BM25 ``QueryResponse``. The
            rest of the pipeline (retrieved_chunks / citations /
            evidence builder / synthesizer / checks) operates on
            this object regardless of mode, so the response
            contract (citations, run-scope checks, evidence pack)
            is preserved across all three modes.
          * ``native_answer``      — non-empty when native ran AND
            produced an answer. The caller uses it to override
            ``synthesized_answer`` so the LLM-generated text the
            FE renders is the native one (when in
            ``rag_native_primary``).
          * ``debug_extras``       — provider-mode telemetry merged
            into the response debug payload.
          * ``native_latency_ms``  — wall-clock duration of the
            native call (None when native didn't run).

        Engine behaviours:

          ``bm25_debug`` (legacy ``bm25_primary``)
            Single BM25 call. Native provider untouched.
            Renamed to ``bm25_debug`` to reflect its post-audit
            role: a lexical / debug retriever, not the
            "AI answer" path.

          ``lightrag_native``
            PURE native. ``rag.aquery`` runs against the per-run
            workspace. **No BM25** unless
            ``enable_bm25_fallback`` is set AND native failed.
            Citations are populated from native if available; in
            practice LightRAG doesn't return citation metadata
            in J1's shape, so citations come back empty and
            ``citation_source`` is reported as
            ``"none_or_native_unavailable"``. This is honest —
            operators see the gap rather than a silent BM25
            substitution. Use this engine for the audit-driven
            "is the index actually working?" diagnosis.

          ``lightrag_native_with_bm25_evidence``
            (legacy ``rag_native_primary``). Native call first.
            BM25 is also called to populate citations / evidence
            blocks (LightRAG doesn't expose them natively).
            Debug records ``citation_augmentation_used=True``.
            If native fails AND ``enable_bm25_fallback``, falls
            back to BM25 for the answer too. Renamed for honesty
            — the BM25 augmentation is now obvious from the name.

          ``hybrid_ab``
            BM25 is always the stable answer. Native runs
            best-effort for observability; its answer + latency
            land in ``debug_extras`` so operators can compare. A
            native failure never affects the response.
        """
        mode = self._query_engine_mode
        engine_query = self._query_engine.query  # bound method, cheap to alias

        if mode == QUERY_ENGINE_BM25_QUALITY_DEBUG:
            bm25_start = time.monotonic()
            response = engine_query(ctx, query_request)
            bm25_latency = int((time.monotonic() - bm25_start) * 1000)
            return _DispatchResult(
                response=response,
                native_answer=None,
                native_latency_ms=None,
                answer_provider="bm25",
                debug_extras={
                    "query_provider_mode": mode,
                    "native_query_enabled": (
                        self._native_query_provider is not None
                    ),
                    "native_query_used": False,
                    "bm25_query_used": True,
                    "bm25_latency_ms": bm25_latency,
                    "fallback_used": False,
                    "citation_augmentation_used": False,
                    "bm25_answer_preview": _answer_preview(response.answer),
                    "native_answer_preview": None,
                    # BM25 IS the answer here — by design. The
                    # engine name makes it explicit, and
                    # ``bm25_purpose`` records it so a downstream
                    # auditor reading only the JSON can tell.
                    "bm25_participated_in_answer": True,
                    "bm25_purpose": BM25_PURPOSE_LEXICAL_DEBUG,
                },
            )

        if mode == QUERY_ENGINE_LIGHTRAG_NATIVE:
            return self._dispatch_lightrag_native(
                ctx=ctx, run=run, query_request=query_request,
            )

        if mode == QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE:
            return self._dispatch_lightrag_with_quality_evidence(
                ctx=ctx, run=run, query_request=query_request,
            )

        if mode == QUERY_ENGINE_HYBRID_AB:
            return self._dispatch_hybrid_ab(
                ctx=ctx, run=run, query_request=query_request,
            )

        # Defensive — constructor validates mode, but a future
        # refactor that adds a new mode without updating dispatch
        # falls through to bm25_quality_debug with a warning
        # rather than 500ing.
        _log.warning(
            "unhandled query_engine=%r in dispatcher; "
            "falling back to bm25_quality_debug",
            mode,
        )
        response = engine_query(ctx, query_request)
        return _DispatchResult(
            response=response,
            native_answer=None,
            native_latency_ms=None,
            answer_provider="bm25",
            debug_extras={
                "query_provider_mode": QUERY_ENGINE_BM25_QUALITY_DEBUG,
                "native_query_enabled": (
                    self._native_query_provider is not None
                ),
                "native_query_used": False,
                "bm25_query_used": True,
                "bm25_latency_ms": 0,
                "fallback_used": False,
                "citation_augmentation_used": False,
                "bm25_answer_preview": _answer_preview(response.answer),
                "native_answer_preview": None,
                "bm25_participated_in_answer": True,
                "bm25_purpose": BM25_PURPOSE_LEXICAL_DEBUG,
            },
        )

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

    def _dispatch_lightrag_native(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        query_request: "QueryRequest",
    ) -> "_DispatchResult":
        """``lightrag_native`` dispatch: pure native, NO BM25 unless
        fallback is explicitly enabled.

        This is the audit-driven path. We call ``rag.aquery`` and
        return its answer. Citations come from native if available;
        in practice LightRAG doesn't return citation metadata in
        J1's shape, so the response carries empty citations and the
        debug payload reports ``citation_source=
        "none_or_native_unavailable"``. The required
        ``retrieved_chunks_present`` validation check will FAIL in
        this engine — that's intentional and operator-visible: it
        cleanly separates "native answered the question" from
        "we can produce evidence for it." Use
        ``lightrag_native_with_bm25_evidence`` when you need both.

        BM25 fallback only fires when ``enable_bm25_fallback=True``
        AND native didn't produce an answer (failure / timeout /
        empty). The fallback case is recorded in
        ``fallback_used=True`` so the operator sees the path.
        """
        native_answer, native_latency, native_error = self._run_native_query(
            ctx=ctx, run=run, query_request=query_request,
        )

        if native_answer is not None:
            # Happy path. Build a minimal QueryResponse so the
            # downstream pipeline (run_checks, evidence builder)
            # operates on the same shape it always has. Sources
            # are empty — that's the honest report when native
            # doesn't expose them.
            response = self._build_native_only_response(native_answer)
            return _DispatchResult(
                response=response,
                native_answer=native_answer,
                native_latency_ms=native_latency,
                answer_provider="native",
                debug_extras={
                    "query_provider_mode": QUERY_ENGINE_LIGHTRAG_NATIVE,
                    "native_query_enabled": True,
                    "native_query_used": True,
                    "bm25_query_used": False,
                    "native_query_failed_reason": None,
                    "native_latency_ms": native_latency,
                    "bm25_latency_ms": 0,
                    "fallback_used": False,
                    "citation_augmentation_used": False,
                    "bm25_answer_preview": None,
                    "native_answer_preview": _answer_preview(native_answer),
                    "bm25_participated_in_answer": False,
                    "bm25_purpose": None,
                },
            )

        # Native failed / timed out / not wired. Fallback only if
        # explicitly enabled — pure-native operators wanted to see
        # the failure, not a silent BM25 substitution.
        if self._enable_bm25_fallback:
            bm25_start = time.monotonic()
            bm25_response = self._query_engine.query(ctx, query_request)
            bm25_latency = int((time.monotonic() - bm25_start) * 1000)
            return _DispatchResult(
                response=bm25_response,
                native_answer=None,
                native_latency_ms=native_latency,
                answer_provider="bm25_fallback",
                debug_extras={
                    "query_provider_mode": QUERY_ENGINE_LIGHTRAG_NATIVE,
                    "native_query_enabled": True,
                    "native_query_used": False,
                    "bm25_query_used": True,
                    "native_query_failed_reason": native_error,
                    "native_latency_ms": native_latency,
                    "bm25_latency_ms": bm25_latency,
                    "fallback_used": True,
                    "citation_augmentation_used": False,
                    "bm25_answer_preview": _answer_preview(
                        bm25_response.answer,
                    ),
                    "native_answer_preview": None,
                    # Explicit opt-in fallback — BM25 IS the answer
                    # text. Audit trail records the
                    # ``fallback_answer`` purpose.
                    "bm25_participated_in_answer": True,
                    "bm25_purpose": BM25_PURPOSE_FALLBACK_ANSWER,
                },
            )

        # No fallback — surface the native failure honestly. We
        # still need SOME response object so the rest of the
        # service contract holds; build an empty native-only
        # response. ``run_checks`` will flag missing retrieval /
        # missing answer; that's the desired outcome.
        empty_response = self._build_native_only_response("")
        return _DispatchResult(
            response=empty_response,
            native_answer=None,
            native_latency_ms=native_latency,
            answer_provider="native_unavailable",
            # Suppress synthesis: no BM25 evidence exists here
            # (BM25 didn't run) and the local synthesizer would
            # otherwise just emit a vacuous no-evidence stub. Be
            # explicit: when native fails and BM25 hasn't been
            # asked to help, the answer surface is empty.
            suppress_synthesis=True,
            debug_extras={
                "query_provider_mode": QUERY_ENGINE_LIGHTRAG_NATIVE,
                "native_query_enabled": True,
                "native_query_used": False,
                "bm25_query_used": False,
                "native_query_failed_reason": native_error,
                "native_latency_ms": native_latency,
                "bm25_latency_ms": 0,
                "fallback_used": False,
                "citation_augmentation_used": False,
                "bm25_answer_preview": None,
                "native_answer_preview": None,
                "bm25_participated_in_answer": False,
                "bm25_purpose": None,
            },
        )

    def _build_native_only_response(self, answer: str):
        """Construct a ``QueryResponse`` from a native answer string,
        with no sources / graph paths. Used by ``lightrag_native``
        so the downstream pipeline operates on the same shape as
        the BM25 path but the empty source list makes the
        "no citations from native" reality observable in the
        response."""
        from j1.query.models import QueryResponse
        return QueryResponse(
            answer=answer,
            mode_used="lightrag_native",
            sources=[],
            graph_paths=[],
        )

    def _dispatch_lightrag_with_quality_evidence(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        query_request: "QueryRequest",
    ) -> "_DispatchResult":
        """``lightrag_native_with_quality_evidence`` dispatch.

        Native ``aquery`` is the answer source. BM25 runs in
        parallel ONLY to populate the auxiliary data-quality /
        evidence inspection surface — its answer text never
        drives the user-visible answer. On native failure, the
        behaviour depends on the explicit ``enable_bm25_fallback``
        flag:

          * fallback ON  → BM25 answer becomes the user answer;
                           ``bm25_participated_in_answer=True``;
                           ``bm25_purpose=fallback_answer``.
          * fallback OFF → answer surface is EMPTY (native
                           failed and BM25 is auxiliary-only by
                           policy). Synthesis is suppressed so
                           the local LLM can't sneak the BM25
                           evidence into a generated answer.
                           BM25 evidence stays on the response
                           shell for inspection.

        The legacy alias ``rag_native_primary`` resolves to
        this engine.
        """
        native_answer, native_latency, native_error = self._run_native_query(
            ctx=ctx, run=run, query_request=query_request,
        )

        # Always run BM25 — even when native succeeds. Its
        # output drives the AUXILIARY data-quality / citation
        # surface; the response contract (run-scope checks,
        # citation lineage) stays intact across all modes.
        bm25_start = time.monotonic()
        bm25_response = self._query_engine.query(ctx, query_request)
        bm25_latency = int((time.monotonic() - bm25_start) * 1000)

        bm25_preview = _answer_preview(bm25_response.answer)

        if native_answer is None:
            # Native failed / timed out / not wired.
            if self._enable_bm25_fallback:
                # Explicit operator opt-in: use BM25 as the
                # answer source. Audited as ``fallback_answer``.
                return _DispatchResult(
                    response=bm25_response,
                    native_answer=None,
                    native_latency_ms=native_latency,
                    answer_provider="bm25_fallback",
                    debug_extras={
                        "query_provider_mode": (
                            QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
                        ),
                        "native_query_enabled": True,
                        "native_query_used": False,
                        "bm25_query_used": True,
                        "native_query_failed_reason": native_error,
                        "native_latency_ms": native_latency,
                        "bm25_latency_ms": bm25_latency,
                        "fallback_used": True,
                        "citation_augmentation_used": False,
                        "bm25_answer_preview": bm25_preview,
                        "native_answer_preview": None,
                        "bm25_participated_in_answer": True,
                        "bm25_purpose": BM25_PURPOSE_FALLBACK_ANSWER,
                    },
                )
            # Fallback disabled. Policy: BM25 must NOT supply
            # the answer text. Return a response with an empty
            # answer but BM25's sources attached so the
            # data-quality / citation inspection surface still
            # works. Synthesis is suppressed so the local LLM
            # can't grab the BM25 evidence and synthesise a
            # near-BM25 answer behind the operator's back.
            from j1.query.models import QueryResponse
            empty_with_evidence = QueryResponse(
                answer="",
                mode_used="lightrag_native_with_quality_evidence",
                sources=list(bm25_response.sources),
                graph_paths=list(bm25_response.graph_paths),
            )
            return _DispatchResult(
                response=empty_with_evidence,
                native_answer=None,
                native_latency_ms=native_latency,
                answer_provider="native_unavailable",
                suppress_synthesis=True,
                debug_extras={
                    "query_provider_mode": (
                        QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
                    ),
                    "native_query_enabled": True,
                    "native_query_used": False,
                    "bm25_query_used": True,
                    "native_query_failed_reason": native_error,
                    "native_latency_ms": native_latency,
                    "bm25_latency_ms": bm25_latency,
                    "fallback_used": False,
                    "citation_augmentation_used": False,
                    "bm25_answer_preview": bm25_preview,
                    "native_answer_preview": None,
                    # BM25 ran, but ONLY to populate the
                    # data-quality / citation surface. It did
                    # NOT supply the answer text.
                    "bm25_participated_in_answer": False,
                    "bm25_purpose": BM25_PURPOSE_DATA_QUALITY,
                },
            )

        # Native succeeded. Native answer drives the user-
        # visible response; BM25 ran solely to populate
        # citations / evidence (data-quality inspection).
        # ``citation_augmentation_used=True`` tells callers
        # that the citation list comes from a different source
        # than the answer text.
        return _DispatchResult(
            response=bm25_response,
            native_answer=native_answer,
            native_latency_ms=native_latency,
            answer_provider="native",
            debug_extras={
                "query_provider_mode": (
                    QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE
                ),
                "native_query_enabled": True,
                "native_query_used": True,
                "bm25_query_used": True,
                "native_query_failed_reason": None,
                "native_latency_ms": native_latency,
                "bm25_latency_ms": bm25_latency,
                "fallback_used": False,
                "citation_augmentation_used": True,
                "bm25_answer_preview": bm25_preview,
                "native_answer_preview": _answer_preview(native_answer),
                "bm25_participated_in_answer": False,
                "bm25_purpose": BM25_PURPOSE_DATA_QUALITY,
            },
        )

    def _dispatch_hybrid_ab(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        query_request: "QueryRequest",
    ) -> "_DispatchResult":
        """``hybrid_ab`` dispatch: BM25 is the stable answer; native
        runs for observability only."""
        bm25_start = time.monotonic()
        bm25_response = self._query_engine.query(ctx, query_request)
        bm25_latency = int((time.monotonic() - bm25_start) * 1000)

        # Best-effort native — failures never affect the response.
        native_answer, native_latency, native_error = self._run_native_query(
            ctx=ctx, run=run, query_request=query_request,
        )

        # Preview slices keep debug payloads bounded. The previews
        # are top-level fields (``bm25_answer_preview`` /
        # ``native_answer_preview``) in every mode so the FE can
        # render them uniformly. The legacy ``hybrid_ab_*`` keys
        # are kept as aliases for one release while existing
        # callers migrate.
        bm25_preview = _answer_preview(bm25_response.answer)
        native_preview = (
            _answer_preview(native_answer) if native_answer else None
        )

        return _DispatchResult(
            response=bm25_response,
            native_answer=None,  # never overrides synthesized_answer
            native_latency_ms=native_latency,
            answer_provider="bm25",
            debug_extras={
                "query_provider_mode": QUERY_ENGINE_HYBRID_AB,
                "native_query_enabled": True,
                # We DID call native — it just doesn't drive the
                # response. ``native_query_used`` records whether
                # the call SUCCEEDED, not whether it influenced
                # output.
                "native_query_used": native_answer is not None,
                "bm25_query_used": True,
                "native_query_failed_reason": native_error,
                "native_latency_ms": native_latency,
                "bm25_latency_ms": bm25_latency,
                "fallback_used": False,
                "citation_augmentation_used": False,
                "bm25_answer_preview": bm25_preview,
                "native_answer_preview": native_preview,
                # Legacy aliases retained for one release.
                "hybrid_ab_native_answer_preview": native_preview or "",
                "hybrid_ab_bm25_answer_preview": bm25_preview,
                # hybrid_ab is the observability engine; BM25 is
                # the stable answer by design — flag it so a
                # downstream auditor reading the JSON can tell.
                "bm25_participated_in_answer": True,
                "bm25_purpose": BM25_PURPOSE_OBSERVABILITY,
            },
        )

    def _is_chunks_expected(self, dispatch: "_DispatchResult") -> bool:
        """Decide whether the engine that produced ``dispatch`` is
        expected to surface retrieved chunks.

        * Pure ``lightrag_native`` (success or no-fallback failure)
          → False. Native doesn't return chunks in J1's shape.
        * ``lightrag_native_with_quality_evidence`` when native
          succeeded → False. Chunks here are AUXILIARY evidence
          (BM25 supplied them) — an empty list is a data-quality
          warning, not a primary-answer failure.
        * ``bm25_quality_debug`` / ``hybrid_ab`` / fallback paths
          → True. The answer text itself comes from a chunk-
          producing engine.
        """
        mode = self._query_engine_mode
        if mode == QUERY_ENGINE_LIGHTRAG_NATIVE:
            return False
        if mode == QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE:
            # Native succeeded → chunks are auxiliary; skip the
            # required check. Native failed (+ no fallback) →
            # same outcome, surfaced via ``data_quality_evidence``.
            if dispatch.answer_provider in {"native", "native_unavailable"}:
                return False
        return True

    def _stamp_canonical_metadata(
        self,
        *,
        debug: dict[str, Any],
        ctx: ProjectContext,
        run: IngestionRun,
        dispatch: "_DispatchResult",
        retrieved: list[RetrievedChunkRefDTO],
        request: "ManualTestQueryRequest | None" = None,
        llm_trace: "LLMTraceDTO | None" = None,
        synthesized_answer: str | None = None,
        requested_top_k: int | None = None,
        candidate_top_k_used: int | None = None,
        chunks_expected: bool = True,
    ) -> None:
        """Stamp the canonical query metadata onto ``debug`` in place.

        Vocabulary used by FE / external debug consumers:

          * ``query_engine``                — canonical engine name.
          * ``answer_source`` /             — same value; the
            ``final_answer_source``           explicit "what
                                              produced the user-
                                              visible answer text"
                                              answer.
          * ``citation_source`` /           — same value; the
            ``evidence_source``               provider behind the
                                              citation / evidence
                                              list. One of
                                              ``"native"``,
                                              ``"bm25"``,
                                              ``"bm25_augmentation"``,
                                              ``"none_or_native_unavailable"``.
          * ``bm25_used``                   — did BM25 run at all.
          * ``bm25_participated_in_answer`` — did BM25 produce
                                              answer text. False
                                              in the production
                                              path; True only on
                                              the explicit
                                              fallback / debug /
                                              observability
                                              engines.
          * ``bm25_purpose``                — null when BM25 was
                                              not used; otherwise
                                              one of
                                              ``data_quality_evidence_inspection``,
                                              ``fallback_answer``,
                                              ``lexical_debug_answer``,
                                              ``observability_answer``.
          * ``workspace_id`` /              — per-run LightRAG
            ``workspace_path``                workspace identifier
                                              + absolute path.
          * ``run_id`` / ``document_id``    — explicit identifiers
                                              so the FE doesn't
                                              have to thread them
                                              through.
          * ``data_quality_evidence``       — auxiliary BM25
                                              section (chunk
                                              count + per-field
                                              metadata-quality
                                              flags). Present
                                              only when BM25 ran.
          * ``warnings``                    — list of operator-
                                              facing advisories.
        """
        extras = dispatch.debug_extras
        bm25_used = bool(extras.get("bm25_query_used"))
        citation_aug = bool(extras.get("citation_augmentation_used"))
        fallback_used = bool(extras.get("fallback_used"))
        answer_source = dispatch.answer_provider
        bm25_participated = bool(
            extras.get("bm25_participated_in_answer", False),
        )
        bm25_purpose = extras.get("bm25_purpose")

        if citation_aug:
            citation_source = "bm25_augmentation"
        elif answer_source == "native_unavailable" and not bm25_used:
            citation_source = "none_or_native_unavailable"
        elif answer_source == "native" and not bm25_used:
            citation_source = "none_or_native_unavailable"
        elif bm25_used:
            # BM25 supplied citations; whether it also supplied
            # the answer is captured separately via
            # ``bm25_participated_in_answer``.
            citation_source = "bm25"
        else:
            citation_source = "none_or_native_unavailable"

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
                workspace_path = None
        tenant = getattr(ctx, "tenant_id", None) or ""
        project = getattr(ctx, "project_id", None) or ""
        if tenant and project and run.document_id and run.run_id:
            workspace_id = (
                f"{tenant}/{project}/{run.document_id}/{run.run_id}"
            )
        else:
            workspace_id = ""

        warnings: list[str] = []
        if answer_source == "native_unavailable":
            warnings.append("native_unavailable_no_fallback")
        if fallback_used:
            warnings.append("native_failed_fallback_to_bm25")
        if citation_aug:
            warnings.append("citations_from_bm25_not_native")
        if (
            self._query_engine_mode
            in {
                QUERY_ENGINE_LIGHTRAG_NATIVE,
                QUERY_ENGINE_LIGHTRAG_WITH_QUALITY_EVIDENCE,
                QUERY_ENGINE_HYBRID_AB,
            }
            and self._native_query_provider is None
        ):
            warnings.append("native_provider_not_wired")

        debug["query_engine"] = self._query_engine_mode
        debug["answer_source"] = answer_source
        # ``final_answer_source`` is the post-clarification
        # canonical name; ``answer_source`` is kept as a same-value
        # alias for one release while callers migrate.
        debug["final_answer_source"] = answer_source
        debug["citation_source"] = citation_source
        debug["evidence_source"] = citation_source
        debug["bm25_used"] = bm25_used
        # Alias for callers that don't think of BM25 as "BM25" —
        # the deterministic / lexical retriever is BM25 today, so
        # the two are the same. Adding the alias makes the
        # "did we hit the deterministic retriever?" question
        # easy to answer without coupling consumers to the engine
        # name.
        debug["deterministic_retriever_used"] = bm25_used
        debug["bm25_participated_in_answer"] = bm25_participated
        debug["bm25_purpose"] = bm25_purpose
        debug["workspace_id"] = workspace_id
        debug["workspace_path"] = workspace_path
        debug["run_id"] = run.run_id
        debug["document_id"] = run.document_id
        debug["warnings"] = warnings

        # Synthesis-toggle state — captures the bug operators hit
        # where the UI showed the toggle as ON but the answer
        # panel said "LLM synthesis is off". The three fields
        # together let the FE render the right message without
        # inferring from ``llm.called``.
        #
        # ``synthesize_answer_effective`` reads as: "did the
        # operator's synthesize=True request result in an answer
        # being shown?" — TRUE when the user got an answer they
        # asked for, regardless of whether the LLM synthesizer or
        # the native engine produced it. This is the
        # operator-friendly semantics: the FE renders the right
        # message when ``effective=False`` and the reason field
        # explains why.
        synth_requested = bool(getattr(request, "synthesize", False))
        has_answer_text = bool(synthesized_answer)
        synth_effective = synth_requested and has_answer_text
        synth_disabled_reason: str | None
        if synth_effective:
            synth_disabled_reason = None
        elif not synth_requested:
            synth_disabled_reason = "user_disabled"
        elif dispatch.suppress_synthesis:
            synth_disabled_reason = "native_unavailable_no_fallback"
        elif self._synthesizer is None:
            synth_disabled_reason = "no_synthesizer_wired"
        elif llm_trace is not None and llm_trace.error == "no_evidence":
            synth_disabled_reason = "no_evidence_blocks"
        elif llm_trace is not None and llm_trace.error:
            synth_disabled_reason = llm_trace.error
        else:
            synth_disabled_reason = None
        debug["synthesize_answer_requested"] = synth_requested
        debug["synthesize_answer_effective"] = synth_effective
        debug["synthesize_answer_disabled_reason"] = synth_disabled_reason

        # Sectioned response surface. The FE renders these as
        # three distinct panels (Native Answer / Auxiliary
        # Evidence-Data Quality / LLM Synthesis) so operators can
        # tell at a glance which engine produced WHAT — the bug
        # operators flagged was that the validation tab was
        # conflating "native didn't answer" with "BM25 found
        # nothing" into a single "Final Answer failed" panel.
        native_was_attempted = bool(extras.get("native_query_enabled"))
        native_success = answer_source == "native" or bool(
            dispatch.native_answer,
        )
        native_warnings: list[str] = []
        native_failed_reason = extras.get("native_query_failed_reason")
        if native_failed_reason:
            native_warnings.append(str(native_failed_reason))
        debug["native_answer"] = {
            "engine": "lightrag_native",
            "attempted": native_was_attempted,
            "success": native_success,
            "answer_preview": extras.get("native_answer_preview"),
            "latency_ms": extras.get("native_latency_ms"),
            "warnings": native_warnings,
        }
        # ``attempted`` here means the local LLM synthesizer actually
        # ran (called=True) — distinct from ``effective`` above
        # which means an answer was rendered (possibly via native
        # override). Keeping the two distinct lets the FE
        # distinguish "the LLM synthesizer did its work" vs "the
        # user got an answer".
        llm_actually_called = bool(llm_trace and llm_trace.called)
        debug["llm_synthesis"] = {
            "requested": synth_requested,
            "attempted": llm_actually_called,
            "skipped_reason": synth_disabled_reason,
            "answer_preview": (
                synthesized_answer[:_ANSWER_PREVIEW_CAP]
                if synthesized_answer
                else None
            ),
        }

        # Auxiliary data-quality / evidence inspection section.
        # Present only when BM25 / the deterministic retriever
        # ran. Operators read this under a separate panel — the
        # data-quality view is auxiliary to the native answer
        # and never IS the answer (outside of the explicit
        # ``bm25_quality_debug`` / fallback paths).
        if bm25_used:
            debug["data_quality_evidence"] = self._build_data_quality_section(
                retrieved=retrieved,
                purpose=bm25_purpose or BM25_PURPOSE_DATA_QUALITY,
            )
        # When the deterministic retriever returned zero chunks,
        # attach a debug block that says WHY — operators can then
        # tell whether the issue is wrong workspace, missing
        # metadata, empty index, or scope filtering, without
        # having to grep server logs.
        if bm25_used and len(retrieved) == 0:
            debug["retrieval_debug"] = self._build_retrieval_debug(
                ctx=ctx,
                run=run,
                requested_top_k=requested_top_k,
                candidate_top_k_used=candidate_top_k_used,
                chunks_expected=chunks_expected,
            )

    @staticmethod
    def _build_data_quality_section(
        *,
        retrieved: list[RetrievedChunkRefDTO],
        purpose: str,
    ) -> dict[str, Any]:
        """Aggregate BM25-derived chunks into a small, FE-renderable
        data-quality report.

        The ``metadata_quality`` block reports whether every
        retrieved chunk has the four identifiers it should
        carry. A ``false`` here points at a STORAGE / REGISTRATION
        bug — BM25 only surfaces what was registered, so missing
        metadata is upstream-of-BM25 by definition.
        """
        count = len(retrieved)
        if count == 0:
            metadata_quality = {
                "run_id_present": True,
                "document_id_present": True,
                "artifact_id_present": True,
            }
        else:
            metadata_quality = {
                "run_id_present": all(bool(r.run_id) for r in retrieved),
                "document_id_present": all(
                    bool(r.document_id) for r in retrieved
                ),
                "artifact_id_present": all(
                    bool(r.artifact_id) for r in retrieved
                ),
            }
        warnings: list[str] = []
        for field_name, present in metadata_quality.items():
            if not present:
                # E.g. ``run_id_missing_on_some_chunks``. The label
                # tells the operator the issue is in the storage /
                # registration layer, not in the query path.
                key = field_name.replace("_present", "")
                warnings.append(f"{key}_missing_on_some_chunks")
        return {
            "source": "bm25",
            "purpose": purpose,
            "match_count": count,
            "metadata_quality": metadata_quality,
            "warnings": warnings,
        }

    def _build_retrieval_debug(
        self,
        *,
        ctx: ProjectContext,
        run: IngestionRun,
        requested_top_k: int | None,
        candidate_top_k_used: int | None,
        chunks_expected: bool,
    ) -> dict[str, Any]:
        """Operator-facing zero-retrieval debug block.

        When the deterministic retriever returned no chunks, the
        operator needs to answer: is the index empty, did
        run-scoping filter everything out, was the wrong workspace
        loaded, or is metadata missing? This block surfaces every
        input that goes into that diagnosis without forcing the
        operator to grep server logs.

        The SQLite FTS index path is computed from the workspace
        resolver so the operator can ``sqlite3 <path>`` it
        directly if needed.
        """
        index_path: str | None = None
        if self._workspace is not None:
            try:
                index_path = str(
                    self._workspace.area(ctx, WorkspaceArea.SEARCH)
                    / "search.sqlite"
                )
            except Exception as exc:  # noqa: BLE001 — debug only
                _log.debug(
                    "retrieval_debug: workspace.area() failed: %s", exc,
                )
                index_path = None
        warnings: list[str] = []
        if not chunks_expected:
            warnings.append(
                "engine_does_not_promise_chunks_so_empty_is_expected",
            )
        return {
            "retriever_name": "j1.bm25_fts5",
            "scope": "this_run",
            "run_id_filter": run.run_id,
            "document_id_filter": run.document_id,
            "top_k_requested": requested_top_k,
            "candidate_top_k_used": candidate_top_k_used,
            # ``candidate_count_before/after_scope_filter`` are
            # idealised distinct counts; today the FTS query
            # already runs scoped, so the two values collapse to
            # the same number — but the field shape is what the
            # operator's spec asked for. When we split the two
            # paths (separate raw vs scoped count), the field
            # names already match.
            "candidate_count_before_scope_filter": 0,
            "candidate_count_after_scope_filter": 0,
            "index_name_or_path": index_path,
            "warnings": warnings,
        }

    def _build_evidence_blocks_for_run(
        self,
        *,
        ctx: ProjectContext,
        request: ManualTestQueryRequest,
        retrieved: list[RetrievedChunkRefDTO],
        response: Any | None = None,
        active_document_id: str | None = None,
        active_run_id: str | None = None,
        diagnostics: "Any | None" = None,
    ) -> list[EvidenceBlockDTO]:
        """Materialise the clean evidence blocks the synthesizer will
 actually see. Returns `[]` when synthesis is opted out (saves
 the file IO) or when the workspace isn't wired (legacy paths).
 The same list is echoed back on the response so the FE can
 render "Evidence Sent to LLM".

 Graph-only fallback: when textual evidence is empty BUT the
 engine returned graph paths (the case operators reported as
 "Retrieval preview shows graph relationships but
 'Evidence Sent to LLM (0) / no_evidence'"), this method
 synthesises a single ``artifact_type='graph_paths'``
 evidence block rendering the paths as a bullet list so the
 synthesizer has something to ground on. Prevents the
 false-empty case where retrieval succeeded but evidence
 building dropped every source via ``_SKIP_KINDS``
 (graph_json is intentionally skipped by the textual path
 because the synthesizer is text-only — but the parsed
 paths ARE valid prose).
 """
        if not request.synthesize or self._synthesizer is None:
            return []
        if self._workspace is None:
            return []

        def _resolver(record):
            from pathlib import Path, PurePosixPath
            location = record.location
            parts = PurePosixPath(location).parts
            if len(parts) < 2:
                return Path(location)
            area_name, *rest = parts
            area = WorkspaceArea(area_name)
            return self._workspace.area(ctx, area).joinpath(*rest)  # type: ignore[union-attr]

        textual_blocks: list[EvidenceBlockDTO] = []
        if retrieved:
            # Pass the question through so build_evidence_blocks
            # runs the general-purpose reranker
            # (j1.validation.rerank) over the candidate set
            # instead of relying solely on raw retriever topK
            # order. The reranker scores each candidate on
            # source-trust / lexical-coverage / phrase / numeric
            # / structural / intent signals, then selects the
            # final evidence by greedy coverage of query
            # aspects. This is the "decouple final-evidence
            # quality from raw topK" change operators asked for
            # — increasing K helps recall, but final block
            # selection is now driven by evidence quality.
            # Build a per-call ``RerankConfig`` so the reranker
            # honours the service's ``evidence_max_blocks`` setting
            # rather than the module default. Same value also caps
            # the legacy priority-sort path via ``max_blocks``
            # below.
            from j1.validation.rerank import RerankConfig as _RerankConfig
            _config = _RerankConfig(
                evidence_max_blocks=self._validation_evidence_max_blocks,
            )
            textual_blocks = build_evidence_blocks(
                ctx=ctx,
                retrieved=retrieved,
                artifact_registry=self._artifacts,
                path_resolver=_resolver,
                query=request.question,
                rerank_config=_config,
                max_blocks=self._validation_evidence_max_blocks,
                # Phase-1 wiring: the retrieval-quality modules
                # ride on these three optional kwargs. When the
                # caller passes ``diagnostics`` + at least one
                # scope identifier, build_evidence_blocks runs
                # the scope filter + intent router + boilerplate
                # demoter + check_pack pipeline.
                active_document_id=active_document_id,
                active_run_id=active_run_id,
                diagnostics=diagnostics,
            )

        # Graph-paths fallback. Fires when:
        #   * the engine returned graph paths (e.g. graph-typed
        #     question routed to GraphQueryProvider), AND
        #   * the textual evidence path produced nothing usable —
        #     usually because every retrieved source was
        #     ``graph_json`` (which ``_SKIP_KINDS`` correctly drops
        #     from the textual prompt).
        # Without this fallback the synthesizer gets ``evidence=[]``
        # and emits ``no_evidence`` despite real graph relationships
        # being visible in the retrieval preview.
        if not textual_blocks and response is not None:
            graph_paths = getattr(response, "graph_paths", None) or []
            sources = getattr(response, "sources", None) or []
            if graph_paths:
                from j1.validation.evidence import build_graph_path_evidence
                return build_graph_path_evidence(
                    graph_paths, sources=sources,
                )

        return textual_blocks

    def _maybe_synthesize_answer(
        self,
        *,
        request: ManualTestQueryRequest,
        evidence: list[EvidenceBlockDTO],
    ) -> tuple[str | None, LLMTraceDTO]:
        """Run the LLM synthesizer when opted in AND wired.

 Three branches on the LLMTraceDTO:
   * `called=False, error=None`  — opt-out via request.synthesize=False
   * `called=False, error="no LLM client configured"` — deployment
     didn't pass `answer_synthesizer`. The FE shows an actionable
     message instead of silently dropping to retrieval-only.
   * `called=True`  — synthesis attempted; `answer` and `error`
     reflect outcome (success / no-evidence / client failure).
 """
        if not request.synthesize:
            return None, LLMTraceDTO(called=False)

        if self._synthesizer is None:
            return None, LLMTraceDTO(
                called=False,
                error="no LLM client configured",
            )

        result = self._synthesizer.synthesize(
            question=request.question,
            evidence=evidence,
        )
        return result.answer, LLMTraceDTO(
            called=True,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            error=result.error,
        )

    # ---- validation sets ----------------------------------------

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


def _build_manual_query_debug(
    *,
    retrieved: list[RetrievedChunkRefDTO],
    evidence_blocks: list[EvidenceBlockDTO],
    synthesized_answer: str | None,
    llm_trace: LLMTraceDTO | None,
    requested_top_k: int | None = None,
    candidate_top_k_used: int | None = None,
    evidence_max_blocks: int | None = None,
    scope_run_id: str | None = None,
    question: str | None = None,
) -> dict[str, Any]:
    """Build the debug-info dict surfaced on the response.

    Goal: when a tester sees ``"Not in retrieved evidence"`` they
    should be able to tell WHY at a glance — was retrieval empty?
    Were all hits filtered out by the knowledge-state gate? Did
    the synthesizer get evidence but the LLM still abstain?

    The counters answer the most common diagnostic questions:

      * ``retrieved_count``                — hits returned by the engine.
      * ``evidence_items_before_filter``   — usually = retrieved_count.
        Today these are the same; we still expose both so a future
        evidence-side filter (e.g. artifact-type policy) has an
        obvious place to land.
      * ``evidence_items_after_filter``    — what actually reached
        the synthesizer.
      * ``artifact_types_before_filter`` / ``…_after_filter`` — what
        kinds were in play. Helps spot "graph_json dominated" cases.
      * ``total_context_chars``            — sum of evidence text
        lengths sent to the LLM.
      * ``fallback_reason``                — when synthesizer
        produced no answer, the categorical reason
        (``"no_retrieval"``, ``"no_evidence"``, ``"llm_abstained"``,
        ``"llm_error"``, or ``"synthesis_disabled"``).
      * ``top_evidence_preview``           — short prefix of the
        first block's text so the FE can render an inline preview
        without expanding the full evidence panel.
    """
    types_before = sorted({
        c.artifact_kind or "" for c in retrieved if c.artifact_kind
    })
    types_after = sorted({b.artifact_type for b in evidence_blocks if b.artifact_type})
    total_chars = sum(len(b.text or "") for b in evidence_blocks)
    top_preview = ""
    if evidence_blocks:
        first_text = evidence_blocks[0].text or ""
        top_preview = first_text[:240]

    # Artifact-type policy debug: which kinds did the policy
    # deprioritize or skip? Useful when "graph_json dominated
    # retrieval but the synthesizer ignored it" — the operator can
    # see the policy at work instead of suspecting a bug.
    #
    # Two buckets:
    #   * ``deprioritized_kinds`` — present in retrieval, has a
    #     low-priority slot in ``_KIND_PRIORITY``, but didn't make it
    #     into evidence (budget exhausted, dedup, etc).
    #   * ``skipped_kinds`` — present in retrieval, but
    #     ``_SKIP_KINDS`` rejected them outright (e.g. ``graph_json``
    #     is now handled by RAGAnything.aquery, not by the local
    #     synthesizer's textual context).
    from j1.validation.evidence import (
        _DEFAULT_KIND_PRIORITY,
        _KIND_PRIORITY,
        _SKIP_KINDS,
    )
    _LOW_PRIORITY_THRESHOLD = 40  # kinds at this priority or worse
    deprioritized_kinds = sorted({
        c.artifact_kind for c in retrieved
        if c.artifact_kind
        and c.artifact_kind not in _SKIP_KINDS
        and _KIND_PRIORITY.get(c.artifact_kind, _DEFAULT_KIND_PRIORITY)
        >= _LOW_PRIORITY_THRESHOLD
        and c.artifact_kind not in types_after
    })
    skipped_kinds = sorted({
        c.artifact_kind for c in retrieved
        if c.artifact_kind and c.artifact_kind in _SKIP_KINDS
    })

    # Per-reason drop counter: maps reason → count of retrieved
    # items that didn't reach the LLM. Lets the operator see "X
    # results dropped as graph_json, Y dropped as enriched.tables"
    # instead of just a single all-or-nothing fallback_reason.
    dropped_result_reasons: dict[str, int] = {}
    for c in retrieved:
        kind = c.artifact_kind or "unknown"
        if kind in _SKIP_KINDS:
            dropped_result_reasons[f"skipped:{kind}"] = (
                dropped_result_reasons.get(f"skipped:{kind}", 0) + 1
            )
        elif kind not in types_after and kind != "":
            # In retrieval but didn't make it into evidence — most
            # likely budget-cap or dedup. Bucket as the kind so the
            # operator sees which kinds are repeatedly losing the
            # filter race.
            dropped_result_reasons[f"deprioritized_or_dedup:{kind}"] = (
                dropped_result_reasons.get(
                    f"deprioritized_or_dedup:{kind}", 0,
                ) + 1
            )

    # Modality counters — call these out explicitly so the FE can
    # render "retrieval found 7 graph results + 0 text chunks" as
    # an actionable hint when synthesis hits the graph-only path.
    graph_result_count = sum(
        1 for c in retrieved if (c.artifact_kind or "") == "graph_json"
    )
    text_chunk_result_count = sum(
        1 for c in retrieved if (c.artifact_kind or "") == "chunk"
    )
    # Did the graph-paths fallback fire? If evidence is non-empty
    # AND its only block is the synthetic ``graph_paths`` type, the
    # synthesizer was driven by the fallback path rather than by
    # textual retrieval. Useful for "why is the answer phrased
    # like graph edges?" troubleshooting.
    graph_paths_fallback_used = (
        len(evidence_blocks) >= 1
        and all(
            (b.artifact_type or "") == "graph_paths" for b in evidence_blocks
        )
    )

    fallback_reason: str | None = None
    if not synthesized_answer:
        # Categorise WHY synthesis didn't produce an answer.
        # Ordered most-specific-first. Critical: check the
        # synthesizer's reported error value BEFORE falling through
        # to the generic ``llm_error`` bucket, because the
        # synthesizer itself emits structured error codes (
        # ``"no_evidence"`` when evidence list was empty,
        # ``"llm_abstained"`` when the model emitted a canonical
        # fallback phrase). Earlier this method bucketed every
        # truthy ``llm_trace.error`` as ``llm_error`` — operators
        # then saw "llm_error" for the trivially-empty-evidence
        # case which is misleading.
        trace_error = (
            llm_trace.error if llm_trace is not None else None
        )
        if llm_trace is None or not llm_trace.called:
            fallback_reason = "synthesis_disabled"
        elif trace_error == "no_evidence":
            # Synthesizer's own no-evidence guard fired.
            # Disambiguate by upstream cause for the operator.
            if not retrieved:
                fallback_reason = "no_retrieval"
            elif skipped_kinds and not types_after:
                fallback_reason = "all_sources_skipped_no_graph_paths"
            else:
                fallback_reason = "no_evidence"
        elif trace_error == "llm_abstained":
            fallback_reason = "llm_abstained"
        elif trace_error:
            # Genuine LLM client failure (timeout, transport, 5xx)
            # — the synthesizer's try/except set this to
            # ``"<ExceptionType>: <message>"``.
            fallback_reason = "llm_error"
        elif not retrieved:
            fallback_reason = "no_retrieval"
        elif not evidence_blocks:
            if skipped_kinds and not types_after:
                fallback_reason = "all_sources_skipped_no_graph_paths"
            else:
                fallback_reason = "no_evidence"
        else:
            fallback_reason = "llm_abstained"

    return {
        "retrieved_count": len(retrieved),
        "evidence_items_before_filter": len(retrieved),
        "evidence_items_after_filter": len(evidence_blocks),
        "artifact_types_before_filter": types_before,
        "artifact_types_after_filter": types_after,
        "total_context_chars": total_chars,
        "fallback_reason": fallback_reason,
        "top_evidence_preview": top_preview,
        "deprioritized_kinds": deprioritized_kinds,
        "skipped_kinds": skipped_kinds,
        # Finer-grained debug per the operator's section-2 request.
        "graph_result_count": graph_result_count,
        "text_chunk_result_count": text_chunk_result_count,
        "graph_paths_fallback_used": graph_paths_fallback_used,
        "dropped_result_reasons": dropped_result_reasons,
        # Decoupled-top-k counters. Operators inspect these to
        # confirm whether a failure was "FTS LIMIT too small"
        # (candidate_top_k_used == requested_top_k AND
        # fts_returned_count == requested_top_k) vs a quality
        # issue downstream (large pool, narrow selection).
        # ``raw_candidate_kinds`` / ``selected_evidence_kinds``
        # are aliases of ``artifact_types_before_filter`` /
        # ``…_after_filter`` so the operator's spec-listed
        # field names also show up verbatim.
        "requested_top_k": requested_top_k,
        "candidate_top_k_used": candidate_top_k_used,
        # ``raw_candidate_count`` is the operator-spec name for
        # the count of rows returned by the engine before any
        # filtering / reranking. Same value as
        # ``fts_returned_count`` (kept for backward compat) —
        # exposing under both names so debug consumers built
        # against either contract keep working.
        "fts_returned_count": len(retrieved),
        "raw_candidate_count": len(retrieved),
        "evidence_max_blocks": evidence_max_blocks,
        "selected_evidence_count": len(evidence_blocks),
        "raw_candidate_kinds": types_before,
        "selected_evidence_kinds": types_after,
        # Top-block preview alias. Same content as
        # ``top_evidence_preview`` (kept for backward compat)
        # but exposed under the spec-listed name so debug
        # consumers can grep for either.
        "selected_evidence_preview": top_preview,
        # Query-anchor coverage: does the selected evidence text
        # contain ANY of the question's content tokens? Boolean,
        # not a score — operators use it as a "does any query
        # term land in the evidence at all" gate. Particularly
        # useful in ``rag_native_primary`` where the answer comes
        # from native but evidence comes from BM25 — if this is
        # False, the augmented citations may not actually support
        # the native answer's claims.
        "query_anchors_in_evidence": _query_anchors_in_evidence(
            evidence_blocks=evidence_blocks, question=question,
        ),
        "scope_run_id": scope_run_id,
    }


def _query_anchors_in_evidence(
    *,
    evidence_blocks: list[EvidenceBlockDTO],
    question: str | None,
) -> bool:
    """Return True iff the selected evidence contains at least one
    of the question's content tokens.

    Reuses the reranker's term extractor (``extract_query_terms``)
    so the anchor vocabulary matches what the reranker already
    scores on. ``False`` is the operator-actionable signal — it
    means BM25 selected blocks whose text doesn't surface any
    question keyword, which is a strong hint that either:

      * the FTS LIMIT was too tight and the right chunk wasn't in
        the candidate pool,
      * the reranker preferred a non-textual / off-topic chunk
        based on other signals, or
      * the question itself doesn't have indexable anchors (rare
        for natural-language queries).

    Empty inputs return ``False`` — there's no evidence to check.
    """
    if not evidence_blocks or not question:
        return False
    from j1.validation.rerank import extract_query_terms
    terms = extract_query_terms(question)
    if not terms:
        return False
    for block in evidence_blocks:
        text = (block.text or "").lower()
        if not text:
            continue
        for term in terms:
            if term in text:
                return True
    return False


def _evidence_blocks_from_chunks(
    chunks: list[_ChunkRecord],
) -> list[EvidenceBlockDTO]:
    """Project the run's chunks into evidence blocks for the
 generator's LLM call.

 We don't sample here — the generator's internal `_sample_chunks`
 is what decides which chunks are *quoted*; here we hand over the
 full set so the LLM can pick whichever chunk contains the most
 useful evidence for each generated case. The generator's
 own budget caps still bound the prompt size.

 Empty chunks (body-less, e.g. a structural index entry) are
 dropped so the LLM doesn't see "[3] " with nothing after it.
 """
    blocks: list[EvidenceBlockDTO] = []
    for chunk in chunks:
        body = (chunk.body or "").strip()
        if not body:
            continue
        # Source artifact id: prefer the parent chunk artifact's id
        # (so the LLM cites the actual artifact registered in the
        # run, not the synthetic chunk id). Falls back to the chunk
        # id when the projector didn't surface a parent — the FE
        # still gets a stable id either way.
        artifact_id = chunk.source_artifact_id or chunk.chunk_id or ""
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


def _retrieved_chunks_from_response(response: Any) -> list[RetrievedChunkRefDTO]:
    """Translate `QueryResponse.sources` into the public chunk-ref DTO.

 `score` is propagated from ``SourceReference.score`` — the
 ``KnowledgeQueryProvider``'s FTS BM25 score now flows end-to-
 end. Earlier this projection hardcoded ``score=0.0`` so the
 downstream reranker (``j1.validation.rerank``) saw every
 candidate as score-zero and could only act on lexical /
 source-trust / coverage signals. Sources without a real score
 (e.g. graph_json walked by ``GraphQueryProvider``) keep the
 0.0 default — that's the legitimate "no raw IR rank" case.

 `artifact_kind` comes from the engine source's
 `artifact_type` (the indexer's column name for it). Used by
 evidence-flag detection + the modality-aware checks.
 """
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
                score=float(getattr(source, "score", 0.0) or 0.0),
                preview=title[:_PREVIEW_MAX_CHARS],
                artifact_kind=getattr(source, "artifact_type", None),
            )
        )
    return out


def _citations_from_response(response: Any) -> list[ValidationCitationDTO]:
    """Project the engine's `SourceReference` list into the local
 validation citation DTO.

 's REST endpoint emits the same list as both
 `retrievedChunks[]` and `citations[]` because the underlying
 `HybridQueryEngine` doesn't yet distinguish "the chunks that
 matched" from "the chunks the answer cites." Splitting the two
 is a + concern (LLM-judge attribution).
 """
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


def _citation_to_dict(citation: ValidationCitationDTO) -> dict[str, Any]:
    """REST schema-friendly camelCase dict.

 The validation service produces dataclasses; the REST adapter
 converts them to the response Pydantic models. Going through a
 plain dict here keeps the REST layer the only place that needs
 to know about CamelModel.
 """
    return {
        "artifactId": citation.artifact_id,
        "artifactType": citation.artifact_type,
        "sourceDocumentId": citation.source_document_id,
        "sourceLocation": citation.source_location,
        "chunkId": citation.chunk_id,
        "runId": citation.run_id,
    }


def _has_artifact_kind(
    retrieved: list[RetrievedChunkRefDTO], kind_prefix: str,
) -> bool:
    """Return True when any retrieved item's artifact_kind starts
 with the given prefix.

 honest signal. Reads the `artifact_kind` field
 surfaced by `_retrieved_chunks_from_response`. For
 runs predating that field — `artifact_kind` arrives as None —
 the function returns False, which matches the earlier
 "we don't know" stub behaviour.
 """
    for chunk in retrieved:
        kind = chunk.artifact_kind or ""
        if kind.startswith(kind_prefix):
            return True
    return False


def _engine_response_to_raw(response: Any) -> dict[str, Any]:
    """Project the engine response into a JSON-friendly dict.

 Callers asking for `?includeRaw=true` get the full server-side
 view of the engine result for debugging — citations, related
 artifacts, graph paths, warnings, mode used. The dict is shallow
 on purpose; deep introspection of vendor objects is out of
 scope.
 """
    return {
        "answer": response.answer,
        "modeUsed": response.mode_used,
        "confidence": response.confidence,
        "reviewRequired": response.review_required,
        "warnings": list(response.warnings),
        "warningCategories": [c.value for c in response.warning_categories],
        "relatedArtifacts": list(response.related_artifacts),
        "graphPaths": [
            {
                "nodes": list(p.nodes),
                "edges": list(p.edges),
                "description": p.description,
            }
            for p in response.graph_paths
        ],
        "sources": [
            {
                "artifactId": s.artifact_id,
                "artifactType": s.artifact_type,
                "title": s.title,
                "sourceDocumentId": s.source_document_id,
                "sourceLocation": s.source_location,
                "chunkId": getattr(s, "chunk_id", None),
                "runId": getattr(s, "run_id", None),
            }
            for s in response.sources
        ],
    }


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


def _inconclusive_response(
    *,
    request_id: str,
    run_id: str,
    question: str,
    error: str,
) -> ManualTestQueryResponseDTO:
    """Build a response for the engine-failure path.

 `validation_status` is `inconclusive` (not `failed`) so the FE
 renders this as "couldn't determine" rather than "the document
 doesn't answer the question." Operators shouldn't act on a
 failed deterministic check that didn't actually run.
 """
    failure_check = ValidationCheckDTO(
        name="engine_invocation",
        severity="required",
        passed=False,
        detail=f"engine raised: {error}",
        expected="successful query",
        actual="exception",
    )
    return ManualTestQueryResponseDTO(
        request_id=request_id,
        run_id=run_id,
        question=question,
        answer="",
        mode_used="",
        retrieved_chunks=[],
        citations=[],
        checks=[failure_check],
        validation_status="inconclusive",
        evidence_flags={
            "graphUsed": False,
            "tablesUsed": False,
            "imagesUsed": False,
        },
        raw_response=None,
    )


# ---- Orchestrator → ManualTestQueryResponseDTO mapping ---------
#
# These helpers project ``SmartQueryOrchestrator`` output into the
# legacy DTO shape so the frontend keeps working unchanged. They are
# module-level (not methods) so the new path can be unit-tested
# without instantiating the 151KB service.


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
