"""Tests for `DefaultTestCaseGenerator`.

The generator's contract changed substantially: it now takes
`evidence_blocks` + optional `domain_guidance` and makes ONE
whole-document LLM call (rather than N per-chunk calls) under a
strict grounding prompt. The hardcoded sports/celebrity/Bitcoin
negative pool has been removed entirely; negatives are now domain-
driven from `DomainValidationGuidance.negative_check_fields`.

What's covered:
 * Sampling helpers (unchanged).
 * Whole-document LLM happy path with stub returning the new
   `test_cases` schema.
 * Anti-hallucination filter — cases whose `evidence_quote`
   doesn't appear in the supplied evidence get dropped.
 * Heuristic fallback (no LLM / LLM raise / no evidence).
 * Domain-driven negative checks fire only when guidance has
   `negative_check_fields`.
 * NO hardcoded off-topic negatives are emitted under any
   configuration (regression test for the "World Cup" issue).

What's NOT covered here:
 * Wiring through the chunk projector — that's the service's job.
 * REST envelope shape — covered in test_rest_validation_endpoints.
 * Idempotency at the persistence layer — that's the service.
"""

from __future__ import annotations

from typing import Any

import pytest

from j1.domains import DomainValidationGuidance
from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation import DefaultTestCaseGenerator
from j1.validation.dtos import EvidenceBlockDTO
from j1.validation.generator import (
    GENERATOR_VERSION,
    GenerationOptions,
    _first_sentence,
    _hash_chunks,
    _heuristic_questions_for_chunk,
    _pages_for_chunk,
    _sample_chunks,
)


def _chunk(
    *,
    chunk_id: str = "c-1",
    body: str = "The proposal is due 20 May 2026.",
    page_start: int | None = 1,
    page_end: int | None = 1,
    section: str | None = None,
) -> _ChunkRecord:
    return _ChunkRecord(
        chunk_id=chunk_id,
        body=body,
        page_start=page_start,
        page_end=page_end,
        section=section,
        title=None,
        token_count=None,
        confidence=None,
        metadata={},
        linked_assets=[],
        source_artifact_id=None,
        source_document_ids=[],
    )


def _evidence(
    *,
    artifact_id: str = "art-1",
    text: str = "The proposal is due 20 May 2026.",
    artifact_type: str = "chunk",
    chunk_id: str | None = "c-1",
) -> EvidenceBlockDTO:
    return EvidenceBlockDTO(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        text=text,
        chunk_id=chunk_id,
        score=0.9,
    )


# ---- Sampling helpers (unchanged behaviour) ------------------------


def test_sample_chunks_returns_all_when_under_cap():
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(3)]
    out = _sample_chunks(chunks, max_samples=8)
    assert [c.chunk_id for c in out] == ["c-0", "c-1", "c-2"]


def test_sample_chunks_strides_evenly_when_over_cap():
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(20)]
    out = _sample_chunks(chunks, max_samples=4)
    assert len(out) == 4
    ids = [c.chunk_id for c in out]
    assert ids[0] == "c-0"
    assert "c-15" in ids or "c-19" in ids


def test_sample_chunks_honours_must_include():
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(10)]
    out = _sample_chunks(chunks, max_samples=4, must_include_ids=("c-7",))
    assert any(c.chunk_id == "c-7" for c in out)


def test_first_sentence_caps_long_inputs():
    body = "Lorem ipsum dolor sit amet " * 50
    out = _first_sentence(body, max_chars=40)
    assert len(out) <= 40


def test_heuristic_questions_returns_empty_for_empty_chunk():
    empty = _chunk(chunk_id="c-1", body="")
    assert _heuristic_questions_for_chunk(empty, budget=1) == []


def test_pages_for_chunk_inclusive_range():
    c = _chunk(chunk_id="c-1", page_start=2, page_end=4)
    assert _pages_for_chunk(c) == [2, 3, 4]


def test_hash_chunks_stable():
    chunks = [_chunk(chunk_id="c-1", body="alpha")]
    assert _hash_chunks(chunks).startswith("sha256:")
    assert _hash_chunks(chunks) == _hash_chunks(chunks)


def test_hash_chunks_changes_with_content():
    a = _hash_chunks([_chunk(chunk_id="c-1", body="alpha")])
    b = _hash_chunks([_chunk(chunk_id="c-1", body="beta")])
    assert a != b


# ---- Whole-document LLM generation --------------------------------


class _StubTextClient:
    """Captures `extract()` calls; returns canned responses in order.

 The new generator calls `extract(prompt, schema, metadata=…)` once
 per generate() invocation (the whole-document flow); we keep the
 stub's interface flexible enough to accept the metadata kwarg.
 """

    provider = "stub_provider"
    model = "stub-model-v1"

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []
        self._responses = responses or []
        self._raise = raise_on_call

    def extract(self, prompt, schema, *, metadata=None):  # noqa: D401
        self.calls.append((prompt, schema, metadata))
        if self._raise:
            raise RuntimeError("simulated LLM failure")
        if not self._responses:
            return ({"test_cases": []}, object())
        return (self._responses.pop(0), object())


def test_generator_emits_only_smoke_when_no_chunks_no_domain():
    """An empty run + no domain → just the smoke case. The old
 generator shipped 2 hardcoded off-topic negatives here ('World
 Cup' / 'Bitcoin'); the new one emits zero — the test locks that
 regression in place."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=[],
    )
    assert vset.source == "generated"
    assert vset.status == "draft"
    assert vset.generator_version == GENERATOR_VERSION
    assert len(vset.test_cases) == 1
    assert vset.test_cases[0].priority == "smoke"
    assert vset.test_cases[0].validation_scope == "generic"


def test_generator_never_emits_hardcoded_world_cup_questions():
    """Regression test for the original bug. Across every combination
 of (no domain, no chunks), (chunks only), (heuristic fallback),
 the generator must NEVER emit the old hardcoded pool of off-topic
 questions ('World Cup', 'Bitcoin', 'capital of Mars', ...)."""
    forbidden_phrases = (
        "world cup", "bitcoin", "chocolate cake", "planet mars",
        "highest-paid celebrity",
    )
    gen = DefaultTestCaseGenerator()
    configurations = [
        # No chunks, no domain.
        dict(run_id="r", document_ids=[], chunks=[]),
        # Chunks but no LLM.
        dict(
            run_id="r", document_ids=[],
            chunks=[_chunk(chunk_id="c-1", body="alpha beta gamma.")],
        ),
        # Chunks + domain guidance (negatives present but domain-driven).
        dict(
            run_id="r", document_ids=[],
            chunks=[_chunk(chunk_id="c-1", body="alpha beta gamma.")],
            domain_guidance=DomainValidationGuidance(
                negative_check_fields=("foo_field", "bar_field"),
            ),
            domain_id="testdomain",
        ),
    ]
    for cfg in configurations:
        vset = gen.generate(**cfg)
        for case in vset.test_cases:
            lowered = case.question.lower()
            for phrase in forbidden_phrases:
                assert phrase not in lowered, (
                    f"forbidden phrase {phrase!r} appeared in {case.question!r}"
                )


def test_generator_emits_grounded_cases_from_llm_with_evidence():
    """LLM happy path under the new flow: ONE whole-document call,
 evidence blocks passed in, structured response materialised
 into typed cases with `expected_answer`, `evidence_quote`,
 `source_artifact_id`, `validation_scope`."""
    stub = _StubTextClient(
        responses=[{
            "test_cases": [
                {
                    "question": "When is the proposal due?",
                    "expected_answer": "20 May 2026.",
                    "question_type": "fact_retrieval",
                    "validation_scope": "generic",
                    "difficulty": "easy",
                    "evidence": [{
                        "source_artifact_id": "art-1",
                        "artifact_type": "chunk",
                        "quote": "The proposal is due 20 May 2026.",
                    }],
                },
            ],
        }],
    )
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="The proposal is due 20 May 2026.")]
    evidence = [_evidence(
        artifact_id="art-1",
        text="The proposal is due 20 May 2026.",
    )]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        evidence_blocks=evidence,
    )

    chunk_case = next(
        tc for tc in vset.test_cases
        if tc.priority != "smoke" and tc.type != "negative"
    )
    assert chunk_case.question == "When is the proposal due?"
    assert chunk_case.expected_answer == "20 May 2026."
    assert chunk_case.evidence_quote == "The proposal is due 20 May 2026."
    assert chunk_case.source_artifact_id == "art-1"
    assert chunk_case.source_artifact_type == "chunk"
    assert chunk_case.validation_scope == "generic"
    assert chunk_case.question_type == "fact_retrieval"
    # Exactly ONE LLM call (whole-document, not per-chunk).
    assert len(stub.calls) == 1
    # Set carries the LLM trace + context summary.
    assert vset.llm is not None
    assert vset.llm.called is True
    assert vset.context_summary.get("evidence_block_count") == 1


def test_generator_drops_llm_cases_whose_quote_isnt_in_evidence():
    """Anti-hallucination filter: if the LLM emits a case whose
 `evidence.quote` doesn't actually appear in the supplied
 evidence blocks (i.e. it invented the quote), the case is
 dropped silently. Locks in the central grounding contract."""
    stub = _StubTextClient(
        responses=[{
            "test_cases": [
                {
                    "question": "Who won the World Cup?",
                    "expected_answer": "France.",
                    "question_type": "fact_retrieval",
                    "validation_scope": "generic",
                    "evidence": [{
                        "source_artifact_id": "art-1",
                        "artifact_type": "chunk",
                        # The 'quote' contains words ABSENT from our evidence.
                        "quote": "France won the FIFA World Cup in 2022.",
                    }],
                },
                {
                    "question": "When is the proposal due?",
                    "expected_answer": "20 May 2026.",
                    "question_type": "fact_retrieval",
                    "validation_scope": "generic",
                    "evidence": [{
                        "source_artifact_id": "art-1",
                        "artifact_type": "chunk",
                        "quote": "The proposal is due 20 May 2026.",
                    }],
                },
            ],
        }],
    )
    gen = DefaultTestCaseGenerator(text_client=stub)
    evidence = [_evidence(text="The proposal is due 20 May 2026.")]

    vset = gen.generate(
        run_id="r", document_ids=["d"], chunks=[],
        evidence_blocks=evidence,
    )

    # Only the grounded case survives — the World Cup hallucination
    # is dropped because its quote isn't in the evidence.
    grounded = [c for c in vset.test_cases if c.type == "answer"]
    assert len(grounded) == 1
    assert grounded[0].question == "When is the proposal due?"
    # And the off-topic phrase never makes it into ANY case.
    for c in vset.test_cases:
        assert "world cup" not in c.question.lower()


def test_generator_drops_llm_cases_with_unknown_source_artifact_id():
    """If the LLM cites a `source_artifact_id` that wasn't in the
 supplied evidence blocks, the case is dropped. Protects against
 the model fabricating an id from outside-world context."""
    stub = _StubTextClient(
        responses=[{
            "test_cases": [{
                "question": "What is the topic?",
                "expected_answer": "Proposals.",
                "question_type": "fact_retrieval",
                "validation_scope": "generic",
                "evidence": [{
                    # The id "art-99" was never in our evidence.
                    "source_artifact_id": "art-99",
                    "artifact_type": "chunk",
                    "quote": "The proposal is due 20 May 2026.",
                }],
            }],
        }],
    )
    gen = DefaultTestCaseGenerator(text_client=stub)
    evidence = [_evidence(
        artifact_id="art-1",
        text="The proposal is due 20 May 2026.",
    )]

    vset = gen.generate(
        run_id="r", document_ids=["d"], chunks=[],
        evidence_blocks=evidence,
    )

    # Hallucinated source id → case dropped, only smoke remains.
    answer_cases = [c for c in vset.test_cases if c.type == "answer"]
    assert answer_cases == []


def test_generator_falls_back_to_heuristic_on_llm_failure():
    """LLM raising must not fail the generator — fall back to the
 deterministic per-chunk heuristic. This is the load-bearing path
 for CI environments without an LLM endpoint."""
    stub = _StubTextClient(raise_on_call=True)
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="Proposal due 20 May 2026.")]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        evidence_blocks=[_evidence(text="Proposal due 20 May 2026.")],
    )

    chunk_case = next(
        tc for tc in vset.test_cases
        if tc.priority != "smoke" and tc.type != "negative"
    )
    assert "What does the document say about" in chunk_case.question
    assert "Proposal due 20 May 2026" in chunk_case.question
    # LLM trace records the failure.
    assert vset.llm is not None
    assert vset.llm.called is True
    assert vset.llm.error is not None


def test_generator_uses_heuristic_when_no_llm_configured():
    """No LLM at construction → no LLM trace, heuristic only."""
    gen = DefaultTestCaseGenerator(text_client=None)
    chunks = [_chunk(chunk_id="c-1", body="Heading content body.")]
    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
    )
    chunk_case = next(
        tc for tc in vset.test_cases
        if tc.priority != "smoke" and tc.type != "negative"
    )
    assert chunk_case.expected_chunks == ["c-1"]
    assert chunk_case.question  # non-empty
    # No LLM was wired → no trace.
    assert vset.llm is None


def test_generator_caps_total_cases():
    """`max_cases` is the hard ceiling, smoke included."""
    stub = _StubTextClient(responses=[{
        "test_cases": [
            {
                "question": f"Q{i}?",
                "expected_answer": "Some answer.",
                "question_type": "fact_retrieval",
                "validation_scope": "generic",
                "evidence": [{
                    "source_artifact_id": "art-1",
                    "artifact_type": "chunk",
                    "quote": "Body text used in evidence for grounding tests.",
                }],
            }
            for i in range(20)
        ],
    }])
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id=f"c-{i}", body=f"Body {i}") for i in range(10)]
    evidence = [_evidence(
        text="Body text used in evidence for grounding tests.",
    )]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        evidence_blocks=evidence,
        options=GenerationOptions(max_cases=5),
    )

    assert len(vset.test_cases) <= 5


def test_generator_threads_citation_required_through():
    """Caller-supplied `citation_required` flips the flag on every
 non-negative case. Negatives intentionally override to False
 because an honest abstain has no citations and shouldn't be
 failed for that."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="Some text.")]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(citation_required=True),
    )

    for tc in vset.test_cases:
        if tc.type == "negative":
            assert tc.citation_required is False
        else:
            assert tc.citation_required is True


def test_generator_records_artifacts_content_hash():
    """Hash is on the set so callers can dedupe `(run_id, hash)` and
 skip regeneration when the content didn't change."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="A")]
    vset_a = gen.generate(run_id="r", document_ids=[], chunks=chunks)
    vset_b = gen.generate(run_id="r", document_ids=[], chunks=chunks)

    assert vset_a.artifacts_content_hash == vset_b.artifacts_content_hash
    assert vset_a.artifacts_content_hash != "sha256:empty"


# ---- Domain-driven negative checks ---------------------------------


def test_generator_emits_no_negatives_without_domain_guidance():
    """Without a domain pack the old hardcoded sports/celebrity pool
 is GONE — we emit zero negatives rather than confuse the tester
 with off-topic questions. Regression test for the "World Cup"
 bug."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]
    vset = gen.generate(run_id="r", document_ids=[], chunks=chunks)
    negatives = [c for c in vset.test_cases if c.type == "negative"]
    assert negatives == []


def test_generator_emits_domain_driven_negatives_when_guidance_provided():
    """Domain pack's `negative_check_fields` produce one negative per
 field. Each negative tests for a specific domain-important
 absence ("Does the document specify <field>?") with the standard
 abstention sentence as expected answer."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]
    guidance = DomainValidationGuidance(
        negative_check_fields=("design_code", "material_strength"),
    )

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        domain_guidance=guidance, domain_id="civil_engineering",
    )

    negatives = [c for c in vset.test_cases if c.type == "negative"]
    assert len(negatives) == 2
    # Each negative is operator-readable and references the field.
    questions = [c.question.lower() for c in negatives]
    assert any("design code" in q for q in questions)
    assert any("material strength" in q for q in questions)
    for case in negatives:
        assert case.validation_scope == "negative_check"
        assert case.question_type == "missing_information_check"
        assert case.expected_behavior == "abstain"
        assert case.citation_required is False
        assert case.evidence_quote is None
        assert case.source_artifact_id is None
        assert case.domain_id == "civil_engineering"
        # The expected answer is the standard abstention sentence.
        assert case.expected_answer is not None
        assert case.expected_answer.lower().startswith("no.")


def test_generator_negative_count_respects_max_cases():
    """When max_cases is tight, domain negatives don't crowd out the
 smoke case. Smoke is always first; negatives fit in the
 remaining budget."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]
    guidance = DomainValidationGuidance(
        negative_check_fields=("a", "b", "c", "d", "e"),
    )

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        options=GenerationOptions(max_cases=3, negative_case_count=5),
        domain_guidance=guidance, domain_id="d",
    )

    assert len(vset.test_cases) <= 3
    assert any(c.priority == "smoke" for c in vset.test_cases)


def test_generator_handles_malformed_llm_response():
    """LLM returns the right shape but unusable entries (missing
 question / empty answer / no evidence quote) — drop the bad
 ones. Caller fallback path runs if none survive."""
    stub = _StubTextClient(responses=[{
        "test_cases": [
            # Empty question — drop.
            {
                "question": "",
                "expected_answer": "x",
                "question_type": "fact_retrieval",
                "validation_scope": "generic",
                "evidence": [{
                    "source_artifact_id": "art-1",
                    "artifact_type": "chunk",
                    "quote": "Body.",
                }],
            },
            # No evidence list — positive case without quote → drop.
            {
                "question": "Why?",
                "expected_answer": "Because.",
                "question_type": "fact_retrieval",
                "validation_scope": "generic",
                "evidence": [],
            },
            # Valid grounded case — keep.
            {
                "question": "What is in the body?",
                "expected_answer": "A body.",
                "question_type": "fact_retrieval",
                "validation_scope": "generic",
                "evidence": [{
                    "source_artifact_id": "art-1",
                    "artifact_type": "chunk",
                    "quote": "Body text used in evidence for grounding tests.",
                }],
            },
        ],
    }])
    gen = DefaultTestCaseGenerator(text_client=stub)
    evidence = [_evidence(
        text="Body text used in evidence for grounding tests.",
    )]
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        evidence_blocks=evidence,
    )

    grounded = [c for c in vset.test_cases if c.type == "answer"]
    assert len(grounded) == 1
    assert grounded[0].question == "What is in the body?"


def test_generator_accepts_llm_emitted_negative_check_without_quote():
    """Negative checks emitted by the LLM legitimately have no
 quote — they assert the evidence is silent. The grounding
 filter must NOT drop them just because evidence_quote is empty."""
    stub = _StubTextClient(responses=[{
        "test_cases": [{
            "question": "Does the document specify the design code?",
            "expected_answer": "No. The provided evidence does not specify the design code.",
            "question_type": "missing_information_check",
            "validation_scope": "negative_check",
            "evidence": [],
        }],
    }])
    gen = DefaultTestCaseGenerator(text_client=stub)
    evidence = [_evidence(text="Body text used in evidence for grounding tests.")]

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        evidence_blocks=evidence,
    )

    llm_neg = [
        c for c in vset.test_cases
        if c.type == "negative" and c.metadata.get("llm_generated") is True
    ]
    assert len(llm_neg) == 1
    assert llm_neg[0].validation_scope == "negative_check"
    assert llm_neg[0].expected_behavior == "abstain"


def test_generator_short_circuits_llm_when_no_evidence_supplied():
    """If the service didn't supply `evidence_blocks`, the generator
 must NOT call the LLM (no body context = no grounded questions).
 Falls back to the heuristic per-chunk path. This is the central
 anti-drift rule — empty evidence is what produced the original
 'World Cup' hallucinations."""
    stub = _StubTextClient(responses=[{"test_cases": [{
        "question": "Untethered question?",
        "expected_answer": "x",
        "question_type": "fact_retrieval",
        "validation_scope": "generic",
        "evidence": [{
            "source_artifact_id": "art-1",
            "artifact_type": "chunk",
            "quote": "x",
        }],
    }]}])
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="Body about widgets.")]

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        # NOTE: no evidence_blocks supplied.
    )

    # The stub LLM was NEVER called — heuristic ran instead.
    assert len(stub.calls) == 0
    # And the generator recorded that the LLM wasn't actually run.
    assert vset.llm is None or vset.llm.called is False
    # The heuristic case still references the chunk content.
    non_smoke = [c for c in vset.test_cases if c.priority != "smoke"]
    assert non_smoke and "widgets" in non_smoke[0].question.lower()


# ---- Graph case dedup + entity-aware questions (Patch 5) ----------


def _graph_artifact(*, artifact_id: str, top_entities: list[str] | None = None):
    """Test helper: build a minimal `ArtifactRecord` with the metadata
 the generator's graph case factory reads (`top_entities`)."""
    from datetime import datetime, timezone
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus
    from j1.projects.context import ProjectContext

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    metadata: dict = {"run_id": "run-1"}
    if top_entities:
        metadata["top_entities"] = top_entities
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="t", project_id="p"),
        kind="graph_json",
        location=f"graph/{artifact_id}.json",
        content_hash=f"sha:{artifact_id}",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata=metadata,
    )


def test_graph_cases_dedupe_artifacts_with_same_top_entities():
    """Regression: previously three graph artifacts that all named
 the SAME top entities produced THREE identical "What are the
 main entities…" cases. The dedup must coalesce them into ONE
 case carrying all matching artifact_ids as `expected_artifacts`."""
    gen = DefaultTestCaseGenerator()
    artifacts = [
        _graph_artifact(artifact_id="g-1", top_entities=["Alice", "Bob"]),
        _graph_artifact(artifact_id="g-2", top_entities=["Alice", "Bob"]),
        _graph_artifact(artifact_id="g-3", top_entities=["Alice", "Bob"]),
    ]
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        graph_artifacts=artifacts,
    )
    graph_cases = [c for c in vset.test_cases if c.type == "graph"]
    assert len(graph_cases) == 1
    # All three artifact ids are listed as expected (so the runner's
    # retrieval check can pass when ANY of them comes back).
    assert set(graph_cases[0].expected_artifacts) == {"g-1", "g-2", "g-3"}
    # Metadata records the dedupe so an operator scanning the set
    # can see "this case covers 3 graph artifacts".
    assert graph_cases[0].metadata.get("deduped_artifact_count") == 3


def test_graph_case_with_entities_emits_entity_specific_question():
    """When the graph artifact carries `top_entities`, the question
 should mention them explicitly rather than the generic
 "What are the main entities…" boilerplate. Pre-populates
 `expected_answer` so the FE has a "Final Answer" anchor."""
    gen = DefaultTestCaseGenerator()
    artifact = _graph_artifact(
        artifact_id="g-1", top_entities=["Alice", "Bob"],
    )
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        graph_artifacts=[artifact],
    )
    graph_case = next(c for c in vset.test_cases if c.type == "graph")
    assert "Alice" in graph_case.question
    assert "Bob" in graph_case.question
    assert "main entities and relationships" not in graph_case.question
    assert graph_case.expected_answer is not None
    assert "Alice" in graph_case.expected_answer
    assert "Bob" in graph_case.expected_answer


def test_graph_case_without_entities_keeps_generic_question():
    """Backward compat: graph artifacts that don't surface
 `top_entities` (older runs, simpler producers) still get a
 case — just with the generic boilerplate question and no
 `expected_answer`. The runner's behavior on these is
 unchanged."""
    gen = DefaultTestCaseGenerator()
    artifact = _graph_artifact(artifact_id="g-1")
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        graph_artifacts=[artifact],
    )
    graph_case = next(c for c in vset.test_cases if c.type == "graph")
    assert "main entities and relationships" in graph_case.question
    assert graph_case.expected_answer is None
    assert graph_case.expected_artifacts == ["g-1"]


def test_graph_cases_distinct_entities_produce_distinct_cases():
    """Two graph artifacts naming DIFFERENT entity sets should produce
 TWO cases — only same-set artifacts coalesce."""
    gen = DefaultTestCaseGenerator()
    artifacts = [
        _graph_artifact(artifact_id="g-1", top_entities=["Alice", "Bob"]),
        _graph_artifact(artifact_id="g-2", top_entities=["Charlie", "Diana"]),
    ]
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=[],
        graph_artifacts=artifacts,
    )
    graph_cases = [c for c in vset.test_cases if c.type == "graph"]
    assert len(graph_cases) == 2
    questions = [c.question for c in graph_cases]
    assert any("Alice" in q and "Bob" in q for q in questions)
    assert any("Charlie" in q and "Diana" in q for q in questions)
