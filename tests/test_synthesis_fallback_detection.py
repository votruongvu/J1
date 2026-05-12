"""Tests for ``DefaultAnswerSynthesizer``'s fallback detection — the
``SynthesisResult.error="llm_abstained"`` classification.

When the LLM is correctly instructed to emit "Not in the retrieved
evidence." for unanswerable questions, the validation service needs
to distinguish that case from a successful answer or an LLM
exception. Previously every successful response set ``error=None``
which made the debug payload's ``fallback_reason`` field ambiguous.

The detector mirrors the ``_FALLBACK_PHRASES`` catalogue used by the
groundedness whitelist — same vocabulary, same normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from j1.validation.dtos import EvidenceBlockDTO
from j1.validation.synthesis import (
    DefaultAnswerSynthesizer,
    _is_fallback_text,
)


@dataclass
class _StubUsage:
    provider: str = "stub"
    model: str = "stub-model"
    input_tokens: int = 10
    output_tokens: int = 5


class _StubClient:
    """LLM client stub that returns a fixed response."""

    provider = "stub"
    model = "stub-model"

    def __init__(self, response: str):
        self._response = response

    def generate(self, prompt, *, system_prompt, max_output_tokens, metadata):  # noqa: ARG002
        return self._response, _StubUsage()


@pytest.fixture
def evidence():
    return [
        EvidenceBlockDTO(
            artifact_id="c-1",
            artifact_type="chunk",
            text="The proposal due date is 20 May 2026.",
            chunk_id="chunk-a",
            score=0.9,
            page_start=3,
            page_end=3,
            section="Schedule",
            source_location="chunks/c-1",
        ),
    ]


def test_is_fallback_text_canonical_phrase():
    assert _is_fallback_text("Not in the retrieved evidence.")
    assert _is_fallback_text("Not enough information.")
    assert _is_fallback_text("not in the retrieved evidence")


def test_is_fallback_text_real_answer_not_fallback():
    assert not _is_fallback_text("The proposal due date is 20 May 2026 [1].")
    assert not _is_fallback_text("")
    assert not _is_fallback_text(None)  # type: ignore[arg-type]


def test_synthesis_classifies_fallback_response_as_llm_abstained(evidence):
    """LLM emits the canonical fallback phrase → SynthesisResult.error
    is ``llm_abstained``. The validation service uses this to populate
    the debug panel's ``fallback_reason`` instead of treating the call
    as a clean success."""
    synth = DefaultAnswerSynthesizer(
        text_client=_StubClient("Not in the retrieved evidence."),
    )
    result = synth.synthesize(
        question="What is the proposal due date?",
        evidence=evidence,
    )
    assert result.error == "llm_abstained"
    assert result.answer == "Not in the retrieved evidence."  # still surfaced


def test_synthesis_clean_answer_has_no_error(evidence):
    """A grounded answer must NOT be classified as llm_abstained —
    that would muddle the debug telemetry."""
    synth = DefaultAnswerSynthesizer(
        text_client=_StubClient(
            "The proposal due date is 20 May 2026 [1]."
        ),
    )
    result = synth.synthesize(
        question="What is the proposal due date?",
        evidence=evidence,
    )
    assert result.error is None
    assert "20 May 2026" in (result.answer or "")


def test_synthesis_no_evidence_short_circuits_with_no_evidence_error():
    """Empty evidence list never reaches the LLM — the synthesizer
    fast-paths to ``error='no_evidence'``. Tests preserved here to
    confirm the fallback-detection change didn't alter the
    pre-existing no-evidence short-circuit."""
    synth = DefaultAnswerSynthesizer(
        text_client=_StubClient("Not in the retrieved evidence."),
    )
    result = synth.synthesize(question="anything", evidence=[])
    assert result.error == "no_evidence"
    assert result.answer is None
