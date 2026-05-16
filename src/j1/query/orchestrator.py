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

import os
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


# Retrieval-broadening gate.
#
# When ON, the orchestrator runs retrieval against ``original_query
# + alias-driven variants`` and deduplicates the results across
# variants. When OFF (default), the orchestrator still captures the
# provider's hints into the trace as diagnostics, but retrieval sees
# only the original query — the answer path is byte-for-byte
# identical to the pre-augmentation pipeline.
#
# The legacy variable name
# ``J1_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL`` is accepted as a
# fallback for one release so deployments mid-rollout don't break.
ENV_QUERY_EXPANSION_ENABLED = "J1_QUERY_EXPANSION_ENABLED"
_LEGACY_ENV_APPLIED_TO_RETRIEVAL = (
    "J1_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL"
)


def _read_truthy(source, name: str) -> bool | None:
    raw = source.get(name)
    if raw is None:
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def is_query_expansion_enabled(
    env: dict[str, str] | None = None,
) -> bool:
    """Read the broadening gate.

    Default ``false``. The canonical env name is
    ``J1_QUERY_EXPANSION_ENABLED``; the legacy
    ``J1_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL`` is honoured as a
    one-release fallback so a mid-rollout deployment doesn't break
    when the new name lands."""
    source = env if env is not None else os.environ
    canonical = _read_truthy(source, ENV_QUERY_EXPANSION_ENABLED)
    if canonical is not None:
        return canonical
    legacy = _read_truthy(source, _LEGACY_ENV_APPLIED_TO_RETRIEVAL)
    if legacy is not None:
        return legacy
    return False


# Cap on variant jobs we spawn per original retrieval job. Bounds
# the retrieval blowup if a domain pack ships a long alias list.
# Aligned with ``MAX_QUERY_EXPANSION_TERMS`` so the two limits move
# together when tuned.
_MAX_EXPANSION_VARIANTS_PER_JOB = 4


_QUERY_VARIANT_EXTRA_KEY = "query_variant"


def _expansions_from_memory_view(view) -> tuple[str, ...]:
    """Read pre-computed expansions off an arbitrary memory view.

    ``DocumentMemoryView`` exposes ``expansions: tuple[str, ...]``;
    other variants (``ProjectActiveMemoryView`` / ``RunMemoryView``)
    don't carry the field today. We use ``getattr`` with a default
    so a view that doesn't expose it returns ``()`` — there's no
    isinstance dance and no import cycle.

    Whitespace-only / empty strings are dropped; duplicates are
    deduplicated. The orchestrator's variant-cloning helper
    (``_build_expansion_jobs``) caps the count downstream.
    """
    if view is None:
        return ()
    raw = getattr(view, "expansions", None)
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for term in raw:
        if not isinstance(term, str):
            continue
        cleaned = term.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen[cleaned] = None
    return tuple(seen.keys())


def _build_expansion_jobs(
    original_jobs: tuple,
    *,
    variants: tuple[str, ...],
    original_query: str,
) -> tuple:
    """Build the retrieval job set the runner sees.

    Always includes ``original_jobs`` unchanged (the originals carry
    the query the planner picked — usually the user's question, but
    sometimes an anchor-stamped variant the planner built itself).
    When ``variants`` is non-empty AND the deployment opted into
    expansion, we clone each original job once per variant with the
    variant text swapped into ``RetrievalJob.query`` and a label
    stamp.

    Cloning preserves every other field (route, max_results,
    filters) so scope filtering / per-route eligibility is byte-
    for-byte identical. The variant label
    (``"<original>::variant:<text>"``) is what the trace surfaces
    in ``routes_executed`` so operators can see which job hit which
    variant.
    """
    from dataclasses import replace as _replace
    out = list(original_jobs)
    if not variants:
        return tuple(out)
    capped_variants = variants[:_MAX_EXPANSION_VARIANTS_PER_JOB]
    for job in original_jobs:
        # Only clone the job whose query equals the user's question —
        # planner-built anchor jobs already encode specific phrasing
        # the planner wants; broadening THOSE with random alias forms
        # is a noisy-evidence hazard. The planner's discretion wins.
        if job.query != original_query:
            continue
        for variant in capped_variants:
            label = (
                f"{job.label or 'primary'}::variant:{variant[:32]}"
            )
            out.append(_replace(job, query=variant, label=label))
    return tuple(out)


def _augmentation_retrieval_stats(
    records: tuple, *, original_query: str,
) -> tuple[int, int, dict[str, int]]:
    """Compute the diagnostic counts the trace surfaces under
    ``augmentation.retrieval_counts`` + ``final_evidence_distribution``.

    Returns ``(original_count, expanded_count, distribution)``.

    ``distribution`` keys mirror the spec's example shape:
    ``original_only`` / ``expanded_only`` / ``both``. Computed
    BEFORE deduplication so callers see how many raw rows each
    variant contributed; the deduplicator then collapses them. The
    distribution counts unique ``(route, artifact_id, chunk_id)``
    triples across all variants of each provenance class.
    """
    original_hits: set[tuple] = set()
    expanded_hits: set[tuple] = set()
    for rec in records:
        is_original = rec.query == original_query
        for cand in rec.candidates:
            key = (cand.route.value, cand.artifact_id, cand.chunk_id)
            (original_hits if is_original else expanded_hits).add(key)
    overlap = original_hits & expanded_hits
    return (
        sum(len(rec.candidates)
            for rec in records if rec.query == original_query),
        sum(len(rec.candidates)
            for rec in records if rec.query != original_query),
        {
            "original_only": len(original_hits - overlap),
            "expanded_only": len(expanded_hits - overlap),
            "both": len(overlap),
        },
    )


def _deduplicate_candidates(
    candidates: tuple,
) -> tuple:
    """Collapse hits that point at the same chunk across variants.

    Dedup key: ``(route, artifact_id, chunk_id)``. The kept record
    has the highest score; ``extra["query_variants"]`` is unioned
    so the trace shows every variant that contributed.

    Returns the dedup'd tuple in stable order (first-seen wins for
    tie-broken positions). Candidates without a ``chunk_id``
    deduplicate at the artifact level — RAGAnything occasionally
    returns artifact-scoped hits and we don't want them collapsed
    incorrectly with chunk-scoped ones from BM25, so the
    ``chunk_id`` field is part of the key (None / specific value
    are distinct)."""
    from dataclasses import replace as _replace
    kept: dict[tuple, object] = {}
    order: list[tuple] = []
    for cand in candidates:
        key = (cand.route.value, cand.artifact_id, cand.chunk_id)
        existing = kept.get(key)
        if existing is None:
            new_extra = dict(cand.extra or {})
            variants: list[str] = []
            existing_variants = new_extra.get(_QUERY_VARIANT_EXTRA_KEY)
            if isinstance(existing_variants, list):
                variants.extend(str(v) for v in existing_variants)
            elif isinstance(existing_variants, str):
                variants.append(existing_variants)
            new_extra[_QUERY_VARIANT_EXTRA_KEY] = (
                list(dict.fromkeys(variants))
            )
            kept[key] = _replace(cand, extra=new_extra)
            order.append(key)
            continue
        # Merge: keep the higher-scoring candidate's identity,
        # union the variant lists.
        prior_score = getattr(existing, "score", 0.0) or 0.0
        new_score = getattr(cand, "score", 0.0) or 0.0
        winner = cand if new_score > prior_score else existing
        prior_variants = (
            dict(existing.extra or {}).get(_QUERY_VARIANT_EXTRA_KEY) or []
        )
        new_variants = (
            dict(cand.extra or {}).get(_QUERY_VARIANT_EXTRA_KEY) or []
        )
        union = list(dict.fromkeys(
            list(prior_variants) + list(new_variants)
        ))
        merged_extra = dict(getattr(winner, "extra", {}) or {})
        merged_extra[_QUERY_VARIANT_EXTRA_KEY] = union
        kept[key] = _replace(winner, extra=merged_extra)
    return tuple(kept[key] for key in order)


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
    # Phase-4: optional UnifiedMemoryView the orchestrator can hand
    # to the augmentation provider so it has access to the active
    # snapshot's enrichment artifact refs. ``None`` when the caller
    # didn't pre-resolve it — the orchestrator gracefully skips
    # augmentation (everything stays "disabled" in diagnostics).
    # Typed as ``object`` to avoid an import cycle between
    # ``j1.memory`` and ``j1.query``; the augmentation provider's
    # ``hints_for`` accepts it directly.
    memory_view: object | None = None


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
        augmentation_provider: object | None = None,
        knowledge_memory_provider: object | None = None,
        knowledge_memory_evidence_resolver: object | None = None,
    ) -> None:
        self._classifier = classifier
        self._routes = route_runner
        self._builder = builder
        self._sufficiency = sufficiency
        self._synth = synthesizer
        self._binder = binder
        self._quality = quality
        # Phase-4 augmentation provider. Optional — when ``None`` the
        # orchestrator behaves identically to the pre-Phase-4 pipeline.
        # When wired, the orchestrator captures the provider's hints
        # into the QueryTrace as diagnostics; retrieval inputs are
        # NOT broadened until ``J1_QUERY_AUGMENTATION_APPLIED_TO_RETRIEVAL``
        # is flipped on (deferred work — Phase-4 ships diagnostics
        # only so the seam can be exercised without changing answer
        # behaviour).
        self._augmentation_provider = augmentation_provider
        # Phase-4 Knowledge Memory query integration (2026-05-16).
        # Opt-in via `J1_QUERY_KNOWLEDGE_MEMORY_ENABLED`. When the
        # provider is wired AND the flag is on, the orchestrator
        # consults the active snapshot's persistent
        # `knowledge_memory` artifact for query expansion +
        # derived-evidence hints. The provider is fail-safe by
        # construction — it never raises into this caller, so a
        # memory-side failure can't fail the query. See
        # `j1.memory.query_provider.KnowledgeMemoryContextProvider`
        # for the hard contract.
        self._knowledge_memory_provider = knowledge_memory_provider
        # Phase 5B (2026-05-16): optional source-ref resolver. When
        # wired AND the memory provider returns selected entries
        # with source refs, the resolver materialises those refs
        # into source-grounded `EvidenceCandidate` rows that the
        # standard evidence pipeline consumes. The resolver is
        # fail-safe — exceptions never propagate; failures surface
        # as `resolver_error:*` warnings on the diagnostic block.
        # Memory entries themselves never become evidence.
        self._knowledge_memory_evidence_resolver = (
            knowledge_memory_evidence_resolver
        )

    # ---- Construction helper ---------------------------------

    @classmethod
    def from_components(
        cls,
        *,
        routes: Mapping[RetrievalRouteKind, RetrievalRoute],
        llm: LLMCallable,
        builder_config: EvidenceBuilderConfig | None = None,
        augmentation_provider: object | None = None,
        knowledge_memory_provider: object | None = None,
        knowledge_memory_evidence_resolver: object | None = None,
    ) -> "SmartQueryOrchestrator":
        return cls(
            classifier=QueryIntentClassifier(),
            route_runner=RouteRunner(routes),
            builder=EvidencePackBuilder(config=builder_config),
            sufficiency=EvidenceSufficiencyGate(),
            synthesizer=AnswerSynthesizer(llm=llm),
            binder=CitationBinder(),
            quality=AnswerQualityGate(),
            augmentation_provider=augmentation_provider,
            knowledge_memory_provider=knowledge_memory_provider,
            knowledge_memory_evidence_resolver=(
                knowledge_memory_evidence_resolver
            ),
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

        # 1.5. Domain query augmentation.
        #
        # Two sources of expansion variants, in precedence order:
        #
        #   A. ``request.memory_view.expansions`` — pre-computed by
        #      the caller (typically the validation service, which
        #      has access to the active domain pack + query). When
        #      populated, the orchestrator uses these verbatim. This
        #      is the production path the spec describes.
        #   B. An injected ``augmentation_provider`` — derives
        #      expansions from the memory view + query on the fly.
        #      Used by tests + future external callers that want
        #      the orchestrator to own the derivation. Skipped when
        #      (A) supplied terms.
        #
        # The diagnostics stamp the same trace fields either way so
        # downstream consumers don't need to branch on the source.
        # Augmentation is advisory — a misconfigured provider / bad
        # expansion list never regresses the answer path.
        augmentation_expansions: tuple[str, ...] = ()
        applied_to_retrieval = False
        aug_source = ""
        aug_terms: tuple[str, ...] = ()
        aug_aliases: tuple[tuple[str, str], ...] = ()
        pre_computed = _expansions_from_memory_view(request.memory_view)
        if pre_computed:
            # Source A — caller-supplied expansions.
            augmentation_expansions = tuple(
                t for t in pre_computed if t != request.question
            )
            applied_to_retrieval = (
                bool(augmentation_expansions)
                and is_query_expansion_enabled()
            )
            aug_source = "memory_view"
        elif (
            self._augmentation_provider is not None
            and request.memory_view is not None
        ):
            # Source B — provider-derived expansions.
            try:
                hints = self._augmentation_provider.hints_for(
                    request.memory_view, request.question,
                )
                from j1.memory.augmentation import compute_query_expansion
                expansions = compute_query_expansion(
                    request.question, hints,
                )
                augmentation_expansions = tuple(
                    t for t in expansions if t != request.question
                )
                applied_to_retrieval = (
                    bool(augmentation_expansions)
                    and is_query_expansion_enabled()
                )
                aug_source = hints.source
                aug_terms = hints.domain_terms
                aug_aliases = hints.aliases
            except Exception:  # noqa: BLE001 — augmentation never fails the call
                augmentation_expansions = ()
                applied_to_retrieval = False
        if aug_source:
            trace = trace.with_augmentation(
                source=aug_source,
                terms=aug_terms,
                aliases=aug_aliases,
                expansions=augmentation_expansions,
                applied_to_retrieval=applied_to_retrieval,
            )
        # Enrichment-alias provenance. Surfaced verbatim from the
        # memory view the caller built — separated from
        # ``with_augmentation`` so the trace can distinguish "the
        # pack contributed" from "Domain Enrichment contributed".
        if request.memory_view is not None:
            available = getattr(
                request.memory_view, "enrichment_aliases_available", 0,
            )
            matched = getattr(
                request.memory_view, "enrichment_aliases_matched", (),
            ) or ()
            if available or matched:
                trace = trace.with_enrichment_alias_diagnostics(
                    available=available, matched=tuple(matched),
                )

        # Phase 4 (2026-05-16): persistent Knowledge Memory query
        # integration. Consults the provider (when wired) for the
        # active snapshot's `knowledge_memory` artifact. The
        # provider returns a structured `KnowledgeMemoryQueryContext`
        # — never raises. Diagnostics surface on the trace; the
        # rest of the orchestrator pipeline (retrieval / synthesis)
        # is unchanged. Base source evidence remains the canonical
        # ground.
        memory_context = None
        mem_settings = None
        if self._knowledge_memory_provider is not None:
            from j1.memory.query_settings import (
                load_knowledge_memory_query_settings,
            )
            mem_settings = load_knowledge_memory_query_settings()
            try:
                memory_context = (
                    self._knowledge_memory_provider.context_for_query(
                        ctx=request.ctx,
                        question=request.question,
                        document_id=request.document_id,
                        settings=mem_settings,
                        # Phase 5A patch (2026-05-16): pass the
                        # caller's eligibility pairs through so the
                        # provider's project-active path can filter
                        # the project-wide artifact walk to the
                        # right ``(document_id, snapshot_id)``
                        # subset. ``None`` for document-active /
                        # default-scope queries — the provider
                        # decides per-mode whether to use them.
                        eligible_snapshot_pairs=(
                            request.eligible_snapshot_pairs
                        ),
                    )
                )
            except Exception:  # noqa: BLE001 — never fail the query
                memory_context = None
        if memory_context is not None:
            trace = trace.with_knowledge_memory(
                memory_context.to_payload(),
            )

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
        # Phase 5A (2026-05-16): fold Knowledge Memory expansion
        # terms into the existing augmentation-expansion pool that
        # broadens retrieval. The merge fires only when ALL of:
        #   * retrieval expansion was enabled
        #     (``J1_QUERY_EXPANSION_ENABLED=true``)
        #   * the memory provider was consulted AND returned
        #     ``status=used``
        #   * the provider produced at least one expansion term
        # The merge is fail-safe — exceptions fall back to the
        # existing augmentation-only variant set. Memory entries
        # themselves are NEVER injected as evidence; only their
        # short-headline / alias / hint strings broaden retrieval.
        # Phase 5A: the memory merge fires whenever retrieval
        # expansion is enabled (``J1_QUERY_EXPANSION_ENABLED``) —
        # NOT only when augmentation has terms. A deployment with
        # the memory provider wired but no domain augmentation
        # provider still wants memory expansion to broaden
        # retrieval. ``applied_to_retrieval`` gates the
        # augmentation source-of-truth on the trace; the
        # broadening gate below is the more permissive
        # ``is_query_expansion_enabled()``.
        retrieval_expansion_enabled = is_query_expansion_enabled()
        retrieval_variants = (
            augmentation_expansions if applied_to_retrieval else ()
        )
        if (
            retrieval_expansion_enabled
            and memory_context is not None
            and getattr(memory_context, "status", "") == "used"
            and memory_context.expansion_terms
            and mem_settings is not None
        ):
            try:
                from j1.memory.expansion_merge import (
                    merge_memory_expansion_terms,
                )
                merge_result = merge_memory_expansion_terms(
                    augmentation_terms=retrieval_variants,
                    memory_terms=memory_context.expansion_terms,
                    max_memory_terms=mem_settings.max_expansion_terms,
                )
                retrieval_variants = merge_result.final_terms
                # Re-stamp the trace with the applied diagnostic.
                # Phase 4 already stamped the base block; we
                # replace it with the post-merge view so the FE
                # / diagnostic JSON shows the actual retrieval
                # impact.
                diagnostic = dict(memory_context.to_payload())
                diagnostic["applied_expansion_terms"] = list(
                    merge_result.applied_memory_terms,
                )
                diagnostic["expansion_terms_applied"] = (
                    merge_result.applied
                )
                diagnostic["expansion_terms_truncated"] = (
                    merge_result.truncated
                )
                trace = trace.with_knowledge_memory(diagnostic)
            except Exception:  # noqa: BLE001 — fall back to augmentation-only
                # Memory merge failure must NOT regress retrieval —
                # surface the warning on the trace and continue
                # with the augmentation-only variant set.
                diagnostic = dict(memory_context.to_payload())
                existing_warnings = list(diagnostic.get("warnings") or [])
                if (
                    "knowledge_memory_expansion_merge_failed"
                    not in existing_warnings
                ):
                    existing_warnings.append(
                        "knowledge_memory_expansion_merge_failed",
                    )
                diagnostic["warnings"] = existing_warnings
                diagnostic["expansion_terms_applied"] = False
                diagnostic["applied_expansion_terms"] = []
                diagnostic["expansion_terms_truncated"] = False
                trace = trace.with_knowledge_memory(diagnostic)
                retrieval_variants = (
                    augmentation_expansions if applied_to_retrieval else ()
                )

        # Build the job set. When expansion is OFF the job set is
        # identical to ``plan.retrieval_jobs`` — pre-expansion
        # behaviour is byte-for-byte preserved.
        jobs_to_run = _build_expansion_jobs(
            plan.retrieval_jobs,
            variants=retrieval_variants,
            original_query=request.question,
        )
        records = self._routes.run_all(jobs_to_run, route_ctx)
        trace = trace.with_routes(records)
        # Provenance + dedup. Each candidate carries its source
        # variant in ``extra["query_variant"]``; the deduper unions
        # variants across hits of the same ``(route, artifact_id,
        # chunk_id)`` triple so the trace shows which variants
        # contributed to each candidate.
        original_count, expanded_count, distribution = (
            _augmentation_retrieval_stats(
                records, original_query=request.question,
            )
        )
        deduped = _deduplicate_candidates(trace.all_candidates)
        # Replace ``all_candidates`` on the trace with the dedup'd
        # set so the evidence builder sees unique candidates. The
        # per-route records (``routes_executed``) keep the raw hits
        # for the manual-test diagnostic surface — operators can
        # still inspect which variant produced each raw row.
        trace = trace.with_deduped_candidates(deduped)
        # Retrieval-side stats are populated ONLY when expansion was
        # actually applied. Stamping them when ``applied_to_retrieval``
        # is False would falsely imply variants were dispatched —
        # operators reading the trace need an honest "expansion did
        # not run" signal (zero counts) versus "expansion ran and
        # found nothing" (zero distribution but variant route calls
        # in ``routes_executed``).
        if applied_to_retrieval:
            trace = trace.with_augmentation_retrieval_stats(
                original_count=original_count,
                expanded_count=expanded_count,
                deduplicated_total=len(deduped),
                distribution=distribution,
            )
        all_cands = trace.all_candidates

        # Phase 5B (2026-05-16): resolve memory entries' source refs
        # into source-grounded evidence candidates. Runs AFTER the
        # canonical routes + dedup so the resolver can dedupe its
        # candidates against the already-retrieved pool — a chunk
        # already produced by RAGAnything / BM25 never gets re-
        # injected by the memory side.
        #
        # Hard guardrails (mirroring Phase 4 / 5A invariants):
        #   * Resolver is opt-in via wiring. Unwired → no-op.
        #   * Memory disabled / not-used → no injection.
        #   * Failures never propagate — resolver returns an empty
        #     resolution + a warning code.
        #   * Memory entries themselves are NEVER added; only their
        #     resolvable source refs.
        if (
            self._knowledge_memory_evidence_resolver is not None
            and memory_context is not None
            and getattr(memory_context, "status", "") == "used"
            and mem_settings is not None
            and memory_context.selected_entries
            and memory_context.resolved_source_ref_count > 0
        ):
            try:
                from j1.memory.source_ref_resolver import (
                    collect_existing_keys,
                )
                existing_keys = collect_existing_keys(all_cands)
                resolution = (
                    self._knowledge_memory_evidence_resolver.resolve(
                        ctx=request.ctx,
                        selected_entries=(
                            memory_context.selected_entries
                        ),
                        settings=mem_settings,
                        eligible_snapshot_pairs=(
                            request.eligible_snapshot_pairs
                        ),
                        existing_keys=existing_keys,
                        document_id=request.document_id,
                        project_id=request.ctx.project_id,
                    )
                )
            except Exception:  # noqa: BLE001 — resolver never fails query
                resolution = None
            if resolution is not None:
                # Fold injected candidates into the canonical pool.
                # Order: canonical first (so the evidence builder's
                # priority ranking sees the normal-route candidates
                # at the front), memory-guided appended.
                if resolution.injected:
                    injected_candidates = tuple(
                        r.candidate for r in resolution.injected
                    )
                    all_cands = tuple(all_cands) + injected_candidates
                    trace = trace.with_deduped_candidates(all_cands)
                # Stamp the resolution diagnostic onto the memory
                # trace block alongside the Phase 4 / 5A fields.
                diagnostic = dict(trace.knowledge_memory or {})
                diagnostic.update(resolution.to_diagnostic())
                # Merge warnings additively — the Phase 4 / 5A
                # warnings stay; resolver warnings append.
                existing_warnings = list(diagnostic.get("warnings") or [])
                for w in resolution.warnings:
                    if w not in existing_warnings:
                        existing_warnings.append(w)
                diagnostic["warnings"] = existing_warnings
                trace = trace.with_knowledge_memory(diagnostic)
        elif (
            self._knowledge_memory_evidence_resolver is not None
            and memory_context is not None
        ):
            # Resolver wired but no injection happened (memory
            # disabled, no entries, or no source refs). Stamp the
            # zero-state diagnostic so dashboards see "resolver
            # consulted, nothing to inject" rather than absence.
            diagnostic = dict(trace.knowledge_memory or {})
            diagnostic.setdefault("resolved_source_ref_count", 0)
            diagnostic.setdefault("injected_evidence_count", 0)
            diagnostic.setdefault("deduped_evidence_count", 0)
            diagnostic.setdefault("unresolved_source_ref_count", 0)
            diagnostic.setdefault(
                "source_ref_resolution_warnings", [],
            )
            diagnostic.setdefault("evidence_injection_applied", False)
            trace = trace.with_knowledge_memory(diagnostic)

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
