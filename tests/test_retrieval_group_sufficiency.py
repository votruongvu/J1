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


# =====================================================================
# 2. Answer-quality: refusal regardless of length
# =====================================================================


def test_long_refusal_over_400_chars_still_fails():
    """The previous shortcut (len > 400 → assume substantive)
    let long apologetic refusals through. Now: refusal pattern
    fires regardless of length."""
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    long_refusal = (
        "I'm sorry, but I was not able to find this information "
        "in the retrieved evidence. The evidence does not contain "
        "any clear statement about the design progression. I have "
        "carefully reviewed the provided context, but the chunks "
        "that were retrieved appear to relate to compliance and "
        "proposal-formatting requirements rather than the design "
        "stages you asked about. To answer this question, I would "
        "need additional evidence covering conceptual engineering, "
        "60% design, 90% design, and 100% design — none of which "
        "appears in the current evidence pack. Without that "
        "evidence I cannot provide the stage-by-stage mapping the "
        "question requires."
    )
    assert len(long_refusal) > 400
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=long_refusal,
        retrieved_chunks=[], citations=[],
        citation_required=False,
        artifact_registry=None,
        chunks_expected=True,
        question=_FAILING_QUERY,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is False
    assert "refusal" in (check.detail or "").lower() or (
        "no-evidence" in (check.detail or "").lower()
    )


# =====================================================================
# 3. Stage-aware substantive answer check
# =====================================================================


def test_answer_must_cover_3_stages_for_stage_progression():
    """A substantive-looking answer that only mentions ONE stage
    is NOT acceptable for a question asking about 4 stages."""
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    one_stage_answer = (
        "At 60% design the deliverables include preliminary "
        "structural drawings and a Class IV cost estimate."
    )
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=one_stage_answer,
        retrieved_chunks=[], citations=[],
        citation_required=False, artifact_registry=None,
        chunks_expected=True,
        question=_FAILING_QUERY,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is False
    assert "stage" in (check.detail or "").lower()


def test_answer_must_mention_deliverables_for_stage_progression():
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    no_deliverable = (
        "Conceptual engineering uses a Class V cost estimate. "
        "60% design uses Class IV. 90% design uses Class III. "
        "100% design uses Class II."
    )
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=no_deliverable,
        retrieved_chunks=[], citations=[],
        citation_required=False, artifact_registry=None,
        chunks_expected=True,
        question=_FAILING_QUERY,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is False
    assert "deliverable" in (check.detail or "").lower()


def test_answer_must_mention_estimate_for_stage_progression():
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    no_estimate = (
        "Conceptual engineering deliverables include the "
        "feasibility report. 60% design submittals include "
        "drawings. 90% design package contains coordinated "
        "plans. 100% design final package."
    )
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=no_estimate,
        retrieved_chunks=[], citations=[],
        citation_required=False, artifact_registry=None,
        chunks_expected=True,
        question=_FAILING_QUERY,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is False
    assert (
        "estimate" in (check.detail or "").lower()
        or "class" in (check.detail or "").lower()
    )


def test_well_formed_stage_answer_passes():
    """A complete answer covering 3+ stages + deliverable +
    estimate passes the substantive check."""
    from j1.validation.checks import (
        _check_answer_non_empty, _CheckContext,
    )
    answer = (
        "Conceptual engineering produces deliverables including "
        "the feasibility report (Class V AACE estimate). At 60% "
        "design the deliverables are coordinated drawings (Class "
        "IV cost estimate). The 90% design submittals carry a "
        "Class III estimate. 100% design package: Class II "
        "AACE estimate."
    )
    ctx = _CheckContext(
        ctx=ProjectContext(tenant_id="t", project_id="p"),
        run_id="run-A",
        answer=answer,
        retrieved_chunks=[], citations=[],
        citation_required=False, artifact_registry=None,
        chunks_expected=True,
        question=_FAILING_QUERY,
    )
    check = _check_answer_non_empty(ctx)
    assert check.passed is True


# =====================================================================
# 4. Overall status gating: both must pass
# =====================================================================


def test_overall_status_not_passed_when_evidence_insufficient():
    """When check_pack reports stage-group failure, the
    aggregated validation_status surfaces a failure-style state
    — not Passed."""
    from j1.validation.checks import aggregate_status
    from j1.validation.dtos import ValidationCheckDTO

    # Mix of checks: answer non-empty passes, but the
    # synthetic "evidence sufficiency" check we add (one per
    # case) fails. ``aggregate_status`` returns failed.
    checks = [
        ValidationCheckDTO(
            name="answer_non_empty",
            severity="required",
            passed=True,
        ),
        ValidationCheckDTO(
            name="evidence_anchor_coverage_for_stage_progression",
            severity="required",
            passed=False,
            detail="missing 2 stages + estimate group",
        ),
    ]
    assert aggregate_status(checks) == "failed"


def test_overall_status_passed_only_when_both_pass():
    from j1.validation.checks import aggregate_status
    from j1.validation.dtos import ValidationCheckDTO

    checks = [
        ValidationCheckDTO(
            name="answer_non_empty",
            severity="required", passed=True,
        ),
        ValidationCheckDTO(
            name="evidence_anchor_coverage_for_stage_progression",
            severity="required", passed=True,
        ),
        ValidationCheckDTO(
            name="retrieved_chunks_present",
            severity="required", passed=True,
        ),
    ]
    assert aggregate_status(checks) == "passed"
