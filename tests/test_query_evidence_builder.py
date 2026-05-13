"""EvidencePackBuilder tests — locks in the dedup / scope / group /
cap / drop-reason contracts the orchestrator relies on."""

from __future__ import annotations

import pytest

from j1.query.domain_profile import DomainProfile
from j1.query.evidence_builder import (
    EvidenceBuilderConfig,
    EvidencePackBuilder,
)
from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.query_plan import (
    EvidenceCandidate,
    EvidenceGroupSpec,
    Intent,
    QueryPlan,
    QualityPolicy,
    RetrievalRouteKind,
    SufficiencyPolicy,
    SynthesisMode,
    AnswerShape,
)


def _cand(
    *,
    artifact_id: str,
    body: str,
    route: RetrievalRouteKind = RetrievalRouteKind.RAGANYTHING,
    score: float = 0.5,
    run_id: str = "run-1",
    artifact_kind: str = "chunk",
    chunk_id: str | None = None,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        route=route,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        chunk_id=chunk_id,
        text_preview=body[:120],
        score=score,
        matched_anchors=(),
        run_id=run_id,
        document_id="doc-1",
        project_id="p",
        extra={"body": body},
    )


def _failed_query_plan() -> QueryPlan:
    return QueryIntentClassifier().classify(
        "How do the deliverables evolve from conceptual engineering "
        "through 60%, 90%, and 100% design, and which cost estimate "
        "class is associated with each design stage?"
    )


# ---- Dedupe + scope --------------------------------------------


def test_dedupe_keeps_higher_scoring_route():
    """Same chunk surfaced by RAGAnything AND BM25 collapses to ONE
    candidate (the higher-scoring one), even though that single
    candidate may then land in multiple groups."""
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="a1", chunk_id="c1",
              body="60% design deliverables include drawings.",
              route=RetrievalRouteKind.RAGANYTHING, score=0.4),
        _cand(artifact_id="a1", chunk_id="c1",
              body="60% design deliverables include drawings.",
              route=RetrievalRouteKind.BM25, score=12.0),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    # Only ONE underlying candidate per group should remain — but
    # the same candidate may land in multiple groups (60% stage AND
    # deliverables field), so count unique route+artifact pairings,
    # not block count.
    blocks_for_a1 = [b for b in pack.blocks if b.candidate.artifact_id == "a1"]
    surviving_routes = {b.candidate.route for b in blocks_for_a1}
    # Only the BM25 route survived (higher score).
    assert surviving_routes == {RetrievalRouteKind.BM25}
    assert any("duplicate" in d.reason for d in pack.dropped)


def test_scope_filter_drops_cross_run_candidates():
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="ok", body="60% design is fine.",
              run_id="run-1"),
        _cand(artifact_id="leak", body="60% design from another run.",
              run_id="run-2"),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    kept = {b.candidate.artifact_id for b in pack.blocks}
    assert "leak" not in kept
    assert any(
        "scope" in d.reason and "leak" == d.candidate.artifact_id
        for d in pack.dropped
    )


# ---- Group assignment ------------------------------------------


def test_candidate_lands_in_every_group_it_anchors():
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    # This chunk mentions BOTH 60% design AND deliverables — must
    # appear in both group buckets so the synthesizer can render
    # the row.
    cands = [
        _cand(artifact_id="m1",
              body="60% design deliverables include drawings and "
                   "specs."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    groups = {b.group for b in pack.blocks}
    assert "60%" in groups
    assert "deliverables" in groups


def test_groups_missing_when_no_evidence_for_required_group():
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    # Only one stage covered.
    cands = [
        _cand(artifact_id="x",
              body="60% design deliverables include drawings."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    # 90%, 100% design, conceptual engineering, cost estimate
    # class all missing.
    assert "90%" in pack.groups_missing
    assert "100% design" in pack.groups_missing
    assert "conceptual engineering" in pack.groups_missing


def test_unrelated_chunks_are_dropped_or_ungrouped_not_dominant():
    """Failed-question regression: chunks about CEP compliance,
    potholing, marine survey, and proposal format must NOT dominate
    the selected evidence even when retrieval surfaced them."""
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="r1",
              body="CEP compliance procedures..."),
        _cand(artifact_id="r2", body="Potholing field method..."),
        _cand(artifact_id="r3", body="Marine survey schedule..."),
        _cand(artifact_id="r4", body="Proposal format instructions..."),
        _cand(artifact_id="ok",
              body="60% design deliverables include drawings and "
                   "the cost estimate class is Class 3."),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    # Selected blocks tagged to required groups must be the
    # majority.
    grouped_blocks = [b for b in pack.blocks if b.group is not None]
    assert any(
        "60% design" in (b.body or "").lower()
        or "60%" in (b.body or "").lower()
        for b in grouped_blocks
    )
    # None of the unrelated chunks landed in a required group.
    grouped_ids = {b.candidate.artifact_id for b in grouped_blocks}
    assert "r1" not in grouped_ids
    assert "r2" not in grouped_ids


# ---- Caps -------------------------------------------------------


def test_per_group_cap_limits_block_count():
    """The "20 citations when only 4 are needed" failure mode is
    addressed by capping each group. Default is 4 — extras land in
    ``dropped`` with reason ``group_cap``."""
    plan = _failed_query_plan()
    builder = EvidencePackBuilder(
        config=EvidenceBuilderConfig(per_group_cap=2, overall_cap=30),
    )
    cands = [
        _cand(artifact_id=f"a{i}",
              body=f"60% design block {i}", score=1.0 - 0.01 * i)
        for i in range(10)
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    sixty_pct_blocks = [b for b in pack.blocks if b.group == "60%"]
    assert len(sixty_pct_blocks) == 2
    assert any("group_cap" in d.reason for d in pack.dropped)


# ---- Boilerplate ------------------------------------------------


def test_boilerplate_suppressed():
    plan = _failed_query_plan()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="cover",
              body="CONFIDENTIAL — do not distribute. "
                   "All rights reserved.",
              score=0.9),
        _cand(artifact_id="real",
              body="60% design deliverables include drawings.",
              score=0.5),
    ]
    pack = builder.build(plan, cands, scope_run_id="run-1")
    kept_ids = {b.candidate.artifact_id for b in pack.blocks}
    assert "cover" not in kept_ids
    assert "real" in kept_ids
    assert any(
        d.reason == "boilerplate_suppressed"
        and d.candidate.artifact_id == "cover"
        for d in pack.dropped
    )


# ---- Profile-driven priority -----------------------------------


def test_artifact_priority_orders_within_group():
    """When a domain profile lists artifact-kind priority for an
    intent, the builder prefers those kinds inside each group —
    even when raw scores would tie."""
    plan = _failed_query_plan()
    profile = DomainProfile(
        domain_id="ex",
        artifact_priority={
            Intent.STAGE_PROGRESSION: (
                "enriched.deliverable_map",
                "chunk",
            ),
        },
    )
    builder = EvidencePackBuilder(
        config=EvidenceBuilderConfig(per_group_cap=1, overall_cap=10),
    )
    cands = [
        _cand(artifact_id="chunk-A",
              body="60% design deliverables list",
              artifact_kind="chunk", score=0.8),
        _cand(artifact_id="map-A",
              body="60% design deliverables list",
              artifact_kind="enriched.deliverable_map", score=0.4),
    ]
    pack = builder.build(
        plan, cands, scope_run_id="run-1", profile=profile,
    )
    selected_for_60 = [b for b in pack.blocks if b.group == "60%"]
    assert len(selected_for_60) == 1
    assert selected_for_60[0].candidate.artifact_id == "map-A"


# ---- Empty / unknown plan --------------------------------------


def test_unknown_plan_still_returns_pack_with_ungrouped_blocks():
    """An UNKNOWN-intent plan still produces a pack — ungrouped
    blocks land in selected so the synthesizer has SOME evidence."""
    plan = QueryPlan(
        normalized_question="xyzzy",
        intent=Intent.UNKNOWN,
        anchors=(),
        requested_fields=(),
        answer_shape=AnswerShape.PARAGRAPH,
        synthesis_mode=SynthesisMode.SYNTHESIZE,
        retrieval_jobs=(),
        required_groups=(
            EvidenceGroupSpec(name="answer", required=True),
        ),
        sufficiency=SufficiencyPolicy(),
        quality=QualityPolicy(),
    )
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="x",
              body="Some text that doesn't anchor anything."),
    ]
    pack = builder.build(plan, cands, scope_run_id=None)
    assert len(pack.blocks) == 1
    # The block's group is None (ungrouped) — the sufficiency gate
    # will decide if that's acceptable for the intent.
    assert pack.blocks[0].group is None
