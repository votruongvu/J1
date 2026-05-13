"""Quality-first refactor tests.

Pins the post-feedback policy:
  * NO smoke / boilerplate cases
  * NO minimum case count / padding
  * Each operator-flagged bad question shape is rejected
  * Noise-term entities (PDF, The, Expected, …) never become topics
  * Answer-in-question shapes are rejected
  * Every positive case carries ``expected_answer`` + ``expected_evidence``
"""

from __future__ import annotations

import pytest

from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation import DefaultTestCaseGenerator
from j1.validation.context import (
    NOISE_TERMS,
    _clean_entity_name,
    _looks_like_identifier,
)
from j1.validation.dtos import ValidationTestCaseDTO
from j1.validation.generator import (
    GenerationOptions,
    _is_acceptable_topic,
    _is_clean_graph_entity,
    _looks_like_answer_in_question,
    _passes_quality,
    _topic_from_fact,
)


# ---- Helpers -------------------------------------------------------


def _chunk(
    *, chunk_id: str, body: str,
    page_start: int | None = None,
    section: str | None = None,
) -> _ChunkRecord:
    return _ChunkRecord(
        chunk_id=chunk_id,
        body=body,
        page_start=page_start,
        page_end=page_start,
        section=section,
        title=None,
        token_count=None,
        confidence=None,
        metadata={},
        linked_assets=[],
        source_artifact_id="art-1",
        source_document_ids=["doc-1"],
    )


def _case(
    *, question: str,
    scope: str = "evidence",
    expected_answer: str | None = "default expected answer",
    expected_evidence: str | None = "page 1",
    metadata: dict | None = None,
) -> ValidationTestCaseDTO:
    return ValidationTestCaseDTO(
        test_case_id="tc-test",
        question=question,
        type="retrieval",
        priority="normal",
        expected_behavior="answer_with_citations",
        validation_scope=scope,  # type: ignore[arg-type]
        expected_answer=expected_answer,
        expected_evidence=expected_evidence,
        metadata=metadata or {},
    )


# ---- Each operator-flagged bad question must be rejected -----------


def test_question_what_is_this_document_about_is_never_emitted():
    """Operator-flagged: "What is this document about?" The
    smoke case was the source; it has been removed entirely.
    The quality gate ALSO rejects the bare phrasing when
    nothing else is constructed."""
    case = _case(
        question="What is this document about?",
        scope="document",
        expected_answer=None,  # smoke had no answer
        expected_evidence=None,
    )
    from j1.validation.context import ValidationQuestionContext
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_question_with_noisy_entity_pdf_is_rejected():
    """Operator-flagged: "What is the role of PDF in the document?"
    The context extractor MUST reject ``PDF`` as a candidate
    entity, and the post-emit filter is a second line of
    defense."""
    assert _clean_entity_name("PDF") is None
    case = _case(
        question="What is the role of PDF in the document?",
        scope="retrieval",
    )
    from j1.validation.context import ValidationQuestionContext
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_question_with_noisy_entity_expected_is_rejected():
    """Operator-flagged: "What is the role of Expected in the
    document?" — same noise filter."""
    assert _clean_entity_name("Expected") is None
    case = _case(
        question="What is the role of Expected in the document?",
        scope="retrieval",
    )
    from j1.validation.context import ValidationQuestionContext
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_question_with_noisy_entity_the_is_rejected():
    """Operator-flagged: "What does the document say about The?"
    "The" is a determiner that was leaking through as a
    capitalised noun-run topic."""
    assert _clean_entity_name("The") is None
    case = _case(
        question="What does the document say about The?",
        scope="retrieval",
    )
    from j1.validation.context import ValidationQuestionContext
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_answer_in_question_shape_is_rejected():
    """Operator-flagged: "What is the project ID of
    J1-CE-TEST-0426?" — the identifier IS the answer."""
    assert _looks_like_answer_in_question(
        "What is the project ID of J1-CE-TEST-0426?"
    ) is True
    case = _case(
        question="What is the project ID of J1-CE-TEST-0426?",
        scope="evidence",
    )
    from j1.validation.context import ValidationQuestionContext
    ctx = ValidationQuestionContext()
    assert _passes_quality(case, context=ctx) is False


def test_relationship_between_noise_entities_is_rejected():
    """Operator-flagged: "What is the relationship between CB-2 and
    PDF in this document?" Identifier (``CB-2``) and noise
    (``PDF``) both fail the graph-entity cleanliness check."""
    assert _is_clean_graph_entity("CB-2") is False
    assert _is_clean_graph_entity("PDF") is False


# ---- Identifier / topic extractor unit tests -----------------------


def test_clean_entity_name_rejects_all_noise_terms():
    """Every term in the canonical noise list must be rejected as
    a bare entity candidate so it can't surface as a question
    topic."""
    for noisy in ("PDF", "The", "Expected", "Document", "Page", "ID"):
        assert _clean_entity_name(noisy) is None, (
            f"noise term {noisy!r} should be rejected"
        )


def test_clean_entity_name_accepts_real_proper_nouns():
    """Real proper nouns must pass — the filter is conservative
    against noise, not against legitimate short names."""
    for name in ("Bob", "Alice", "DOT", "Stage 1 Risk Assessment"):
        assert _clean_entity_name(name) is not None, (
            f"legitimate name {name!r} should pass"
        )


def test_looks_like_identifier_catches_dashed_ids():
    """``J1-CE-TEST-0426``, ``DOT-2024``, ``CB-2`` are answers,
    not topics."""
    for ident in ("J1-CE-TEST-0426", "DOT-2024", "CB-2", "FOO-BAR-BAZ"):
        assert _looks_like_identifier(ident) is True, (
            f"identifier {ident!r} should be detected"
        )


def test_looks_like_identifier_does_not_flag_normal_phrases():
    for phrase in ("Stage 1", "Risk Register", "Quality Plan"):
        assert _looks_like_identifier(phrase) is False


def test_topic_from_fact_skips_leading_determiner():
    """``The proposal documents the Risk Assessment workflow.``
    used to yield topic="The" because "The" matches the first
    capitalised noun-run. The new extractor walks the matches
    and rejects noise-list candidates, returning the next
    acceptable one ("Risk Assessment workflow")."""
    text = "The proposal documents the Risk Assessment workflow."
    topic = _topic_from_fact(text)
    assert topic is not None
    assert topic.lower() != "the"
    assert "risk" in topic.lower() or "proposal" in topic.lower()


def test_topic_from_fact_returns_none_when_only_noise():
    """Fact whose only capitalised tokens are noise → no topic
    extractable → the fact emitter SKIPS the fact entirely."""
    text = "The PDF includes Expected results across all pages of the document."
    topic = _topic_from_fact(text)
    # All capitalised tokens in this sentence are noise terms.
    assert topic is None


def test_is_acceptable_topic_rejects_identifier_topic():
    assert _is_acceptable_topic("J1-CE-TEST-0426", "irrelevant") is False


def test_is_acceptable_topic_rejects_full_fact_body():
    """Defence against raw-chunk-injection: a "topic" equal to
    the entire fact body would re-inject text."""
    fact = "The proposal is due 20 May 2026."
    # Whole-body candidate as topic.
    assert _is_acceptable_topic(fact.rstrip("."), fact) is False


# ---- End-to-end: quality > quantity --------------------------------


def test_generator_produces_zero_cases_for_sparse_document():
    """A chunk corpus too sparse to surface clean topics yields
    ZERO cases. The generator never pads to a minimum — the
    operator's complaint was that padding produced low-value
    questions."""
    gen = DefaultTestCaseGenerator()
    # Bodies are too short to pass ``_is_useful_sentence``.
    chunks = [
        _chunk(chunk_id="c-1", body="A short header."),
        _chunk(chunk_id="c-2", body="The end."),
    ]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
    )
    assert vset.test_cases == []


def test_generator_produces_few_strong_cases_for_small_document():
    """Small but useful document → small but useful set. NO
    padding to reach a 10-question minimum."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(
        chunk_id="c-1",
        section="Overview",
        page_start=1,
        body=(
            "The Risk Assessment is performed at Stage 1. "
            "Stage 1 reviews drawings, calculations, and the Quality Plan "
            "submitted by the engineer of record."
        ),
    )]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(max_cases=12, negative_case_count=0),
    )
    # Small doc → small set, not 10+. Operator-stated bound: 3-8
    # is the typical range for a single-chunk doc.
    assert 1 <= len(vset.test_cases) <= 8
    # Every case carries the required evidence + answer fields.
    for case in vset.test_cases:
        if case.validation_scope in {"guardrail", "negative_check"}:
            continue
        assert case.expected_answer, (
            f"missing expected_answer: {case.question!r}"
        )
        assert case.expected_evidence, (
            f"missing expected_evidence: {case.question!r}"
        )
        # Scope must be specific.
        assert case.validation_scope != "generic"


def test_generator_caps_at_max_cases_but_never_pads_below():
    """``max_cases`` is the CEILING, not a target. The generator
    emits as many cases as the document supports, up to the
    ceiling — never above, never artificially below."""
    gen = DefaultTestCaseGenerator()
    # Rich-ish corpus.
    chunks = [
        _chunk(
            chunk_id=f"c-{i}",
            section=f"Section {i}",
            page_start=i,
            body=(
                f"The Section {i} describes the Risk Assessment workflow. "
                f"Stage {i} reviews drawings and calculations "
                f"submitted by the engineer of record."
            ),
        )
        for i in range(1, 5)
    ]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(max_cases=3, negative_case_count=0),
    )
    assert len(vset.test_cases) <= 3


def test_no_case_has_noise_term_as_topic_anchor():
    """End-to-end: across a rich-ish corpus the generator must
    NEVER ship a case whose topic anchor (the X in "about X" /
    "of X") is on the noise list."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(
        chunk_id="c-1",
        section="Inventory",
        page_start=1,
        body=(
            "The PDF document captures the Inventory Plan and the "
            "Quality Checklist submitted to the auditor. Expected "
            "results are documented in Appendix B."
        ),
    )]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
    )
    for case in vset.test_cases:
        lowered = case.question.lower()
        for noisy in ("about pdf", "about the?", "about expected", "of pdf"):
            assert noisy not in lowered, (
                f"case shipped with noise anchor: {case.question!r}"
            )


def test_llm_is_always_called_when_evidence_supplied():
    """Regression: prior ordering ran the context emitters BEFORE
    the LLM, so a rich document could fill the budget with
    context-derived cases and the LLM never got called. The new
    order is LLM-first: every run with a wired LLM + non-empty
    evidence_blocks must produce an LLM trace with ``called=True``.

    The LLM is the highest-signal source; the context emitters
    are the deterministic fallback for slots the LLM didn't fill.
    """
    from j1.validation.dtos import EvidenceBlockDTO

    class _StubLLM:
        provider = "stub"
        model = "stub-1"

        def __init__(self) -> None:
            self.calls: list = []

        def extract(self, prompt, schema, *, metadata=None):
            self.calls.append((prompt, schema))
            return ({"test_cases": [
                {
                    "question": "What does the document say about the workflow?",
                    "expected_answer": "It validates the proposal.",
                    "question_type": "fact_retrieval",
                    "validation_scope": "evidence",
                    "evidence": [
                        {"source_artifact_id": "art-1", "quote": "workflow"},
                    ],
                },
            ]}, object())

    stub = _StubLLM()
    gen = DefaultTestCaseGenerator(text_client=stub)
    # Rich corpus that would fill the budget via context alone.
    chunks = [
        _chunk(
            chunk_id=f"c-{i}",
            section=f"Section {i}",
            page_start=i,
            body=(
                f"The Section {i} describes the workflow stage "
                f"validating the proposal. Stage {i} reviews drawings "
                f"and calculations submitted by the engineer of record."
            ),
        )
        for i in range(1, 5)
    ]
    evidence = [EvidenceBlockDTO(
        artifact_id="art-1",
        artifact_type="chunk",
        text="The workflow validates the proposal at every stage.",
        chunk_id="c-1",
        score=0.9,
    )]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
        evidence_blocks=evidence,
        options=GenerationOptions(max_cases=12, negative_case_count=0),
    )
    # The LLM stub was actually invoked.
    assert len(stub.calls) == 1
    # And the trace records ``called=True``.
    assert vset.llm is not None and vset.llm.called is True
    # And the LLM case shipped (passes the quality gate).
    llm_cases = [c for c in vset.test_cases if c.metadata.get("llm_generated")]
    assert len(llm_cases) >= 1


def test_no_case_has_answer_in_question_shape():
    """End-to-end: no case may have the malformed
    ``What is the X of <identifier>?`` shape that operators
    flagged."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(
        chunk_id="c-1",
        section="Identification",
        page_start=1,
        body=(
            "The Project ID J1-CE-TEST-0426 is registered to the "
            "Risk Assessment workflow. The deadline is 20 May 2026."
        ),
    )]
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=chunks,
    )
    for case in vset.test_cases:
        assert not _looks_like_answer_in_question(case.question), (
            f"answer-in-question shape leaked: {case.question!r}"
        )
