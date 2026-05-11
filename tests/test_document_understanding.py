"""Document Understanding regression tests.

Each test pins one (title × type × evidence) combination so the
heuristics stay auditable without reading the implementation.
The cases mirror the spec's "Testing requirements" §1."""

from __future__ import annotations

import pytest

from j1.processing.document_understanding import (
    TITLE_QUALITY_AMBIGUOUS,
    TITLE_QUALITY_CLEAR,
    TITLE_QUALITY_GENERIC,
    TITLE_QUALITY_MISSING,
    DocumentMetadata,
    DocumentType,
    assess_document_understanding,
)
from j1.processing.manifest import (
    ParsedContentItem,
    ParsedContentManifest,
    ParsedContentStats,
)


def _manifest(
    *,
    items=None,
    text_blocks: int = 100,
    tables: int = 0,
    images: int = 0,
    parse_quality: float | None = 0.85,
    page_count: int | None = 10,
) -> ParsedContentManifest:
    return ParsedContentManifest(
        document_id="doc-1",
        document_hash="h",
        parser="raganything",
        parser_version="1",
        parse_method="auto",
        profile=None,
        stats=ParsedContentStats(
            text_blocks=text_blocks, tables=tables, images=images,
            equations=0, total_items=text_blocks + tables + images,
            page_count=page_count, parse_quality_score=parse_quality,
        ),
        items=list(items or []),
    )


def _meta(filename: str | None = None, **kw) -> DocumentMetadata:
    return DocumentMetadata(
        document_id="doc-1",
        filename=filename,
        **kw,
    )


def test_clear_architecture_title_picks_software_architecture():
    """A clear architecture title plus a matching first heading
 classifies as software_architecture."""
    items = [ParsedContentItem(
        item_id="t1", type="heading", page_idx=1,
        text_preview="Software Architecture Design for J1 Ingestion Pipeline",
    )]
    u = assess_document_understanding(
        metadata=_meta("arch_design.pdf"),
        manifest=_manifest(items=items),
    )
    assert u.document_type == DocumentType.SOFTWARE_ARCHITECTURE
    assert u.title_quality == TITLE_QUALITY_CLEAR
    assert u.recommended_analysis_bias.prefer_graph_extraction is True


def test_clear_srs_title_picks_system_requirement_specification():
    items = [ParsedContentItem(
        item_id="t1", type="heading", page_idx=1,
        text_preview="System Requirement Specification for QLCV",
    )]
    u = assess_document_understanding(
        metadata=_meta("SRS_QLCV.pdf"),
        manifest=_manifest(items=items),
    )
    assert u.document_type == DocumentType.SYSTEM_REQUIREMENT_SPECIFICATION
    assert u.recommended_analysis_bias.prefer_requirement_extraction is True


def test_generic_title_falls_back_to_first_page_digest():
    """Title 'Final Report' is generic; we should drop to first-page
 inspection. With status-report content on page 1, expect REPORT."""
    items = [
        ParsedContentItem(
            item_id="t1", type="heading", page_idx=1,
            text_preview="Final Report",
        ),
        ParsedContentItem(
            item_id="t2", type="paragraph", page_idx=1,
            text_preview=(
                "This monthly report summarises the team's progress for "
                "the period and is intended for executive review."
            ),
        ),
    ]
    u = assess_document_understanding(
        metadata=_meta("final_report.pdf"),
        manifest=_manifest(items=items),
    )
    # Title quality is generic → fallback signals decide. The
    # first-page paragraph contains "monthly report" → REPORT.
    assert u.title_quality == TITLE_QUALITY_GENERIC
    assert u.document_type == DocumentType.REPORT


def test_filename_only_scan_marks_title_unclear():
    """A filename-only `scan_2026_05_01.pdf` produces an unclear
 title and warnings about the fallback path."""
    u = assess_document_understanding(
        metadata=_meta("scan_2026_05_01.pdf"),
        manifest=_manifest(items=[]),
    )
    assert u.title_quality in {TITLE_QUALITY_GENERIC, TITLE_QUALITY_AMBIGUOUS}
    assert any("title" in w.lower() for w in u.warnings)


def test_invoice_first_page_picks_invoice():
    items = [ParsedContentItem(
        item_id="t1", type="paragraph", page_idx=1,
        text_preview="Invoice Number: INV-12345 — Total Due $1,200",
    )]
    u = assess_document_understanding(
        metadata=_meta("inv-12345.pdf"),
        manifest=_manifest(items=items, tables=2),
    )
    assert u.document_type == DocumentType.INVOICE
    assert u.recommended_analysis_bias.prefer_table_enrichment is True


def test_meeting_minutes_title_picks_meeting_minutes():
    items = [ParsedContentItem(
        item_id="t1", type="heading", page_idx=1,
        text_preview="Meeting Minutes — Sprint Review 2026-05-01",
    )]
    u = assess_document_understanding(
        metadata=_meta("sprint_review_minutes.pdf"),
        manifest=_manifest(items=items),
    )
    assert u.document_type == DocumentType.MEETING_MINUTES


def test_unknown_document_keeps_low_confidence_and_conservative_bias():
    """A document without title or recognisable keywords stays
 UNKNOWN with low confidence. The bias must not enable expensive
 extractions for unknown docs."""
    u = assess_document_understanding(
        metadata=_meta(None),
        manifest=_manifest(items=[]),
    )
    assert u.document_type == DocumentType.UNKNOWN
    assert u.document_type_confidence < 0.5
    bias = u.recommended_analysis_bias
    assert bias.prefer_requirement_extraction is False
    assert bias.prefer_graph_extraction is False
    assert bias.prefer_visual_enrichment is False


def test_warnings_when_parse_quality_low():
    """A low parse-quality score emits a manual-review warning so
 the rule-based assessor can see it."""
    u = assess_document_understanding(
        metadata=_meta("doc.pdf", metadata_title="Quarterly Report 2026"),
        manifest=_manifest(parse_quality=0.4),
    )
    assert any("parse" in w.lower() for w in u.warnings)


def test_metadata_title_wins_over_filename_when_clear():
    """Explicit metadata title is the highest-quality source."""
    u = assess_document_understanding(
        metadata=_meta(
            "scan_2026_01.pdf",
            metadata_title="Project Charter for J1 Ingestion Programme",
        ),
        manifest=_manifest(items=[]),
    )
    assert u.title_source == "metadata"
    assert u.title_quality == TITLE_QUALITY_CLEAR


@pytest.mark.parametrize("title,expected_quality", [
    ("", TITLE_QUALITY_MISSING),
    ("Final", TITLE_QUALITY_GENERIC),
    ("Document v3", TITLE_QUALITY_GENERIC),
    ("scan_2025_05_01", TITLE_QUALITY_GENERIC),
    ("ABC", TITLE_QUALITY_AMBIGUOUS),
    ("System Architecture for J1 Ingestion", TITLE_QUALITY_CLEAR),
])
def test_title_quality_grading(title, expected_quality):
    """Coarse title-quality table. Pinning the cases keeps the
 heuristic auditable."""
    u = assess_document_understanding(
        metadata=_meta("file.pdf", metadata_title=title or None),
        manifest=_manifest(items=[]),
    )
    if title == "":
        # Filename fallback kicks in; assert the source isn't `metadata`.
        assert u.title_source != "metadata"
        return
    assert u.title_quality == expected_quality
