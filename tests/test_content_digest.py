"""Lightweight Content Digest tests.

Mirrors the spec's "Testing requirements" §2: caps respected, page
references preserved, no full document content."""

from __future__ import annotations

from j1.processing.content_digest import build_content_digest
from j1.processing.document_understanding import (
    DocumentMetadata,
    assess_document_understanding,
)
from j1.processing.manifest import (
    ParsedContentItem,
    ParsedContentManifest,
    ParsedContentStats,
)


def _manifest_with_n_blocks(n: int, *, max_pages: int = 5) -> ParsedContentManifest:
    items = []
    for i in range(n):
        page = (i % max_pages) + 1
        items.append(ParsedContentItem(
            item_id=f"b{i}", type="paragraph", page_idx=page,
            text_preview=f"Content of block {i}: " + ("x" * 1500),
        ))
    return ParsedContentManifest(
        document_id="doc-1",
        document_hash="h",
        parser="raganything",
        parser_version="1",
        parse_method="auto",
        profile=None,
        stats=ParsedContentStats(
            text_blocks=n, total_items=n, page_count=max_pages,
        ),
        items=items,
    )


def test_max_sample_blocks_respected():
    manifest = _manifest_with_n_blocks(50)
    digest = build_content_digest(
        manifest=manifest, understanding=None,
        max_sample_blocks=10, max_preview_chars=300, max_early_pages=3,
    )
    assert len(digest.sample_text_blocks) == 10
    assert digest.applied_max_sample_blocks == 10


def test_max_preview_chars_respected():
    """Each preview must be capped to `max_preview_chars`."""
    manifest = _manifest_with_n_blocks(20)
    digest = build_content_digest(
        manifest=manifest, understanding=None,
        max_sample_blocks=20, max_preview_chars=120, max_early_pages=3,
    )
    for block in digest.sample_text_blocks:
        assert len(block.preview) <= 121  # 120 + ellipsis


def test_max_early_pages_respected():
    """`early_page_digest` only contains pages ≤ max_early_pages."""
    manifest = _manifest_with_n_blocks(50, max_pages=8)
    digest = build_content_digest(
        manifest=manifest, understanding=None,
        max_sample_blocks=20, max_preview_chars=300, max_early_pages=3,
    )
    pages = {p.page for p in digest.early_page_digest}
    assert all(p <= 3 for p in pages)


def test_page_references_preserved():
    manifest = _manifest_with_n_blocks(10)
    digest = build_content_digest(
        manifest=manifest, understanding=None,
        max_sample_blocks=10, max_preview_chars=300, max_early_pages=3,
    )
    # Each sampled block keeps its page_idx.
    for block in digest.sample_text_blocks:
        assert block.page is not None
        assert isinstance(block.page, int)


def test_no_full_raw_document_content():
    """Privacy invariant: no preview is allowed to be larger than
 the configured cap. The fixture creates 1500-char items but the
 digest must truncate."""
    manifest = _manifest_with_n_blocks(20)
    digest = build_content_digest(
        manifest=manifest, understanding=None,
        max_sample_blocks=20, max_preview_chars=300, max_early_pages=3,
    )
    for block in digest.sample_text_blocks:
        assert len(block.preview) <= 301


def test_empty_manifest_returns_empty_digest_with_caps_recorded():
    """Caller-supplied caps are echoed even when the manifest is
 empty — the audit trail needs to know what would have been
 enforced."""
    digest = build_content_digest(
        manifest=None, understanding=None,
        max_sample_blocks=20, max_preview_chars=300, max_early_pages=3,
    )
    assert digest.sample_text_blocks == ()
    assert digest.applied_max_sample_blocks == 20
    assert digest.applied_max_preview_chars == 300


def test_summary_includes_understanding_title_when_available():
    items = [ParsedContentItem(
        item_id="t1", type="heading", page_idx=1,
        text_preview="System Requirement Specification for J1",
    )]
    manifest = ParsedContentManifest(
        document_id="doc-1", document_hash="h", parser="raganything",
        parser_version=None, parse_method="auto", profile=None,
        stats=ParsedContentStats(text_blocks=10, total_items=10, page_count=5),
        items=items,
    )
    understanding = assess_document_understanding(
        metadata=DocumentMetadata(document_id="doc-1", filename="srs.pdf"),
        manifest=manifest,
    )
    digest = build_content_digest(
        manifest=manifest, understanding=understanding,
        max_sample_blocks=5, max_preview_chars=200, max_early_pages=3,
    )
    assert "System Requirement Specification" in digest.summary
    assert "system_requirement_specification" in digest.summary
