"""PR-07 contract — Settings, env, and docs cleanup.

Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-07, J1
MUST guarantee:

  1. Confirmed-dead env vars are absent from ``.env.example`` —
     a stale knob in the operator-facing file misleads operators
     into setting no-op values.
  2. Active operator-facing env vars are documented — both the
     env-file (with a comment) AND ``docs/settings.md`` for the
     load-bearing ones.
  3. Sections are grouped — operators scanning the env file find
     related knobs together via the ``# ---- <Group>`` headers.
  4. The renamed ``J1_ASSESSMENT_ENABLED`` lives in the file (the
     ``J1_INGEST_PLANNER_ENABLED`` rename happened in Phase 2).
  5. The "DEPRECATED" phrasing that pointed at the wrong direction
     (the dead ``J1_INGEST_PLAN_MODE`` calling the live
     ``J1_LLM_PLANNING_ENABLED`` deprecated) is gone.

Adjacent test files cover finer-grained env-shape edges; this
module is the single navigable PR-07 regression document.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_SETTINGS_DOC = _REPO_ROOT / "docs/settings.md"


@pytest.fixture(scope="module")
def env_text() -> str:
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def settings_md_text() -> str:
    return _SETTINGS_DOC.read_text(encoding="utf-8")


# ---- Contract 1: confirmed-dead vars are absent -----------------


@pytest.mark.parametrize("dead_var", [
    # Removed Phase 2: renamed to J1_ASSESSMENT_ENABLED.
    "J1_INGEST_PLANNER_ENABLED",
    # Removed Phase 2: no code reads it.
    "J1_RAGANYTHING_MODE",
    # Removed PR-07: no Python code reads this. The planning
    # vocabulary settled on J1_LLM_PLANNING_ENABLED.
    "J1_INGEST_PLAN_MODE",
    # Removed PR-07: dead override for the (already-removed)
    # candidate sizing pair.
    "J1_VALIDATION_EVIDENCE_MAX_BLOCKS",
    # Removed PR-07: legacy alias of a query-engine selector that
    # was never wired. The selector itself is documented as
    # NOT YET WIRED — the legacy alias has no purpose.
    "J1_QUERY_PROVIDER_MODE",
    # Removed PR-07: legacy alias of J1_ENABLE_BM25_FALLBACK,
    # which is itself NOT YET WIRED.
    "J1_RAG_NATIVE_QUERY_FALLBACK_TO_BM25",
])
def test_contract_1_dead_var_absent_from_env_example(
    env_text: str, dead_var: str,
):
    """A dead var found in `.env.example` — uncommented or
    commented as an assignment — would confuse operators copying
    the file. Hard fail."""
    for raw in env_text.splitlines():
        stripped = raw.strip()
        if (
            stripped.startswith(f"{dead_var}=")
            or stripped.startswith(f"# {dead_var}=")
        ):
            pytest.fail(
                f"{dead_var} appears as an assignment in "
                f".env.example (line: {raw!r}). It has been "
                "retired; remove the assignment from the file."
            )


# ---- Contract 2: live vars are documented -----------------------


@pytest.mark.parametrize("live_var", [
    # Workspace + core
    "J1_DATA_ROOT",
    "J1_RAGANYTHING_WORKDIR",
    # Temporal
    "J1_TEMPORAL_TARGET",
    "J1_TEMPORAL_NAMESPACE",
    # API
    "J1_API_PORT",
    # LLM roles
    "J1_TEXT_LLM_PROVIDER",
    "J1_VISION_LLM_PROVIDER",
    "J1_FAST_LLM_PROVIDER",
    # Assessment
    "J1_ASSESSMENT_ENABLED",
    "J1_LLM_PLANNING_ENABLED",
    # Profile policy
    "J1_DEFAULT_INGEST_PROFILE",
    # Ingest trace
    "J1_INGEST_TRACE_ENABLED",
    "J1_INGEST_TRACE_OUTPUT",
    # Advanced LLM-driven assessment
    "J1_LLM_ADVANCED_ASSESSMENT_ENABLED",
])
def test_contract_2_live_var_documented_in_env_example(
    env_text: str, live_var: str,
):
    """Each operator-facing live var MUST appear in `.env.example`
    (uncommented assignment OR commented example). Operators
    searching by var name find at least one occurrence."""
    found = any(
        line.startswith(f"{live_var}=")
        or line.startswith(f"# {live_var}=")
        for line in env_text.splitlines()
    )
    assert found, (
        f"{live_var} not documented in .env.example. Add a "
        "commented default with a one-line operator-facing comment."
    )


@pytest.mark.parametrize("important_var", [
    # The ones the settings reference doc MUST cover. These are
    # vars where the operator's decision changes behaviour materially.
    "J1_ASSESSMENT_ENABLED",
    "J1_INGEST_TRACE_ENABLED",
    "J1_RAGANYTHING_BACKEND",
    "J1_TEXT_LLM_PROVIDER",
])
def test_contract_2_important_var_documented_in_settings_doc(
    settings_md_text: str, important_var: str,
):
    """The settings reference doc carries the canonical knob list.
    Operators reading it MUST find every important var. A var
    that's in `.env.example` but missing from `settings.md` is a
    docs gap."""
    assert important_var in settings_md_text, (
        f"{important_var} is missing from docs/settings.md. Add a "
        "row to the appropriate table so operators reading the "
        "reference doc find it."
    )


# ---- Contract 3: section headers present ------------------------


@pytest.mark.parametrize("section_header", [
    # Phase 2 grouping anchors operators scan against.
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
    "# ---- Validation / manual-query retrieval",
    "# ---- Planning Report stage",
])
def test_contract_3_required_section_header_present(
    env_text: str, section_header: str,
):
    """Section headers anchor the layout. Operators scan the file
    by header; a missing one is a regression."""
    assert section_header in env_text, (
        f"section header missing: {section_header!r}. The env "
        "file must group related knobs."
    )


# ---- Contract 4: J1_ASSESSMENT_ENABLED is the canonical name ----


def test_contract_4_assessment_enabled_replaces_planner_enabled(
    env_text: str,
):
    """The Phase 2 rename of ``J1_INGEST_PLANNER_ENABLED`` →
    ``J1_ASSESSMENT_ENABLED`` MUST have a uncommented assignment
    in the env file so the dev stack honours it without operator
    intervention."""
    assignments = [
        l for l in env_text.splitlines()
        if l.startswith("J1_ASSESSMENT_ENABLED=")
    ]
    assert assignments, (
        "J1_ASSESSMENT_ENABLED missing from .env.example (or only "
        "commented). Phase 2 promoted it from a planned rename to "
        "the canonical name — it must be active in the dev file."
    )


# ---- Contract 5: misleading "DEPRECATED" comment is gone --------


def test_contract_5_no_misleading_deprecation_pointer(env_text: str):
    """Before PR-07 the env file pointed operators at
    ``J1_INGEST_PLAN_MODE`` (dead) and labelled the LIVE
    ``J1_LLM_PLANNING_ENABLED`` as deprecated. The reversal was a
    confusion trap. Pin that the misleading wording is gone."""
    text = env_text
    # The dead var is fully out.
    assert "J1_INGEST_PLAN_MODE" not in text
    # The misleading deprecation note is out.
    assert "DEPRECATED: J1_LLM_PLANNING_ENABLED" not in text


# ---- Bonus: unwired query-engine block is honestly flagged -----


def test_unwired_query_engine_block_is_marked_not_wired(env_text: str):
    """The query-engine selector vocabulary was documented but
    never wired in code. Until the selector lands, the env block
    MUST honestly say "NOT YET WIRED" so operators copying the
    file don't think setting ``J1_QUERY_ENGINE`` does something."""
    # If the env vars are still present (they may stay as
    # documentation seams), the block MUST advertise that they
    # aren't wired.
    if "J1_QUERY_ENGINE" in env_text:
        assert "NOT YET WIRED" in env_text or "NOT WIRED" in env_text, (
            "J1_QUERY_ENGINE remains in .env.example without a "
            "NOT WIRED warning. Either remove it or flag it so "
            "operators don't try to use a non-existent selector."
        )
