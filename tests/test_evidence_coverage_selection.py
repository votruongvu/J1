"""Integration tests for the retrieval-quality layer (spec sections
A–G). These exercise the full ``rerank_and_select`` pipeline so a
single test failure points at one of: intent detection, scoring,
or coverage selection.

The tests use generic mocked candidates — no document-specific
content, no domain values — so the behavior generalises across
documents and domains, as the spec requires.
"""

from __future__ import annotations

from j1.validation.rerank import (
    QueryIntent,
    RerankConfig,
    detect_intents,
    rerank_and_select,
)


def _bodies(*texts):
    """Build the (payload, body) list ``rerank_and_select`` expects.
    Payload is just the index — opaque to the reranker."""
    return [(i, t) for i, t in enumerate(texts)]


# ---- Test A: source-first fact lookup ---------------------------


def test_a_source_first_for_factual_lookup():
    """Given mixed candidates (source text + derived analysis),
    a factual query should rank source-grounded evidence above
    the derived interpretation."""
    selected, scores, intents, _ = rerank_and_select(
        bodies=_bodies(
            # Derived analysis with the right keywords.
            "The schedule appears tight based on team capacity.",
            # Source chunk with the actual answer.
            "The proposal due date is 20 May 2026.",
        ),
        raw_scores=[0.9, 0.5],  # the derived one even has a higher raw score
        sections=[None, None],
        artifact_kinds=["enriched.consistency_findings", "chunk"],
        query="What is the proposal due date?",
    )
    # The chunk (payload 1) should win despite lower raw score.
    assert selected[0] == 1
    assert QueryIntent.FACT_LOOKUP in intents


# ---- Test B: compound lookup ------------------------------------


def test_b_compound_lookup_covers_multiple_aspects():
    """Given a compound query asking for two separate facts,
    when no single candidate covers both, the evidence should
    include multiple complementary candidates."""
    selected, scores, intents, _ = rerank_and_select(
        bodies=_bodies(
            "The start date is March 1.",          # covers start
            "The end date is December 31.",        # covers end
            "Unrelated discussion of architecture.",
        ),
        raw_scores=[0.5, 0.5, 0.9],
        sections=[None, None, None],
        artifact_kinds=["chunk", "chunk", "chunk"],
        query="List the start date and the end date.",
        config=RerankConfig(evidence_max_blocks=3),
    )
    assert QueryIntent.COMPOUND_LOOKUP in intents
    # Both the start-date and end-date candidates should be picked
    # (coverage selection prefers complementary blocks).
    assert 0 in selected
    assert 1 in selected
    # The unrelated one is selected last (or dropped) because it
    # doesn't cover any new query terms.
    if len(selected) >= 3:
        assert selected.index(2) == 2


# ---- Test C: numeric lookup -------------------------------------


def test_c_numeric_lookup_prefers_text_with_exact_numbers():
    """Given a numeric query, candidates with the exact value
    co-located with query terms should outrank vague summaries."""
    selected, scores, _, _ = rerank_and_select(
        bodies=_bodies(
            # Summary without the number.
            "The platform has a defined retry policy with several modes.",
            # Source text with the exact value next to the topic word.
            "The retry rate is 3 attempts per minute.",
        ),
        raw_scores=[0.6, 0.6],
        sections=[None, None],
        artifact_kinds=["enriched.document_map", "chunk"],
        query="What is the retry rate value?",
    )
    # Source chunk with the value wins.
    assert selected[0] == 1


# ---- Test D: table/field lookup ---------------------------------


def test_d_table_field_query_boosts_kv_content():
    """Field-shaped questions → key-value or table candidates
    score above prose containing the same keywords."""
    selected, _, intents, _ = rerank_and_select(
        bodies=_bodies(
            # Prose mentioning the keyword.
            "The system tracks several status fields per record.",
            # Key-value content.
            "Field: status\nValue: ACTIVE\nField: owner\nValue: alice",
        ),
        raw_scores=[0.9, 0.5],
        sections=[None, None],
        artifact_kinds=["compiled.text", "compiled.text"],
        query="What is the value of the status field?",
    )
    assert QueryIntent.TABLE_OR_FIELD_LOOKUP in intents
    assert selected[0] == 1


# ---- Test E: interpretive query ---------------------------------


def test_e_interpretive_query_keeps_source_and_derived():
    """Given an interpretive query, both source-grounded and
    derived artifacts should remain reachable — the penalty
    that downranks derived for factual queries is gated to
    NOT fire for interpretive intent."""
    selected, _, intents, _ = rerank_and_select(
        bodies=_bodies(
            "The migration policy mandates a 30-day grace period.",
            "Risk: the 30-day window may be insufficient for legacy data.",
        ),
        raw_scores=[0.6, 0.6],
        sections=[None, None],
        artifact_kinds=["chunk", "enriched.risks"],
        query="What risk does the migration policy pose?",
        config=RerankConfig(evidence_max_blocks=3),
    )
    assert QueryIntent.INTERPRETIVE_QUERY in intents
    # Both kinds should be considered; both reachable in selection.
    assert 0 in selected
    assert 1 in selected


# ---- Test F: evidence cap maximises coverage --------------------


def test_f_evidence_cap_maximises_coverage_over_raw_topk():
    """When candidate_top_k > evidence_max_blocks, the cap should
    select evidence by query coverage, not by simply keeping the
    raw top-K results in order."""
    # Use multi-character distinguishing tokens — single-letter
    # tokens are filtered by the term extractor (treated as
    # noise, same as stopwords). Body wording is kept minimal so
    # the only overlap with the query is the intended target
    # term — that way coverage selection's behaviour is the
    # only variable.
    selected, _, _, _ = rerank_and_select(
        bodies=_bodies(
            # First raw result — high score, body contains
            # topic-alpha only.
            "Topic-alpha is described first.",
            # Second raw result — covers the same target term
            # again (near-coverage / duplicate).
            "Topic-alpha is referenced once more.",
            # Third raw result — lower raw score but covers
            # topic-beta.
            "Topic-beta appears in another paragraph.",
            # Fourth — covers topic-gamma.
            "Topic-gamma surfaces at the end.",
        ),
        raw_scores=[1.0, 0.95, 0.4, 0.2],
        sections=[None, None, None, None],
        artifact_kinds=["chunk", "chunk", "chunk", "chunk"],
        # Bare topic terms — no verbs / question shape — so the
        # extractor returns exactly the three target tokens, no
        # noise that would dilute lexical coverage proportions.
        query="topic-alpha topic-beta topic-gamma",
        config=RerankConfig(evidence_max_blocks=3),
    )
    # Coverage selection should include #2 (topic-beta) and #3
    # (topic-gamma) — not the near-coverage #1, even though #1
    # had a higher raw score.
    assert 2 in selected, f"topic-beta not selected: {selected}"
    assert 3 in selected, f"topic-gamma not selected: {selected}"


def test_f_pure_score_path_when_coverage_selection_disabled():
    """Operators can fall back to pure-score ordering by
    disabling coverage selection in the config. Pin the
    behaviour for both modes."""
    selected, _, _, _ = rerank_and_select(
        bodies=_bodies(
            "First (highest raw score).",
            "Second.",
            "Third (lowest raw score).",
        ),
        raw_scores=[1.0, 0.5, 0.1],
        sections=[None, None, None],
        artifact_kinds=["chunk", "chunk", "chunk"],
        query="anything",
        config=RerankConfig(
            evidence_max_blocks=2,
            enable_coverage_selection=False,
        ),
    )
    # Pure-score path: the top two by total score, in order.
    assert selected[0] == 0
    assert selected[1] == 1


# ---- Test G: run/project isolation contract preserved -----------


def test_g_reranker_does_not_alter_isolation_inputs():
    """The reranker is pure / opaque to its payload. Run/project
    isolation is enforced by the caller (retrieval scope filter,
    citation checks). The reranker only orders what it's given —
    pin the contract that payloads survive unchanged."""
    payloads = [{"artifact_id": "a", "run_id": "r1"},
                {"artifact_id": "b", "run_id": "r1"}]
    selected, _, _, _ = rerank_and_select(
        bodies=[(payloads[0], "first"), (payloads[1], "second")],
        raw_scores=[0.5, 0.5],
        sections=[None, None],
        artifact_kinds=["chunk", "chunk"],
        query="anything",
    )
    # Payloads are returned by-reference, unmodified.
    assert all(p in payloads for p in selected)
    for p in selected:
        assert p["run_id"] == "r1"


# ---- Dedup behaviour --------------------------------------------


def test_dedup_drops_near_duplicate_with_same_prefix():
    """Two candidates with the same leading prefix should NOT
    both end up in the selection — the second one is dropped
    by the prefix-dedup gate."""
    same_prefix = "The proposal due date is 20 May 2026." + " " * 100
    selected, _, _, _ = rerank_and_select(
        bodies=_bodies(
            same_prefix + "And then more context.",
            same_prefix + "And then different context.",
            "A genuinely different sentence with the same content area.",
        ),
        raw_scores=[1.0, 0.9, 0.5],
        sections=[None, None, None],
        artifact_kinds=["chunk", "chunk", "chunk"],
        query="What is the proposal due date?",
        config=RerankConfig(evidence_max_blocks=3),
    )
    # The two same-prefix candidates can't both be in the
    # selection — the second is dropped by dedup.
    assert not (0 in selected and 1 in selected)
