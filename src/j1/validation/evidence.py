"""Builds clean evidence blocks (with real chunk text) for the LLM
answer-synthesis path.

The query engine projects FTS hits into `SourceReference` records —
metadata only, no body. The Validation tab's synthesizer needs the
actual chunk text to ground its answer, so this module sits between
retrieval and synthesis: it takes the engine's metadata projection
plus the artifact registry, loads each chunk's real body, applies
artifact-kind rules + dedup + context budget, and produces a list
of `EvidenceBlockDTO` ready for the prompt builder.

Rules (kept simple on purpose — easy to tune from operator feedback):

 * `kind=chunk` artifacts: the dominant case. Load the chunk NDJSON
   via the existing `ChunkProjector` and match by `chunk_id`.
 * `kind=compiled.text`: read the file directly and use a leading
   window. Lower-priority — chunks are the granular ground truth
   and `compiled.text` is the whole document.
 * `kind=enriched.document_map`: extract the document outline
   (sections + summaries + headings) as prose. Spec section 7
   says document_map should be usable as textual evidence.
 * `kind=enriched.tables` / `enriched.visuals`: skipped. Pure
   metadata kinds with no usable text body (their `hit.preview`
   is just the artifact title).
 * `kind=graph_json` / other: appears only when textual evidence
   doesn't fill the context budget (see `_KIND_PRIORITY` below);
   the synthesizer's answer is textual, so graph blobs are
   downranked rather than skipped.

Dedup: tracks the first 200 chars of each block's text. A `compiled.text`
window whose head matches an already-emitted chunk's body is dropped
so context tokens aren't burned on duplicates.

Budget: cumulative-text-chars cap. The synthesizer enforces its own
prompt-size cap downstream, but we cut here too so the response's
`evidence_sent_to_llm[]` mirrors what the model actually saw.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from j1.retrieval.diagnostics import RetrievalDiagnostics

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactNotFoundError, ArtifactRegistry
from j1.ingestion_review.projectors.chunks import (
    ChunkProjector,
    _ChunkRecord,
)
from j1.processing.results import ARTIFACT_KIND_CHUNK
from j1.projects.context import ProjectContext
from j1.validation.dtos import EvidenceBlockDTO, RetrievedChunkRefDTO

_log = logging.getLogger("j1.validation.evidence")


# Per-block text cap. Chunks are typically ~500-1500 chars after
# normalization; this allows the full chunk to pass through unless
# it's a giant table cell. compiled.text windows get the same cap.
_PER_BLOCK_CHAR_CAP = 1500

# Cumulative budget across blocks. Tracks the synthesizer's prompt
# cap so the FE's "Evidence Sent to LLM" panel reflects what the
# model actually saw (no surprise truncation downstream).
_TOTAL_BUDGET_CHARS = 6000

# Number of leading characters used to detect duplicate blocks.
# Short enough to catch overlapping text from compiled.text + chunk
# pairs; long enough to avoid false-positives across distinct
# headings.
_DEDUP_PREFIX_LEN = 200

# Compiled-text fallback window. We don't read the entire compiled
# document — just a leading slice large enough to carry useful
# context if no chunk artifacts matched.
_COMPILED_TEXT_WINDOW_CHARS = 3000

# Artifact kinds we deliberately skip when building evidence.
#
# Pure-metadata / non-textual kinds whose body would only confuse
# the synthesizer (their `hit.preview` is just an artifact title or
# a serialized JSON blob).
#
# ``graph_json`` is in this set on purpose: the LightRAG-produced
# knowledge graph (entities + relationships + GraphML) is NOT a
# textual evidence source. RAGAnything's own ``aquery(mode=…)``
# pipeline is the supported graph-aware query path — it walks the
# graph storage internally, retrieves entity/relation context, and
# returns prose grounded in chunk text. Feeding raw graph_json JSON
# blobs to our local synthesizer instead causes the model to
# (a) waste context budget on serialized JSON and (b) confidently
# claim "the evidence contains JSON blocks but not enough context
# to identify entities" — the symptom the operator reported.
#
# Graph QA flow is:
#   * for graph-typed questions the engine routes to
#     ``RAGAnythingQueryProvider`` which calls
#     ``rag.aquery(mode="hybrid")`` — the entity/relation
#     traversal happens inside LightRAG, not here.
#   * raw graph_json artifacts remain on disk as inspection /
#     audit artifacts (loadable via the Knowledge Graph tab) but
#     never reach the answer-synthesizer prompt.
#
# ``enriched.tables`` / ``enriched.visuals`` are skipped because
# their bodies are pure metadata (cell coordinates, image
# references); the synthesizer can't use them as prose.
_SKIP_KINDS: frozenset[str] = frozenset({
    "enriched.tables",
    "enriched.visuals",
    "graph_json",
})


# Intents for which the structure-aware planner OWNS final
# evidence selection. The legacy rerank + select_by_coverage
# pre-picks a top-N for everyone else; that's the wrong layer
# for these because their answer shape is a small graph (actor→
# action, stage→stage, A→depends-on→B). The planner reads the
# document's heading hierarchy + the candidate pool's artifact-
# kind mix to plan distinct-section coverage + source grounding.
_STRUCTURED_INTENTS: frozenset[str] = frozenset({
    "responsibility_mapping",
    "dependency_mapping",
    "stage_progression",
    "deliverable_mapping",
    "issue_risk_mapping",
    "decision_trace",
    "list_extraction",
})


# Artifact-type policy (spec section 7): textual kinds win the
# evidence budget. `chunk` is the canonical ground truth (smallest
# unit, highest fidelity); `compiled.text` and
# `parsed_content_manifest` come next as document-level fallbacks.
# Domain-extracted text kinds (`enriched.requirements`, etc.) fill
# remaining space. `graph_json` and `enriched.formulas` are
# DELIBERATELY low-priority so they don't crowd out chunks even
# when retrieval scored them higher — the answer synthesizer is
# a TEXTUAL surface; relationship/equation blobs belong on
# dedicated graph/formula tabs.
#
# Lower number = filled into context budget first. Python's stable
# sort preserves engine score ordering within each priority tier,
# so within "all chunks" the highest-scoring chunk still comes
# first.
_KIND_PRIORITY: dict[str, int] = {
    # Tier 1 — textual ground truth.
    "chunk": 0,
    "compiled.text": 1,
    "parsed_content_manifest": 2,
    # Document outline / map (sections + summaries + headings).
    # Slightly lower priority than the canonical text kinds but
    # still preferred over domain-extracted enrichments because
    # an outline gives the LLM the document's structure even when
    # specific chunks didn't match.
    "enriched.document_map": 3,
    # Tier 2 — domain-extracted prose (operator-relevant
    # natural-language outputs).
    "enriched.requirements": 5,
    "enriched.risks": 5,
    "enriched.consistency_findings": 5,
    "enriched.source_map": 5,
    "enriched.confidence_assessment": 5,
    # Tier 3 — non-textual kinds that the synthesizer CAN consume
    # (preview-only) but should never let dominate. Spec rule:
    # "Do not let graph_json or formulas dominate normal retrieval
    # answers."
    #
    # NOTE: ``graph_json`` used to be deprioritised here; it is now
    # in ``_SKIP_KINDS`` entirely — graph QA goes through
    # ``RAGAnythingQueryProvider`` (which calls LightRAG's own
    # ``aquery``), not through this synthesizer.
    "enriched.formulas": 50,
}
_DEFAULT_KIND_PRIORITY = 30  # unknown kinds — middle of pack


def _kind_priority(kind: str) -> int:
    """Stable priority for the evidence iteration order. Lower =
    higher priority (filled into context budget first)."""
    return _KIND_PRIORITY.get(kind, _DEFAULT_KIND_PRIORITY)

# Compiled-text kinds we fall back to when no chunk body is
# available. Listed explicitly so we don't accidentally try to
# read arbitrary binary artifacts (e.g. parsed_pdf).
_COMPILED_TEXT_KINDS: frozenset[str] = frozenset({
    "compiled.text",
})


class PathResolver(Protocol):
    """Resolves an artifact record's on-disk path. Same shape the
 chunk projector + ingestion-review service already use; the
 validation service builds the closure once and passes it here."""

    def __call__(self, record: ArtifactRecord): ...


def build_evidence_blocks(
    *,
    ctx: ProjectContext,
    retrieved: list[RetrievedChunkRefDTO],
    artifact_registry: ArtifactRegistry,
    path_resolver: PathResolver,
    max_blocks: int = 5,
    total_budget_chars: int = _TOTAL_BUDGET_CHARS,
    query: str | None = None,
    rerank_config: "RerankConfig | None" = None,
    # Phase-1 retrieval-quality wiring. All four are OPTIONAL —
    # behavior is unchanged when omitted (existing tests + callers
    # that don't opt in see the legacy flow byte-for-byte).
    #
    # ``active_document_id`` / ``active_run_id``: when supplied,
    # ``enforce_active_scope`` drops any candidate whose source
    # doc/run doesn't match BEFORE rerank — the contamination
    # guard. Cross-document search is the both-None case.
    #
    # ``diagnostics``: per-query collector. When supplied, scope
    # filter + intent router + boilerplate demoter + plan_evidence
    # + check_pack all activate and emit the 9 stable
    # ``j1.retrieval.*`` audit events.
    active_document_id: str | None = None,
    active_run_id: str | None = None,
    diagnostics: "RetrievalDiagnostics | None" = None,
) -> list[EvidenceBlockDTO]:
    """Materialise clean evidence blocks from the engine's retrieval
 projection.

 When ``query`` is supplied AND ``rerank_config.enabled`` is True
 (default), runs the general-purpose reranker
 (``j1.validation.rerank``) on the loaded candidate set: scores
 each candidate by source-trust / lexical-coverage / phrase /
 numeric / structural / intent-compat / interpretive-penalty,
 then selects the final ``max_blocks`` greedily by query-aspect
 coverage. This decouples final evidence quality from raw topK
 ordering — increasing the retriever's K helps recall, but the
 actual blocks that reach the LLM are chosen by evidence-quality
 signals downstream.

 Without ``query`` (legacy callers / batch validation paths that
 want deterministic, single-signal ranking), falls back to the
 historical priority-by-kind sort. The fallback preserves
 backwards-compatibility and the existing test surface.

 In both modes: kind filtering (``_SKIP_KINDS``), per-block
 character cap, dedup by leading prefix, and cumulative
 character budget all apply unchanged. Run/project isolation
 checks are unaffected — this module only orders what the
 caller has already supplied.

 Returns an empty list when retrieval was empty — the synthesizer
 short-circuits to "no_evidence" downstream.
 """
    blocks: list[EvidenceBlockDTO] = []
    used_chars = 0
    seen_prefixes: set[str] = set()
    # Cache projected chunks per chunk-artifact so we don't reread
    # the same NDJSON file when multiple hits point at the same
    # parent artifact.
    chunk_cache: dict[str, dict[str, _ChunkRecord]] = {}
    projector = ChunkProjector(path_resolver=path_resolver)

    # ---- Phase-1 retrieval-quality gate -----------------------------
    # ``retrieval_active`` is True when the caller wired the
    # diagnostics + at least one scope identifier. When True we run
    # the new pipeline: enforce_active_scope → detect_intent →
    # boilerplate demotion → rerank → plan_evidence → check_pack.
    # When False, legacy behavior runs unchanged.
    retrieval_active = (
        diagnostics is not None and (
            active_document_id is not None
            or active_run_id is not None
        )
    )
    detected_intent = None
    if diagnostics is not None:
        # Always emit ``query.received`` + ``intent.selected`` so
        # the audit log records EVERY query, even when the new
        # gate isn't enabled (e.g. cross-document search).
        diagnostics.record_query_received(
            max_results=max_blocks,
            scope_kind=(
                "active" if retrieval_active else "cross_document"
            ),
        )
        if query:
            from j1.retrieval.intent_router import detect_intent
            det = detect_intent(query)
            detected_intent = det.intent
            diagnostics.record_intent_selected(
                det.intent.value, signals=det.signals_payload(),
            )

    if retrieval_active:
        from j1.retrieval.scope import enforce_active_scope
        admitted, _scopes = enforce_active_scope(
            retrieved,
            active_document_id=active_document_id,
            active_run_id=active_run_id,
            diagnostics=diagnostics,
        )
        # IMPORTANT: rebind ``retrieved`` so all downstream code in
        # this function works with the scope-filtered list. Drops
        # were emitted on ``diagnostics`` inside enforce_active_scope.
        rejected_count = len(retrieved) - len(admitted)
        retrieved = list(admitted)
        diagnostics.record_scope_applied(
            active_run_id=active_run_id,
            active_document_id=active_document_id,
            admitted=len(admitted),
            rejected=rejected_count,
            scope_kind="active",
        )

    # If a query is provided AND reranking is on, load every
    # eligible candidate's body up-front, score it, and select
    # by coverage. Otherwise fall through to the legacy
    # priority-sort + first-fit-budget flow.
    from j1.validation.rerank import (
        RerankConfig as _RerankConfig,
        rerank_and_select,
    )
    effective_config = rerank_config or _RerankConfig()
    use_rerank = bool(query) and effective_config.enabled

    if use_rerank:
        # Stage 1: load body text for every retrieved candidate
        # eligible by kind. Skip-kinds drop early so the reranker
        # doesn't waste cycles scoring them.
        prepared: list[tuple[RetrievedChunkRefDTO, str]] = []
        kinds: list[str] = []
        raws: list[float] = []
        sections: list[str | None] = []
        for hit in retrieved:
            kind = (hit.artifact_kind or "").strip()
            if kind in _SKIP_KINDS:
                continue
            text = _resolve_text_for_hit(
                ctx=ctx, hit=hit, kind=kind,
                registry=artifact_registry, path_resolver=path_resolver,
                projector=projector, chunk_cache=chunk_cache,
            )
            if text is None:
                continue
            text = text.strip()
            if not text:
                continue
            if len(text) > _PER_BLOCK_CHAR_CAP:
                text = text[:_PER_BLOCK_CHAR_CAP].rstrip() + "…"
            # Pre-fetch section so the reranker can score
            # section-match without re-walking the chunk cache.
            page_start, page_end, section = _page_info(
                ctx=ctx, hit=hit, kind=kind,
                registry=artifact_registry, projector=projector,
                chunk_cache=chunk_cache, path_resolver=path_resolver,
            )
            prepared.append((hit, text))
            kinds.append(kind)
            raws.append(float(hit.score or 0.0))
            sections.append(section)

        # Phase-1: emit the retrieved-candidates event AFTER scope
        # filter + kind skip + body load. This is the candidate
        # pool the reranker actually sees.
        if diagnostics is not None:
            from j1.retrieval.diagnostics import CandidateDiagnostic
            retrieved_diags = [
                CandidateDiagnostic.from_search_hit(p[0])
                for p in prepared
            ]
            diagnostics.record_candidates_retrieved(
                retrieved_diags, source="bm25+rerank",
            )

        # Stage 2: scoring + selection.
        #
        # Two paths, dispatched on the detected intent:
        #
        #   * STRUCTURED intents (responsibility / dependency /
        #     stage_progression / deliverable / issue_risk /
        #     decision_trace / list_extraction) — the planner
        #     OWNS final selection. We bypass rerank's
        #     ``select_by_coverage`` by setting
        #     ``enable_coverage_selection=False`` so we get the
        #     full scored set back, apply boilerplate demotion to
        #     EVERY candidate (not just the rerank's pre-picked
        #     top-N), then call ``plan_evidence`` to drive the
        #     final pack with section diversity + source
        #     grounding + boilerplate-as-last-resort logic.
        #
        #   * Non-structured intents — the existing
        #     ``rerank_and_select`` (with coverage selection)
        #     keeps its previous behaviour. Backward-compatible
        #     for ``exact_fact_lookup`` / ``generic_lookup`` /
        #     ``summary_lookup``.
        # ``planner_reason_by_payload`` / ``planner_score_by_payload``
        # are populated by the structured-intent dispatch. Used at
        # projection time to (a) emit the correct ``reason_selected``
        # on the selected event and (b) carry the planner's
        # rerank_score / final_score onto the audit payload (else the
        # ``from_search_hit`` projection produces all-None scores).
        planner_reason_by_payload: dict[int, str] = {}
        planner_score_by_payload: dict[int, tuple[float, float]] = {}
        structured = (
            diagnostics is not None
            and detected_intent is not None
            and detected_intent.value in _STRUCTURED_INTENTS
        )
        if structured:
            # Score-only mode: ``enable_coverage_selection=False``
            # gives us all candidates back, ranked by total score
            # but NOT pruned to a budget. We pass len(prepared)
            # as the cap so nothing is dropped at this layer.
            from dataclasses import replace as _replace
            score_only_config = _replace(
                effective_config,
                enable_coverage_selection=False,
                evidence_max_blocks=len(prepared) or 1,
            )
            scored_payloads, scored_scores, intents, query_terms = (
                rerank_and_select(
                    bodies=prepared,
                    raw_scores=raws,
                    sections=sections,
                    artifact_kinds=kinds,
                    query=query,
                    config=score_only_config,
                )
            )
            (
                selected_payloads, selected_scores,
                planner_reason_by_payload, planner_score_by_payload,
            ) = _structured_intent_select(
                ctx=ctx,
                prepared=prepared,
                scored_payloads=scored_payloads,
                scored_scores=scored_scores,
                detected_intent=detected_intent,
                max_blocks=max_blocks,
                diagnostics=diagnostics,
                artifact_registry=artifact_registry,
            )
        else:
            # Legacy path: rerank + coverage selection picks for us.
            selected_payloads, selected_scores, intents, query_terms = (
                rerank_and_select(
                    bodies=prepared,
                    raw_scores=raws,
                    sections=sections,
                    artifact_kinds=kinds,
                    query=query,
                    config=effective_config,
                )
            )
            # Still emit the reranked event + apply boilerplate
            # demotion via a re-sort so non-structured intents
            # benefit from boilerplate demotion too. This is the
            # previous behaviour, preserved.
            if diagnostics is not None:
                _emit_legacy_demotion_diagnostics(
                    selected_payloads=selected_payloads,
                    selected_scores=selected_scores,
                    detected_intent=detected_intent,
                    diagnostics=diagnostics,
                )
                # Re-sort selected_payloads by post-demotion score.
                from j1.retrieval.boilerplate import (
                    boilerplate_demotion, is_boilerplate_chunk,
                )
                paired = []
                for p, s in zip(selected_payloads, selected_scores):
                    base = float(getattr(s, "total", s))
                    section = getattr(p, "source_location", None)
                    kind = (getattr(p, "artifact_kind", "") or "").strip()
                    bp = is_boilerplate_chunk(section_path=section)
                    if bp is not None:
                        base *= boilerplate_demotion(
                            bp.category, detected_intent,
                        )
                    paired.append((p, base))
                paired.sort(key=lambda pr: pr[1], reverse=True)
                selected_payloads = [p for p, _ in paired]

        # Stage 3: project the winners into EvidenceBlockDTOs,
        # applying the per-call total-budget cap (the rerank
        # module's budget cap is per-selection; this is the
        # real prompt-budget enforcement).
        prepared_by_payload = {id(p[0]): p[1] for p in prepared}
        for hit in selected_payloads:
            if len(blocks) >= max_blocks:
                break
            if used_chars >= total_budget_chars:
                break
            text = prepared_by_payload.get(id(hit), "")
            if not text:
                continue
            prefix_key = text[:_DEDUP_PREFIX_LEN]
            if prefix_key in seen_prefixes:
                continue
            seen_prefixes.add(prefix_key)
            remaining = total_budget_chars - used_chars
            if len(text) > remaining:
                text = text[:remaining].rstrip() + "…"
            used_chars += len(text)
            kind = (hit.artifact_kind or "").strip()
            page_start, page_end, section = _page_info(
                ctx=ctx, hit=hit, kind=kind,
                registry=artifact_registry, projector=projector,
                chunk_cache=chunk_cache, path_resolver=path_resolver,
            )
            blocks.append(EvidenceBlockDTO(
                artifact_id=hit.artifact_id,
                artifact_type=kind or hit.artifact_kind or "",
                text=text,
                chunk_id=hit.chunk_id,
                score=hit.score,
                page_start=page_start,
                page_end=page_end,
                section=section,
                source_location=hit.source_location,
            ))
            # Phase-1: record one ``evidence_pack.selected`` event
            # per emitted block so the FE / audit can render the
            # final pack composition. Reason comes from the
            # structured-intent dispatch when it ran; falls back
            # to the legacy ``rerank_top`` for non-structured paths.
            if diagnostics is not None:
                from j1.retrieval.diagnostics import CandidateDiagnostic
                d = CandidateDiagnostic.from_search_hit(hit)
                d.section_path = section or d.section_path
                reason = planner_reason_by_payload.get(
                    id(hit), "rerank_top",
                )
                scores = planner_score_by_payload.get(id(hit))
                if scores is not None:
                    d.rerank_score, d.final_score = scores
                diagnostics.record_selected(d, reason=reason)
        _log.debug(
            "rerank selected %d/%d blocks "
            "(query_terms=%d intents=%s)",
            len(blocks), len(prepared), len(query_terms),
            sorted(i.value for i in intents),
        )
        # Phase-1: pre-LLM quality check + optional one-pass
        # fallback. The fallback runs at most ONCE — never a
        # loop. Recoverable failures (boilerplate / diversity /
        # source-grounding) trigger a re-plan with stricter
        # rules; unrecoverable failures (cross-document leak,
        # empty pack with no candidates) skip the fallback.
        if diagnostics is not None:
            from j1.retrieval.quality_checks import check_pack
            result = check_pack(
                blocks,
                intent=detected_intent,
                active_document_id=active_document_id,
                active_run_id=active_run_id,
            )
            fallback_triggered = False
            fallback_succeeded: bool | None = None
            failures_before_fallback: list[str] = []
            if (
                structured
                and not result.ok
                and _has_recoverable_failure(result.failures)
                and prepared
            ):
                fallback_triggered = True
                failures_before_fallback = list(result.failures)
                # Re-run the structured-intent selector with
                # stricter rules. Same scope, same intent.
                fb_blocks = _run_structured_fallback(
                    ctx=ctx,
                    prepared=prepared,
                    scored_payloads=scored_payloads,
                    scored_scores=scored_scores,
                    detected_intent=detected_intent,
                    max_blocks=max(max_blocks + 2, max_blocks),
                    total_budget_chars=total_budget_chars,
                    diagnostics=diagnostics,
                    artifact_registry=artifact_registry,
                    projector=projector,
                    chunk_cache=chunk_cache,
                    path_resolver=path_resolver,
                )
                if fb_blocks:
                    fb_result = check_pack(
                        fb_blocks,
                        intent=detected_intent,
                        active_document_id=active_document_id,
                        active_run_id=active_run_id,
                    )
                    fallback_succeeded = fb_result.ok
                    # Swap in the fallback pack regardless of
                    # whether it fully passed — even a partial
                    # improvement is better than the original
                    # failing pack.
                    blocks = fb_blocks[:max_blocks]
                    result = fb_result
                else:
                    fallback_succeeded = False
            diagnostics.record_evidence_pack_finalized(
                pack_size=len(blocks),
                fallback_triggered=fallback_triggered,
                fallback_succeeded=fallback_succeeded,
                checks_passed=result.ok,
                check_failures=result.failures,
                check_failures_before_fallback=failures_before_fallback,
            )
        return blocks

    # ---- Legacy priority-sort path (no query supplied) -------------
    # Artifact-type policy: re-order hits so textual kinds fill the
    # evidence budget first. Python's `sorted` is stable, so within
    # each priority tier the engine's score order is preserved —
    # e.g. within "all chunks" the highest-scoring chunk still
    # comes first.
    ordered = sorted(
        retrieved,
        key=lambda r: _kind_priority((r.artifact_kind or "").strip()),
    )

    for hit in ordered:
        if len(blocks) >= max_blocks:
            break
        if used_chars >= total_budget_chars:
            break

        kind = (hit.artifact_kind or "").strip()
        if kind in _SKIP_KINDS:
            continue

        text = _resolve_text_for_hit(
            ctx=ctx,
            hit=hit,
            kind=kind,
            registry=artifact_registry,
            path_resolver=path_resolver,
            projector=projector,
            chunk_cache=chunk_cache,
        )
        if text is None:
            continue

        text = text.strip()
        if not text:
            continue
        if len(text) > _PER_BLOCK_CHAR_CAP:
            text = text[:_PER_BLOCK_CHAR_CAP].rstrip() + "…"

        prefix_key = text[:_DEDUP_PREFIX_LEN]
        if prefix_key in seen_prefixes:
            continue
        seen_prefixes.add(prefix_key)

        remaining = total_budget_chars - used_chars
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "…"
        used_chars += len(text)

        page_start, page_end, section = _page_info(
            ctx=ctx,
            hit=hit,
            kind=kind,
            registry=artifact_registry,
            projector=projector,
            chunk_cache=chunk_cache,
            path_resolver=path_resolver,
        )

        blocks.append(EvidenceBlockDTO(
            artifact_id=hit.artifact_id,
            artifact_type=kind or hit.artifact_kind or "",
            text=text,
            chunk_id=hit.chunk_id,
            score=hit.score,
            page_start=page_start,
            page_end=page_end,
            section=section,
            source_location=hit.source_location,
        ))
    return blocks


# ---- helpers -------------------------------------------------------


# ---- Recoverable check_pack failures ------------------------------
#
# Set of ``check_pack`` failure codes that the one-pass fallback
# can plausibly fix by re-planning with stricter rules. Empty-pack
# / cross-document failures are NOT here — fallback can't conjure
# candidates that aren't in the prepared set, and a cross-doc leak
# is a scope-filter bug, not a planner one.
_RECOVERABLE_CHECK_FAILURES: frozenset[str] = frozenset({
    "no_boilerplate_unless_intent_allows",
    "section_diversity_for_structured_intents",
    "source_grounding_for_enriched_anchored_packs",
})


def _has_recoverable_failure(failures: list[str]) -> bool:
    """True iff ANY of the check failures are in the recoverable
    set. We could intersect more strictly (require all failures to
    be recoverable), but it's better to attempt fallback even when
    a mix of recoverable + unrecoverable failures fire — the
    fallback may at least improve the recoverable ones."""
    return any(f in _RECOVERABLE_CHECK_FAILURES for f in failures)


def _run_structured_fallback(
    *,
    ctx,
    prepared,
    scored_payloads,
    scored_scores,
    detected_intent,
    max_blocks: int,
    total_budget_chars: int,
    diagnostics,
    artifact_registry,
    projector,
    chunk_cache,
    path_resolver,
):
    """One-pass fallback: re-run structured-intent selection with
    stricter rules + slightly larger budget. Returns the new
    ``list[EvidenceBlockDTO]`` or an empty list when no improvement
    was possible.

    Stricter rules (vs. the first pass):

      * ``strict_boilerplate=True`` — never select a boilerplate
        candidate even as last-resort. If the corpus contains
        only boilerplate the fallback returns an empty pack and
        the caller surfaces that as ``fallback_succeeded=False``.
      * ``max_blocks + 2`` — moderate budget bump so the planner
        can pick more distinct sections without re-retrieving.

    Pure replanning over the already-prepared candidate set —
    no new retrieval, no LLM call. Cheap and bounded."""
    from j1.retrieval.diagnostics import CandidateDiagnostic

    if diagnostics is None:
        return []

    (
        fb_selected, fb_scores,
        fb_reasons, fb_score_map,
    ) = _structured_intent_select(
        ctx=ctx,
        prepared=prepared,
        scored_payloads=scored_payloads,
        scored_scores=scored_scores,
        detected_intent=detected_intent,
        max_blocks=max_blocks,
        diagnostics=diagnostics,
        artifact_registry=artifact_registry,
        strict_boilerplate=True,
    )
    if not fb_selected:
        return []

    # Project the fallback selection into EvidenceBlockDTO using the
    # same body cache + budgeting as the first pass. We can't reuse
    # the legacy projection loop directly without copy-pasting — so
    # rebuild the minimal projection here. Identical block shape so
    # downstream consumers don't see any difference.
    prepared_by_payload = {id(p[0]): p[1] for p in prepared}
    blocks: list[EvidenceBlockDTO] = []
    used_chars = 0
    seen_prefixes: set[str] = set()
    for hit in fb_selected:
        if len(blocks) >= max_blocks:
            break
        if used_chars >= total_budget_chars:
            break
        text = prepared_by_payload.get(id(hit), "")
        if not text:
            continue
        prefix_key = text[:_DEDUP_PREFIX_LEN]
        if prefix_key in seen_prefixes:
            continue
        seen_prefixes.add(prefix_key)
        remaining = total_budget_chars - used_chars
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "…"
        used_chars += len(text)
        kind = (hit.artifact_kind or "").strip()
        page_start, page_end, section = _page_info(
            ctx=ctx, hit=hit, kind=kind,
            registry=artifact_registry, projector=projector,
            chunk_cache=chunk_cache, path_resolver=path_resolver,
        )
        blocks.append(EvidenceBlockDTO(
            artifact_id=hit.artifact_id,
            artifact_type=kind or hit.artifact_kind or "",
            text=text,
            chunk_id=hit.chunk_id,
            score=hit.score,
            page_start=page_start,
            page_end=page_end,
            section=section,
            source_location=hit.source_location,
        ))
        d = CandidateDiagnostic.from_search_hit(hit)
        d.section_path = section or d.section_path
        reason = fb_reasons.get(id(hit), "fallback_top")
        # Annotate with ``fallback:`` prefix so the audit reader
        # distinguishes first-pass selects from fallback-pass selects.
        diagnostics.record_selected(d, reason=f"fallback:{reason}")
    return blocks


def _structured_intent_select(
    *,
    ctx,
    prepared,
    scored_payloads,
    scored_scores,
    detected_intent,
    max_blocks: int,
    diagnostics,
    artifact_registry,
    strict_boilerplate: bool = False,
):
    """Planner-driven selection for structured intents.

    Flow:
      1. Score every prepared candidate (caller already did this
         via ``rerank_and_select(enable_coverage_selection=False)``).
      2. Apply boilerplate demotion to each score using the
         active intent.
      3. Split into non-boilerplate + boilerplate pools.
      4. Build proxy candidate objects (with section_path /
         artifact_type / chunk_id / metadata / rerank_score)
         that ``plan_evidence`` can read.
      5. Call ``plan_evidence`` on the non-boilerplate pool
         first. If the planner returns an empty pack, fall back
         to the full pool and tag the chosen boilerplate
         candidates with the documented last-resort reason.
      6. Surface ``grounding_method`` (from the source-grounding
         swap in the planner) on the selected events.

    Returns ``(selected_payloads_in_order, scores_aligned)``.
    The caller projects those into ``EvidenceBlockDTO`` using
    the existing prepared-body cache.
    """
    from j1.retrieval.boilerplate import (
        boilerplate_demotion, is_boilerplate_chunk,
    )
    from j1.retrieval.diagnostics import (
        CandidateDiagnostic, DropReason,
    )
    from j1.retrieval.evidence_planner import plan_evidence

    # Build per-payload section/kind quick-lookups from prepared.
    section_by_payload: dict[int, str | None] = {}
    kind_by_payload: dict[int, str] = {}
    for hit, _body in prepared:
        section_by_payload[id(hit)] = (
            getattr(hit, "source_location", None)
        )
        kind_by_payload[id(hit)] = (
            getattr(hit, "artifact_kind", "") or ""
        ).strip()

    # Step 2-4: build candidate proxies that carry the post-
    # demotion score the planner sorts on. Same shape the
    # synthetic tests use so plan_evidence can read it without a
    # special path.
    @dataclass
    class _Proxy:
        artifact_id: str
        artifact_type: str
        rerank_score: float
        score: float
        section_path: str | None
        title: str | None
        chunk_id: str | None
        metadata: dict
        payload: object
        is_boilerplate: bool
        boilerplate_category: str | None

    proxies: list[_Proxy] = []
    reranked_diags: list[CandidateDiagnostic] = []
    for payload, score in zip(scored_payloads, scored_scores):
        section = section_by_payload.get(id(payload))
        kind = kind_by_payload.get(id(payload), "")
        bp = is_boilerplate_chunk(section_path=section)
        base = float(getattr(score, "total", score))
        if bp is not None:
            mult = boilerplate_demotion(bp.category, detected_intent)
            demoted = base * mult
        else:
            demoted = base
        proxy_meta = {
            "section_path": section,
            "chunk_id": getattr(payload, "chunk_id", None),
        }
        # The retrieved-hit DTO doesn't carry artifact metadata.
        # For enriched / compiled candidates the source-grounding
        # picker NEEDS those metadata keys (``source_chunk_ids``,
        # ``source_artifact_id``, ``page_range``, ``section_path``)
        # — look them up from the registry on demand.
        artifact_id = getattr(payload, "artifact_id", None)
        if artifact_id and artifact_registry is not None:
            try:
                record = artifact_registry.get(ctx, artifact_id)
                rec_meta = getattr(record, "metadata", None) or {}
                if isinstance(rec_meta, dict):
                    for k in (
                        "source_chunk_ids", "source_artifact_id",
                        "page_range", "section_path", "chunk_id",
                    ):
                        if k in rec_meta and k not in proxy_meta:
                            proxy_meta[k] = rec_meta[k]
            except Exception:  # noqa: BLE001 — diagnostics must not break
                pass
        proxy = _Proxy(
            artifact_id=str(getattr(payload, "artifact_id", "")),
            artifact_type=kind,
            rerank_score=demoted,
            score=demoted,
            section_path=section,
            title=getattr(payload, "title", None),
            chunk_id=getattr(payload, "chunk_id", None),
            metadata=proxy_meta,
            payload=payload,
            is_boilerplate=bp is not None,
            boilerplate_category=bp.category.value if bp else None,
        )
        proxies.append(proxy)

        # Audit: emit one diagnostic per scored candidate so the
        # reranked event captures the full pool (not just rerank's
        # pre-selection).
        d = CandidateDiagnostic.from_search_hit(payload)
        d.rerank_score = base
        d.final_score = demoted
        d.section_path = section or d.section_path
        reranked_diags.append(d)

    diagnostics.record_candidates_reranked(reranked_diags)

    # Step 5: planner runs on non-boilerplate pool first.
    non_bp = [p for p in proxies if not p.is_boilerplate]
    bp_pool = [p for p in proxies if p.is_boilerplate]

    plan = plan_evidence(
        non_bp,
        intent=detected_intent,
        max_blocks=max_blocks,
    )
    selected_proxies = list(plan.selected)
    last_resort_used = False
    if not selected_proxies and not strict_boilerplate:
        # No non-boilerplate alternative existed. The user spec
        # allows boilerplate as a documented last resort — but
        # only on the FIRST pass. The fallback path (strict
        # mode) refuses this exit so a check_pack failure can
        # actually be improved.
        last_resort_plan = plan_evidence(
            bp_pool,
            intent=detected_intent,
            max_blocks=max_blocks,
        )
        selected_proxies = list(last_resort_plan.selected)
        plan = last_resort_plan
        last_resort_used = True

    # Step 6: emit drop events for the planner's swaps with
    # grounding_method preserved.
    for dropped_obj, reason in plan.dropped:
        d = CandidateDiagnostic.from_search_hit(dropped_obj)
        diagnostics.record_dropped(d, reason=str(reason))

    # Build the (payload, reason) map the projection loop uses
    # to emit ``record_selected`` exactly once per block with the
    # correct planner reason. Reason variants:
    #   * ``planner_top_for_intent``                         — normal pick
    #   * ``planner_top_for_intent:<grounding_method>``      — when source
    #         grounding swapped this chunk in for an enriched anchor
    #   * ``selected_as_last_resort_no_non_boilerplate_candidate``
    #         — corpus has only boilerplate candidates in scope
    # Pre-compute pack composition for grounding_status tagging.
    # When the selection naturally contains BOTH an enriched
    # anchor and a source chunk, the source-grounding requirement
    # was satisfied WITHOUT a swap. Tag the chunk-side select event
    # with the documented status so an audit reader doesn't have
    # to infer absence-of-swap from missing drop events.
    pack_has_enriched = any(
        p.artifact_type.startswith("enriched.") for p in selected_proxies
    )
    pack_has_source = any(
        p.artifact_type in ("chunk", "compiled.text")
        for p in selected_proxies
    )
    natural_grounding = pack_has_enriched and pack_has_source

    reason_by_payload: dict[int, str] = {}
    score_by_payload: dict[int, tuple[float, float]] = {}
    selected_payloads_out: list = []
    selected_scores_out: list = []
    for proxy in selected_proxies:
        # Stash the rerank + post-demotion scores so the
        # projection loop can decorate the selected event with
        # the planner's final numbers (otherwise the legacy
        # ``CandidateDiagnostic.from_search_hit`` projection
        # produces all-None scores).
        score_by_payload[id(proxy.payload)] = (
            proxy.rerank_score, proxy.score,
        )
        if last_resort_used and proxy.is_boilerplate:
            reason = (
                "selected_as_last_resort_no_non_boilerplate_candidate"
            )
        else:
            reason = "planner_top_for_intent"
        method = _grounding_method_for(proxy, plan.dropped)
        if method is not None:
            reason = f"{reason}:{method}"
        # Generic grounding status: tag chunk-kind selects when
        # the pack also has an enriched anchor + no swap drop
        # accounted for the grounding. Lower-priority fix from
        # the spec — strictly additive.
        if (
            natural_grounding
            and proxy.artifact_type in ("chunk", "compiled.text")
            and method is None
        ):
            reason = f"{reason}:source_already_selected"
        reason_by_payload[id(proxy.payload)] = reason
        selected_payloads_out.append(proxy.payload)
        selected_scores_out.append(proxy.score)
    return (
        selected_payloads_out, selected_scores_out,
        reason_by_payload, score_by_payload,
    )


def _grounding_method_for(proxy, dropped_pairs) -> str | None:
    """When ``plan_evidence`` swapped an enriched anchor for this
    proxy via the source-grounding helper, the swap's drop event
    carries ``swapped_for_source_grounding:<method>``. Extract the
    method so the SELECTED event can mirror it."""
    if proxy.artifact_type not in ("chunk", "compiled.text"):
        return None
    for _dropped_obj, reason in dropped_pairs:
        if isinstance(reason, str) and reason.startswith(
            "swapped_for_source_grounding:",
        ):
            return reason.split(":", 1)[1]
    return None


def _emit_legacy_demotion_diagnostics(
    *,
    selected_payloads,
    selected_scores,
    detected_intent,
    diagnostics,
) -> None:
    """For non-structured intents we keep the existing rerank
    pre-pick + a post-hoc reranked event with boilerplate-
    demoted final_score. The legacy paths still see the same
    selected_payloads order, only post-demotion if a non-bp
    candidate ranked higher post-demotion."""
    from j1.retrieval.boilerplate import (
        boilerplate_demotion, is_boilerplate_chunk,
    )
    from j1.retrieval.diagnostics import CandidateDiagnostic

    reranked_diags: list[CandidateDiagnostic] = []
    for payload, score in zip(selected_payloads, selected_scores):
        d = CandidateDiagnostic.from_search_hit(payload)
        base = float(getattr(score, "total", score))
        d.rerank_score = base
        bp = is_boilerplate_chunk(section_path=d.section_path)
        if bp is not None:
            d.final_score = base * boilerplate_demotion(
                bp.category, detected_intent,
            )
        else:
            d.final_score = base
        reranked_diags.append(d)
    diagnostics.record_candidates_reranked(reranked_diags)


from dataclasses import dataclass  # noqa: E402 — used by _Proxy above


def _resolve_text_for_hit(
    *,
    ctx: ProjectContext,
    hit: RetrievedChunkRefDTO,
    kind: str,
    registry: ArtifactRegistry,
    path_resolver: PathResolver,
    projector: ChunkProjector,
    chunk_cache: dict[str, dict[str, _ChunkRecord]],
) -> str | None:
    """Dispatch on artifact kind to fetch the body text. Returns None
 when no usable text is available (artifact missing, kind skipped,
 chunk not found in projection)."""
    if kind == ARTIFACT_KIND_CHUNK:
        chunk = _load_chunk_record(
            ctx=ctx,
            artifact_id=hit.artifact_id,
            chunk_id=hit.chunk_id,
            registry=registry,
            projector=projector,
            cache=chunk_cache,
        )
        return chunk.body if chunk else None
    if kind in _COMPILED_TEXT_KINDS:
        return _load_compiled_text_window(
            ctx=ctx,
            artifact_id=hit.artifact_id,
            registry=registry,
            path_resolver=path_resolver,
        )
    if kind == "enriched.document_map":
        return _load_document_map_text(
            ctx=ctx,
            artifact_id=hit.artifact_id,
            registry=registry,
            path_resolver=path_resolver,
        )
    # Unknown kinds: surface the hit's preview (typically the
    # artifact title). Better than nothing but a clear signal in the
    # UI that the body wasn't loadable.
    if hit.preview:
        return hit.preview
    return None


def _load_chunk_record(
    *,
    ctx: ProjectContext,
    artifact_id: str,
    chunk_id: str | None,
    registry: ArtifactRegistry,
    projector: ChunkProjector,
    cache: dict[str, dict[str, _ChunkRecord]],
) -> _ChunkRecord | None:
    """Load (and cache) the chunk record matching `(artifact_id, chunk_id)`.

 Cache key is `artifact_id` because one chunk artifact typically
 contains many chunks. When `chunk_id` is None we return the
 first chunk in the artifact — better than nothing, and matches
 the engine's behaviour when it returns an artifact-level hit
 without chunk granularity."""
    if artifact_id not in cache:
        try:
            artifact = registry.get(ctx, artifact_id)
        except ArtifactNotFoundError:
            cache[artifact_id] = {}
            return None
        try:
            records = projector.project_records([artifact])
        except Exception as exc:  # noqa: BLE001 — projector must not 500
            _log.warning(
                "chunk projection failed for artifact %s: %s",
                artifact_id, exc,
            )
            cache[artifact_id] = {}
            return None
        cache[artifact_id] = {r.chunk_id: r for r in records}

    by_id = cache[artifact_id]
    if not by_id:
        return None
    if chunk_id and chunk_id in by_id:
        return by_id[chunk_id]
    # Fallback: first chunk in the artifact.
    return next(iter(by_id.values()))


def _load_compiled_text_window(
    *,
    ctx: ProjectContext,
    artifact_id: str,
    registry: ArtifactRegistry,
    path_resolver: PathResolver,
) -> str | None:
    """Read a leading window of a `compiled.text` artifact's content.

 We deliberately don't load the full document — even short PDFs
 produce multi-page compiled text that would blow the LLM context.
 The leading slice is usually enough for header/overview lookups;
 chunk artifacts cover the deeper content."""
    try:
        artifact = registry.get(ctx, artifact_id)
    except ArtifactNotFoundError:
        return None
    try:
        path = path_resolver(artifact)
    except Exception as exc:  # noqa: BLE001 — resolver may raise on bad locations
        _log.warning(
            "compiled.text path resolution failed for %s: %s",
            artifact_id, exc,
        )
        return None
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read(_COMPILED_TEXT_WINDOW_CHARS)
    except Exception as exc:  # noqa: BLE001 — IO may fail on slow disks
        _log.warning(
            "compiled.text read failed for %s: %s",
            artifact_id, exc,
        )
        return None
    # PDF compilers produce text with collapsed/duplicated whitespace
    # and inserted soft-line-breaks (e.g. "Section\n  3.1  Scope" or
    # "due\n  20  May  2026"). Without normalisation, the LLM sees
    # the broken whitespace + sometimes misses obvious matches —
    # one of the failure modes the latest validation report flagged
    # ("expected_chunk_in_topk passes but synthesis still abstains").
    # Collapse newlines + runs of spaces into single spaces. The
    # token content is preserved.
    return _normalise_pdf_whitespace(raw)


# Max characters lifted out of one document_map artifact. Caps the
# evidence-builder contribution so a document with hundreds of
# sections can't flood the prompt budget.
_DOCUMENT_MAP_TEXT_CAP = 1500


def _load_document_map_text(
    *,
    ctx: ProjectContext,
    artifact_id: str,
    registry: ArtifactRegistry,
    path_resolver: PathResolver,
) -> str | None:
    """Read an ``enriched.document_map`` JSON artifact and project
    it into prose suitable for the synthesizer's context.

    The on-disk schema isn't pinned (the enricher is allowed to
    emit any shape the domain pack wants), so the extractor is
    permissive: it looks for the common textual keys and skips
    anything it can't reason about.

    Recognised keys, ordered by usefulness for QA grounding:

      * ``summary`` (top-level)       — one-paragraph document
        summary, the highest-value text in the map.
      * ``outline``                   — same idea, alternative name.
      * ``sections[].title``          — section headings; gives
        the LLM the document's structure.
      * ``sections[].summary``        — per-section prose.
      * ``headings[]``                — list of strings, used when
        a flat outline is all that's available.
      * ``chapters[]`` / ``toc[]``    — same-shape aliases the
        enricher may use.

    Returns ``None`` when the file is missing/unreadable/invalid
    JSON, OR when none of the recognised keys yielded text — the
    caller then falls back to the generic preview path.
    """
    try:
        artifact = registry.get(ctx, artifact_id)
    except ArtifactNotFoundError:
        return None
    try:
        path = path_resolver(artifact)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "document_map path resolution failed for %s: %s",
            artifact_id, exc,
        )
        return None
    if not path.is_file():
        return None
    try:
        import json as _json
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = _json.load(fh)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "document_map JSON parse failed for %s: %s",
            artifact_id, exc,
        )
        return None
    return _document_map_to_prose(data)


def _document_map_to_prose(data: object) -> str | None:
    """Pure-function extractor. Walks a parsed document_map dict
    and concatenates the recognised textual fields into a single
    operator-readable string. Truncates to
    ``_DOCUMENT_MAP_TEXT_CAP`` chars."""
    if not isinstance(data, dict):
        return None
    parts: list[str] = []

    # Top-level summary / outline — highest value.
    for key in ("summary", "outline", "description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
            break  # one is enough; aliases of the same concept

    # Flat headings list — useful when there's no per-section prose.
    # Skip non-string entries (a defensive enricher might emit a
    # mixed list); we only want clean strings in the prose render.
    for key in ("headings", "section_titles"):
        value = data.get(key)
        if isinstance(value, list):
            headings = [
                h.strip() for h in value
                if isinstance(h, str) and h.strip()
            ]
            if headings:
                parts.append("Headings: " + " · ".join(headings))
                break

    # Per-section data. Accepts a few shape aliases the enricher
    # could choose (`sections` / `chapters` / `toc`); first non-
    # empty wins.
    for key in ("sections", "chapters", "toc"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        section_lines: list[str] = []
        for section in value:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "").strip()
            summary = str(
                section.get("summary") or section.get("description") or "",
            ).strip()
            if title and summary:
                section_lines.append(f"- {title}: {summary}")
            elif title:
                section_lines.append(f"- {title}")
        if section_lines:
            parts.append("Sections:\n" + "\n".join(section_lines))
            break

    if not parts:
        return None
    text = "\n\n".join(parts)
    if len(text) > _DOCUMENT_MAP_TEXT_CAP:
        text = text[:_DOCUMENT_MAP_TEXT_CAP].rstrip() + "…"
    return text


def _page_info(
    *,
    ctx: ProjectContext,
    hit: RetrievedChunkRefDTO,
    kind: str,
    registry: ArtifactRegistry,
    projector: ChunkProjector,
    chunk_cache: dict[str, dict[str, _ChunkRecord]],
    path_resolver: PathResolver,
) -> tuple[int | None, int | None, str | None]:
    """Best-effort page/section enrichment. Only chunk artifacts
 carry page info today; other kinds return None for all three."""
    if kind != ARTIFACT_KIND_CHUNK:
        return (None, None, None)
    record = _load_chunk_record(
        ctx=ctx,
        artifact_id=hit.artifact_id,
        chunk_id=hit.chunk_id,
        registry=registry,
        projector=projector,
        cache=chunk_cache,
    )
    if record is None:
        return (None, None, None)
    return (record.page_start, record.page_end, record.section)


# Whitespace runs (including newlines) → single space. Same shape as
# ``judge._normalise_text`` and the generator's anti-hallucination
# normaliser — keep them aligned so the LLM sees the same shape of
# text the grounding judge will diff against.
_WS_RE = re.compile(r"\s+")


def _normalise_pdf_whitespace(text: str) -> str:
    """Collapse PDF-style whitespace artifacts. Preserves token
    content; only flattens runs of whitespace into single spaces and
    trims edges."""
    return _WS_RE.sub(" ", text or "").strip()


# Cap on the synthesised graph-paths evidence block. Keeps the
# fallback evidence bounded so a giant graph doesn't blow the
# prompt budget all by itself. ~600 chars is comfortably under the
# 1500-char per-block cap with room for header text.
_GRAPH_PATHS_TEXT_CAP = 600


def build_graph_path_evidence(
    graph_paths,
    *,
    sources=None,
    max_lines: int = 8,
) -> list[EvidenceBlockDTO]:
    """Render ``QueryResponse.graph_paths`` as one synthetic evidence
    block the synthesizer can ground on.

    Background: graph-only retrieval (where the engine returns a
    list of entity→relation paths and no textual chunks) used to
    produce ``error="no_evidence"`` at the synthesizer. The path
    sources are all ``kind="graph_json"`` and ``_SKIP_KINDS``
    rightly excludes them from the textual evidence prompt — but
    the engine ALSO surfaces the parsed paths via
    ``response.graph_paths``, which IS usable as prose. This
    function bridges the gap: it converts each ``GraphPath`` into
    a one-line bullet ("J1 Platform → MinerU (related_to)") and
    bundles them into a single ``EvidenceBlockDTO`` of
    ``artifact_type='graph_paths'``.

    Returns ``[]`` when ``graph_paths`` is empty — the caller still
    falls through to "no_evidence" when there is genuinely
    nothing retrieved, which is the correct contract.

    ``sources`` (optional) — when provided, the first graph_json
    source's ``artifact_id`` is used to anchor the synthetic
    block's citation lineage so the grounding judge can match the
    evidence back to a real artifact. Falls back to a synthetic id
    when no graph source is present.

    Args:
      graph_paths: ``Sequence[GraphPath]`` from
        ``QueryResponse.graph_paths``. Each path's ``nodes``,
        ``edges`` and (optional) ``description`` are rendered.
      sources: ``Sequence[SourceReference]``; the first
        graph_json among them anchors the synthetic block's
        ``artifact_id``.
      max_lines: bound on rendered bullets; the rest are dropped
        rather than overflow the text cap.
    """
    if not graph_paths:
        return []
    bullets: list[str] = []
    for path in graph_paths[:max_lines]:
        nodes = list(getattr(path, "nodes", []) or [])
        edges = list(getattr(path, "edges", []) or [])
        if len(nodes) < 2:
            continue
        # Two-node path: "A → B (edge)". Multi-hop: "A → B → C
        # (edge1, edge2)". Descriptions, when present, override
        # the default rendering.
        if getattr(path, "description", None):
            bullets.append(f"- {path.description}")
            continue
        arrow_chain = " → ".join(nodes)
        edge_suffix = f" ({', '.join(edges)})" if edges else ""
        bullets.append(f"- {arrow_chain}{edge_suffix}")
    if not bullets:
        return []
    text = "Graph relationships in this document:\n" + "\n".join(bullets)
    if len(text) > _GRAPH_PATHS_TEXT_CAP:
        text = text[:_GRAPH_PATHS_TEXT_CAP].rstrip() + "…"

    # Anchor to the first graph_json source so the grounding judge
    # can see lineage back to a real artifact in the registry.
    anchor_artifact_id = ""
    if sources:
        for src in sources:
            if (getattr(src, "artifact_type", "") or "") == "graph_json":
                anchor_artifact_id = getattr(src, "artifact_id", "") or ""
                if anchor_artifact_id:
                    break
    if not anchor_artifact_id:
        anchor_artifact_id = "graph_paths:synthetic"

    return [EvidenceBlockDTO(
        artifact_id=anchor_artifact_id,
        artifact_type="graph_paths",
        text=text,
        chunk_id=None,
        score=0.0,
    )]


__all__ = [
    "PathResolver",
    "build_evidence_blocks",
    "build_graph_path_evidence",
]
