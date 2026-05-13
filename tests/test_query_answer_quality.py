"""AnswerSynthesizer + CitationBinder + AnswerQualityGate tests.

These three components decide whether the orchestrator returns
``passed`` or ``failed``. The critical regressions to lock in:

  * refusal / "not in retrieved evidence" answers FAIL, regardless
    of length (the legacy length shortcut is gone)
  * cited blocks must be a subset of selected blocks (no rogue
    citations from outside the pack)
  * requested fields must appear in the answer for tabular intents
"""

from __future__ import annotations

import pytest

from j1.query.answer_synthesizer import (
    AnswerSynthesizer,
    SynthesisOutput,
    SynthesisRequest,
)
from j1.query.answer_quality import (
    AnswerQualityGate,
    GATE_ANSWER_NONEMPTY,
    GATE_ANSWER_NOT_REFUSAL,
    GATE_ANSWER_SHAPE,
    GATE_CITATIONS_SUBSET,
    GATE_REQUIRED_FIELDS,
    QueryFinalStatus,
)
from j1.query.citation_binder import CitationBinder
from j1.query.evidence_builder import EvidencePackBuilder
from j1.query.intent_classifier import QueryIntentClassifier
from j1.query.query_plan import (
    EvidenceBlock,
    EvidenceCandidate,
    RetrievalRouteKind,
)


def _cand(*, artifact_id: str, body: str) -> EvidenceCandidate:
    return EvidenceCandidate(
        route=RetrievalRouteKind.RAGANYTHING,
        artifact_id=artifact_id,
        artifact_kind="chunk",
        chunk_id=f"c-{artifact_id}",
        text_preview=body[:80],
        score=0.7,
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


def _good_pack():
    plan = _stage_plan()
    builder = EvidencePackBuilder()
    cands = [
        _cand(artifact_id="a", body="60% design deliverables: drawings."),
        _cand(artifact_id="b", body="90% design deliverables: specs."),
        _cand(artifact_id="c", body="100% design deliverables: final set."),
        _cand(artifact_id="d", body="cost estimate class is Class 3."),
    ]
    return plan, builder.build(plan, cands, scope_run_id="run-1")


# ---- Synthesizer ----------------------------------------------


def test_synthesizer_passes_blocks_to_llm_and_extracts_indices():
    plan, pack = _good_pack()
    captured: list[SynthesisRequest] = []

    def _stub(req: SynthesisRequest) -> str:
        captured.append(req)
        return (
            "| Stage | Deliverables | Cost estimate class | Citation |\n"
            "| --- | --- | --- | --- |\n"
            "| 60% | drawings [#1] | not in retrieved evidence | [#1] |\n"
            "| 90% | specs [#2] | not in retrieved evidence | [#2] |\n"
            "| 100% design | final set [#3] | Class 3 [#4] | [#4] |"
        )
    synth = AnswerSynthesizer(llm=_stub)
    out = synth.synthesize(plan, pack.blocks)
    # The LLM saw all blocks.
    assert len(captured) == 1
    assert "Question:" in captured[0].user_prompt
    assert "[#1]" in captured[0].user_prompt
    # Used indices extracted from the [#N] tags.
    assert out.used_block_indices == (0, 1, 2, 3)


def test_synthesizer_handles_llm_error_softly():
    plan, pack = _good_pack()

    def _broken(req):
        raise RuntimeError("vendor 500")
    synth = AnswerSynthesizer(llm=_broken)
    out = synth.synthesize(plan, pack.blocks)
    assert out.answer == ""
    assert out.used_block_indices == ()
    assert "vendor 500" in out.raw_llm_output


def test_synthesizer_ignores_out_of_range_indices():
    plan, pack = _good_pack()

    def _stub(req):
        # [#99] is out of range; must be silently ignored.
        return "Answer [#1] [#99]"
    synth = AnswerSynthesizer(llm=_stub)
    out = synth.synthesize(plan, pack.blocks)
    assert 99 - 1 not in out.used_block_indices
    assert 0 in out.used_block_indices  # [#1] survived


# ---- Citation binder ------------------------------------------


def test_binder_returns_subset_of_selected():
    plan, pack = _good_pack()
    output = SynthesisOutput(
        answer="x", used_block_indices=(0, 2),
    )
    cited = CitationBinder().bind(pack.blocks, output)
    assert len(cited) == 2
    selected_keys = {
        (b.candidate.artifact_id, b.candidate.chunk_id)
        for b in pack.blocks
    }
    for c in cited:
        assert (
            (c.candidate.artifact_id, c.candidate.chunk_id)
            in selected_keys
        )


def test_binder_dedupes_repeated_indices():
    plan, pack = _good_pack()
    output = SynthesisOutput(
        answer="x", used_block_indices=(0, 0, 1, 0),
    )
    cited = CitationBinder().bind(pack.blocks, output)
    # Three index entries, but the duplicate ``0`` collapses.
    assert len(cited) == 2


def test_binder_drops_out_of_range_indices():
    plan, pack = _good_pack()
    output = SynthesisOutput(
        answer="x", used_block_indices=(0, 999),
    )
    cited = CitationBinder().bind(pack.blocks, output)
    assert len(cited) == 1


# ---- Quality gate ---------------------------------------------


def test_refusal_fails_regardless_of_length():
    """The exact failed-question regression: a long polite refusal
    must FAIL. No length shortcut."""
    plan, pack = _good_pack()
    long_refusal = (
        "I'm sorry, but the deliverables and cost estimate classes "
        "for the design stages mentioned in the question are not "
        "in the retrieved evidence. The available evidence focuses "
        "on other topics that don't directly address the question. "
        "More context would be required to answer accurately. "
        + ("Additional commentary to push the answer past the legacy "
           "400-character length threshold. ") * 3
    )
    assert len(long_refusal) > 400  # Specifically over the legacy threshold.
    output = SynthesisOutput(answer=long_refusal, used_block_indices=())
    gate = AnswerQualityGate()
    results, status = gate.check(
        plan, output, cited=(), selected=pack.blocks,
    )
    assert status == QueryFinalStatus.FAILED
    refusal_gate = next(r for r in results if r.name == GATE_ANSWER_NOT_REFUSAL)
    assert refusal_gate.passed is False


def test_empty_answer_fails():
    plan, pack = _good_pack()
    output = SynthesisOutput(answer="", used_block_indices=())
    results, status = AnswerQualityGate().check(
        plan, output, cited=(), selected=pack.blocks,
    )
    assert status == QueryFinalStatus.FAILED
    nonempty = next(r for r in results if r.name == GATE_ANSWER_NONEMPTY)
    assert nonempty.passed is False


def test_substantive_table_answer_passes_quality_gate():
    plan, pack = _good_pack()
    answer = (
        "| Stage | Deliverables | Cost estimate class | Citation |\n"
        "| --- | --- | --- | --- |\n"
        "| 60% | drawings | n/a | [#1] |\n"
        "| 90% | specs | n/a | [#2] |\n"
        "| 100% design | final set | Class 3 | [#3] [#4] |"
    )
    output = SynthesisOutput(
        answer=answer, used_block_indices=(0, 1, 2, 3),
    )
    cited = CitationBinder().bind(pack.blocks, output)
    results, status = AnswerQualityGate().check(
        plan, output, cited=cited, selected=pack.blocks,
    )
    assert status == QueryFinalStatus.PASSED
    # All required gates passed.
    for r in results:
        if r.severity == "required":
            assert r.passed, f"unexpected failure on {r.name}"


def test_missing_required_field_fails():
    plan, pack = _good_pack()
    # Answer doesn't mention "cost estimate class" anywhere.
    answer = (
        "| Stage | Deliverables | Citation |\n"
        "| --- | --- | --- |\n"
        "| 60% | drawings | [#1] |"
    )
    output = SynthesisOutput(answer=answer, used_block_indices=(0,))
    cited = CitationBinder().bind(pack.blocks, output)
    results, status = AnswerQualityGate().check(
        plan, output, cited=cited, selected=pack.blocks,
    )
    assert status == QueryFinalStatus.FAILED
    fields = next(r for r in results if r.name == GATE_REQUIRED_FIELDS)
    assert fields.passed is False


def test_field_with_filler_passes_when_head_noun_present():
    """A classifier-extracted field like ``"modules involved"`` must
    pass when the answer talks about modules, even if the answer
    doesn't repeat the filler word ``"involved"`` verbatim. The
    gate splits the field and ignores stopwords (a/the/of/involved/
    associated/...) so it tests for the content noun(s)."""
    from j1.query.answer_quality import _fields_covered

    # Filler-word case: the head noun is present.
    answer = "The modules are A, B, and C."
    passed, missing = _fields_covered(answer, ("modules involved",))
    assert passed is True
    assert missing == []

    # Multi-token content field still requires every content token.
    passed, missing = _fields_covered(
        answer, ("cost estimate class",),
    )
    assert passed is False
    assert "cost estimate class" in missing

    # Field that's nothing but stopwords (classifier noise) is
    # treated as satisfied — refusing here would punish the answer
    # for a bug upstream.
    passed, missing = _fields_covered(answer, ("the of",))
    assert passed is True
    assert missing == []


def test_paragraph_answer_for_non_table_intent_passes_shape():
    from j1.query.query_plan import QualityPolicy, AnswerShape
    plan = QueryIntentClassifier().classify("Summarize the document.")
    # Quality policy says PARAGRAPH; substantive paragraph passes.
    output = SynthesisOutput(
        answer="The document discusses the project's design phases. [#1]",
        used_block_indices=(0,),
    )
    pack = (EvidenceBlock(
        candidate=_cand(artifact_id="a", body="design phases"),
        body="design phases", group=None, rank_in_group=0,
    ),)
    cited = CitationBinder().bind(pack, output)
    results, status = AnswerQualityGate().check(
        plan, output, cited=cited, selected=pack,
    )
    # PARAGRAPH shape passes; required_fields is empty so it's
    # advisory; refusal pattern absent.
    assert status == QueryFinalStatus.PASSED


def test_citation_subset_rule_rejects_rogue_block():
    """If somehow a cited block exists that isn't in selected
    (impossible via the binder, but defence-in-depth), the gate
    fails."""
    plan, pack = _good_pack()
    rogue_block = EvidenceBlock(
        candidate=_cand(artifact_id="OUTSIDE",
                        body="not in the pack"),
        body="not in the pack", group=None, rank_in_group=0,
    )
    output = SynthesisOutput(
        answer="x [#1]", used_block_indices=(0,),
    )
    results, status = AnswerQualityGate().check(
        plan, output, cited=(rogue_block,), selected=pack.blocks,
    )
    subset = next(r for r in results if r.name == GATE_CITATIONS_SUBSET)
    assert subset.passed is False
    assert status == QueryFinalStatus.FAILED


def test_citation_lookup_intent_does_not_fail_on_refusal_phrasing():
    """``"not in retrieved evidence"`` is a legitimate answer for
    a citation-lookup question (the user asked where something is;
    "not there" IS the answer). The plan disables refusal gating
    for this intent."""
    plan = QueryIntentClassifier().classify(
        "Where does it say 60% design submittal?",
    )
    output = SynthesisOutput(
        answer="Not in the retrieved evidence.",
        used_block_indices=(),
    )
    results, status = AnswerQualityGate().check(
        plan, output, cited=(), selected=(),
    )
    # The refusal gate is advisory for this intent → not_refusal
    # doesn't fail the status. But required_fields is empty + shape
    # is SHORT_FACT which the answer matches.
    refusal_gate = next(
        r for r in results if r.name == GATE_ANSWER_NOT_REFUSAL
    )
    assert refusal_gate.severity == "advisory"
    assert status == QueryFinalStatus.PASSED
