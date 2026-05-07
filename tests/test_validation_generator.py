"""Tests for `DefaultTestCaseGenerator`.

Exercises the generator with a stub LLM (so test runs stay fast and
deterministic) plus the no-LLM path so the heuristic fallback is
locked in.

What's NOT covered here:
  * Wiring through the chunk projector — that's the service's job.
  * REST envelope shape — covered in test_rest_validation_sets.
  * Idempotency at the persistence layer — that's the service.
"""

from __future__ import annotations

from typing import Any

import pytest

from j1.ingestion_review.projectors.chunks import _ChunkRecord
from j1.validation import DefaultTestCaseGenerator
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


# ---- Sampling ------------------------------------------------------


def test_sample_chunks_returns_all_when_under_cap():
    """Small docs (≤ cap) get every chunk sampled — no diversity
    sacrifice, full coverage."""
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(3)]
    out = _sample_chunks(chunks, max_samples=8)
    assert [c.chunk_id for c in out] == ["c-0", "c-1", "c-2"]


def test_sample_chunks_strides_evenly_when_over_cap():
    """For docs larger than the cap, take every Nth chunk so the
    sample spans the whole document instead of just the head."""
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(20)]
    out = _sample_chunks(chunks, max_samples=4)
    assert len(out) == 4
    # Stride-based — first should be c-0, last should not be the
    # final chunk (otherwise we'd have a head-bias).
    ids = [c.chunk_id for c in out]
    assert ids[0] == "c-0"
    assert "c-15" in ids or "c-19" in ids  # late portion represented


def test_sample_chunks_honours_must_include():
    """Phase 5 will use must-include for incremental regeneration —
    the contract is locked here so the field doesn't get repurposed."""
    chunks = [_chunk(chunk_id=f"c-{i}") for i in range(20)]
    out = _sample_chunks(
        chunks, max_samples=4, must_include_ids=("c-19", "c-15"),
    )
    ids = {c.chunk_id for c in out}
    assert "c-19" in ids
    assert "c-15" in ids


def test_sample_chunks_empty_input_returns_empty():
    assert _sample_chunks([], max_samples=8) == []


# ---- Heuristic question producer -----------------------------------


def test_heuristic_question_uses_first_sentence():
    chunk = _chunk(body="The proposal is due 20 May 2026. Vendors must register first.")
    out = _heuristic_questions_for_chunk(chunk, budget=2)
    assert len(out) == 1
    assert "20 May 2026" in out[0]["question"]
    assert out[0]["type"] == "retrieval"


def test_heuristic_question_skips_empty_body():
    """Empty / whitespace-only chunks are dropped — there's nothing
    to ask a question about, and emitting a placeholder would
    pollute the set with noise."""
    assert _heuristic_questions_for_chunk(_chunk(body=""), budget=1) == []
    assert _heuristic_questions_for_chunk(_chunk(body="   \n  "), budget=1) == []


def test_first_sentence_caps_at_max_chars():
    """Pathological inputs (no punctuation) must get truncated so
    the generator doesn't emit a 10k-char question."""
    long = "x" * 1000
    out = _first_sentence(long, max_chars=140)
    assert len(out) == 140


# ---- Page derivation -----------------------------------------------


def test_pages_for_chunk_single_page():
    assert _pages_for_chunk(_chunk(page_start=3, page_end=3)) == [3]


def test_pages_for_chunk_range_inclusive():
    assert _pages_for_chunk(_chunk(page_start=2, page_end=4)) == [2, 3, 4]


def test_pages_for_chunk_no_page_returns_empty():
    """Producer didn't surface page info → no page check. The runner
    treats `expected_pages=[]` as 'skip the check' rather than
    'must have no pages'."""
    assert _pages_for_chunk(_chunk(page_start=None, page_end=None)) == []


# ---- Hash idempotency ----------------------------------------------


def test_hash_stable_for_same_chunks():
    chunks = [_chunk(chunk_id="a", body="alpha"), _chunk(chunk_id="b", body="beta")]
    assert _hash_chunks(chunks) == _hash_chunks(chunks)


def test_hash_changes_on_content_change():
    chunks_a = [_chunk(chunk_id="a", body="alpha")]
    chunks_b = [_chunk(chunk_id="a", body="ALPHA")]
    assert _hash_chunks(chunks_a) != _hash_chunks(chunks_b)


def test_hash_empty_input_has_stable_sentinel():
    assert _hash_chunks([]) == "sha256:empty"


# ---- DefaultTestCaseGenerator (end-to-end) -------------------------


class _StubTextClient:
    """Records every extract() call + returns canned questions.

    Tests can vary `responses` to simulate different LLM behaviours
    (success, malformed JSON, exception)."""

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or []
        self._raise = raise_on_call

    def extract(self, prompt: str, schema: dict[str, Any]):
        self.calls.append((prompt, schema))
        if self._raise:
            raise RuntimeError("simulated LLM failure")
        if not self._responses:
            return ({"questions": []}, object())
        return (self._responses.pop(0), object())


def test_generator_emits_smoke_case_when_no_chunks():
    """An empty run still gets a smoke case — the runner will fail
    `retrieved_chunks_present`, which is the right operator signal
    for 'the run produced nothing queryable.' Negatives are added
    by Phase 3 even on an empty run because the off-topic test is
    still meaningful (engine should abstain regardless of index
    state)."""
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=[],
    )
    assert vset.source == "generated"
    assert vset.status == "draft"
    assert vset.generator_version == GENERATOR_VERSION
    # Always at least the smoke case; negatives ride along by
    # default (Phase 3). Cap at 3 = smoke + 2 default negatives.
    assert len(vset.test_cases) >= 1
    assert vset.test_cases[0].priority == "smoke"
    # No chunks → no chunk cases — the rest are negatives.
    non_smoke = [c for c in vset.test_cases if c.priority != "smoke"]
    assert all(c.type == "negative" for c in non_smoke)


def test_generator_uses_llm_questions_when_available():
    """LLM happy path: the generator forwards the chunk content to
    `extract()` and emits the LLM-supplied questions verbatim."""
    stub = _StubTextClient(
        responses=[
            {
                "questions": [
                    {
                        "question": "When is the proposal due?",
                        "type": "retrieval",
                        "expected_answer_points": ["20 May 2026"],
                    },
                ],
            },
        ],
    )
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="Proposal due 20 May 2026.")]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        # Disable negatives so this test stays focused on the
        # LLM-question-extraction path. Negative-case generation
        # has its own dedicated test.
        options=GenerationOptions(negative_case_count=0),
    )

    # Smoke + 1 LLM-derived chunk question (negatives disabled).
    assert len(vset.test_cases) == 2
    chunk_case = next(tc for tc in vset.test_cases if tc.priority != "smoke")
    assert chunk_case.question == "When is the proposal due?"
    assert chunk_case.expected_answer_points == ["20 May 2026"]
    assert chunk_case.expected_chunks == ["c-1"]
    # Source traceability: every chunk case must carry its source
    # chunk id so a tester can audit the generator's reasoning.
    assert chunk_case.source_traceability == ["c-1"]
    # The LLM was actually called (no caching short-circuit).
    assert len(stub.calls) == 1


def test_generator_falls_back_to_heuristic_on_llm_failure():
    """LLM raising must not fail the generator — fall back to the
    deterministic question. Tests run in CI without an LLM, so this
    path is the load-bearing one."""
    stub = _StubTextClient(raise_on_call=True)
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="Proposal due 20 May 2026.")]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(negative_case_count=0),
    )

    chunk_case = next(
        tc for tc in vset.test_cases
        if tc.priority != "smoke" and tc.type != "negative"
    )
    # Heuristic question shape — "What does the document say about: …?"
    assert "What does the document say about" in chunk_case.question
    assert "Proposal due 20 May 2026" in chunk_case.question
    assert chunk_case.expected_chunks == ["c-1"]


def test_generator_uses_heuristic_when_no_llm_configured():
    """No LLM at construction time → straight-to-heuristic, no
    extract() calls. Lets validation work on a deployment that
    didn't wire the FAST role."""
    gen = DefaultTestCaseGenerator(text_client=None)
    chunks = [_chunk(chunk_id="c-1", body="Heading content body.")]
    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(negative_case_count=0),
    )
    chunk_case = next(
        tc for tc in vset.test_cases
        if tc.priority != "smoke" and tc.type != "negative"
    )
    assert chunk_case.expected_chunks == ["c-1"]
    assert chunk_case.question  # non-empty


def test_generator_caps_total_cases(monkeypatch):
    """`max_cases` is the hard ceiling (incl. the smoke case).
    Phase 2 plan: synchronous in-process, ≤ 50 cases — the cap
    flows from REST → service → generator without changes."""
    stub_responses = [
        {
            "questions": [
                {"question": f"Q{i}-1?", "type": "retrieval"},
                {"question": f"Q{i}-2?", "type": "retrieval"},
            ],
        }
        for i in range(10)
    ]
    stub = _StubTextClient(responses=stub_responses)
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id=f"c-{i}", body=f"Body {i}") for i in range(10)]

    vset = gen.generate(
        run_id="run-1", document_ids=["doc-1"], chunks=chunks,
        options=GenerationOptions(max_cases=5),
    )

    assert len(vset.test_cases) <= 5


def test_generator_threads_citation_required_through(monkeypatch):
    """Caller-supplied `citation_required` flips the flag on every
    NON-NEGATIVE case — the runner reads this to enable/skip the
    `citation_present` check. Negatives intentionally override to
    `False` because an honest abstain has no citations and shouldn't
    be failed for that."""
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


def test_generator_emits_negative_cases_by_default():
    """Phase 3: every generated set carries N negative cases drawn
    from the deterministic pool. Defaults to 2; respects budget."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]

    vset = gen.generate(run_id="r", document_ids=[], chunks=chunks)

    negatives = [c for c in vset.test_cases if c.type == "negative"]
    assert len(negatives) >= 1
    for case in negatives:
        # Negatives must abstain, not require citations, and have
        # no expected chunks (there's nothing to cite).
        assert case.expected_behavior == "abstain"
        assert case.citation_required is False
        assert case.expected_chunks == []
        assert case.metadata.get("negative") is True


def test_generator_negative_count_zero_disables_negatives():
    """Caller can opt out via `negative_case_count=0`. Useful for
    very small budgets or smoke-only sets."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        options=GenerationOptions(negative_case_count=0),
    )

    negatives = [c for c in vset.test_cases if c.type == "negative"]
    assert negatives == []


def test_generator_negative_count_respects_max_cases():
    """When max_cases is tight, negatives don't crowd out smoke +
    chunk cases. Smoke is always first; negatives fit in the
    remaining budget."""
    gen = DefaultTestCaseGenerator()
    chunks = [_chunk(chunk_id="c-1", body="alpha")]

    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        # Budget = smoke + 1 negative + 1 chunk = 3
        options=GenerationOptions(max_cases=3, negative_case_count=5),
    )

    assert len(vset.test_cases) <= 3
    # Smoke always survives.
    assert any(c.priority == "smoke" for c in vset.test_cases)


def test_generator_handles_malformed_llm_response():
    """LLM returns the right shape but an unusable entry (empty
    question, wrong type) — the generator drops the bad ones,
    keeps the good ones, falls back to heuristic if all are bad."""
    stub = _StubTextClient(
        responses=[
            {
                "questions": [
                    {"question": "", "type": "retrieval"},  # empty — drop
                    {"question": "OK?", "type": "retrieval"},  # keep
                    {"type": "retrieval"},  # missing question — drop
                ],
            },
        ],
    )
    gen = DefaultTestCaseGenerator(text_client=stub)
    chunks = [_chunk(chunk_id="c-1", body="Body.")]
    vset = gen.generate(
        run_id="r", document_ids=[], chunks=chunks,
        options=GenerationOptions(negative_case_count=0),
    )

    chunk_cases = [
        c for c in vset.test_cases
        if c.priority != "smoke" and c.type != "negative"
    ]
    assert len(chunk_cases) == 1
    assert chunk_cases[0].question == "OK?"
