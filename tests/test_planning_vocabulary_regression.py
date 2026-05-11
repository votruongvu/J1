"""Regression guard — planning vocabulary (wave / phase / slice /
milestone) MUST NOT reappear in runtime identifiers, user-visible
strings, API routes, or artifact kinds.

These terms were used only to organise the refactor work; they're
NOT product concepts. Reintroducing them in runtime code is a
naming bug.

Allowed exceptions (legitimate non-planning uses):
  * "two-phase compile" — established technical feature name for
    the assessment → trigger-compile workflow gate.
  * Identifier-shaped tokens inside legitimate document content
    (test fixtures simulating real-world tables with a "Phase"
    column header, etc.).
  * Generic stage labels like "verification phase" / "assessment
    phase" inside internal docstrings — these read as synonyms for
    "stage" and are not planning markers. Migrate as new code is
    added.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "j1"
FRONTEND_SRC = REPO / "frontend" / "src"


# ---- 1. No runtime PY identifiers contain wave/phase/slice -------


def _python_identifiers(path: Path) -> set[str]:
    """Walk one Python module's AST + collect every identifier
    (function names, class names, top-level vars, parameter names,
    attribute accesses). Skips comments + docstrings."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    ids: set[str] = set()
    for node in ast.walk(tree):
        for attr in ("name", "id", "arg", "attr"):
            v = getattr(node, attr, None)
            if isinstance(v, str):
                ids.add(v)
    return ids


# Planning-marker shape: `wave_8`, `wave12`, `Phase3Module`, `slice_2`,
# `milestone7`. The marker is the planning-vocabulary stem IMMEDIATELY
# followed by a digit (or by an underscore + digit / capitalised
# digit-bearing suffix). Standalone names like `slice_` (Python's
# builtin-shadowing convention) or `phase` (legitimate non-planning
# noun) DO NOT match — only the numbered/lettered planning markers.
_PLANNING_IDENTIFIER_RE = re.compile(
    r"^(?:wave|phase|slice|milestone)_?\d",
    re.IGNORECASE,
)


def test_no_runtime_python_identifier_uses_planning_vocabulary():
    """Walk every `.py` in `src/j1/` + assert no top-level
    identifier starts with `wave` / `phase` / `slice` / `milestone`
    followed by a digit or underscore (the planning-marker shape).

    This is the AST-level check that catches `_wave8_x` /
    `Wave6Foo` / `phase_3_helper` style names regardless of where
    they live in the module."""
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        for ident in _python_identifiers(path):
            if _PLANNING_IDENTIFIER_RE.match(ident):
                bad.append(f"{path.relative_to(REPO).as_posix()}: {ident}")
    assert not bad, (
        "Planning-vocabulary identifiers found in runtime code:\n  - "
        + "\n  - ".join(bad)
    )


# ---- 2. No runtime PY file name starts with planning marker -------


def test_no_runtime_python_file_starts_with_planning_marker():
    bad: list[str] = []
    pattern = re.compile(r"^(wave|phase|slice|milestone)[0-9_]", re.IGNORECASE)
    for path in SRC.rglob("*.py"):
        if pattern.match(path.stem):
            bad.append(path.relative_to(REPO).as_posix())
    assert not bad, (
        "Planning-marker file names in runtime code:\n  - "
        + "\n  - ".join(bad)
    )


# ---- 3. No FE source file starts with planning marker -------------


def test_no_frontend_source_file_starts_with_planning_marker():
    """FE source files (not tests). Tests under `__tests__/` have
    their own checks; this test guards the operator-visible side
    of the FE — components, pages, helpers."""
    bad: list[str] = []
    pattern = re.compile(r"^(wave|phase|slice|milestone)[0-9_-]", re.IGNORECASE)
    if not FRONTEND_SRC.exists():
        pytest.skip("frontend/ not present")
    for path in FRONTEND_SRC.rglob("*.ts"):
        if "__tests__" in path.parts:
            continue
        if pattern.match(path.stem):
            bad.append(path.relative_to(REPO).as_posix())
    for path in FRONTEND_SRC.rglob("*.tsx"):
        if "__tests__" in path.parts:
            continue
        if pattern.match(path.stem):
            bad.append(path.relative_to(REPO).as_posix())
    assert not bad, (
        "Planning-marker file names in FE source:\n  - "
        + "\n  - ".join(bad)
    )


# ---- 4. No API route contains planning marker ---------------------


def test_no_rest_api_route_contains_planning_marker():
    """Scan the REST app for route decorators referencing `wave`
    / `phase` / `slice` / `milestone` in the URL path."""
    app_py = SRC / "adapters" / "rest" / "app.py"
    if not app_py.is_file():
        pytest.skip("REST app module not present")
    src_text = app_py.read_text(encoding="utf-8")
    # All `@app.get("/...")` / `@app.post(...)` / `@app.put(...)` /
    # `@app.delete(...)` strings.
    route_pattern = re.compile(
        r'@app\.(?:get|post|put|delete|patch)\(\s*"(/[^"]+)"',
    )
    bad: list[str] = []
    forbidden = re.compile(
        r"(?:wave|phase|slice|milestone)[0-9_-]",
        re.IGNORECASE,
    )
    for m in route_pattern.finditer(src_text):
        route = m.group(1)
        if forbidden.search(route):
            bad.append(route)
    assert not bad, (
        f"Planning-marker REST routes:\n  - " + "\n  - ".join(bad)
    )


# ---- 5. No artifact kind constant uses planning marker ------------


def test_no_artifact_kind_uses_planning_marker():
    """`ARTIFACT_KIND_*` constants are the wire-stable kind strings.
    Renaming one breaks every persisted run, so this test catches
    new planning-marker kinds at definition time."""
    results_py = SRC / "processing" / "results.py"
    if not results_py.is_file():
        pytest.skip("processing.results not present")
    text = results_py.read_text(encoding="utf-8")
    pattern = re.compile(
        r'ARTIFACT_KIND_[A-Z_]+\s*=\s*"([^"]+)"',
    )
    bad: list[str] = []
    forbidden = re.compile(
        r"(?:wave|phase|slice|milestone)[0-9_-]",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        kind = m.group(1)
        if forbidden.search(kind):
            bad.append(kind)
    assert not bad, (
        f"Planning-marker artifact kinds:\n  - " + "\n  - ".join(bad)
    )


# ---- 6. No FE display / status label contains planning marker -----


def test_no_frontend_status_or_event_label_uses_planning_marker():
    """`StatusDisplay` + `EventTypeDisplay` ship label strings the
    FE renders verbatim. Planning vocabulary in either would be
    operator-visible."""
    display_ts = FRONTEND_SRC / "lib" / "display.ts"
    if not display_ts.is_file():
        pytest.skip("display.ts not present")
    text = display_ts.read_text(encoding="utf-8")
    # Find every quoted string literal.
    string_lit = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')
    forbidden = re.compile(
        r"\b(?:wave|phase|slice|milestone)\b",
        re.IGNORECASE,
    )
    bad: list[str] = []
    for m in string_lit.finditer(text):
        s = m.group(1)
        if forbidden.search(s):
            bad.append(s)
    assert not bad, (
        f"Planning-vocabulary string literals in display.ts:\n  - "
        + "\n  - ".join(bad)
    )


# ---- 7. No active docs describe architecture in planning terms ----


_ACTIVE_DOCS = (
    REPO / "docs" / "architecture.md",
    REPO / "docs" / "architecture" / "ingestion-pipeline.md",
    REPO / "docs" / "architecture" / "domain-profiles.md",
    REPO / "docs" / "architecture" / "enrichment-overlay.md",
    REPO / "docs" / "architecture" / "final-ingestion-report.md",
    REPO / "docs" / "guides" / "adding-a-domain-profile.md",
    REPO / "docs" / "guides" / "adding-an-enrichment-module.md",
    REPO / "docs" / "operations" / "production-worker-wiring.md",
    REPO / "docs" / "reference" / "artifacts.md",
    REPO / "docs" / "reference" / "ui-copy.md",
)


_DOC_ACTIVE_PLANNING_PHRASES = (
    "wave 6 introduced",
    "wave 8 introduced",
    "wave 9 introduced",
    "wave 10 introduced",
    "wave 11 introduced",
    "in phase 8",
    "phase 6 compatibility",
    "implementation wave",
    "refactor wave",
)


@pytest.mark.parametrize("doc", _ACTIVE_DOCS)
def test_active_doc_does_not_describe_architecture_in_planning_terms(doc):
    if not doc.is_file():
        pytest.skip(f"doc {doc.name} not present")
    text = doc.read_text(encoding="utf-8").lower()
    for phrase in _DOC_ACTIVE_PLANNING_PHRASES:
        assert phrase not in text, (
            f"{doc.name} describes architecture using planning phrase "
            f"{phrase!r}"
        )


# ---- 8. Backward compatibility — pre-cleanup runs still readable -


def test_legacy_skipped_reason_field_still_readable_on_old_payloads():
    """Older `enrichment_result` payloads used `reason` for the
    top-level skip reason. Newer payloads use `skipped_reason`. The
    final-report builder MUST read both — pinned here so a future
    refactor doesn't drop the back-compat path."""
    from j1.processing.final_ingestion_report import (
        ReportSourceInputs, build_final_ingestion_report,
    )

    legacy_payload = {
        "status": "skipped",
        # legacy field name
        "reason": "compile produced no chunks",
        "module_outcomes": [],
    }
    inputs = ReportSourceInputs(
        run_id="r-1",
        document_id="d-1",
        document_name=None,
        tenant_id=None,
        project_id=None,
        started_at=None,
        completed_at=None,
        framework_final_status="completed",
        failure_code=None,
        failure_message=None,
        enrichment_result=legacy_payload,
    )
    report = build_final_ingestion_report(inputs)
    # Old field name still surfaces in the summary's skipped_reason.
    assert report.enrichment_summary.skipped_reason == (
        "compile produced no chunks"
    )
