"""Structural validation for the version-controlled retrieval-
broadening query set.

The harness's ``load_queries`` already validates a queries file at
runtime, but it's CLI-time validation — a malformed
``sample_queries.json`` would only fail when an operator tried to
run the harness. This test pulls the validation into CI so a bad
edit fails at PR time instead.

Coverage matches the spec's required checks:

  1. ``sample_queries.json`` is valid JSON.
  2. Root is an object with a ``queries`` list.
  3. Every query has ``id`` + ``question`` (non-empty).
  4. Query IDs are unique.
  5. No empty questions.

Plus a couple of small invariants worth pinning so future edits
don't quietly break the harness contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Repo-anchored path so the test works regardless of pytest cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_QUERIES_PATH = (
    _REPO_ROOT / "evaluation" / "retrieval_broadening" / "sample_queries.json"
)


@pytest.fixture(scope="module")
def queries_payload() -> dict:
    """Parse the file once per module run. A JSON error here is
    the first thing operators want to see — surface it as a fixture
    failure rather than re-parsing in every test."""
    raw = _QUERIES_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


# ---- 1. Valid JSON / object root ---------------------------------


def test_file_parses_as_json(queries_payload):
    assert isinstance(queries_payload, dict)


# ---- 2. ``queries`` list -----------------------------------------


def test_root_has_queries_list(queries_payload):
    queries = queries_payload.get("queries")
    assert isinstance(queries, list)
    assert len(queries) > 0


def test_file_has_between_ten_and_thirty_queries(queries_payload):
    """Spec says 10–20. Cap a touch above 20 to leave room for the
    next edit; below 10 is a regression that should fail loudly."""
    queries = queries_payload["queries"]
    assert 10 <= len(queries) <= 30, (
        f"sample_queries.json has {len(queries)} entries; "
        "spec says 10–20"
    )


# ---- 3. Every entry has id + non-empty question ------------------


def test_every_query_has_id_and_question(queries_payload):
    for index, entry in enumerate(queries_payload["queries"], start=1):
        assert isinstance(entry, dict), (
            f"entry {index} is not an object"
        )
        qid = entry.get("id")
        assert isinstance(qid, str) and qid.strip(), (
            f"entry {index} missing or blank 'id'"
        )
        question = entry.get("question")
        assert isinstance(question, str) and question.strip(), (
            f"entry {qid!r} missing or blank 'question'"
        )


# ---- 4. Unique ids ------------------------------------------------


def test_query_ids_are_unique(queries_payload):
    ids = [q["id"] for q in queries_payload["queries"]]
    duplicates = {qid for qid in ids if ids.count(qid) > 1}
    assert not duplicates, (
        f"sample_queries.json has duplicate ids: {sorted(duplicates)}"
    )


# ---- 5. No empty questions ---------------------------------------


def test_no_empty_or_whitespace_questions(queries_payload):
    """Whitespace-only questions would pass JSON validation but
    fail the harness's ``load_queries`` at runtime — catch them
    here."""
    blanks = [
        q["id"] for q in queries_payload["queries"]
        if not q.get("question", "").strip()
    ]
    assert not blanks, f"blank questions: {blanks}"


# ---- 6. Required category coverage -------------------------------


def test_all_required_categories_present(queries_payload):
    """Pin the category buckets so a regression that accidentally
    drops (say) the negative_stoplist examples fails CI. Spec
    enumerates 10 categories; we require ALL of them to surface
    at least once."""
    required = {
        "alias",
        "canonical_to_alias",
        "domain_synonym",
        "multi_word_concept",
        "unrelated",
        "should_not_broaden",
        "scope_safety",
        "lowercase_common",
        "negative_stoplist",
        "civil_engineering",
    }
    present = {
        q.get("category", "")
        for q in queries_payload["queries"]
    }
    missing = required - present
    assert not missing, f"missing required categories: {sorted(missing)}"


# ---- 7. Harness can consume the file unchanged -------------------


def test_load_queries_accepts_sample_file():
    """End-to-end: the harness's own loader (the
    ``load_queries`` function the CLI calls) accepts the file
    verbatim. This is what guarantees PR1's acceptance criterion
    'existing A/B harness can consume it without code changes'."""
    from j1.tools.evaluate_retrieval_broadening import load_queries

    loaded = load_queries(_QUERIES_PATH)
    assert len(loaded) >= 10
    # IDs round-trip identically (the loader passes them through).
    by_id = {q.id: q.question for q in loaded}
    assert "alias_boq_001" in by_id
    assert by_id["alias_boq_001"].strip() != ""
