"""Group-based sufficiency + tightened answer-quality regression.

Spec acceptance gates (the user's exact list):

  * Evidence with only 1-2 stage anchors → fails sufficiency
  * Evidence missing cost-estimate/class → fails sufficiency
  * Evidence missing deliverables/submittals → fails sufficiency
  * Long refusal (>400 chars) does NOT pass just because of length
  * Overall status is not Passed unless evidence sufficiency AND
    answer quality both pass

The reference question (used verbatim across multiple tests):

  "How do the deliverables evolve from conceptual engineering
   through 60%, 90%, and 100% design, and which cost estimate
   class is associated with each design stage?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.projects.context import ProjectContext
from j1.retrieval.anchors import (
    stage_progression_coverage,
    stage_progression_groups,
)
from j1.retrieval.intent_router import QueryIntentLabel
from j1.retrieval.quality_checks import check_pack


_FAILING_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Fake DTO for check_pack -------------------------------------


@dataclass
class _FakeBlock:
    artifact_id: str
    artifact_type: str
    text: str
    section_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _block(aid, text, section="Section"):
    return _FakeBlock(
        artifact_id=aid, artifact_type="compiled.text",
        text=text, section_path=section,
        metadata={"source_document_id": "doc-A", "run_id": "run-A"},
    )


# =====================================================================
# 1. Group-based sufficiency: ≥3 stages + deliverable + estimate
# =====================================================================


def test_groups_helper_extracts_4_stage_anchors_from_query():
    """The user's query specifies 4 design stages — extractor
    must pull all 4 (and NOT mix in "cost estimate class")."""
    g = stage_progression_groups(_FAILING_QUERY)
    assert g is not None
    requested = {s.lower() for s in g.stages_requested}
    assert "60%" in requested
    assert "90%" in requested
    # 100% may render as "100% design"
    assert any("100%" in s for s in requested)
    # conceptual variant
    assert any("conceptual" in s for s in requested)
    # ``cost estimate class`` is NOT a stage — it's a separate group.
    assert not any(
        "cost estimate class" == s.lower() for s in requested
    )


# ---- Failure case A: only 1-2 stage anchors ------------------------


def test_evidence_with_only_one_stage_fails_sufficiency():
    """Pack covers 1 stage + has deliverable + has estimate.
    Group rule requires ≥3 stages → must fail."""
    pack = [
        _block("E1", (
            "At 60% design the deliverables include drawings "
            "and a Class IV cost estimate."
        )),
    ]
    g = stage_progression_groups(_FAILING_QUERY)
    result = check_pack(
        pack,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_groups=g,
    )
    assert "evidence_anchor_coverage_for_stage_progression" in (
        result.failures
    ), f"expected sufficiency fail; details={result.details}"
    assert result.details["stage_groups"]["stages_covered"] == 1


def test_evidence_with_two_stages_fails_sufficiency():
    """Even with 2 stages (1 short of the ≥3 minimum) → fail."""
    pack = [
        _block("E1", (
            "60% design deliverables include drawings; Class IV "
            "cost estimate."
        )),
        _block("E2", (
            "90% design submittals carry a Class III cost estimate."
        )),
    ]
    g = stage_progression_groups(_FAILING_QUERY)
    result = check_pack(
        pack,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_groups=g,
    )
    assert "evidence_anchor_coverage_for_stage_progression" in (
        result.failures
    )
    assert result.details["stage_groups"]["stages_covered"] == 2


# ---- Failure case B: missing estimate group ------------------------


def test_evidence_missing_estimate_fails_sufficiency():
    """3 stages + deliverable + NO estimate/class → fail."""
    pack = [
        _block("E1", (
            "Conceptual engineering deliverables include the "
            "feasibility report."
        )),
        _block("E2", (
            "60% design submittals include preliminary drawings."
        )),
        _block("E3", (
            "90% design package contains coordinated plans."
        )),
    ]
    g = stage_progression_groups(_FAILING_QUERY)
    result = check_pack(
        pack,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_groups=g,
    )
    assert "evidence_anchor_coverage_for_stage_progression" in (
        result.failures
    )
    assert result.details["stage_groups"]["estimate_present"] is False


# ---- Failure case C: missing deliverable group ---------------------


def test_evidence_missing_deliverables_fails_sufficiency():
    """3 stages + estimate + NO deliverable/submittal mention →
    fail. Deliverable shapes include 'deliverable', 'submittal',
    'submission', 'report/memo/drawing/...will/include/...'."""
    pack = [
        _block("E1", (
            "Conceptual engineering: a Class V cost estimate "
            "covers the high-level scope."
        )),
        _block("E2", (
            "60% design milestone: Class IV cost estimate."
        )),
        _block("E3", (
            "90% design milestone: Class III cost estimate."
        )),
    ]
    g = stage_progression_groups(_FAILING_QUERY)
    result = check_pack(
        pack,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_groups=g,
    )
    assert "evidence_anchor_coverage_for_stage_progression" in (
        result.failures
    )
    assert result.details["stage_groups"]["deliverable_present"] is False


# ---- Pass case: all three groups satisfied -------------------------


def test_evidence_with_all_groups_passes_sufficiency():
    """3 stages + deliverable + estimate → check_pack passes
    the anchor-coverage gate."""
    pack = [
        _block("E1", (
            "Conceptual engineering deliverables include the "
            "feasibility report; Class V (AACE) cost estimate."
        )),
        _block("E2", (
            "60% design submittals include drawings; Class IV "
            "cost estimate."
        )),
        _block("E3", (
            "90% design package contains coordinated plans; "
            "Class III cost estimate."
        )),
        _block("E4", (
            "100% design final package; Class II AACE estimate."
        )),
    ]
    g = stage_progression_groups(_FAILING_QUERY)
    result = check_pack(
        pack,
        intent=QueryIntentLabel.STAGE_PROGRESSION,
        active_document_id="doc-A", active_run_id="run-A",
        stage_groups=g,
    )
    assert "evidence_anchor_coverage_for_stage_progression" not in (
        result.failures
    ), f"unexpected failure; details={result.details}"


# NOTE: tests that exercised the legacy ``_check_answer_non_empty``
# / ``aggregate_status`` API were removed when
# ``j1.validation.checks`` was deleted. The orchestrator's
# ``AnswerQualityGate`` owns refusal detection + status
# aggregation now; equivalent coverage lives in
# ``test_query_answer_quality.py`` and the e2e orchestrator
# tests in ``test_query_orchestrator.py``.
