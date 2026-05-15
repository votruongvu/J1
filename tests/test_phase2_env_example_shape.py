"""Phase 2: ``.env.example`` shape regression guard.

The dev `.env.example` is the canonical reference operators copy
into `.env` before starting the dev stack. After Phase 2's settings
cleanup, two contracts hold:

  * Stale vars (`J1_INGEST_PLANNER_ENABLED`, `J1_RAGANYTHING_MODE`)
    are gone — they were renamed or retired.
  * High-leverage operator groups (ingest trace, enrichment
    concurrency, advanced assessment, RAGAnything advanced, LLM
    context budgets) are present so an operator searching for a
    knob can find one example.

A future refactor that drops a group should land alongside this
test update — the failure surfaces immediately if a group goes
silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


@pytest.fixture(scope="module")
def env_text() -> str:
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


# ---- Dead vars must be absent ------------------------------------


@pytest.mark.parametrize("var", [
    "J1_INGEST_PLANNER_ENABLED",  # renamed → J1_ASSESSMENT_ENABLED
    "J1_RAGANYTHING_MODE",        # no code reads it
])
def test_dead_var_is_absent(env_text: str, var: str):
    """A dead var found anywhere in `.env.example` — uncommented or
    commented out — would confuse operators copying the file. Hard
    fail."""
    for raw in env_text.splitlines():
        line = raw.strip()
        # Allow the var to appear inside a free-form comment paragraph
        # (e.g. migration note); fail only on assignment lines.
        if line.startswith(f"{var}=") or line.startswith(f"# {var}="):
            pytest.fail(
                f"{var} appears as an assignment in .env.example "
                f"(line: {raw!r}). It has been retired/renamed; "
                "remove it from the example file."
            )


# ---- Renamed var is present --------------------------------------


def test_assessment_enabled_replaces_planner_enabled(env_text: str):
    """The replacement var must be present (commented or
    uncommented). Pins the rename so the dev stack still has a
    knob to flip."""
    lines = [
        l for l in env_text.splitlines()
        if l.startswith("J1_ASSESSMENT_ENABLED=")
        or l.startswith("# J1_ASSESSMENT_ENABLED=")
    ]
    assert lines, (
        "J1_ASSESSMENT_ENABLED missing from .env.example. The "
        "rename from J1_INGEST_PLANNER_ENABLED landed without its "
        "replacement."
    )


# ---- Required operator groups are present ------------------------


@pytest.mark.parametrize("section_header", [
    "# ---- Workspace",
    "# ---- Temporal",
    "# ---- API service",
    "# ---- Enrichment",
    "# ---- RAGAnything provider",
    "# ---- LLM roles",
    "# ---- Adaptive ingestion assessment",
    "# ---- Execution profile policy",
    "# ---- Ingest performance trace",
    "# ---- Advanced (LLM-driven) assessment",
])
def test_required_setting_group_is_present(
    env_text: str, section_header: str,
):
    """Section headers anchor the layout. Operators scan the file
    by header; a missing one is a regression even if the underlying
    vars still exist further down."""
    assert section_header in env_text, (
        f"section header missing from .env.example: {section_header!r}. "
        "Add the group with at least one commented example var so "
        "operators can find the knob."
    )


# ---- High-leverage vars per group --------------------------------


@pytest.mark.parametrize("var", [
    # Ingest trace
    "J1_INGEST_TRACE_ENABLED",
    "J1_INGEST_TRACE_OUTPUT",
    "J1_INGEST_TRACE_SLOW_STAGE_MS",
    # Enrichment concurrency
    "J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS",
    "J1_ENRICHMENT_TIMEOUT_SECONDS",
    # Advanced assessment
    "J1_LLM_ADVANCED_ASSESSMENT_ENABLED",
    # RAGAnything advanced
    "J1_RAGANYTHING_BACKEND",
    "J1_RAGANYTHING_PARSE_METHOD",
    # LLM context
    "J1_TEXT_LLM_CONTEXT_WINDOW_TOKENS",
])
def test_high_leverage_var_documented_in_env_example(
    env_text: str, var: str,
):
    """Each of these vars is a primary operator dial. They must be
    documented (commented assignment is fine) so an operator
    grepping the file finds an example."""
    found = any(
        line.startswith(f"{var}=") or line.startswith(f"# {var}=")
        for line in env_text.splitlines()
    )
    assert found, (
        f"{var} not documented in .env.example. Add a commented "
        "default with a one-line operator-facing comment."
    )
