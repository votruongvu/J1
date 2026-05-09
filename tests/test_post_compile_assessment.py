"""Rule-based Post-Compile Assessment tests.

Mirrors the spec's "Testing requirements" §3: per-document-type
rules pin expected step decisions."""

from __future__ import annotations

import pytest

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
from j1.processing.post_compile_assessment import (
    CHUNK_STRATEGY_PAGE_AWARE,
    CHUNK_STRATEGY_SECTION_AWARE,
    PROFILE_BALANCED,
    PROFILE_DIAGNOSTIC,
    PROFILE_FAST,
    PROFILE_PREMIUM,
    STEP_GRAPH_EXTRACTION,
    STEP_QUALITY_ASSESSMENT,
    STEP_REQUIREMENT_EXTRACTION,
    STEP_RISK_EXTRACTION,
    STEP_TABLE_ENRICHMENT,
    STEP_VISION_ENRICHMENT,
    build_post_compile_assessment,
)


def _build(
    *,
    title: str | None = None,
    filename: str | None = None,
    items: list | None = None,
    text_blocks: int = 100,
    tables: int = 0,
    images: int = 0,
    parse_quality: float | None = 0.85,
    text_extractable: float | None = 0.9,
    page_count: int | None = 8,
):
    if items is None:
        items = []
    if title and not any(it.type == "heading" for it in items):
        items = [ParsedContentItem(
            item_id="title", type="heading", page_idx=1, text_preview=title,
        )] + items
    manifest = ParsedContentManifest(
        document_id="doc-1", document_hash="h", parser="raganything",
        parser_version="1", parse_method="auto", profile=None,
        stats=ParsedContentStats(
            text_blocks=text_blocks, tables=tables, images=images,
            equations=0, total_items=text_blocks + tables + images,
            page_count=page_count, parse_quality_score=parse_quality,
            text_extractable_ratio=text_extractable,
        ),
        items=items,
    )
    md = DocumentMetadata(document_id="doc-1", filename=filename)
    understanding = assess_document_understanding(
        metadata=md, manifest=manifest,
    )
    digest = build_content_digest(
        manifest=manifest, understanding=understanding,
        max_sample_blocks=20, max_preview_chars=300, max_early_pages=3,
    )
    return build_post_compile_assessment(
        understanding=understanding,
        manifest=manifest,
        profile=None,
        digest=digest,
    )


def _step(assessment, name):
    return next(s for s in assessment.execution_plan.steps if s.step == name)


def test_clean_text_doc_picks_fast_profile_with_no_vision_or_graph():
    """A clean text document with clear headings, no tables, no
    images → fast profile, no vision, no graph, no req/risk."""
    items = [
        ParsedContentItem(
            item_id="h1", type="heading", page_idx=1,
            text_preview="Knowledge Article: Setting up Local Dev",
        ),
        ParsedContentItem(
            item_id="p1", type="paragraph", page_idx=1,
            text_preview="To set up local dev, install …",
        ),
    ]
    a = _build(items=items, filename="kb_local_dev.pdf")
    assert a.recommended_profile == PROFILE_FAST
    assert _step(a, STEP_VISION_ENRICHMENT).enabled is False
    assert _step(a, STEP_GRAPH_EXTRACTION).enabled is False
    assert _step(a, STEP_REQUIREMENT_EXTRACTION).enabled is False


def test_table_heavy_proposal_enables_table_and_risk_extraction():
    items = [
        ParsedContentItem(
            item_id="h1", type="heading", page_idx=1,
            text_preview="Project Proposal: J1 Ingestion Programme",
        ),
        ParsedContentItem(
            item_id="t1", type="table", page_idx=5,
            text_preview="Item | Cost", metadata={"row_count": 12},
        ),
        ParsedContentItem(
            item_id="t2", type="table", page_idx=7,
            text_preview="Phase | Effort", metadata={"row_count": 8},
        ),
    ]
    a = _build(
        items=items, tables=2, text_blocks=120,
        filename="proposal_j1.pdf",
    )
    assert _step(a, STEP_TABLE_ENRICHMENT).enabled is True
    assert _step(a, STEP_TABLE_ENRICHMENT).pages == (5, 7)
    assert _step(a, STEP_RISK_EXTRACTION).enabled is True
    assert a.recommended_profile == PROFILE_PREMIUM


def test_srs_enables_requirement_risk_and_graph():
    items = [
        ParsedContentItem(
            item_id="h1", type="heading", page_idx=1,
            text_preview="System Requirement Specification for J1 Ingestion",
        ),
        ParsedContentItem(
            item_id="t1", type="table", page_idx=4,
            text_preview="Req-ID | Description", metadata={"row_count": 30},
        ),
    ]
    a = _build(
        items=items, tables=1, text_blocks=200,
        filename="srs_j1.pdf",
    )
    assert _step(a, STEP_REQUIREMENT_EXTRACTION).enabled is True
    assert _step(a, STEP_RISK_EXTRACTION).enabled is True
    assert _step(a, STEP_GRAPH_EXTRACTION).enabled is True
    assert _step(a, STEP_TABLE_ENRICHMENT).enabled is True


def test_architecture_doc_is_a_graph_candidate():
    items = [
        ParsedContentItem(
            item_id="h1", type="heading", page_idx=1,
            text_preview="Software Architecture Design for J1",
        ),
        ParsedContentItem(
            item_id="i1", type="image", page_idx=2,
            caption="Component diagram",
            metadata={"detected_type": "diagram"},
        ),
    ]
    a = _build(items=items, images=1, filename="arch.pdf", text_blocks=80)
    graph_step = _step(a, STEP_GRAPH_EXTRACTION)
    assert graph_step.enabled is True
    assert "system" in graph_step.candidate_entity_types or len(
        graph_step.candidate_entity_types
    ) > 0


def test_invoice_skips_graph_and_narrative_enrichers():
    items = [
        ParsedContentItem(
            item_id="p1", type="paragraph", page_idx=1,
            text_preview="Invoice Number: INV-9001 — Total $4,200",
        ),
        ParsedContentItem(
            item_id="t1", type="table", page_idx=1,
            text_preview="Item | Qty | Price", metadata={"row_count": 5},
        ),
    ]
    a = _build(
        items=items, tables=1, text_blocks=12,
        filename="invoice_INV-9001.pdf", page_count=1,
    )
    assert _step(a, STEP_GRAPH_EXTRACTION).enabled is False
    assert _step(a, STEP_REQUIREMENT_EXTRACTION).enabled is False
    assert _step(a, STEP_RISK_EXTRACTION).enabled is False
    assert _step(a, STEP_TABLE_ENRICHMENT).enabled is True


def test_low_parse_quality_picks_diagnostic_profile_and_review():
    a = _build(
        title="Quarterly Status Report",
        filename="quarterly.pdf",
        parse_quality=0.3,
        text_extractable=0.2,
        page_count=20,
    )
    assert a.recommended_profile == PROFILE_DIAGNOSTIC
    assert _step(a, STEP_QUALITY_ASSESSMENT).enabled is True
    assert a.quality_report.parse_confidence in {"low", "medium"}


def test_clear_headings_select_section_aware_chunking():
    items = [
        ParsedContentItem(item_id="h1", type="h1", page_idx=1, text_preview="System Requirement Specification for J1"),
        ParsedContentItem(item_id="h2", type="h2", page_idx=2, text_preview="2. Functional Requirements"),
        ParsedContentItem(item_id="h3", type="h2", page_idx=3, text_preview="3. Non-Functional Requirements"),
    ]
    a = _build(items=items, filename="srs.pdf")
    assert a.execution_plan.chunking.strategy == CHUNK_STRATEGY_SECTION_AWARE


def test_no_headings_picks_page_or_semantic_chunking():
    """Without heading items, chunking falls back to page-aware (when
    multi-page) or semantic (when block density is high)."""
    items = [
        ParsedContentItem(
            item_id=f"p{i}", type="paragraph", page_idx=(i // 5) + 1,
            text_preview="content " * 30,
        )
        for i in range(60)
    ]
    a = _build(items=items, text_blocks=60, page_count=12)
    assert a.execution_plan.chunking.strategy == CHUNK_STRATEGY_PAGE_AWARE


def test_balanced_profile_for_moderate_documents():
    items = [
        ParsedContentItem(item_id="h1", type="heading", page_idx=1, text_preview="Quarterly Report 2026"),
        ParsedContentItem(item_id="t1", type="table", page_idx=3, text_preview="Metric | Value"),
    ]
    a = _build(items=items, tables=1, text_blocks=80, filename="q1_report.pdf")
    assert a.recommended_profile in {PROFILE_BALANCED, PROFILE_FAST}
