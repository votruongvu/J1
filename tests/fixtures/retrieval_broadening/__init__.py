"""Fixture loader for retrieval-broadening A/B reports.

These fixtures are small, deterministic JSON files used by the
summarizer / comparator / validator / Markdown-export tests. They
mirror the shape :mod:`j1.tools.evaluate_retrieval_broadening`
emits so the consumer tests can exercise the real wire contract
without seeding chunks / runs / artifacts.

Use ``load_fixture("report_basic")`` instead of hand-built dicts
when the test is about consumer behaviour, not producer detail.
Tests that pin specific producer edge cases keep their inline
shapes — fixtures are a convenience, not a forced refactor.
"""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).resolve().parent


def load_fixture(name: str) -> dict:
    """Read a fixture JSON file by stem (no extension). Raises if
    the file is missing or malformed so a typo / accidental
    deletion fails the test loudly."""
    path = _FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_path(name: str) -> Path:
    """Path-only helper for tests that need to pass the file
    directly to a CLI (the validator's ``--input`` argument, for
    example)."""
    return _FIXTURE_DIR / f"{name}.json"


__all__ = ["fixture_path", "load_fixture"]
