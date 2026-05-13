"""EvidenceSufficiencyGate tests — the gate decides whether the
synthesizer is allowed to run. The failed-question regression is
the critical one: a pack with one stage covered + missing
deliverables + missing cost-estimate-class must fail with status
``evidence_insufficient``, NOT produce a Passed validation."""

from __future__ import annotations

from j1.query.evidence_builder import EvidencePackBuilder
from j1.query.evidence_sufficiency import (
    EvidenceSufficiencyGate,
    GATE_MIN_TOTAL_BLOCKS,
    GATE_REQUIRED_GROUPS,
    GATE_RETRIEVAL_NONEMPTY,
    first_failure_reason,
)
from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalRouteKind,
)


def _cand(*, artifact_id: str, body: str) -> EvidenceCandidate:
    return EvidenceCandidate(
        route=RetrievalRouteKind.RAGANYTHING,
        artifact_id=artifact_id,
        artifact_kind="chunk",
        chunk_id=None,
        text_preview=body[:120],
        score=0.5,
        matched_anchors=(),
        run_id="run-1",
        document_id="doc-1",
        project_id="p",
        extra={"body": body},
    )


def _stage_plan():
    return QueryIntentClassifier().classify(
        "How do the deliverables evolve from conceptual engineering "
        "through 60%, 90%, and 100% design, and which cost estimate "
        "class is associated with each design stage?"
    )


# ---- Failed-question regressions -------------------------------


def test_zero_candidates_returns_retrieval_insufficient():
    plan = _stage_plan()
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    pack = builder.build(plan, [], scope_run_id="run-1")
    results, status = gate.check(plan, pack, total_candidates=0)
    assert status == "retrieval_insufficient"
    assert any(
        r.name == GATE_RETRIEVAL_NONEMPTY and not r.passed
        for r in results
    )


def test_one_stage_only_fails_required_groups():
    """The failed-question shape: only one of four stages has
    evidence + no deliverables + no estimate-class. Must fail —
    never reach synthesis."""
    plan = _stage_plan()
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    # Body mentions only the 60% stage — no anchors for any other
    # required group. Avoid words that would accidentally match the
    # deliverables / cost-estimate groups via substring scan.
    pack = builder.build(
        plan,
        [_cand(artifact_id="x",
               body="60% design submittal mentioned only.")],
        scope_run_id="run-1",
    )
    results, status = gate.check(plan, pack, total_candidates=1)
    assert status == "evidence_insufficient"
    groups_gate = next(r for r in results if r.name == GATE_REQUIRED_GROUPS)
    assert groups_gate.passed is False
    assert "missing" in groups_gate.detail
    # 90%, 100% design, conceptual engineering, deliverables,
    # estimate class — all should appear in missing.
    missing = set(groups_gate.detail["missing"])
    assert "90%" in missing
    assert "100% design" in missing
    assert "conceptual engineering" in missing
    assert "deliverables" in missing
    assert (
        "cost estimate class" in missing
        or "cost estimate" in missing
    )


def test_three_stages_plus_deliverables_plus_estimate_passes():
    """The plan's minimum is 3 required groups. Cover three
    stages + deliverables + estimate-class and the gate passes."""
    plan = _stage_plan()
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="a", body="60% design deliverables drawings."),
        _cand(artifact_id="b", body="90% design deliverables specs."),
        _cand(artifact_id="c", body="100% design deliverables final set."),
        _cand(artifact_id="d", body="cost estimate class 3 budgetary."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    results, status = gate.check(
        plan, pack, total_candidates=len(cands),
    )
    assert status == "ok"
    assert all(r.passed for r in results if r.severity == "required")


# ---- Other gate paths -----------------------------------------


def test_min_total_blocks_independent_from_groups():
    """A plan with min_total_blocks=10 and only 4 blocks must fail
    even if all required groups are covered."""
    plan = _stage_plan()
    # Tighten the policy.
    from dataclasses import replace
    plan = replace(plan, sufficiency=replace(
        plan.sufficiency, min_total_blocks=10,
    ))
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="a", body="60% design deliverables drawings."),
        _cand(artifact_id="b", body="90% design deliverables specs."),
        _cand(artifact_id="c", body="100% design deliverables final set."),
        _cand(artifact_id="d", body="cost estimate class 3 budgetary."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    results, status = gate.check(plan, pack, total_candidates=4)
    assert status == "evidence_insufficient"
    blocks_gate = next(r for r in results if r.name == GATE_MIN_TOTAL_BLOCKS)
    assert blocks_gate.passed is False


def test_first_failure_reason_returns_none_when_all_passed():
    plan = _stage_plan()
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="a", body="60% design deliverables drawings."),
        _cand(artifact_id="b", body="90% design deliverables specs."),
        _cand(artifact_id="c", body="100% design deliverables final set."),
        _cand(artifact_id="d", body="cost estimate class 3 budgetary."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    results, _ = gate.check(plan, pack, total_candidates=len(cands))
    assert first_failure_reason(results) is None


def test_first_failure_reason_returns_first_failing_required_gate():
    plan = _stage_plan()
    gate = EvidenceSufficiencyGate()
    builder = EvidencePackBuilder()
    pack = builder.build(plan, [], scope_run_id="run-1")
    results, _ = gate.check(plan, pack, total_candidates=0)
    msg = first_failure_reason(results)
    assert msg is not None
    assert "zero candidates" in msg
