"""ParsedContentManifest — normalized parser-output boundary.

Documents the contract between the compile stage and downstream
consumers (post-compile replan, quality projector, future tools that
want to inspect "what did the parser actually find?" without re-
walking the storage_dir or coupling to a specific vendor's output).

The manifest is intentionally:

  * **Stats-first**, not items-first. A full per-element list is
    expensive to persist (a 1000-page PDF can have tens of thousands
    of items) and most consumers only need aggregate counts. Items
    are an optional list, capped by the producer.
  * **Vendor-neutral**. Field names match the planner's signal
    vocabulary (`has_images`, `has_tables`, `text_extractable_ratio`)
    rather than vendor names (MinerU's `*_content_list.json` shape,
    LightRAG's storage layout). Producers translate.
  * **Forward-compatible**. New top-level keys are additive; readers
    must tolerate missing keys with explicit None semantics. The
    `parser_version` and `manifest_schema_version` fields let
    consumers detect format drift.

Persisted as `kind=ARTIFACT_KIND_PARSED_CONTENT_MANIFEST` artifacts.
The compile activity emits exactly one per document; readers locate
it by `(run_id, document_id, kind)`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Bump when the manifest's top-level shape changes incompatibly. The
# planner / projector are expected to read both old and new schemas
# until a migration window closes; producers should record the
# version they wrote.
MANIFEST_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ParsedContentItem:
    """Optional per-element entry. Producers may omit (manifest stays
    stats-only) or include a small subset (e.g. images for triage).
    Item-level data is the heaviest field; consumers must tolerate an
    empty list."""

    item_id: str
    type: str
    page_idx: int | None = None
    source_path: str | None = None
    text_preview: str | None = None
    caption: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedContentStats:
    """Aggregate counts the planner reads for replan decisions and the
    quality projector reads for the Quality tab.

    Field semantics:
      * `text_blocks` / `images` / `tables` / `equations` are direct
        counts from the parser's structured output.
      * `scanned_pages` and `decorative_images` may be None when the
        parser doesn't classify (today: MinerU doesn't differentiate).
      * `total_items` is the sum across all categories (informational;
        `len(items)` may be smaller when the producer dropped
        per-element entries).
      * `text_chars`, `text_extractable_ratio`, and quality scores
        match the same fields on `DocumentProfile` so post-compile
        replan can overlay them with `_merge_compile_signals`.
    """

    text_blocks: int = 0
    images: int = 0
    tables: int = 0
    equations: int = 0
    scanned_pages: int | None = None
    decorative_images: int | None = None
    diagrams: int | None = None
    total_items: int = 0

    page_count: int | None = None
    text_chars: int = 0
    text_extractable_ratio: float | None = None
    parse_quality_score: float | None = None
    text_sufficiency_score: float | None = None
    layout_complexity_score: float | None = None


@dataclass(frozen=True)
class ParsedContentManifest:
    """One document's parser-output manifest.

    Persisted as a JSON `ArtifactDraft` of kind
    `ARTIFACT_KIND_PARSED_CONTENT_MANIFEST`. The producer is the
    compile activity; readers are the post-compile replan call site
    and the quality / review API surfaces.
    """

    document_id: str
    document_hash: str
    parser: str
    parser_version: str | None
    parse_method: str | None
    profile: str | None
    stats: ParsedContentStats
    items: list[ParsedContentItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION

    def to_json_bytes(self) -> bytes:
        """Encode to UTF-8 JSON bytes for storage as artifact content."""
        return json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedContentManifest":
        """Reconstruct from a stored JSON document. Tolerant to
        missing keys (forward-compat): unknown top-level fields are
        silently ignored, missing nested fields fall back to defaults.
        """
        stats_raw = data.get("stats") or {}
        items_raw = data.get("items") or []
        return cls(
            document_id=str(data.get("document_id", "")),
            document_hash=str(data.get("document_hash", "")),
            parser=str(data.get("parser", "")),
            parser_version=_str_or_none(data.get("parser_version")),
            parse_method=_str_or_none(data.get("parse_method")),
            profile=_str_or_none(data.get("profile")),
            stats=ParsedContentStats(
                text_blocks=int(stats_raw.get("text_blocks", 0) or 0),
                images=int(stats_raw.get("images", 0) or 0),
                tables=int(stats_raw.get("tables", 0) or 0),
                equations=int(stats_raw.get("equations", 0) or 0),
                scanned_pages=_int_or_none(stats_raw.get("scanned_pages")),
                decorative_images=_int_or_none(
                    stats_raw.get("decorative_images"),
                ),
                diagrams=_int_or_none(stats_raw.get("diagrams")),
                total_items=int(stats_raw.get("total_items", 0) or 0),
                page_count=_int_or_none(stats_raw.get("page_count")),
                text_chars=int(stats_raw.get("text_chars", 0) or 0),
                text_extractable_ratio=_float_or_none(
                    stats_raw.get("text_extractable_ratio"),
                ),
                parse_quality_score=_float_or_none(
                    stats_raw.get("parse_quality_score"),
                ),
                text_sufficiency_score=_float_or_none(
                    stats_raw.get("text_sufficiency_score"),
                ),
                layout_complexity_score=_float_or_none(
                    stats_raw.get("layout_complexity_score"),
                ),
            ),
            items=[
                ParsedContentItem(
                    item_id=str(item.get("item_id", "")),
                    type=str(item.get("type", "")),
                    page_idx=_int_or_none(item.get("page_idx")),
                    source_path=_str_or_none(item.get("source_path")),
                    text_preview=_str_or_none(item.get("text_preview")),
                    caption=_str_or_none(item.get("caption")),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in items_raw
                if isinstance(item, dict)
            ],
            warnings=[
                str(w) for w in (data.get("warnings") or [])
                if isinstance(w, str)
            ],
            manifest_schema_version=str(
                data.get("manifest_schema_version", MANIFEST_SCHEMA_VERSION),
            ),
        )


def manifest_from_compile_stats(
    *,
    document_id: str,
    document_hash: str,
    parser: str,
    parser_version: str | None,
    parse_method: str | None,
    profile: str | None,
    compile_stats: dict[str, Any] | None,
) -> ParsedContentManifest:
    """Build a manifest from the compile activity's existing
    `content_stats` dict. Bridges the legacy stats shape (already
    consumed by the planner via `_merge_compile_signals`) into the
    canonical manifest format without changing the producer's
    intermediate representation.

    `compile_stats` is whatever the bridge's `_build_content_manifest`
    surfaced — empty dict / None when the parser produced nothing.
    """
    raw = compile_stats or {}
    images_list = raw.get("images") if isinstance(raw.get("images"), list) else []
    return ParsedContentManifest(
        document_id=document_id,
        document_hash=document_hash,
        parser=parser,
        parser_version=parser_version,
        parse_method=parse_method,
        profile=profile,
        stats=ParsedContentStats(
            text_blocks=int(raw.get("text_block_count", 0) or 0),
            images=int(raw.get("image_count", 0) or 0),
            tables=int(raw.get("table_count", 0) or 0),
            equations=int(raw.get("equation_count", 0) or 0),
            # Today's bridge doesn't differentiate scanned/decorative
            # images. Surface as None ("unknown") rather than 0
            # ("definitely zero") so consumers can tell the difference.
            scanned_pages=None,
            decorative_images=None,
            diagrams=None,
            total_items=(
                int(raw.get("text_block_count", 0) or 0)
                + int(raw.get("image_count", 0) or 0)
                + int(raw.get("table_count", 0) or 0)
                + int(raw.get("equation_count", 0) or 0)
            ),
            page_count=_int_or_none(raw.get("page_count")),
            text_chars=int(raw.get("total_text_chars", 0) or 0),
            text_extractable_ratio=_float_or_none(
                raw.get("text_extractable_ratio"),
            ),
            parse_quality_score=_float_or_none(raw.get("parse_quality_score")),
            text_sufficiency_score=_float_or_none(
                raw.get("text_sufficiency_score"),
            ),
            layout_complexity_score=_float_or_none(
                raw.get("layout_complexity_score"),
            ),
        ),
        # Items intentionally empty — image triage decisions live on
        # the IngestPlan's `vision_decisions` field; persisting them
        # again here would duplicate. A future producer that wants
        # full per-element passthrough can fill items[] explicitly.
        items=[],
        warnings=[],
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
