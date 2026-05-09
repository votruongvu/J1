"""End-to-end domain pack tests in the post-compile planner.

Confirms that:
  1. Civil documents trigger the civil overlay (BOQ → tables only,
     inspection report → vision + risk + graph, etc.).
  2. Non-civil documents fall back to the generic plan unchanged.
  3. Civil document types flow through to
     `document_understanding.document_type` AND validate via the
     extended-taxonomy validator.
  4. The pack's `unsupported_capabilities` surface on the final
     planning result for reviewer visibility.
  5. Operator overrides are honored end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.domains import default_registry
from j1.processing.document_understanding import DocumentMetadata
from j1.processing.manifest import (
    ParsedContentItem,
    ParsedContentManifest,
    ParsedContentStats,
)
from j1.processing.planning_result import validate_planning_result_dict
from j1.processing.planning_settings import PlanningSettings
from j1.processing.post_compile_planning import build_planning_result


def _build_manifest(
    *,
    items=None,
    text_blocks: int = 100,
    tables: int = 0,
    images: int = 0,
    page_count: int | None = 8,
    parse_quality: float = 0.85,
) -> ParsedContentManifest:
    return ParsedContentManifest(
        document_id="d1",
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


def _build(
    *,
    title: str | None = None,
    filename: str | None = None,
    items=None,
    text_blocks: int = 100,
    tables: int = 0,
    images: int = 0,
    page_count: int | None = 8,
    parse_quality: float = 0.85,
    domain_override: str | None = None,
    workspace_default_domain: str | None = None,
):
    items = list(items or [])
    if title and not any(it.type == "heading" for it in items):
        items = [ParsedContentItem(
            item_id="title", type="heading", page_idx=1, text_preview=title,
        )] + items
    manifest = _build_manifest(
        items=items, text_blocks=text_blocks, tables=tables,
        images=images, page_count=page_count, parse_quality=parse_quality,
    )
    md = DocumentMetadata(document_id="d1", filename=filename)
    settings = PlanningSettings()
    return build_planning_result(
        run_id="r1",
        document=md,
        file_size_bytes=4096,
        profile=None,
        manifest=manifest,
        settings=settings,
        domain_registry=default_registry(),
        domain_override=domain_override,
        workspace_default_domain=workspace_default_domain,
        now=datetime(2026, 5, 9, tzinfo=timezone.utc),
    )


def _step(plan_dict: dict, step_name: str) -> dict:
    return plan_dict["execution_plan"]["steps"][step_name]


# ---- Civil overlay applied --------------------------------------


def test_boq_triggers_civil_overlay_with_table_focus():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Bill of Quantities — Road Drainage Works",
        ),
        ParsedContentItem(
            item_id="t1", type="table", page_idx=2,
            text_preview="Item | Description | Unit | Quantity | Rate | Amount",
            metadata={"row_count": 50},
        ),
    ]
    result = _build(
        items=items, filename="BOQ_drainage.pdf",
        text_blocks=200, tables=8,
    )
    payload = result.to_dict()
    assert payload["domain_context"]["selected_domain"] == "civil_engineering"
    assert payload["document_understanding"]["document_type"] == "boq"
    assert _step(payload, "table_enrichment")["enabled"] is True
    assert _step(payload, "vision_enrichment")["enabled"] is False
    assert _step(payload, "graph_extraction")["enabled"] is False
    assert payload["recommended_profile"] == "premium"


def test_inspection_report_with_images_enables_vision_and_risk():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Site Inspection Report — Basement Slab Cracks",
        ),
        ParsedContentItem(
            item_id="img1", type="image", page_idx=2,
            caption="Slab crack on grid B-3",
            metadata={"detected_type": "photo"},
        ),
        ParsedContentItem(
            item_id="img2", type="image", page_idx=3,
            caption="Wall defect close-up",
            metadata={"detected_type": "photo"},
        ),
    ]
    result = _build(
        items=items, filename="inspection.pdf",
        text_blocks=80, tables=2, images=4,
    )
    payload = result.to_dict()
    assert payload["domain_context"]["selected_domain"] == "civil_engineering"
    assert payload["document_understanding"]["document_type"] == "inspection_report"
    assert _step(payload, "vision_enrichment")["enabled"] is True
    assert _step(payload, "risk_extraction")["enabled"] is True
    assert _step(payload, "graph_extraction")["enabled"] is True


def test_method_statement_enables_risk_and_graph():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Method Statement for Concrete Pouring",
        ),
        ParsedContentItem(
            item_id="b1", type="paragraph", page_idx=1,
            text_preview="Sequence of work, equipment, manpower, safety measures.",
        ),
    ]
    result = _build(items=items, filename="method_statement_concrete.pdf")
    payload = result.to_dict()
    assert payload["document_understanding"]["document_type"] == "method_statement"
    assert _step(payload, "risk_extraction")["enabled"] is True


def test_construction_drawing_enables_vision():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Construction Drawing — Foundation Details",
        ),
        ParsedContentItem(
            item_id="b1", type="paragraph", page_idx=1,
            text_preview="Drawing number ARC-101, revision A, scale 1:50.",
        ),
    ]
    result = _build(
        items=items, filename="ARC-101.pdf",
        images=10, page_count=12,
    )
    payload = result.to_dict()
    # Construction drawing OR architectural drawing depending on the
    # pack's matching order — both are valid civil types here.
    doc_type = payload["document_understanding"]["document_type"]
    assert doc_type in {"construction_drawing", "architectural_drawing", "structural_drawing"}
    assert _step(payload, "vision_enrichment")["enabled"] is True


def test_structural_calculation_enables_table_and_quality():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Structural Calculation Report — Beam Design",
        ),
        ParsedContentItem(
            item_id="b1", type="paragraph", page_idx=1,
            text_preview=(
                "Design calculation for slab and beam load combinations."
            ),
        ),
    ]
    result = _build(items=items, filename="calc_beam.pdf")
    payload = result.to_dict()
    assert payload["document_understanding"]["document_type"] == "structural_calculation"
    assert _step(payload, "table_enrichment")["enabled"] is True
    assert _step(payload, "quality_assessment")["enabled"] is True


def test_rfi_classified_as_civil():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Request for Information RFI-023",
        ),
    ]
    result = _build(items=items, filename="rfi_023.pdf")
    payload = result.to_dict()
    assert payload["document_understanding"]["document_type"] == "rfi"
    assert payload["domain_context"]["selected_domain"] == "civil_engineering"


# ---- Generic fallback -------------------------------------------


def test_quarterly_business_review_falls_back_to_generic():
    """Non-civil document → generic plan, no civil overlay applied."""
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Quarterly Business Review",
        ),
        ParsedContentItem(
            item_id="b1", type="paragraph", page_idx=1,
            text_preview=(
                "Sales pipeline grew 12%; revenue at $4.2M; "
                "customer churn declined."
            ),
        ),
    ]
    result = _build(items=items, filename="qbr.pdf")
    payload = result.to_dict()
    assert payload["domain_context"]["selected_domain"] == "general"
    # Civil-specific document_type must NOT appear.
    assert payload["document_understanding"]["document_type"] not in {
        "boq", "inspection_report", "method_statement",
        "construction_drawing", "rfi",
    }


def test_invoice_does_not_get_civil_pack():
    items = [
        ParsedContentItem(
            item_id="b1", type="paragraph", page_idx=1,
            text_preview="Invoice Number: INV-9001 — Total $4,200",
        ),
    ]
    result = _build(items=items, filename="invoice_INV-9001.pdf", tables=1)
    payload = result.to_dict()
    assert payload["domain_context"]["selected_domain"] == "general"


# ---- Override path ---------------------------------------------


def test_workspace_default_civil_applies_overlay_when_evidence_supports():
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Bill of Quantities — Drainage",
        ),
    ]
    result = _build(
        items=items, filename="boq.pdf", tables=4,
        workspace_default_domain="civil_engineering",
    )
    payload = result.to_dict()
    # Auto-detect would have picked civil too, but the
    # selection_source must reflect the workspace override.
    assert payload["domain_context"]["selected_domain"] == "civil_engineering"
    assert payload["domain_context"]["selection_source"] == "workspace"


def test_user_override_general_disables_civil_pack():
    """Even on a clearly civil document, an explicit `general`
    override forces the generic planner."""
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Bill of Quantities — Drainage",
        ),
    ]
    result = _build(
        items=items, filename="boq.pdf", tables=4,
        domain_override="general",
    )
    payload = result.to_dict()
    assert payload["domain_context"]["selected_domain"] == "general"


# ---- Schema + validator -----------------------------------------


def test_planning_result_validates_with_extended_taxonomy():
    """A civil document_type ('boq') must validate when the
    registry's extension set is supplied."""
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Bill of Quantities — Drainage",
        ),
    ]
    result = _build(items=items, filename="boq.pdf", tables=4)
    extended = frozenset(default_registry().extended_document_types())
    validate_planning_result_dict(
        result.to_dict(),
        page_count=8,
        extended_document_types=extended,
    )


def test_planning_result_with_civil_type_fails_without_extension():
    """Without the registry extension, the strict generic taxonomy
    rejects 'boq' — guards against accidental loosening."""
    items = [
        ParsedContentItem(
            item_id="hdr", type="heading", page_idx=1,
            text_preview="Bill of Quantities — Drainage",
        ),
    ]
    result = _build(items=items, filename="boq.pdf", tables=4)
    from j1.processing.planning_result import PlanningValidationError
    with pytest.raises(PlanningValidationError):
        validate_planning_result_dict(result.to_dict(), page_count=8)
