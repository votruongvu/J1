"""Unit tests for ``score_candidate`` — each scoring signal pinned
independently so a regression to one signal shows up locally."""

from __future__ import annotations

from j1.validation.rerank import (
    CandidateScore,
    QueryIntent,
    RerankConfig,
    extract_query_numbers,
    extract_query_phrases,
    extract_query_terms,
    score_candidate,
)


# ---- Term / phrase / number extractors ---------------------------


def test_extract_query_terms_drops_stopwords():
    terms = extract_query_terms("What is the proposal due date?")
    assert "proposal" in terms
    assert "due" in terms
    assert "date" in terms
    # Stopwords gone.
    assert "the" not in terms
    assert "is" not in terms
    assert "what" not in terms


def test_extract_query_terms_keeps_identifiers_and_hyphens():
    """Identifiers, hyphenated terms, dotted tokens — all valid
    search material."""
    terms = extract_query_terms('Find J1-TEXT-001 in version 1.0')
    # The hyphen-joined token survives the tokenizer.
    assert any("j1-text" in t or "j1" in t for t in terms)
    # Numbers as tokens are kept.
    assert "1.0" in terms or "1" in terms


def test_extract_query_phrases_returns_quoted_text():
    phrases = extract_query_phrases(
        'Find "compile strategy" in the report.'
    )
    assert phrases == ["compile strategy"]


def test_extract_query_numbers_matches_digit_strings():
    nums = extract_query_numbers("Was the proposal due 20 May 2026?")
    assert "20" in nums
    assert "2026" in nums


# ---- Source-trust signal ----------------------------------------


def test_source_trust_chunk_outranks_interpretive():
    """Per spec section 5: source-grounded artifacts outrank
    derived/interpretive ones for factual queries."""
    chunk_score = score_candidate(
        artifact_kind="chunk",
        body_text="some content",
        raw_score=0.0,
        query_terms=[],
        query_phrases=[],
        query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    derived_score = score_candidate(
        artifact_kind="enriched.consistency_findings",
        body_text="some content",
        raw_score=0.0,
        query_terms=[],
        query_phrases=[],
        query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert chunk_score.source_trust > derived_score.source_trust
    assert chunk_score.total > derived_score.total


def test_source_trust_unknown_kind_gets_default():
    """A future / unrecognised kind gets a neutral positive trust
    score so it stays in play but doesn't outrank known sources."""
    s = score_candidate(
        artifact_kind="unknown.new_kind",
        body_text="some content",
        raw_score=0.0,
        query_terms=[], query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.source_trust > 0
    # But less than the chunk trust.
    chunk = score_candidate(
        artifact_kind="chunk",
        body_text="some content",
        raw_score=0.0,
        query_terms=[], query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.source_trust < chunk.source_trust


# ---- Lexical coverage signal ------------------------------------


def test_lexical_coverage_more_terms_match_higher_score():
    """A candidate matching all query terms scores higher than
    one matching only half."""
    full_match = score_candidate(
        artifact_kind="chunk",
        body_text="The proposal due date is 20 May 2026.",
        raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    partial_match = score_candidate(
        artifact_kind="chunk",
        body_text="The proposal lives in the index.",
        raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert full_match.lexical_coverage > partial_match.lexical_coverage


def test_lexical_coverage_zero_when_no_query_terms():
    """Defensive: empty query terms → zero lexical signal (avoids
    divide-by-zero)."""
    s = score_candidate(
        artifact_kind="chunk",
        body_text="anything",
        raw_score=0.0,
        query_terms=[], query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.lexical_coverage == 0.0


def test_proximity_bonus_when_terms_co_located():
    """When multiple query terms appear within a short window of
    each other, a small proximity bonus fires — the answer is
    "together in the text"."""
    co_located = score_candidate(
        artifact_kind="chunk",
        body_text="The proposal due date is here.",
        raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    spread = score_candidate(
        artifact_kind="chunk",
        body_text=(
            "The proposal is real. " + ("x " * 200)
            + "It is due. " + ("y " * 200)
            + "Date specified."
        ),
        raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    # Both match all 3 terms, but co_located gets the proximity boost.
    assert co_located.lexical_coverage > spread.lexical_coverage


# ---- Phrase match signal ----------------------------------------


def test_phrase_match_quoted_phrase_in_body():
    s = score_candidate(
        artifact_kind="chunk",
        body_text="The compile strategy is documented here.",
        raw_score=0.0,
        query_terms=[],
        query_phrases=["compile strategy"],
        query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.phrase_match > 0


def test_phrase_match_zero_when_phrase_absent():
    s = score_candidate(
        artifact_kind="chunk",
        body_text="Unrelated content.",
        raw_score=0.0,
        query_terms=[], query_phrases=["compile strategy"],
        query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.phrase_match == 0


# ---- Numeric/unit signal ----------------------------------------


def test_numeric_signal_boosted_by_numeric_intent():
    """The same numeric overlap scores HIGHER when numeric intent
    is detected — the reranker shifts weight toward source text
    that contains exact values."""
    s_no_numeric = score_candidate(
        artifact_kind="chunk",
        body_text="Page 14 references the schedule.",
        raw_score=0.0,
        query_terms=["page"],
        query_phrases=[], query_numbers=["14"],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    s_numeric = score_candidate(
        artifact_kind="chunk",
        body_text="Page 14 references the schedule.",
        raw_score=0.0,
        query_terms=["page"],
        query_phrases=[], query_numbers=["14"],
        intents=frozenset({QueryIntent.NUMERIC_LOOKUP, QueryIntent.FACT_LOOKUP}),
    )
    assert s_numeric.numeric_unit > s_no_numeric.numeric_unit


def test_numeric_co_location_bonus():
    """A number that appears near a matching query term scores
    higher than the same number floating alone — 'rate of 5%'
    beats 'page 5'."""
    co_located = score_candidate(
        artifact_kind="chunk",
        body_text="The applicable rate is 5%.",
        raw_score=0.0,
        query_terms=["rate"],
        query_phrases=[], query_numbers=["5"],
        intents=frozenset({QueryIntent.NUMERIC_LOOKUP}),
    )
    far = score_candidate(
        artifact_kind="chunk",
        body_text=(
            "Section 5. " + ("Lots of unrelated text. " * 20)
            + "The rate is elsewhere."
        ),
        raw_score=0.0,
        query_terms=["rate"],
        query_phrases=[], query_numbers=["5"],
        intents=frozenset({QueryIntent.NUMERIC_LOOKUP}),
    )
    assert co_located.numeric_unit > far.numeric_unit


# ---- Structural signal ------------------------------------------


def test_structural_kv_pattern_when_table_intent():
    """Field/value blocks score above plain prose when the query
    is field-shaped."""
    structured = score_candidate(
        artifact_kind="compiled.text",
        body_text="Field: Project Name\nValue: J1\nField: Owner\nValue: Alice",
        raw_score=0.0,
        query_terms=["project", "name"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.TABLE_OR_FIELD_LOOKUP}),
    )
    prose = score_candidate(
        artifact_kind="compiled.text",
        body_text="The project name is J1, owned by Alice.",
        raw_score=0.0,
        query_terms=["project", "name"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.TABLE_OR_FIELD_LOOKUP}),
    )
    assert structured.structural > prose.structural


def test_structural_pipe_table_when_table_intent():
    """Markdown / ASCII pipe tables get the structural bonus."""
    s = score_candidate(
        artifact_kind="compiled.text",
        body_text="| Field | Value |\n| name | J1 |\n| version | 1.0 |",
        raw_score=0.0,
        query_terms=["version"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.TABLE_OR_FIELD_LOOKUP}),
    )
    assert s.structural > 0


def test_structural_noop_when_not_table_intent():
    """Structural signal only fires for table/numeric intents —
    a regular fact lookup doesn't get the bonus, so prose isn't
    arbitrarily downranked."""
    s = score_candidate(
        artifact_kind="compiled.text",
        body_text="Field: x\nValue: y\nField: a\nValue: b",
        raw_score=0.0,
        query_terms=["x"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert s.structural == 0


# ---- Section-match signal ---------------------------------------


def test_section_match_hits_when_terms_in_section_header():
    """When the query's terms appear in the chunk's section
    heading, that's a strong signal."""
    s_with_section = score_candidate(
        artifact_kind="chunk",
        body_text="...",
        raw_score=0.0,
        query_terms=["schedule"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
        section="Section 4: Schedule and Milestones",
    )
    s_no_section = score_candidate(
        artifact_kind="chunk",
        body_text="...",
        raw_score=0.0,
        query_terms=["schedule"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
        section=None,
    )
    assert s_with_section.section_match > s_no_section.section_match


# ---- Intent compatibility ---------------------------------------


def test_summary_query_prefers_document_map():
    """Summary queries → document_map gets an intent-compat bonus."""
    doc_map = score_candidate(
        artifact_kind="enriched.document_map",
        body_text="overview text",
        raw_score=0.0,
        query_terms=["overview"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.SUMMARY_QUERY}),
    )
    chunk = score_candidate(
        artifact_kind="chunk",
        body_text="overview text",
        raw_score=0.0,
        query_terms=["overview"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.SUMMARY_QUERY}),
    )
    assert doc_map.intent_compatibility > chunk.intent_compatibility


def test_interpretive_query_gives_derived_artifacts_a_bonus():
    """Interpretive queries → derived/risk artifacts get a small
    boost to offset the factual-query penalty."""
    risk = score_candidate(
        artifact_kind="enriched.risks",
        body_text="risk content",
        raw_score=0.0,
        query_terms=["risk"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.INTERPRETIVE_QUERY}),
    )
    chunk = score_candidate(
        artifact_kind="chunk",
        body_text="risk content",
        raw_score=0.0,
        query_terms=["risk"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.INTERPRETIVE_QUERY}),
    )
    assert risk.intent_compatibility > chunk.intent_compatibility


# ---- Interpretive penalty ---------------------------------------


def test_interpretive_kinds_penalised_for_factual_queries():
    """Per spec section 5: derived kinds shouldn't dominate
    factual queries. The penalty pulls them below source text."""
    interpretive = score_candidate(
        artifact_kind="enriched.consistency_findings",
        body_text="Some derived analysis...",
        raw_score=0.0,
        query_terms=["analysis"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
    )
    assert interpretive.interpretive_penalty > 0


def test_no_interpretive_penalty_when_intent_is_interpretive():
    """Same artifact, interpretive intent → no penalty fires."""
    s = score_candidate(
        artifact_kind="enriched.consistency_findings",
        body_text="Some analysis",
        raw_score=0.0,
        query_terms=["analysis"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.INTERPRETIVE_QUERY}),
    )
    assert s.interpretive_penalty == 0


# ---- Total + config ---------------------------------------------


def test_total_is_sum_minus_penalties():
    s = CandidateScore(
        raw_score=1.0,
        source_trust=5.0,
        lexical_coverage=3.0,
        phrase_match=2.0,
        numeric_unit=1.0,
        structural=1.0,
        section_match=2.0,
        intent_compatibility=1.0,
        interpretive_penalty=2.0,
        duplicate_penalty=1.0,
    )
    # 1 + 5 + 3 + 2 + 1 + 1 + 2 + 1 - 2 - 1 = 13
    assert s.total == 13.0


def test_config_disables_individual_signals():
    """An operator who wants to A/B the contribution of one
    signal can disable just that one. The reranker code path
    stays unchanged; the score drops to zero for that signal."""
    body = "The proposal due date is 20 May 2026."
    with_lex = score_candidate(
        artifact_kind="chunk", body_text=body, raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
        config=RerankConfig(enable_lexical_coverage=True),
    )
    without_lex = score_candidate(
        artifact_kind="chunk", body_text=body, raw_score=0.0,
        query_terms=["proposal", "due", "date"],
        query_phrases=[], query_numbers=[],
        intents=frozenset({QueryIntent.FACT_LOOKUP}),
        config=RerankConfig(enable_lexical_coverage=False),
    )
    assert with_lex.lexical_coverage > 0
    assert without_lex.lexical_coverage == 0
