"""Regression tests proving the engine's ``_compose_answer``
templates do NOT echo the question into the answer.

Bug fixed:
   ``GraphQueryProvider._compose_answer`` and
   ``KnowledgeQueryProvider._compose_answer`` used to start with
   ``f"Graph relationships for: {question}\\n\\n"`` /
   ``f"Knowledge results for: {question}\\n\\n"``. The
   groundedness judge then treated the echoed question text as
   an unsupported factual claim from the answer ("first
   unsupported claim: '<the original question>'") — operators saw
   this as a warning on otherwise-correct graph answers.
   Removing the echo eliminates the false positive.
"""

from __future__ import annotations

from j1.query.providers import GraphQueryProvider, KnowledgeQueryProvider
from j1.query.models import GraphPath


def test_graph_compose_answer_does_not_echo_question():
    """The composed answer must not contain the question text.
    Question is no longer a parameter — that's the structural
    proof it can't be echoed."""
    answer = GraphQueryProvider._compose_answer(  # noqa: SLF001
        paths=[
            GraphPath(nodes=["A", "B"], edges=["depends_on"]),
        ],
    )
    # And it still says useful things.
    assert "A" in answer and "B" in answer
    assert "depends_on" in answer


def test_graph_compose_answer_empty_paths_does_not_echo_question():
    """When no paths are found, the no-paths message is also
    question-free."""
    answer = GraphQueryProvider._compose_answer(paths=[])  # noqa: SLF001
    assert "?" not in answer  # the question would've ended with "?"
    assert "No graph relationships found" in answer


def test_knowledge_compose_answer_does_not_echo_question():
    """Same for the knowledge / FTS path."""
    from j1.search.indexer import SearchHit
    hit = SearchHit(
        artifact_id="a-1",
        artifact_type="compiled.text",
        title="example.pdf",
        source_document_id="doc-1",
        source_location=None,
        confidence=0.8,
        review_status="not_required",
        checksum="hash-1",
        created_at="2026-01-01T00:00:00+00:00",
        byte_size=100,
        extracted_text="The document covers the J1 ingestion pipeline.",
        run_id="run-1",
        chunk_id=None,
    )
    answer = KnowledgeQueryProvider._compose_answer(  # noqa: SLF001
        hits=[hit],
        question="What is this document about?",
    )
    assert "What is this document about" not in answer
    # And the content the hit carried still appears.
    assert "J1 ingestion pipeline" in answer


def test_knowledge_compose_answer_empty_hits_does_not_echo_question():
    answer = KnowledgeQueryProvider._compose_answer(  # noqa: SLF001
        hits=[], question="anything?",
    )
    assert "anything" not in answer
