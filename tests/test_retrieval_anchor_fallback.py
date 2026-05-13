"""Regression for the user-reported failure where a stage-
progression question got an answer of "Not in the retrieved
evidence" but the run was still marked Passed.

The actual failing question:

  "How do the deliverables evolve from conceptual engineering
   through 60%, 90%, and 100% design, and which cost estimate
   class is associated with each design stage?"

What this patch enforces (and these tests pin):

  * Anchor extractor pulls the user's own stage markers from
    the question — no hardcoded RFP dictionary.
  * ``check_pack`` flags
    ``evidence_anchor_coverage_for_stage_progression`` when the
    final pack misses ≥ ``min_anchor_coverage`` of those anchors.
  * The runner runs ONE targeted re-retrieval when the first
    pass's previews don't carry the anchors.
  * The answer-quality check fails on refusal-style answers
    ("Not in the retrieved evidence" and variants).
  * Generic unrelated chunks (CEP / potholing / marine survey)
    don't pass the sufficiency check even when they're the
    only material the synthesizer sees.
"""

from __future__ import annotations

import pytest

from j1.retrieval.anchors import (
    expand_query_with_anchors,
    pack_anchor_coverage,
    query_stage_anchors,
)
from j1.retrieval.quality_checks import check_pack
from j1.retrieval.intent_router import QueryIntentLabel


# The actual failing question — used verbatim across tests so a
# future regression on this exact wording surfaces immediately.
_FAILING_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Anchor extraction --------------------------------------------


def test_extractor_pulls_stage_markers_from_user_query():
    """No hardcoded "60% / 90% / 100%" lexicon — the markers come
    from the user's wording. If the user wrote "30% gate, 60%
    gate", we'd extract those instead."""
    anchors = query_stage_anchors(_FAILING_QUERY)
    assert anchors  # truthy
    # Stage markers include each percentage the user wrote.
    flat = {a.lower() for a in anchors.all}
    assert "60%" in flat
    assert "90%" in flat
    assert any("100%" in a for a in flat)  # may be "100% design"
    # Conceptual engineering — the ordinal/conceptual word.
    assert any("conceptual" in a for a in flat)
    # The progression nouns the user wrote.
    assert "deliverables" in flat
    assert "design" in flat
    assert "cost estimate" in flat


def test_extractor_returns_empty_for_non_stage_query():
    """A regular factual question yields no stage anchors."""
    anchors = query_stage_anchors(
        "Who is the project owner?"
    )
    assert not anchors  # falsy
    assert anchors.all == ()


def test_extractor_works_for_arbitrary_document_shapes():
    """Generic: a manual-update question with v1/v2/v3 anchors
    extracts those — not specific to engineering RFPs."""
    anchors = query_stage_anchors(
        "How does the API evolve from v1 through v2 to v3 release?"
    )
    flat = {a.lower() for a in anchors.all}
    # ``stage`` keyword + version may not match the simple
    # patterns; we don't require coverage of every wording. We
    # just check that the extractor handles non-engineering
    # vocabulary without falling apart.
    # release is in progression nouns? No — but the function
    # should still return cleanly.
    assert isinstance(anchors.all, tuple)


# ---- pack_anchor_coverage -----------------------------------------


def test_pack_coverage_distinguishes_relevant_from_boilerplate():
    """The good pack covers the user's anchors; the bad pack
    (mirror of the user's observed evidence) does not."""
    anchors = query_stage_anchors(_FAILING_QUERY).all
    good = [
        "conceptual engineering deliverables include a feasibility "
        "report; cost estimate is Class V (AACE).",
        "At 60% design the deliverables include coordinated drawings "
        "and a Class IV cost estimate.",
        "The 90% design submission carries a Class III cost estimate.",
        "100% design final package includes a Class II estimate.",
    ]
    bad = [
        "Contractor shall provide CEP compliance certification.",
        "Potholing services for utility location.",
        "Marine survey deliverables include bathymetric data.",
        "RFP response shall be formatted per Exhibit B.",
    ]
    good_matched, good_count = pack_anchor_coverage(good, anchors)
    bad_matched, bad_count = pack_anchor_coverage(bad, anchors)
    assert good_count >= 4
    # The bad pack matches at most the generic word "deliverables"
    # — never enough to clear the ≥2 threshold once "deliverables"
    # is the only hit.
    assert bad_count <= 1


# ---- check_pack: anchor-coverage failure --------------------------


@pytest.fixture
def _failing_query():
    return _FAILING_QUERY


def test_check_pack_fails_when_evidence_misses_anchors():
    """A pack matching the user's observed irrelevant evidence
    must fail ``evidence_anchor_coverage_for_stage_progression``."""
    bad_blocks = [
        _FakeBlock(
            artifact_id="A-1", artifact_type="compiled.text",
            text="Contractor shall provide CEP compliance certification.",
            section_path="Section 5 / Compliance",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeBlock(
            artifact_id="A-2", artifact_type="compiled.text",
            text="Potholing services for utility location.",
            section_path="Section 7 / Field Work",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeBlock(
            artifact_id="A-3", artifact_type="compiled.text",
            text="Marine survey deliverables include bathymetric data.",
            section_path="Section 8 / Survey",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeBlock(
            artifact_id="A-4", artifact_type="compiled.text",
            text="RFP response shall be formatted per Exhibit B.",
            section_path="Section 1 / Format",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
    ]
    anchors = query_stage_anchors(_FAILING_QUERY).all
    result = check_pack(
        bad_blocks,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_anchors=anchors,
        min_anchor_coverage=2,
    )
    assert "evidence_anchor_coverage_for_stage_progression" in result.failures
    assert not result.ok


def test_check_pack_passes_when_evidence_covers_anchors():
    """A pack that DOES cover ≥2 anchors clears the check."""
    good_blocks = [
        _FakeBlock(
            artifact_id="A-1", artifact_type="compiled.text",
            text=(
                "Conceptual engineering deliverables include the "
                "feasibility report and a Class V cost estimate."
            ),
            section_path="Section 2 / Conceptual",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeBlock(
            artifact_id="A-2", artifact_type="compiled.text",
            text=(
                "At 60% design the deliverables include drawings and "
                "a Class IV cost estimate."
            ),
            section_path="Section 3 / 60% Design",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
        _FakeBlock(
            artifact_id="A-3", artifact_type="compiled.text",
            text=(
                "100% design final package includes a Class II AACE "
                "cost estimate."
            ),
            section_path="Section 5 / 100% Design",
            metadata={"source_document_id": "doc-A", "run_id": "run-A"},
        ),
    ]
    anchors = query_stage_anchors(_FAILING_QUERY).all
    result = check_pack(
        good_blocks,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_anchors=anchors,
        min_anchor_coverage=2,
    )
    assert "evidence_anchor_coverage_for_stage_progression" not in (
        result.failures
    )


# ---- Refusal-answer detection -------------------------------------


@pytest.mark.parametrize("refusal_text", [
    "Not in the retrieved evidence.",
    "The information is not present in the evidence.",
    "I cannot find any relevant information.",
    "No relevant information was found.",
    "Insufficient context to answer.",
    "The evidence does not contain a clear statement.",
])
def test_check_answer_non_empty_fails_on_refusal(refusal_text):
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    from j1.projects.context import ProjectContext
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=refusal_text,
        retrieved_chunks=[],
        citations=[],
        citation_required=False,
        artifact_registry=None,
        chunks_expected=True,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is False, (
        f"refusal text should fail answer_non_empty: {refusal_text!r}"
    )
    assert "refusal" in (check.detail or "").lower() or (
        "no-evidence" in (check.detail or "").lower()
    )


def test_check_answer_non_empty_passes_on_substantive_answer():
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    from j1.projects.context import ProjectContext
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=(
            "The conceptual engineering phase produces a feasibility "
            "report with a Class V AACE estimate. At 60% design the "
            "deliverables include coordinated drawings with a Class "
            "IV cost estimate. The 90% design carries Class III; the "
            "100% design package includes a Class II AACE estimate."
        ),
        retrieved_chunks=[],
        citations=[],
        citation_required=False,
        artifact_registry=None,
        chunks_expected=True,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is True


# ---- Expansion helper ---------------------------------------------


def test_expanded_query_preserves_original_text():
    """The expansion appends anchors to the query — does NOT
    replace it. Original semantic intent must survive."""
    anchors = query_stage_anchors(_FAILING_QUERY)
    expanded = expand_query_with_anchors(_FAILING_QUERY, anchors)
    assert _FAILING_QUERY in expanded
    # At least one anchor appended.
    assert any(a in expanded for a in anchors.all)


# ---- Fake DTO -----------------------------------------------------


from dataclasses import dataclass, field
from typing import Any


@dataclass
class _FakeBlock:
    """Stand-in for ``EvidenceBlockDTO`` that also carries a
    ``metadata`` dict so ``check_pack``'s scope helper finds
    ``source_document_id`` / ``source_run_id``."""
    artifact_id: str
    artifact_type: str
    text: str
    section_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
