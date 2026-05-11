"""Lightweight Content Digest builder.

Produces a compact, privacy-preserving representation of the parsed
document used by both the rule-based post-compile assessor AND the
optional LLM-assisted planner. The digest is the single boundary
between "what the parser saw" and "what an external LLM may see":
the privacy guarantees live here.

Contract: never include the full raw document. Sample text/table/
image previews under the deployment's caps. Keep page references so
the LLM can reason about location-specific decisions (e.g. "tables
on pages 5-7 are meaningful, page 12's table is decorative")."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from j1.processing.document_understanding import (
    DocumentUnderstanding,
    TitleCandidate,
)
from j1.processing.manifest import ParsedContentItem, ParsedContentManifest


__all__ = [
    "ContentDigest",
    "DigestImageSample",
    "DigestPageSummary",
    "DigestTableSample",
    "DigestTextBlock",
    "build_content_digest",
]


# Item-type labels (lower-cased) that carry useful narrative text.
_TEXT_ITEM_TYPES: frozenset[str] = frozenset({
    "text", "paragraph", "para", "narrative", "list_item",
    "list", "code", "code_block",
})

# Item-type labels that indicate headings.
_HEADING_ITEM_TYPES: frozenset[str] = frozenset({
    "heading", "h1", "h2", "h3", "h4", "h5", "h6", "title",
})

# Item-type labels for tables / images.
_TABLE_ITEM_TYPES: frozenset[str] = frozenset({
    "table", "table_html", "table_caption",
})
_IMAGE_ITEM_TYPES: frozenset[str] = frozenset({
    "image", "figure", "diagram", "chart",
})


@dataclass(frozen=True)
class DigestTextBlock:
    page: int | None
    type: str
    preview: str


@dataclass(frozen=True)
class DigestTableSample:
    page: int | None
    row_count: int | None
    column_count: int | None
    preview: str


@dataclass(frozen=True)
class DigestImageSample:
    page: int | None
    detected_type: str
    nearby_text: str
    image_id: str | None = None


@dataclass(frozen=True)
class DigestPageSummary:
    """One entry in `early_page_digest`. Keeps the per-page structural
 fingerprint the LLM uses to attribute decisions to pages."""

    page: int
    headings: tuple[str, ...]
    paragraph_previews: tuple[str, ...]
    table_hints: tuple[str, ...]
    image_hints: tuple[str, ...]


@dataclass(frozen=True)
class ContentDigest:
    """Compact projection of the manifest fed to the rule-based
 assessor and (optionally) the LLM planner.

 Caps applied:
 * `max_sample_blocks` — across `sample_text_blocks`.
 * `max_preview_chars` — per individual preview string.
 * `max_early_pages` — bound on `early_page_digest`.
 """

    summary: str
    title_candidates: tuple[TitleCandidate, ...]
    heading_outline: tuple[tuple[int, str, int | None], ...]  # (level, text, page)
    early_page_digest: tuple[DigestPageSummary, ...]
    sample_text_blocks: tuple[DigestTextBlock, ...]
    sample_tables: tuple[DigestTableSample, ...]
    sample_images: tuple[DigestImageSample, ...]
    # Caps recorded so the FE / audit log can show what was sampled
    # vs what was skipped — auditors verify the privacy boundary.
    applied_max_sample_blocks: int = 0
    applied_max_preview_chars: int = 0
    applied_max_early_pages: int = 0


def build_content_digest(
    *,
    manifest: ParsedContentManifest | None,
    understanding: DocumentUnderstanding | None,
    max_sample_blocks: int,
    max_preview_chars: int,
    max_early_pages: int,
) -> ContentDigest:
    """Build the digest from the manifest.

 The digest is deterministic — same inputs → same output. Pure
 function. No I/O.

 `understanding` is optional; when present its
 `title_candidates` are passed through verbatim so the digest
 carries the same provenance the FE Planning Report renders.
 """
    if manifest is None:
        return ContentDigest(
            summary="",
            title_candidates=(),
            heading_outline=(),
            early_page_digest=(),
            sample_text_blocks=(),
            sample_tables=(),
            sample_images=(),
            applied_max_sample_blocks=max_sample_blocks,
            applied_max_preview_chars=max_preview_chars,
            applied_max_early_pages=max_early_pages,
        )

    items = list(manifest.items)

    heading_outline = _build_heading_outline(items, max_chars=max_preview_chars)
    early_pages = _build_early_pages(
        items,
        max_early_pages=max_early_pages,
        max_preview_chars=max_preview_chars,
    )
    text_blocks = _sample_text_blocks(
        items,
        max_blocks=max_sample_blocks,
        max_chars=max_preview_chars,
    )
    tables = _sample_tables(items, max_chars=max_preview_chars)
    images = _sample_images(items, max_chars=max_preview_chars)

    summary = _summary_string(
        manifest=manifest,
        understanding=understanding,
        heading_outline=heading_outline,
        early_pages=early_pages,
    )

    title_candidates = (
        understanding.title_candidates
        if understanding is not None
        else ()
    )

    return ContentDigest(
        summary=summary,
        title_candidates=title_candidates,
        heading_outline=heading_outline,
        early_page_digest=early_pages,
        sample_text_blocks=text_blocks,
        sample_tables=tables,
        sample_images=images,
        applied_max_sample_blocks=max_sample_blocks,
        applied_max_preview_chars=max_preview_chars,
        applied_max_early_pages=max_early_pages,
    )


# ---- Internals --------------------------------------------------------


def _build_heading_outline(
    items: Iterable[ParsedContentItem], *, max_chars: int,
) -> tuple[tuple[int, str, int | None], ...]:
    """Walk the manifest items and produce a (level, text, page)
 tuple per heading. Level inference is approximate — `h1`/`h2`/
 `h3` strings carry the level explicitly; `heading`/`title`
 default to level 1."""
    out: list[tuple[int, str, int | None]] = []
    for item in items:
        if not item.type or item.type.lower() not in _HEADING_ITEM_TYPES:
            continue
        if not item.text_preview:
            continue
        level = _heading_level(item.type)
        out.append((level, _truncate(item.text_preview, max_chars), item.page_idx))
    return tuple(out)


def _heading_level(item_type: str) -> int:
    t = item_type.lower()
    if t in {"h1", "title"}:
        return 1
    if t == "h2":
        return 2
    if t == "h3":
        return 3
    if t == "h4":
        return 4
    if t == "h5":
        return 5
    if t == "h6":
        return 6
    return 1  # bare "heading"


def _build_early_pages(
    items: list[ParsedContentItem],
    *,
    max_early_pages: int,
    max_preview_chars: int,
) -> tuple[DigestPageSummary, ...]:
    """Bucket items by page; return per-page summaries up to
 `max_early_pages`. Pages without explicit indices fall under
 page=0 and are dropped — only items with a real page get
 surfaced (LLM provenance depends on it)."""
    by_page: dict[int, list[ParsedContentItem]] = {}
    for item in items:
        if item.page_idx is None:
            continue
        if item.page_idx > max_early_pages:
            continue
        by_page.setdefault(item.page_idx, []).append(item)

    out: list[DigestPageSummary] = []
    for page in sorted(by_page):
        page_items = by_page[page]
        headings = tuple(
            _truncate(it.text_preview, max_preview_chars)
            for it in page_items
            if it.type and it.type.lower() in _HEADING_ITEM_TYPES
            and it.text_preview
        )
        paragraphs = tuple(
            _truncate(it.text_preview, max_preview_chars)
            for it in page_items
            if it.type and it.type.lower() in _TEXT_ITEM_TYPES
            and it.text_preview
        )[:3]  # 3 paragraphs per page is plenty for early-page signal
        tables = tuple(
            _truncate(it.text_preview or it.caption or "", max_preview_chars)
            for it in page_items
            if it.type and it.type.lower() in _TABLE_ITEM_TYPES
            and (it.text_preview or it.caption)
        )
        images = tuple(
            _truncate(it.caption or it.text_preview or "", max_preview_chars)
            for it in page_items
            if it.type and it.type.lower() in _IMAGE_ITEM_TYPES
            and (it.caption or it.text_preview)
        )
        out.append(DigestPageSummary(
            page=page,
            headings=headings,
            paragraph_previews=paragraphs,
            table_hints=tables,
            image_hints=images,
        ))
    return tuple(out)


def _sample_text_blocks(
    items: list[ParsedContentItem],
    *,
    max_blocks: int,
    max_chars: int,
) -> tuple[DigestTextBlock, ...]:
    """Sample up to `max_blocks` text-typed items, prioritising:
 1. Page-1 paragraphs (highest topical signal).
 2. Items adjacent to headings.
 3. Otherwise, walk in document order."""
    if max_blocks <= 0:
        return ()
    candidates: list[ParsedContentItem] = []
    for item in items:
        if not item.type or item.type.lower() not in _TEXT_ITEM_TYPES:
            continue
        if not item.text_preview:
            continue
        candidates.append(item)
    if not candidates:
        return ()

    # Priority sort: page-1 first, then by page, then in original order.
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (
        pair[1].page_idx is None,
        pair[1].page_idx or 9_999,
        pair[0],
    ))

    out: list[DigestTextBlock] = []
    seen_pages: set[int] = set()
    # First pass: one block per page until cap.
    for _, item in indexed:
        if len(out) >= max_blocks:
            break
        page = item.page_idx
        if page in seen_pages:
            continue
        out.append(DigestTextBlock(
            page=page,
            type=(item.type or "paragraph").lower(),
            preview=_truncate(item.text_preview, max_chars),
        ))
        if page is not None:
            seen_pages.add(page)
    # Second pass: fill remainder with extra blocks (any page).
    for _, item in indexed:
        if len(out) >= max_blocks:
            break
        block = DigestTextBlock(
            page=item.page_idx,
            type=(item.type or "paragraph").lower(),
            preview=_truncate(item.text_preview, max_chars),
        )
        if block in out:
            continue
        out.append(block)
    return tuple(out)


def _sample_tables(
    items: list[ParsedContentItem],
    *,
    max_chars: int,
    max_samples: int = 8,
) -> tuple[DigestTableSample, ...]:
    """One sample per table item, up to `max_samples`."""
    out: list[DigestTableSample] = []
    for item in items:
        if not item.type or item.type.lower() not in _TABLE_ITEM_TYPES:
            continue
        if len(out) >= max_samples:
            break
        meta = item.metadata or {}
        out.append(DigestTableSample(
            page=item.page_idx,
            row_count=_int_or_none(meta.get("row_count")),
            column_count=_int_or_none(meta.get("column_count")),
            preview=_truncate(
                item.text_preview or item.caption or "",
                max_chars,
            ),
        ))
    return tuple(out)


def _sample_images(
    items: list[ParsedContentItem],
    *,
    max_chars: int,
    max_samples: int = 12,
) -> tuple[DigestImageSample, ...]:
    """One sample per image item, up to `max_samples`. We rely on the
 parser's per-image triage metadata (`role` / `detected_type`) to
 flag decorative content; the LLM planner can use this to
 deprioritise vision enrichment for logos/icons."""
    out: list[DigestImageSample] = []
    for item in items:
        if not item.type or item.type.lower() not in _IMAGE_ITEM_TYPES:
            continue
        if len(out) >= max_samples:
            break
        meta = item.metadata or {}
        detected = str(
            meta.get("detected_type")
            or meta.get("role")
            or "unknown"
        )
        nearby = item.caption or item.text_preview or ""
        out.append(DigestImageSample(
            page=item.page_idx,
            detected_type=detected,
            nearby_text=_truncate(nearby, max_chars),
            image_id=item.item_id or None,
        ))
    return tuple(out)


def _summary_string(
    *,
    manifest: ParsedContentManifest,
    understanding: DocumentUnderstanding | None,
    heading_outline: tuple[tuple[int, str, int | None], ...],
    early_pages: tuple[DigestPageSummary, ...],
) -> str:
    """Operator-readable one-line summary used as the `summary` field
 in the digest. No raw document content beyond what's already in
 the heading outline."""
    pieces: list[str] = []
    if understanding and understanding.detected_title:
        pieces.append(understanding.detected_title)
    elif heading_outline:
        pieces.append(heading_outline[0][1])

    stats = manifest.stats
    pieces.append(
        f"{stats.page_count or 'unknown'} pages, "
        f"{stats.text_blocks} blocks, "
        f"{stats.tables} tables, "
        f"{stats.images} images, "
        f"{stats.equations} formulas"
    )
    if understanding:
        pieces.append(f"detected as {understanding.document_type.value}")
    return " — ".join(pieces)


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
