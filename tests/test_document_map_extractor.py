"""Tests for `_document_map_to_prose` — the
``enriched.document_map`` textual evidence extractor.

The enricher's exact JSON shape isn't pinned yet (domain packs can
emit different layouts), so the extractor is permissive: it looks
for a handful of recognised textual keys and assembles a single
prose string for the synthesizer's context. These tests pin the
recognised vocabulary so a future enricher knows what fields land
in the LLM prompt.
"""

from __future__ import annotations

from j1.validation.evidence import _document_map_to_prose


# ---- Top-level summary -------------------------------------------


def test_extractor_picks_top_level_summary():
    data = {
        "summary": "This is a one-paragraph overview of the document.",
    }
    out = _document_map_to_prose(data)
    assert out == "This is a one-paragraph overview of the document."


def test_extractor_accepts_outline_alias():
    """`outline` is a common alternative key name; extractor must
 accept it."""
    out = _document_map_to_prose({"outline": "Some outline text."})
    assert out == "Some outline text."


def test_extractor_accepts_description_alias():
    out = _document_map_to_prose({"description": "Some description."})
    assert out == "Some description."


def test_extractor_picks_first_of_summary_outline_description():
    """When multiple aliases are present, summary wins (canonical)."""
    out = _document_map_to_prose({
        "summary": "Canonical summary.",
        "outline": "Different outline.",
    })
    assert "Canonical summary" in out
    assert "Different outline" not in out


# ---- Sections list ----------------------------------------------


def test_extractor_emits_section_titles_and_summaries():
    """When `sections[]` carries titles + summaries, each becomes
 a bullet in the prose. Lets the LLM see the document's
 structure even when no specific chunk matched."""
    data = {
        "sections": [
            {"title": "Introduction", "summary": "Background and scope."},
            {"title": "Methodology", "summary": "Approach and tools."},
        ],
    }
    out = _document_map_to_prose(data)
    assert "Sections:" in out
    assert "Introduction: Background and scope." in out
    assert "Methodology: Approach and tools." in out


def test_extractor_handles_title_only_sections():
    """A section with only a title (no summary) emits just the title.
 Better than dropping the section entirely — the LLM can still
 reason about structure."""
    data = {
        "sections": [
            {"title": "Conclusions"},
        ],
    }
    out = _document_map_to_prose(data)
    assert "- Conclusions" in out


def test_extractor_accepts_chapters_and_toc_aliases():
    """Domain packs may use `chapters` or `toc` instead of
 `sections`. The extractor accepts all three."""
    for key in ("chapters", "toc"):
        out = _document_map_to_prose({
            key: [{"title": "Chapter 1", "summary": "Beginning."}],
        })
        assert out is not None
        assert "Chapter 1" in out


def test_extractor_uses_description_as_section_summary_alias():
    """Section-level `description` is treated the same as
 `summary` (consistent with top-level vocabulary)."""
    data = {
        "sections": [
            {"title": "Scope", "description": "What's in scope."},
        ],
    }
    out = _document_map_to_prose(data)
    assert "Scope: What's in scope." in out


# ---- Flat headings list -----------------------------------------


def test_extractor_emits_flat_headings_when_no_sections():
    """Simpler schemas may just carry a flat `headings[]` list.
 The extractor renders them as a single dotted line."""
    data = {
        "headings": ["Introduction", "Methodology", "Results"],
    }
    out = _document_map_to_prose(data)
    assert "Headings: Introduction · Methodology · Results" in out


def test_extractor_accepts_section_titles_alias():
    out = _document_map_to_prose({
        "section_titles": ["A", "B"],
    })
    assert "Headings: A · B" in out


# ---- Combinations -----------------------------------------------


def test_extractor_combines_summary_headings_and_sections():
    """When multiple recognised fields are present, the extractor
 combines them in order: summary → headings → sections."""
    data = {
        "summary": "Doc overview.",
        "headings": ["Top1", "Top2"],
        "sections": [
            {"title": "Top1", "summary": "Detail of top1."},
        ],
    }
    out = _document_map_to_prose(data)
    assert "Doc overview." in out
    assert "Headings: Top1 · Top2" in out
    assert "Top1: Detail of top1." in out
    # Order: summary first, then headings, then sections.
    assert out.index("Doc overview") < out.index("Headings:")
    assert out.index("Headings:") < out.index("Sections:")


# ---- Edge cases -------------------------------------------------


def test_extractor_returns_none_for_empty_dict():
    """No recognised keys → None. Caller falls back to the generic
 preview path."""
    assert _document_map_to_prose({}) is None


def test_extractor_returns_none_for_non_dict():
    """Defensive: a list / string / int at the top level isn't a
 document_map. Caller falls through gracefully."""
    assert _document_map_to_prose(["a", "b"]) is None
    assert _document_map_to_prose("just a string") is None
    assert _document_map_to_prose(42) is None
    assert _document_map_to_prose(None) is None


def test_extractor_drops_empty_string_fields():
    """Whitespace-only `summary` is treated as missing — same as
 actually-missing."""
    out = _document_map_to_prose({"summary": "  \n\t  "})
    assert out is None


def test_extractor_drops_non_string_headings():
    """A `headings` list with mixed types (strings + dicts) picks
 only the strings; if NO strings remain, falls through to the
 next field."""
    # All non-string entries → not emitted, but other fields
    # should still surface.
    data = {
        "headings": [{}, None, 42],
        "summary": "Backup summary.",
    }
    out = _document_map_to_prose(data)
    assert "Headings:" not in out
    assert "Backup summary." in out


def test_extractor_truncates_giant_input():
    """Defense against a pathological enricher: cap the output so
 a 50-page outline can't flood the LLM prompt budget."""
    from j1.validation.evidence import _DOCUMENT_MAP_TEXT_CAP
    big_summary = "x" * (_DOCUMENT_MAP_TEXT_CAP * 5)
    out = _document_map_to_prose({"summary": big_summary})
    assert out is not None
    assert len(out) <= _DOCUMENT_MAP_TEXT_CAP + 1  # +1 for "…"
    assert out.endswith("…")


# ---- Priority integration ---------------------------------------


def test_document_map_priority_higher_than_graph_json():
    """Spec rule: document_map should serve as textual evidence,
 NOT be deprioritised like graph_json."""
    from j1.validation.evidence import _kind_priority
    assert _kind_priority("enriched.document_map") < _kind_priority("graph_json")


def test_document_map_priority_lower_than_chunk():
    """`chunk` remains the canonical ground truth — document_map
 is a useful fallback but never preferred over actual chunk text."""
    from j1.validation.evidence import _kind_priority
    assert _kind_priority("chunk") < _kind_priority("enriched.document_map")
