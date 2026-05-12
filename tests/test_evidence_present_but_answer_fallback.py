"""Tests for the ``evidence_present_but_answer_fallback`` check.

The latest validation report flagged two retrieval cases that
shipped as PASSED even though the answer was "Not in the retrieved
evidence" while citations included a chunk and ``compiled.text``.
The operator's read: "this should not be considered a quality pass
when expected_chunk_in_topk=true". The check pinned here makes
that pseudo-pass a required failure instead.
"""

from __future__ import annotations

import pytest

from j1.projects.context import ProjectContext
from j1.validation.dtos import RetrievedChunkRefDTO


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _hit(*, artifact_id, kind, preview="some real text", chunk_id=None):
    return RetrievedChunkRefDTO(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        run_id="run-1",
        document_id="doc-1",
        source_location=None,
        score=0.5,
        preview=preview,
        artifact_kind=kind,
    )


def _check_ctx(*, answer, retrieved):
    """Build the dataclass the check function consumes. The fields
    are positional in the source — replicating the shape here keeps
    the test independent of refactors to ``run_checks``."""
    from j1.validation.checks import _CheckContext

    return _CheckContext(
        ctx=ProjectContext(tenant_id="t1", project_id="p1"),
        run_id="run-1",
        answer=answer,
        retrieved_chunks=retrieved,
        citations=[],
        citation_required=False,
        artifact_registry=None,  # not used by this check
    )


def test_fails_when_chunk_present_but_answer_abstains():
    """Headline regression: chunk in retrieval + abstain answer →
    required failure."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="Not in the retrieved evidence.",
        retrieved=[_hit(
            artifact_id="c-1", kind="chunk",
            preview="The proposal due date is 20 May 2026.",
            chunk_id="ch-a",
        )],
    ))
    assert check is not None
    assert check.passed is False
    assert check.severity == "required"
    assert check.name == "evidence_present_but_answer_fallback"


def test_fails_when_compiled_text_present_but_answer_abstains():
    """``compiled.text`` is in the textual evidence set — same
    treatment as chunk."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="The document does not contain that information.",
        retrieved=[_hit(
            artifact_id="ct-1", kind="compiled.text",
            preview="Section 3.1 Scope. The proposal due date is 20 May 2026.",
        )],
    ))
    assert check is not None
    assert check.passed is False


def test_passes_when_no_retrieval():
    """No retrieval → ``retrieved_chunks_present`` already fails for
    this case. The fallback check skips (returns None) so we don't
    double-count."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="Not in the retrieved evidence.",
        retrieved=[],
    ))
    assert check is None


def test_passes_when_answer_is_not_fallback():
    """A real, grounded answer → check skips (returns None) so a
    PASS doesn't get downgraded to noise."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="The proposal due date is 20 May 2026 [1].",
        retrieved=[_hit(artifact_id="c-1", kind="chunk")],
    ))
    assert check is None


def test_passes_when_only_graph_json_retrieved():
    """``graph_json`` is NOT in the textual-evidence set — abstaining
    when retrieval only surfaced graph JSON blobs is legitimate
    (raw graph isn't usable prose for the local synthesizer)."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="Not in the retrieved evidence.",
        retrieved=[_hit(
            artifact_id="g-1", kind="graph_json",
            preview='{"entities": [...]}',
        )],
    ))
    assert check is None


def test_passes_when_textual_chunk_preview_is_empty():
    """A chunk with empty preview can't have grounded the answer —
    abstaining is acceptable. The check looks for chunks with
    *non-empty* body, mirroring the synthesizer's reality."""
    from j1.validation.checks import (
        _check_evidence_present_but_answer_fallback,
    )

    check = _check_evidence_present_but_answer_fallback(_check_ctx(
        answer="Not in the retrieved evidence.",
        retrieved=[_hit(
            artifact_id="c-empty", kind="chunk",
            preview="   ",  # whitespace-only
        )],
    ))
    assert check is None


def test_run_checks_wires_the_new_check_for_positive_cases():
    """End-to-end: ``run_checks`` for a non-negative case actually
    appends the new check when the fallback condition fires.

    Pins the wiring — the check itself is unit-tested above; this
    confirms it's reachable from the runner."""
    from j1.validation.checks import run_checks

    checks = run_checks(
        ctx=ProjectContext(tenant_id="t1", project_id="p1"),
        run_id="run-1",
        answer="Not in the retrieved evidence.",
        retrieved_chunks=[_hit(
            artifact_id="c-1", kind="chunk",
            preview="The proposal due date is 20 May 2026.",
            chunk_id="ch-a",
        )],
        citations=[],
        citation_required=False,
        artifact_registry=None,
        case_type="retrieval",
    )

    names = [c.name for c in checks]
    assert "evidence_present_but_answer_fallback" in names
    fb = [c for c in checks if c.name == "evidence_present_but_answer_fallback"][0]
    assert fb.passed is False
    assert fb.severity == "required"


def test_run_checks_skips_new_check_for_negative_cases():
    """Negative test cases legitimately expect an abstain. The new
    check must NOT fire for them — that would punish the right
    behaviour."""
    from j1.validation.checks import run_checks

    checks = run_checks(
        ctx=ProjectContext(tenant_id="t1", project_id="p1"),
        run_id="run-1",
        answer="Not in the retrieved evidence.",
        retrieved_chunks=[_hit(
            artifact_id="c-1", kind="chunk",
            preview="some content",
        )],
        citations=[],
        citation_required=False,
        artifact_registry=None,
        case_type="negative",
    )
    names = [c.name for c in checks]
    assert "evidence_present_but_answer_fallback" not in names
