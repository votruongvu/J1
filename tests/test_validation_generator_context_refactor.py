"""Coverage tests for the context-driven generator refactor.

The previous generator emitted ~3 generic-scoped cases for
document-rich runs (operators flagged it for a domain validation
packet). The refactor:
  * builds a ``ValidationQuestionContext`` from chunks + enriched
    artifacts + final report + domain pack
  * runs context-driven category emitters (workflow / entity /
    section / fact / domain)
  * applies a quality filter (no raw-chunk-title injection, no
    vague "this document"-only phrasing) + dedup pass
  * extends DTO with ``generated_from`` / ``confidence`` /
    ``reason`` / ``expected_evidence`` and new scope vocabulary

This file pins the new behaviour so the regression doesn't
silently come back the next time someone touches the generator.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.domains.models import DomainValidationGuidance
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.validation import DefaultTestCaseGenerator
from j1.validation.context import (
    ContextEntity,
    ContextFact,
    ContextSection,
    ValidationQuestionContext,
    build_question_context,
)
from j1.validation.dtos import EvidenceBlockDTO, ValidationTestCaseDTO
from j1.validation.generator import (
    GenerationOptions,
    _contains_raw_chunk_quote,
    _filter_and_dedupe_cases,
    _passes_quality,
)


# ---- Helpers -------------------------------------------------------


def _chunk(
    *, chunk_id: str, body: str,
    page_start: int | None = None,
    section: str | None = None,
    title: str | None = None,
    source_artifact_id: str | None = "art-1",
) -> _ChunkRecord:
    return _ChunkRecord(
        chunk_id=chunk_id,
        body=body,
        page_start=page_start,
        page_end=page_start,
        section=section,
        title=title,
        token_count=None,
        confidence=None,
        metadata={},
        linked_assets=[],
        source_artifact_id=source_artifact_id,
        source_document_ids=["doc-1"],
    )


def _packet_chunks() -> list[_ChunkRecord]:
    """Mini-document that exercises every context category:
    title, sections, entity candidates, workflow stages, facts."""
    return [
        _chunk(
            chunk_id="c-1",
            section="Overview",
            page_start=1,
            title="Validation Packet Overview",
            body=(
                "The Risk Assessment validates the proposed design at "
                "Stage 1 of the workflow. Stage 1 reviews drawings and "
                "calculations submitted by the engineer of record."
            ),
        ),
        _chunk(
            chunk_id="c-2",
            section="Stage 2 Review",
            page_start=3,
            body=(
                "Stage 2 of the review checks the Quality Plan and the "
                "Hazard Analysis. The Quality Plan must reference the "
                "Inspection Checklist."
            ),
        ),
        _chunk(
            chunk_id="c-3",
            section="Sign-off",
            page_start=5,
            body=(
                "The Final Sign-off is performed by the Project Manager. "
                "The Project Manager confirms the Risk Register is "
                "complete and accepted."
            ),
        ),
    ]


# ---- Context builder ----------------------------------------------


def test_context_extracts_title_from_chunk_title():
    ctx = build_question_context(chunks=_packet_chunks())
    assert ctx.document_title is not None
    assert "Validation Packet" in ctx.document_title


def test_context_extracts_facts_without_raw_chunk_injection():
    ctx = build_question_context(chunks=_packet_chunks())
    assert len(ctx.facts) >= 3
    # Every fact is a clean single sentence — no metadata blocks,
    # no pipe-separated noise.
    for fact in ctx.facts:
        assert "|" not in fact.text
        assert 25 <= len(fact.text) <= 220


def test_context_extracts_workflow_stages():
    ctx = build_question_context(chunks=_packet_chunks())
    # Stage 1 and Stage 2 should both surface.
    stage_names = " ".join(ctx.workflow_stages)
    assert "Stage" in stage_names
    assert "Stage 1" in stage_names or "Stage 2" in stage_names


def test_context_extracts_entities_from_chunks():
    ctx = build_question_context(chunks=_packet_chunks())
    names = [e.name for e in ctx.entities]
    joined = " ".join(names)
    # The domain entities should surface from the capitalised
    # noun-run scanner.
    assert "Risk" in joined or "Quality" in joined


def test_context_extracts_sections_in_order():
    ctx = build_question_context(chunks=_packet_chunks())
    titles = [s.title for s in ctx.sections]
    assert titles == ["Overview", "Stage 2 Review", "Sign-off"]


def test_context_uses_final_report_for_title_when_present():
    """``final_ingestion_report.document_name`` wins over chunk
    title heuristics — it's the authoritative source."""
    ctx = build_question_context(
        chunks=_packet_chunks(),
        final_report={"document_name": "the-authoritative-title.pdf"},
    )
    assert ctx.document_title == "the-authoritative-title"


def test_context_empty_when_no_inputs():
    ctx = build_question_context(chunks=[])
    assert ctx.has_any_facts() is False


# ---- Generator behaviour pinned --------------------------------------


def test_generator_emits_minimum_count_for_rich_context():
    """For a document-rich set of chunks (3 sections, 3 stages,
    several entities, several facts), the generator should emit
    well above the prior ~3 cases. Operator-stated target was
    10–15; we assert >= 8 to leave slack for the de-duper
    catching near-duplicates without making the test brittle."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=20, negative_case_count=0),
    )
    assert len(vset.test_cases) >= 8


def test_generator_no_longer_emits_all_generic_scope():
    """Spec rule 7: ``generic`` is the exception, not the default.
    A rich document context should produce cases across multiple
    scopes (``document``, ``workflow``, ``evidence``,
    ``retrieval``, possibly ``graph``)."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=20, negative_case_count=0),
    )
    scopes = {c.validation_scope for c in vset.test_cases}
    assert "generic" not in scopes or len(scopes) > 1, (
        f"all cases stamped scope=generic — context-driven "
        f"emitters didn't fire. Got: {scopes}"
    )
    # At least 3 distinct scopes for a doc this rich.
    assert len(scopes) >= 3, f"too few scopes covered: {scopes}"


def test_generator_emits_no_raw_chunk_quote_injection():
    """Spec rule 3 + section 12: question must not embed long
    raw chunk text inside quotes. The prior heuristic emitted
    ``What does the document say about "<long body>"?``; the
    new flow must never produce these."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=20, negative_case_count=0),
    )
    for case in vset.test_cases:
        assert not _contains_raw_chunk_quote(case.question), (
            f"raw chunk injected into question: {case.question!r}"
        )


def test_generator_covers_multiple_categories():
    """At least four of {workflow, entity, fact, section, smoke,
    domain} categories should be represented in the output for a
    document-rich set."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=20, negative_case_count=0),
    )
    sources = {c.generated_from for c in vset.test_cases if c.generated_from}
    expected = {"smoke", "workflow_stage", "entity", "section", "fact"}
    assert len(sources & expected) >= 4, (
        f"category coverage too narrow: got {sources}, expected at "
        f"least 4 of {expected}"
    )


def test_generator_no_longer_emits_smoke_case():
    """Quality-first refactor (post operator feedback): the smoke
    case is GONE. Operators flagged "What is this document
    about?" as a low-value generic question. The generator now
    relies entirely on content-driven cases — no boilerplate
    smoke is added regardless of how rich the document is."""
    gen = DefaultTestCaseGenerator()
    chunks = _packet_chunks()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(max_cases=10, negative_case_count=0),
    )
    smoke_cases = [c for c in vset.test_cases if c.metadata.get("smoke")]
    assert smoke_cases == []
    smoke_provenance = [
        c for c in vset.test_cases if c.generated_from == "smoke"
    ]
    assert smoke_provenance == []


def test_generator_stamps_provenance_fields_on_every_case():
    """Every emitted case must carry ``generated_from`` so
    operators can audit ``where did this question come from?``
    without re-running the generator. Confidence + reason are
    optional but should be present on the context-driven cases."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=15, negative_case_count=0),
    )
    for case in vset.test_cases:
        assert case.generated_from is not None, (
            f"case missing generated_from: {case.test_case_id} "
            f"({case.question!r})"
        )


def test_generator_works_without_llm_using_context():
    """Spec rule 13: fallback must use document context, not
    generic templates. With no LLM wired the context-driven
    emitters still produce a useful document-specific set."""
    gen = DefaultTestCaseGenerator(text_client=None)
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=15, negative_case_count=0),
    )
    # Trace now explicitly reports ``no_llm_client_wired`` instead
    # of being None — see ``DefaultTestCaseGenerator._llm_generate_grounded_cases``.
    assert vset.llm is not None
    assert vset.llm.called is False
    assert vset.llm.error == "no_llm_client_wired"
    assert len(vset.test_cases) >= 5
    # Every case (apart from smoke) references doc-derived content
    # (Stage / Risk / Quality / Sign-off / Manager).
    document_terms = {
        "Stage", "Risk", "Quality", "Sign-off", "Manager", "Plan",
        "Hazard", "Inspection", "Project",
    }
    non_smoke = [c for c in vset.test_cases if not c.metadata.get("smoke")]
    matched = sum(
        1 for c in non_smoke
        if any(term in c.question for term in document_terms)
    )
    assert matched >= 1, (
        f"none of the {len(non_smoke)} non-smoke cases reference "
        f"document-specific terms"
    )


def test_generator_domain_aware_when_pack_supplied():
    """When a domain pack is wired with ``important_fields``, the
    generator emits at least one ``domain``-scoped case per
    field (capped at 2)."""
    gen = DefaultTestCaseGenerator()
    guidance = DomainValidationGuidance(
        important_fields=("inspection_checklist", "risk_register"),
        negative_check_fields=(),
    )
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"],
        chunks=_packet_chunks(),
        options=GenerationOptions(max_cases=20, negative_case_count=0),
        domain_guidance=guidance,
        domain_id="testdomain",
    )
    domain_cases = [
        c for c in vset.test_cases if c.validation_scope == "domain"
    ]
    assert len(domain_cases) >= 1
    # No domain trivia — domain field names should be reflected.
    joined = " ".join(c.question for c in domain_cases).lower()
    assert "inspection" in joined or "risk register" in joined


def test_generator_dedupes_near_duplicate_cases():
    """Two cases whose normalised question text matches should
    collapse to one. The dedup is what prevents the prior
    "What is this document about?" / "What does the document say
    about this document?" near-cycle output."""
    duplicate_chunks = [
        _chunk(
            chunk_id="c-1", body="Sentence about widgets and gizmos.",
            page_start=1,
        ),
        _chunk(
            chunk_id="c-2", body="Sentence about widgets and gizmos.",
            page_start=2,
        ),
    ]
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r-1", document_ids=["doc-1"], chunks=duplicate_chunks,
        options=GenerationOptions(max_cases=15, negative_case_count=0),
    )
    seen_questions: set[str] = set()
    for case in vset.test_cases:
        norm = case.question.lower().strip().rstrip("?.!").rstrip()
        assert norm not in seen_questions, (
            f"duplicate question survived dedup: {case.question!r}"
        )
        seen_questions.add(norm)


# ---- Quality filter unit tests --------------------------------------


def _case(
    *, question: str, scope: str = "document",
    metadata: dict | None = None,
) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id="tc-test",
        question=question,
        type="retrieval",
        priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope=scope,  # type: ignore[arg-type]
        metadata=metadata or {},
    )


def test_filter_drops_question_with_long_quoted_chunk():
    """A question that quotes a 100+ char chunk fragment is the
    raw-chunk-injection signature and must be dropped."""
    long = "x" * 120
    case = _case(question=f"What does the document say about {long!r}?")
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_filter_drops_vague_this_document_only_question():
    case = _case(question="In this document?")
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_filter_keeps_evidence_anchored_question_with_this_document_ref():
    """Quality-first refactor: ``this document`` is allowed only
    when the case carries a real ``expected_answer`` +
    ``expected_evidence`` (the post-emit gate requires both for
    non-guardrail cases). Section-anchored questions with proper
    evidence stamping pass."""
    case = _case(
        question="What does the section 'Risk Register' in this document cover?",
        scope="workflow",
    )
    # Add the required evidence + answer fields the new gate
    # demands of every non-guardrail case.
    from dataclasses import replace
    case = replace(
        case,
        expected_answer="The Risk Register describes …",
        expected_evidence="page 5, section 'Risk Register'",
    )
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is True


def test_filter_rejects_terse_smoke_case():
    """Quality-first refactor: smoke cases are NO LONGER exempt —
    they're not emitted at all. Even when a caller passes
    ``metadata.smoke=True`` the case must satisfy the same
    expected_answer + expected_evidence + non-generic-scope
    gates as any other positive case."""
    case = _case(
        question="What?",
        metadata={"smoke": True},
    )
    ctx = ValidationQuestionContext()
    # Missing expected_answer + expected_evidence; gate must
    # reject. Smoke-flag does not exempt anymore.
    assert _passes_quality(case, context=ctx) is False


def test_filter_keeps_guardrail_cases():
    case = _case(
        question="x", scope="guardrail",
    )
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is True


def test_dedup_collapses_normalised_duplicates():
    """Three case objects whose normalised question text matches
    each other should collapse to one — regardless of what the
    quality filter does. We bypass the gate by giving each case
    legitimate fields (the quality filter would otherwise reject
    them for being vague)."""
    from dataclasses import replace
    a = _case(question="What does the document say about Stage 1?")
    b = _case(question="What does the document say about Stage 1")
    c = _case(question="WHAT DOES THE DOCUMENT SAY ABOUT STAGE 1?!")
    fields = {
        "expected_answer": "Stage 1 reviews drawings.",
        "expected_evidence": "page 1, section 'Overview'",
    }
    a = replace(a, **fields)
    b = replace(b, **fields)
    c = replace(c, **fields)
    ctx = ValidationQuestionContext()
    out = _filter_and_dedupe_cases([a, b, c], context=ctx)
    assert len(out) == 1
    assert out[0] is a


def test_contains_raw_chunk_quote_predicate():
    # Short quoted section reference is fine.
    assert _contains_raw_chunk_quote(
        "What does the section 'Risk Register' cover?"
    ) is False
    # Long body-injected quote is not.
    long = "y" * 100
    assert _contains_raw_chunk_quote(
        f"What does the document say about {long!r}?"
    ) is True
    # Pipe-separated metadata block is not (even when short).
    assert _contains_raw_chunk_quote(
        "What does this document say about 'Doc ID: X | Version: 1.0 | Date: …'?"
    ) is True
