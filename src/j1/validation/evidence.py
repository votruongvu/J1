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
 * `kind=enriched.document_map` / `enriched.tables` / `enriched.visuals`:
   skipped. JSON enrichment metadata confuses small local LLMs and
   isn't useful for "why/how" question answering. The validation
   surface can still surface them as evidence flags.
 * `kind=graph_json` / other: skipped for now (Cypher-style edges
   are not text the LLM can reason about as prose).

Dedup: tracks the first 200 chars of each block's text. A `compiled.text`
window whose head matches an already-emitted chunk's body is dropped
so context tokens aren't burned on duplicates.

Budget: cumulative-text-chars cap. The synthesizer enforces its own
prompt-size cap downstream, but we cut here too so the response's
`evidence_sent_to_llm[]` mirrors what the model actually saw.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

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
# Pure-metadata kinds with no usable text body (their `hit.preview`
# is just the artifact title). Note: `graph_json` deliberately
# stayed OUT of this set — the artifact-type policy below
# deprioritises it instead of skipping it entirely, so it can
# still appear in the evidence context when chunk/compiled.text
# don't fill the budget.
_SKIP_KINDS: frozenset[str] = frozenset({
    "enriched.document_map",
    "enriched.tables",
    "enriched.visuals",
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
    "graph_json": 50,
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
) -> list[EvidenceBlockDTO]:
    """Materialise clean evidence blocks from the engine's retrieval
 projection.

 Walks `retrieved` in given order (engine emits them score-
 sorted), pulls each hit's real text, applies kind rules,
 dedups, and caps cumulative characters at `total_budget_chars`.

 `max_blocks` is an additional safety bound so a flood of small
 chunks can't crowd out one well-placed long chunk. Defaults
 land us at ~3-5 blocks for typical local-LLM contexts.

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
            return fh.read(_COMPILED_TEXT_WINDOW_CHARS)
    except Exception as exc:  # noqa: BLE001 — IO may fail on slow disks
        _log.warning(
            "compiled.text read failed for %s: %s",
            artifact_id, exc,
        )
        return None


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


__all__ = ["PathResolver", "build_evidence_blocks"]
