"""Tests for `_build_content_manifest` in the raganything bridge.

The manifest is the data structure the post-parse planner uses to
make selective enrichment decisions: image/table/equation counts,
quality scores, and per-image triage decisions. These tests pin the
behaviour of the helper directly — no MinerU dependency required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from j1.providers.raganything._bridge import (
    _build_content_manifest,
    _classify_image,
    _score_layout_complexity,
    _score_parse_quality,
    _score_text_sufficiency,
)


# ---- Empty / missing input -----------------------------------------


def test_missing_output_dir_returns_empty_manifest(tmp_path):
    manifest = _build_content_manifest(tmp_path / "nope")
    assert manifest == {}


def test_empty_output_dir_returns_zero_counts(tmp_path):
    manifest = _build_content_manifest(tmp_path)
    assert manifest["image_count"] == 0
    assert manifest["table_count"] == 0
    assert manifest["text_block_count"] == 0
    assert manifest["has_images"] is False
    assert manifest["has_tables"] is False
    assert manifest["images"] == []


# ---- File-only fallback (no MinerU content_list) -------------------


def test_counts_files_when_no_content_list_present(tmp_path):
    """Bridge should still produce useful counts even when MinerU
    didn't write a structured content_list.json — the fallback uses
    filename + size only."""
    (tmp_path / "doc.md").write_text("# Title\n\nSome text content here.\n" * 50)
    (tmp_path / "image1.png").write_bytes(b"\x89PNG" + b"x" * 50_000)
    (tmp_path / "logo.png").write_bytes(b"\x89PNG" + b"x" * 500)
    manifest = _build_content_manifest(tmp_path)
    assert manifest["image_count"] == 2
    assert manifest["text_block_count"] == 1
    assert manifest["has_images"] is True
    assert manifest["total_text_chars"] > 100


def test_per_image_triage_skips_likely_logo_by_filename(tmp_path):
    (tmp_path / "company_logo.png").write_bytes(b"\x89PNG" + b"x" * 50_000)
    manifest = _build_content_manifest(tmp_path)
    assert len(manifest["images"]) == 1
    entry = manifest["images"][0]
    assert entry["decision"] == "skip"
    assert entry["role"] == "decorative"
    assert "decorative" in entry["reason"].lower()


def test_per_image_triage_skips_tiny_image_as_icon(tmp_path):
    (tmp_path / "img1.png").write_bytes(b"\x89PNG" + b"x" * 100)  # 104 bytes
    manifest = _build_content_manifest(tmp_path)
    assert manifest["images"][0]["decision"] == "skip"
    assert manifest["images"][0]["role"] == "icon"


def test_per_image_triage_enriches_likely_diagram_by_filename(tmp_path):
    (tmp_path / "workflow_diagram.png").write_bytes(b"\x89PNG" + b"x" * 5_000)
    manifest = _build_content_manifest(tmp_path)
    entry = manifest["images"][0]
    assert entry["decision"] == "enrich"
    assert entry["role"] == "diagram"


def test_per_image_triage_enriches_large_image_by_size(tmp_path):
    """Large image with a neutral filename — size alone bumps it to
    enrich."""
    (tmp_path / "img_42.png").write_bytes(b"\x89PNG" + b"x" * 50_000)
    manifest = _build_content_manifest(tmp_path)
    entry = manifest["images"][0]
    assert entry["decision"] == "enrich"
    assert entry["role"] == "large"


def test_per_image_triage_falls_to_triage_for_medium_unknown(tmp_path):
    (tmp_path / "img_42.png").write_bytes(b"\x89PNG" + b"x" * 10_000)
    manifest = _build_content_manifest(tmp_path)
    entry = manifest["images"][0]
    assert entry["decision"] == "triage"
    assert entry["role"] == "unknown"


# ---- MinerU content_list parsing -----------------------------------


def test_uses_content_list_for_page_idx_and_caption(tmp_path):
    """When MinerU surfaces a structured content_list, the bridge
    should pull page indices, captions, and per-image counts from it
    rather than relying on filename heuristics alone."""
    (tmp_path / "img_42.png").write_bytes(b"\x89PNG" + b"x" * 5_000)
    content_list = [
        {
            "type": "image",
            "img_path": "img_42.png",
            "img_caption": "Figure 3: System architecture diagram showing the request flow",
            "page_idx": 4,
        },
        {"type": "text", "text": "Some body text", "page_idx": 0},
        {"type": "table", "page_idx": 2},
        {"type": "table", "page_idx": 5},
        {"type": "equation", "page_idx": 3},
    ]
    (tmp_path / "doc_content_list.json").write_text(json.dumps(content_list))
    manifest = _build_content_manifest(tmp_path)
    assert manifest["table_count"] == 2
    assert manifest["equation_count"] == 1
    # page_count is derived from max page_idx + 1.
    assert manifest["page_count"] == 6
    # The image got a caption-driven enrich decision.
    img = next(i for i in manifest["images"] if i["image_id"] == "img_42.png")
    assert img["decision"] == "enrich"
    assert img["role"] == "captioned"
    assert img["page"] == 4
    assert "Figure 3" in img["caption"]


def test_handles_malformed_content_list_gracefully(tmp_path):
    """A truncated / invalid content_list.json must not crash the
    bridge — manifest still gets the file-based counts."""
    (tmp_path / "img.png").write_bytes(b"\x89PNG" + b"x" * 5_000)
    (tmp_path / "doc_content_list.json").write_text("{ not json")
    manifest = _build_content_manifest(tmp_path)
    assert manifest["image_count"] == 1


# ---- Quality scores -------------------------------------------------


def test_parse_quality_zero_with_no_text():
    assert _score_parse_quality(text_block_count=0, text_chars=0) == 0.0


def test_parse_quality_full_with_substantial_text():
    assert _score_parse_quality(text_block_count=3, text_chars=2_000) == 1.0


def test_parse_quality_partial_with_thin_output():
    assert _score_parse_quality(text_block_count=1, text_chars=50) == 0.3


def test_text_sufficiency_caps_at_one_for_dense_pages():
    assert _score_text_sufficiency(text_chars=10_000, page_count=5) == 1.0


def test_text_sufficiency_zero_with_no_text():
    assert _score_text_sufficiency(text_chars=0, page_count=10) == 0.0


def test_layout_complexity_zero_for_pure_text():
    assert _score_layout_complexity(0, 0, 0, page_count=10) == 0.0


def test_layout_complexity_caps_at_one_for_busy_layout():
    # 100 visuals over 10 pages = 10 per page → cap at 1.0.
    score = _score_layout_complexity(50, 30, 20, page_count=10)
    assert score == 1.0


# ---- Direct classifier coverage ------------------------------------


@pytest.mark.parametrize("filename,expected_decision", [
    ("logo.png", "skip"),
    ("watermark.png", "skip"),
    ("page_header_decoration.png", "skip"),
    ("architecture_diagram.png", "enrich"),
    ("revenue_chart.png", "enrich"),
])
def test_classify_image_by_filename_pattern(filename, expected_decision):
    decision, _, _, _ = _classify_image(
        filename=filename, size_bytes=5_000, caption="",
    )
    assert decision == expected_decision


def test_classify_image_returns_score_and_reason():
    decision, role, score, reason = _classify_image(
        filename="company_logo.png", size_bytes=10_000, caption="",
    )
    assert decision == "skip"
    assert role == "decorative"
    assert 0 <= score <= 1
    assert reason  # non-empty


# ---- Per-element items ---------------------------------------------


def test_manifest_items_populated_from_content_list(tmp_path):
    """The bridge must surface per-element items so the FE Content
    Inventory tab can render text blocks, tables, images, headings —
    not just summary counts. The user-visible bug we're guarding
    against: tab loaded but the items table is empty."""
    content_list = [
        {"type": "title", "text": "Quarterly Report Q1", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "Revenue grew 12% over the prior period.", "page_idx": 0},
        {"type": "image", "img_path": "img_1.png", "img_caption": "Pipeline chart",
         "page_idx": 1},
        {"type": "table", "caption": "Sales by region", "page_idx": 2},
        {"type": "equation", "text": "y = mx + b", "page_idx": 3},
    ]
    (tmp_path / "doc_content_list.json").write_text(json.dumps(content_list))

    manifest = _build_content_manifest(tmp_path)

    items = manifest["items"]
    assert len(items) == 5
    by_type: dict[str, dict] = {it["type"]: it for it in items}
    # Heading projected from `title` raw type.
    assert by_type["heading"]["text_preview"] == "Quarterly Report Q1"
    # Text body comes through as a preview.
    assert "Revenue grew" in by_type["text"]["text_preview"]
    # Image carries caption + path.
    assert by_type["image"]["caption"] == "Pipeline chart"
    assert by_type["image"]["source_path"] == "img_1.png"
    # Table caption surfaces as preview.
    assert by_type["table"]["text_preview"] == "Sales by region"
    # Formula projected from `equation` raw type.
    assert by_type["formula"]["text_preview"] == "y = mx + b"


def test_manifest_items_empty_when_no_content_list(tmp_path):
    """No content_list.json on disk → empty items list, not a crash.
    The FE Content Inventory tab handles the empty state via its
    `status="empty"` projection."""
    manifest = _build_content_manifest(tmp_path)
    assert manifest["items"] == []


def test_manifest_items_drop_empty_text_blocks(tmp_path):
    """Empty text-shaped entries are dropped — emitting blank rows
    in the FE table is worse than omitting them."""
    content_list = [
        {"type": "text", "text": "", "page_idx": 0},
        {"type": "text", "text": "Real content here.", "page_idx": 1},
    ]
    (tmp_path / "doc_content_list.json").write_text(json.dumps(content_list))
    manifest = _build_content_manifest(tmp_path)
    items = manifest["items"]
    assert len(items) == 1
    assert "Real content" in items[0]["text_preview"]


def test_manifest_items_truncate_long_previews(tmp_path):
    """Long text bodies get truncated to keep the manifest artifact
    bounded — a 100-page PDF with full text would otherwise balloon
    the artifact JSON beyond what the audit log can comfortably
    serve."""
    long_body = "x" * 5_000
    content_list = [
        {"type": "text", "text": long_body, "page_idx": 0},
    ]
    (tmp_path / "doc_content_list.json").write_text(json.dumps(content_list))
    manifest = _build_content_manifest(tmp_path)
    preview = manifest["items"][0]["text_preview"]
    # Cap is 280 chars; tolerate a small ellipsis suffix.
    assert len(preview) <= 290
    assert preview.endswith("…")
