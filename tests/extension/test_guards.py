"""Static guards against extension-layer anti-patterns.

These tests are AST + filesystem checks. They run on every PR and
fail fast if:

 * The extension layer accidentally imports a project-specific or
 domain-specific module (e.g. anything under a hypothetical
 `j1.domain.*` namespace).
 * Core modules import the extension layer (the dependency arrow
 must point outward — extension depends on core, never the
 reverse).
 * Workflow code reaches into a concrete provider implementation
 instead of going through a Protocol / registry.
 * Domain names ("civil", "training-phase", "openkb", etc.) appear
 inside any J1 source file.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "j1"
EXTENSION_ROOT = SRC_ROOT / "extension"


def _python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        # Skip __pycache__
        if "__pycache__" in path.parts:
            continue
        yield path


def _imports(path: Path) -> list[str]:
    """Return the dotted module names imported by a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
    return out


# ---- Guard #1: extension layer is domain-neutral -------------------


_DOMAIN_TERMS = (
    # Industry-vertical names that must never appear in J1.
    "civil",
    "engineering",  # narrowly: "civil_engineering" / "engineering_inc"
    "construction_industry",
    "hrm",
    "rfp",
    "zendesk",
    "openkb",
    # Phase-style naming that must never appear.
    "phase_1", "phase_2", "phase_3",
    "phase1", "phase2", "phase3",
    "training_phase",
    # Customer-shaped names that suggest leakage.
    "acme_industries",  # the neutral-example "acme" name alone is OK.
)


def test_no_domain_terms_in_extension_layer():
    """The extension package itself must not reference domain terms."""
    offenders: list[tuple[Path, str]] = []
    for path in _python_files(EXTENSION_ROOT):
        text = path.read_text(encoding="utf-8").lower()
        for term in _DOMAIN_TERMS:
            # Word-boundary regex to avoid false positives.
            if re.search(rf"\b{re.escape(term)}\b", text):
                offenders.append((path, term))
    assert not offenders, (
        f"Extension layer leaks domain terms: {offenders!r}"
    )


def test_no_domain_terms_in_j1_core():
    """Core (everything outside `profiles/` and `domains/`) must not
 reference domain terms.

 Two exemptions:

 1. `profiles/` — the bundled `default` profile is intentionally
 generic; the directory is the documented place for
 deployments to inject domain content WITHOUT touching core.
 2. `domains/` — domain packs (e.g. Civil Engineering) live
 here. Pack code is allowed to reference its own domain
 vocabulary; the abstraction in `domains/models.py` and
 `domains/registry.py` may name specific packs in
 documentation. The wider core (everything outside
 `domains/`) still must stay domain-neutral.
 """
    offenders: list[tuple[Path, str]] = []
    for path in _python_files(SRC_ROOT):
        if "profiles" in path.parts or "domains" in path.parts:
            continue
        text = path.read_text(encoding="utf-8").lower()
        for term in _DOMAIN_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", text):
                offenders.append((path.relative_to(SRC_ROOT), term))
    assert not offenders, (
        f"Core leaks domain terms: {offenders!r}"
    )


# ---- Guard #2: core does not depend on extension -------------------


def test_core_does_not_import_extension():
    """Dependency direction — extension depends on core, NOT the reverse.

 Any core module importing `j1.extension.*` would create a
 coupling that defeats the whole point of the extension layer.

 One **explicit, single-purpose** exception: the composition root
 (`j1.compose.bootstrap`) is allowed to import the bundled mock
 adapters from `j1.extension.mocks` so deployments can select
 `J1_DEFAULT_*=mock` to bring up a deterministic smoke pipeline
 with zero vendor dependencies. The composition root is the one
 place in core where wiring extension-layer reference adapters as
 runnable defaults is legitimate. No other core module may do
 this; no other path under `j1.extension.*` may be imported from
 core.
 """
    # (importing module, imported module) pairs that the rule allows.
    ALLOWED = {
        ("compose/bootstrap.py", "j1.extension.mocks"),
    }
    offenders: list[tuple[str, str]] = []
    for path in _python_files(SRC_ROOT):
        # Skip the extension layer itself.
        if "extension" in path.parts:
            continue
        rel = str(path.relative_to(SRC_ROOT))
        for module in _imports(path):
            if module == "j1.extension" or module.startswith("j1.extension."):
                if (rel, module) in ALLOWED:
                    continue
                offenders.append((rel, module))
    assert not offenders, (
        f"Core modules import the extension layer (forbidden): {offenders!r}. "
        f"If you have a legitimate composition-root use case, document it "
        f"and add the (path, module) pair to ALLOWED in this test."
    )


# ---- Guard #3: extension contracts only depend on primitives -------


def test_extension_contracts_only_imports_primitives_and_legacy_protocols():
    """`j1.extension.contracts` must not pull in concrete provider /
 adapter implementations or domain-side modules.

 Allowed imports:
 * `typing` / standard library
 * `j1.extension.primitives`
 * `j1.processing.contracts` (legacy Protocol aliases)
 """
    contracts_path = EXTENSION_ROOT / "contracts.py"
    assert contracts_path.exists()
    allowed = {
        "typing",
        "__future__",
        "j1.extension.primitives",
        "j1.processing.contracts",
    }
    seen = set(_imports(contracts_path))
    forbidden = {m for m in seen if m not in allowed}
    assert not forbidden, (
        f"j1.extension.contracts imports forbidden modules: {forbidden!r}. "
        f"Allowed: {sorted(allowed)}"
    )


# ---- Guard #4: workflow code goes through Protocols, not concretes -


_WORKFLOW_DIRS = [
    SRC_ROOT / "orchestration" / "workflows",
    SRC_ROOT / "orchestration" / "activities",
]
# Forbidden direct concrete-provider imports inside workflow code.
# The pattern `j1.providers.<vendor>.<concrete-class>` is fine in the
# composition root; workflows must not name vendor classes.
_FORBIDDEN_WORKFLOW_IMPORTS = (
    "j1.providers.raganything",
    "j1.providers.graphify",
    "j1.llm.openai_compat",
    "j1.llm.langchain_adapter",
)


def test_workflows_do_not_import_concrete_providers():
    offenders: list[tuple[Path, str]] = []
    for workflow_dir in _WORKFLOW_DIRS:
        for path in _python_files(workflow_dir):
            for module in _imports(path):
                for forbidden in _FORBIDDEN_WORKFLOW_IMPORTS:
                    if module == forbidden or module.startswith(forbidden + "."):
                        offenders.append((path.relative_to(SRC_ROOT), module))
    assert not offenders, (
        f"Workflow / activity modules reach into concrete provider "
        f"implementations: {offenders!r}. Use Protocol-typed registries "
        f"injected by the composition root instead."
    )


# ---- Guard #5: mocks live with mocks -------------------------------


def test_mocks_only_in_extension_mocks_module():
    """Mock adapters (`Mock*Adapter` / `Mock*Connector`) should live
 in `j1.extension.mocks` only — anywhere else suggests a test
 fixture leaked into core.

 Test fixtures in `tests/` are exempt by living outside `src/`.
 """
    offenders: list[tuple[Path, str]] = []
    pattern = re.compile(r"^class\s+Mock(?:[A-Z]\w*Adapter|SourceConnector)\b",
                         re.MULTILINE)
    for path in _python_files(SRC_ROOT):
        if path.parent.name == "extension" and path.name == "mocks.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append((path.relative_to(SRC_ROOT), match.group(0)))
    assert not offenders, (
        f"Mock adapters defined outside `j1.extension.mocks`: {offenders!r}"
    )
