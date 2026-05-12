"""Regression tests for ``_heuristic_questions_for_chunk`` not
producing questions with raw truncated metadata blocks.

Bug fixed:
   The heuristic question generator emitted
   ``f"What does the document say about: {first_sentence}?"``
   where ``first_sentence`` was the leading 140 chars of the
   chunk body. For documents whose first chunk starts with a
   header block (e.g. ``"J1 Platform - One Page Brief |
   Document ID: J1-TEXT-001 | Version: 1.0 | Date: 12 May 2026
   | Purpose: …"``) the question included the entire metadata
   line as a topic. The synthesizer LLM correctly abstained on
   the malformed question. The latest validation report flagged
   exactly this case via ``evidence_present_but_answer_fallback``.

   The new heuristic uses (in order) chunk section → clean
   sentence → page reference → generic summarisation prompt.
"""

from __future__ import annotations

import pytest

from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation.generator import (
    _heuristic_questions_for_chunk,
    _is_clean_sentence,
)


# ---- _is_clean_sentence: rejects metadata-shaped strings ---------


def test_rejects_pipe_separated_metadata_block():
    """Two-or-more pipes → metadata header, NOT a sentence."""
    assert not _is_clean_sentence(
        "J1 Platform - One Page Text Test Brief Document ID: "
        "J1-TEXT-001 | Version: 1.0 | Date: 12 May 2026 | Purpose: test"
    )


def test_rejects_colon_heavy_id_string():
    """Three-or-more colons → ID block, NOT a sentence."""
    assert not _is_clean_sentence(
        "ID: 123 | Type: foo | Status: ok | Owner: alice"
    )


def test_rejects_header_marker_keywords():
    """Sentences containing 'Document ID', 'Version:', etc. are
    metadata, not real content."""
    assert not _is_clean_sentence(
        "The J1 Platform brief Document ID J1-TEXT-001 explains "
        "the architecture"
    )
    assert not _is_clean_sentence(
        "First paragraph mentions Version: 1.0 of the spec"
    )


def test_rejects_very_short_strings():
    """Fewer than 3 words → not a sentence."""
    assert not _is_clean_sentence("Hello")
    assert not _is_clean_sentence("Two words")
    assert not _is_clean_sentence("")
    assert not _is_clean_sentence("   ")


def test_accepts_real_sentence():
    """Plain prose with 3+ words and no metadata markers."""
    assert _is_clean_sentence(
        "The proposal due date is 20 May 2026."
    )
    assert _is_clean_sentence(
        "Compile stage runs MinerU layout analysis on every page."
    )


# ---- _heuristic_questions_for_chunk: prefers clean shapes --------


def _chunk(*, body, section=None, page_start=None):
    return _ChunkRecord(
        chunk_id="ch-1",
        body=body,
        page_start=page_start,
        page_end=page_start,
        section=section,
        title=None,
        token_count=None,
        confidence=None,
        metadata={},
        linked_assets=[],
        source_artifact_id=None,
        source_document_ids=[],
    )


def test_prefers_section_heading_when_available():
    out = _heuristic_questions_for_chunk(
        _chunk(
            body="Some content here",
            section="Methodology",
        ),
        budget=1,
    )
    assert len(out) == 1
    assert out[0]["question"] == "What does the document say in section 'Methodology'?"


def test_uses_clean_sentence_quoted_when_no_section():
    """Real sentence → quoted excerpt question. Note the QUOTES —
    the LLM sees this as a topic reference rather than a phrasing
    fragment."""
    out = _heuristic_questions_for_chunk(
        _chunk(body="The proposal due date is 20 May 2026."),
        budget=1,
    )
    assert len(out) == 1
    assert (
        out[0]["question"]
        == 'What does the document say about "The proposal due date is 20 May 2026."?'
    )


def test_falls_back_to_page_reference_for_metadata_chunks():
    """The headline regression: chunks whose first 140 chars are
    metadata → use page reference instead of embedding the raw
    text. This is the case the latest validation report flagged."""
    out = _heuristic_questions_for_chunk(
        _chunk(
            body=(
                "J1 Platform - One Page Text Test Brief Document ID: "
                "J1-TEXT-001 | Version: 1.0 | Date: 12 May 2026 | Purpose: "
                "test PDF parsing"
            ),
            page_start=1,
        ),
        budget=1,
    )
    assert len(out) == 1
    assert out[0]["question"] == "What information is on page 1 of the document?"
    # The raw metadata MUST NOT be in the question.
    assert "Document ID" not in out[0]["question"]
    assert "|" not in out[0]["question"]


def test_falls_back_to_generic_question_when_no_section_no_page():
    """Chunks with metadata-shaped body and no page info → generic
    summarisation. Still testable via ``expected_chunk_in_topk``."""
    out = _heuristic_questions_for_chunk(
        _chunk(
            body="ID: 123 | Type: foo | Status: ok | Owner: alice",
            page_start=None,
        ),
        budget=1,
    )
    assert len(out) == 1
    assert out[0]["question"] == (
        "Summarize the key information in this section of the document."
    )


def test_empty_chunk_body_returns_no_question():
    """Defensive: empty body → no question (down from a crashing
    template)."""
    out = _heuristic_questions_for_chunk(
        _chunk(body=""),
        budget=1,
    )
    assert out == []


def test_section_with_runaway_length_falls_through_to_sentence():
    """If the chunk's section is malformed / too long (>80 chars),
    fall through to the sentence path rather than asking
    'What does the document say in section <500-char heading>?'."""
    long_section = "Section " + "x" * 200
    out = _heuristic_questions_for_chunk(
        _chunk(
            body="The compile stage runs MinerU layout analysis.",
            section=long_section,
        ),
        budget=1,
    )
    # Should NOT use the section
    assert long_section not in out[0]["question"]
    # Should use the clean sentence path
    assert "compile stage" in out[0]["question"]
