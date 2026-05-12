"""Unit tests for the deterministic query intent classifier
(``j1.validation.rerank.detect_intents``).

The classifier is intentionally lightweight + general — no
document names, no domain values, no LLM round trips. These tests
pin the catalogue so future tweaks must be deliberate.
"""

from __future__ import annotations

from j1.validation.rerank import QueryIntent, detect_intents


def test_fact_lookup_what_is():
    assert QueryIntent.FACT_LOOKUP in detect_intents("What is the project name?")


def test_fact_lookup_who_when_where():
    for q in (
        "Who is the project lead?",
        "When was the proposal submitted?",
        "Where is the headquarters located?",
        "Which framework was selected?",
    ):
        assert QueryIntent.FACT_LOOKUP in detect_intents(q), q


def test_numeric_lookup_via_hint():
    assert QueryIntent.NUMERIC_LOOKUP in detect_intents(
        "What is the value of the budget?"
    )
    assert QueryIntent.NUMERIC_LOOKUP in detect_intents(
        "How many sections are there?"
    )
    assert QueryIntent.NUMERIC_LOOKUP in detect_intents(
        "What rate applies to overruns?"
    )


def test_numeric_lookup_via_digit_in_query():
    """A standalone digit in the query implies numeric intent
    even without an explicit hint word."""
    assert QueryIntent.NUMERIC_LOOKUP in detect_intents(
        "What happens after 2026?"
    )
    assert QueryIntent.NUMERIC_LOOKUP in detect_intents("Page 14 says what?")


def test_compound_lookup_via_conjunction():
    intents = detect_intents("List the start date and the end date.")
    assert QueryIntent.COMPOUND_LOOKUP in intents


def test_compound_lookup_via_repeated_commas():
    intents = detect_intents("Name the parser, the chunker, and the indexer.")
    assert QueryIntent.COMPOUND_LOOKUP in intents


def test_table_field_lookup():
    for q in (
        "What is the value of field X?",
        "What does column 3 contain?",
        "Look up the label for row 7.",
        "What table holds the schedule?",
    ):
        assert QueryIntent.TABLE_OR_FIELD_LOOKUP in detect_intents(q), q


def test_interpretive_query():
    for q in (
        "Why does the policy require approval?",
        "Explain the workflow.",
        "Assess the risk profile.",
        "Compare this with the previous version.",
        "What is the impact on operations?",
    ):
        assert QueryIntent.INTERPRETIVE_QUERY in detect_intents(q), q


def test_summary_query():
    for q in (
        "Summarize the document.",
        "Give an overview of the design.",
        "What are the main points?",
        "What is this document about?",
    ):
        assert QueryIntent.SUMMARY_QUERY in detect_intents(q), q


def test_section_lookup():
    assert QueryIntent.SECTION_LOOKUP in detect_intents(
        "What does section 3.1 say?"
    )
    assert QueryIntent.SECTION_LOOKUP in detect_intents(
        "Find the paragraph on safety."
    )


def test_multiple_intents_co_exist():
    """A compound numeric fact lookup → three intents fire
    simultaneously. The reranker treats them as additive
    signals; one query can want a number AND list AND fact."""
    intents = detect_intents(
        "List the budget value and the duration in days."
    )
    assert QueryIntent.FACT_LOOKUP in intents
    assert QueryIntent.NUMERIC_LOOKUP in intents
    assert QueryIntent.COMPOUND_LOOKUP in intents


def test_empty_query_defaults_to_fact_lookup():
    """Defensive: never return an empty intent set; downstream
    code unconditionally iterates the result."""
    assert QueryIntent.FACT_LOOKUP in detect_intents("")
    assert QueryIntent.FACT_LOOKUP in detect_intents("   ")


def test_unmatched_query_falls_back_to_fact_lookup():
    """A query that hits no hint at all → fact_lookup. Better
    default than returning nothing (avoids dropping the
    reranker's intent-compat signal)."""
    intents = detect_intents("X y z thinger flobble")
    assert intents == frozenset({QueryIntent.FACT_LOOKUP})
