"""Regression tests for ``j1.query.intent_classifier``.

The classifier is the single source of truth for "what is this
question asking" — every downstream stage reads its output. The
tests below lock in the contract for the failed manual-test
question PLUS the basic shape for each other intent."""

from __future__ import annotations

from j1.query.domain_profile import (
    DomainProfile,
    FieldVocabulary,
    StageVocabulary,
)
from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.query_plan import (
    AnswerShape,
    Intent,
    RetrievalRouteKind,
    SynthesisMode,
)


_FAILED_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Stage-progression contract (the failed manual-test query) ----


def test_stage_progression_intent_detected():
    """The exact failed query must classify as stage_progression
    with high confidence. Any drift here means the orchestrator
    won't apply the stage-aware retrieval policy and the original
    failure mode returns."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    assert plan.intent == Intent.STAGE_PROGRESSION
    assert plan.intent_confidence >= 0.85


def test_stage_progression_anchors_match_user_stages():
    """Anchors come from the query text — never a hardcoded list.
    The four stages the user wrote must all land in plan.anchors,
    no extras (estimate-class / deliverables are FIELDS, not stages)."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    anchors_lower = {a.lower() for a in plan.anchors}
    assert "60%" in anchors_lower
    assert "90%" in anchors_lower
    assert "100% design" in anchors_lower
    assert "conceptual engineering" in anchors_lower
    # Fields must NOT be anchors.
    assert "cost estimate class" not in anchors_lower
    assert "deliverables" not in anchors_lower


def test_stage_progression_requested_fields_contain_deliverables_and_estimate():
    """Both columns the question asks for must surface as
    requested_fields — the synthesizer reads this list to build
    the table headers and the quality gate checks coverage against
    it."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    fields_lower = {f.lower() for f in plan.requested_fields}
    assert "deliverables" in fields_lower
    # Either "cost estimate class" (the precise marker) or
    # "cost estimate" (the progression-term form) is acceptable —
    # the binder treats them as the same column.
    assert (
        "cost estimate class" in fields_lower
        or "cost estimate" in fields_lower
    )


def test_stage_progression_answer_shape_is_table():
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    assert plan.answer_shape == AnswerShape.STAGE_BY_STAGE_TABLE


def test_stage_progression_synthesis_mode_extract_only_grade():
    """STAGE_PROGRESSION must NOT use SYNTHESIZE — the LLM may not
    invent missing rows / classes. PROJECT_STRUCTURED is the
    extract-only project mode."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    assert plan.synthesis_mode == SynthesisMode.PROJECT_STRUCTURED


def test_stage_progression_retrieval_uses_more_than_topk():
    """The failed query failed in part because retrieval was a
    single top-K call. The plan must dispatch RAGAnything PLUS
    BM25 lexical anchors so exact-phrase recall ("60% design")
    catches chunks the semantic retriever missed."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    routes = {j.route for j in plan.retrieval_jobs}
    assert RetrievalRouteKind.RAGANYTHING in routes
    assert RetrievalRouteKind.BM25 in routes
    bm25_queries = [
        j.query for j in plan.retrieval_jobs
        if j.route == RetrievalRouteKind.BM25
    ]
    # At least one BM25 job per stage anchor (4 stages = ≥4 jobs).
    assert len(bm25_queries) >= 4


def test_stage_progression_required_groups_cover_stages_and_fields():
    """The sufficiency gate fails unless each required group has
    evidence. For stage_progression we require ONE group per stage
    AND one per requested field."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    group_names = {g.name.lower() for g in plan.required_groups}
    # All four stages present.
    assert "60%" in group_names
    assert "90%" in group_names
    assert "100% design" in group_names
    assert "conceptual engineering" in group_names
    # Field groups present.
    assert "deliverables" in group_names
    # Estimate column landed under either canonical name.
    assert (
        "cost estimate class" in group_names
        or "cost estimate" in group_names
    )


def test_stage_progression_sufficiency_requires_three_stages():
    """The failed-question spec calls for at least 3 of the
    requested stage groups before synthesis can run."""
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    assert plan.sufficiency.min_required_groups >= 3
    assert plan.sufficiency.fail_when_no_candidates is True


def test_stage_progression_quality_fails_on_refusal():
    plan = QueryIntentClassifier().classify(_FAILED_QUERY)
    assert plan.quality.fail_on_refusal is True
    assert plan.quality.answer_shape == AnswerShape.STAGE_BY_STAGE_TABLE


# ---- Other intents ------------------------------------------------


def test_summary_intent():
    plan = QueryIntentClassifier().classify("Summarize the document.")
    assert plan.intent == Intent.SUMMARY
    assert plan.answer_shape == AnswerShape.PARAGRAPH


def test_single_fact_intent():
    plan = QueryIntentClassifier().classify("What is the project budget?")
    assert plan.intent == Intent.SINGLE_FACT
    assert plan.answer_shape == AnswerShape.SHORT_FACT


def test_requirement_extraction_intent():
    plan = QueryIntentClassifier().classify(
        "List all the requirements in the spec.",
    )
    assert plan.intent == Intent.REQUIREMENT_EXTRACTION
    routes = {j.route for j in plan.retrieval_jobs}
    assert RetrievalRouteKind.ARTIFACT_LOOKUP in routes
    artifact_filters = [
        j.filters.get("artifact_kind")
        for j in plan.retrieval_jobs
        if j.route == RetrievalRouteKind.ARTIFACT_LOOKUP
    ]
    assert "enriched.requirements" in artifact_filters


def test_risk_summary_intent():
    plan = QueryIntentClassifier().classify(
        "Which risks are mentioned in section 4?",
    )
    assert plan.intent == Intent.RISK_SUMMARY
    artifact_filters = [
        j.filters.get("artifact_kind")
        for j in plan.retrieval_jobs
        if j.route == RetrievalRouteKind.ARTIFACT_LOOKUP
    ]
    assert "enriched.risks" in artifact_filters


def test_consistency_check_intent():
    plan = QueryIntentClassifier().classify(
        "Are there any inconsistencies between the spec and the plan?",
    )
    assert plan.intent == Intent.CONSISTENCY_CHECK


def test_scope_question_intent():
    plan = QueryIntentClassifier().classify(
        "What is in scope for phase 2?",
    )
    assert plan.intent == Intent.SCOPE_QUESTION


def test_citation_lookup_intent_does_not_fail_on_refusal():
    """``"not found"`` is a valid answer for citation lookups, so
    the quality gate must NOT treat refusals as failures here."""
    plan = QueryIntentClassifier().classify(
        "Where does it say 60% design submittal?",
    )
    assert plan.intent == Intent.CITATION_LOOKUP
    assert plan.quality.fail_on_refusal is False


def test_comparison_intent():
    plan = QueryIntentClassifier().classify(
        "Compare section 3 and section 5 of the report.",
    )
    assert plan.intent == Intent.COMPARISON
    assert plan.answer_shape == AnswerShape.SIDE_BY_SIDE_TABLE


def test_deliverable_matrix_intent():
    plan = QueryIntentClassifier().classify(
        "Give me a deliverable matrix by phase by party.",
    )
    assert plan.intent == Intent.DELIVERABLE_MATRIX


def test_unknown_intent_lowers_confidence():
    """An unrecognised question still returns a plan — confidence
    is low so the LLM-planner branch (when wired) can pick up."""
    plan = QueryIntentClassifier().classify("xyzzy plugh!")
    assert plan.intent == Intent.UNKNOWN
    assert plan.intent_confidence <= 0.3


# ---- Domain profile integration ----------------------------------


def test_domain_profile_resolves_stage_canonical_names():
    """A domain profile can supply alias → canonical mappings so
    the orchestrator groups "60% design" and "60 percent design"
    into the same bucket."""
    profile = DomainProfile(
        domain_id="example",
        stages=(
            StageVocabulary(
                canonical="design_60",
                aliases=("60%", "60 percent design", "60% design"),
            ),
        ),
    )
    plan = QueryIntentClassifier().classify(
        "How do deliverables evolve from 60% design to 100% design?",
        profile=profile,
    )
    # The 60% surface form should be normalised to the canonical
    # name; "100% design" has no profile entry so it keeps its
    # raw surface form.
    assert "design_60" in plan.anchors


def test_domain_profile_resolves_field_canonical_names():
    profile = DomainProfile(
        domain_id="example",
        fields=(
            FieldVocabulary(
                canonical="deliverables",
                aliases=("submittals", "outputs"),
            ),
        ),
    )
    plan = QueryIntentClassifier().classify(
        "What are the submittals at the final review?",
        profile=profile,
    )
    fields_lower = {f.lower() for f in plan.requested_fields}
    assert "deliverables" in fields_lower
