"""PR-04: regression guard for the docs index.

[docs/README.md](../docs/README.md) is the entry point operators
hit when they search for a doc by intuitive name. It must list
every actual doc under `docs/` — a new doc added without
registering here is silently undiscoverable.

This test does NOT validate the index's prose or the lookup
table's quality; it only enforces the "every file is linked at
least once" minimum. Prose drift is the writer's problem.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"
_DOCS_INDEX = _DOCS_DIR / "README.md"
_TOP_LEVEL_README = _REPO_ROOT / "README.md"


def _index_text() -> str:
    assert _DOCS_INDEX.exists(), (
        f"docs/README.md missing at {_DOCS_INDEX}. The docs index "
        "is load-bearing — operators search by intuitive name and "
        "land here first."
    )
    return _DOCS_INDEX.read_text(encoding="utf-8")


def _doc_filenames() -> list[str]:
    """Every `.md` under `docs/` EXCEPT the index itself.

    Sub-directories are not part of the index contract today —
    if `docs/foo/bar.md` lands later, this test will skip it on
    purpose so the writer decides whether to surface the
    sub-directory in the index. Pin keeps the contract simple."""
    return sorted(
        p.name
        for p in _DOCS_DIR.glob("*.md")
        if p.name != "README.md"
    )


# ---- Index file exists -----------------------------------------


def test_docs_index_exists():
    assert _DOCS_INDEX.exists()
    text = _index_text()
    assert text.strip(), "docs/README.md is empty"


# ---- Every numbered + canonical doc is linked ------------------


@pytest.mark.parametrize("filename", _doc_filenames())
def test_every_doc_is_referenced_in_the_index(filename: str):
    """The index MUST link every `.md` file under `docs/` so a
    new doc can't be silently undiscoverable. Operators rely on
    the index as the single entry point."""
    text = _index_text()
    # Markdown link form: `](filename)`. Substring match keeps the
    # test permissive about whether the link is bare or in a table
    # cell; the writer can format however they like.
    assert f"]({filename})" in text, (
        f"docs/README.md is missing a link to docs/{filename}. "
        "Either add it to the appropriate section of the index OR "
        "delete the doc."
    )


# ---- Top-level README points at the index ----------------------


def test_top_level_readme_links_to_docs_index():
    """The docs index is only valuable if discoverable from the
    repo root. Top-level README MUST reference `docs/README.md`
    so a new contributor lands on it."""
    text = _TOP_LEVEL_README.read_text(encoding="utf-8")
    assert "docs/README.md" in text, (
        "README.md does not reference docs/README.md. The docs "
        "index is meant to be the first hop from the repo root."
    )


# ---- Common-name lookup covers the Phase 2 prompt names --------


@pytest.mark.parametrize("intuitive_name", [
    "architecture",
    "ingest-flow",
    "ingestion-flow",
    "query-flow",
    "domain-enrichment",
    "settings",
    "deployment",
    "developer-onboarding",
    "risks-and-limitations",
])
def test_common_name_lookup_covers_intuitive_doc_name(
    intuitive_name: str,
):
    """The Phase 2 audit prompt listed these doc names; the lookup
    table on docs/README.md must mention each so a contributor
    searching by intuitive name finds the canonical numbered file.
    """
    text = _index_text()
    assert intuitive_name in text, (
        f"docs/README.md's common-name lookup is missing the "
        f"intuitive name {intuitive_name!r}. Either add a row "
        "pointing at the canonical numbered file OR rename the "
        "search term in this test if the vocabulary changed."
    )
