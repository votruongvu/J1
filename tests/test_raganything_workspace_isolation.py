"""Per-run LightRAG workspace isolation tests.

The latest validation report flagged 7 graph_json rows with
``run_id=None`` leaking into retrieval. Root causes included
LightRAG's storage being shared across runs (``working_dir`` defaulted
to a single global path keyed only by ``document_id``) — so a reindex
would overwrite the previous run's graphml.

These tests pin the per-run isolation contract:

  * ``workspace_path_for_run`` builds the expected scoped path.
  * Two reindex attempts for the same document write to DIFFERENT
    subdirectories — neither overwrites the other's graphml.
  * Missing scoping inputs (legacy/test callers) fall back to the
    historical unscoped workdir, preserving backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _settings(workdir: Path):
    from j1.providers.raganything.settings import RAGAnythingSettings

    return RAGAnythingSettings(workdir=str(workdir))


# ---- Path-builder unit tests ------------------------------------


def test_workspace_path_for_run_namespace_layout(tmp_path: Path, ctx):
    """The path is ``{workdir}/runs/{tenant}/{project}/{doc}/{run}/``
    — four levels of namespace so retention/detach/remove can prune
    by deleting the appropriate subtree."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    path = workspace_path_for_run(settings, ctx, "doc-a", "run-1")
    assert path is not None
    assert path == tmp_path / "runs" / "t1" / "p1" / "doc-a" / "run-1"


def test_workspace_path_for_run_returns_none_when_run_id_missing(
    tmp_path: Path, ctx,
):
    """No run_id → no per-run isolation possible → return None and
    let the caller fall back to ``settings.workdir`` (legacy
    behaviour)."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    assert workspace_path_for_run(settings, ctx, "doc-a", None) is None
    assert workspace_path_for_run(settings, ctx, "doc-a", "") is None
    assert workspace_path_for_run(settings, ctx, None, "run-1") is None


def test_workspace_path_for_run_returns_none_when_ctx_missing(tmp_path: Path):
    """No ctx (= no tenant/project) → no namespace → None."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    assert workspace_path_for_run(settings, None, "doc-a", "run-1") is None


def test_workspace_path_for_run_returns_none_when_workdir_missing(ctx):
    """No workdir → can't build the path → None. Defensive: should
    not crash on a settings object that's missing the field."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    class _Empty:
        workdir = ""

    assert workspace_path_for_run(_Empty(), ctx, "doc-a", "run-1") is None


# ---- Isolation contract: two runs do not overwrite each other ----


def test_two_runs_for_same_document_get_distinct_paths(
    tmp_path: Path, ctx,
):
    """The headline regression: an initial run + a reindex run for
    the SAME document produce two distinct working_dirs. Without
    this, the second run's compile would overwrite the first run's
    graphml and queries against the first run's RunScope would
    silently read the second run's data."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    run_a = workspace_path_for_run(settings, ctx, "doc-a", "run-1")
    run_b = workspace_path_for_run(settings, ctx, "doc-a", "run-2")
    assert run_a != run_b
    assert run_a is not None and run_b is not None
    assert run_a.parent == run_b.parent  # same document subtree
    assert run_a.parent.name == "doc-a"  # named for the document


def test_simulated_two_run_compile_does_not_overwrite_graphml(
    tmp_path: Path, ctx,
):
    """End-to-end style: simulate two compile runs writing to their
    own scoped working dirs (the exact behaviour LightRAG's
    NetworkXStorage will exhibit when we pass it the scoped
    ``working_dir``). Both files survive after both runs complete —
    neither overwrote the other."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    run1_dir = workspace_path_for_run(settings, ctx, "doc-a", "run-1")
    run2_dir = workspace_path_for_run(settings, ctx, "doc-a", "run-2")
    assert run1_dir is not None and run2_dir is not None

    # Simulate LightRAG's graphml writes — separate content per run.
    run1_dir.mkdir(parents=True, exist_ok=True)
    run2_dir.mkdir(parents=True, exist_ok=True)
    (run1_dir / "graph_chunk_entity_relation.graphml").write_text(
        "<graph>run-1-content</graph>"
    )
    (run2_dir / "graph_chunk_entity_relation.graphml").write_text(
        "<graph>run-2-content</graph>"
    )

    # Both files still exist — neither overwrote the other.
    assert (run1_dir / "graph_chunk_entity_relation.graphml").read_text() == (
        "<graph>run-1-content</graph>"
    )
    assert (run2_dir / "graph_chunk_entity_relation.graphml").read_text() == (
        "<graph>run-2-content</graph>"
    )


# ---- Cleanup behaviour for retention policy ----------------------


def test_per_document_subtree_can_be_pruned_atomically(tmp_path: Path, ctx):
    """Detach/remove cleanup: deleting ``{document_id}/`` removes
    every run's graph data for that document in one rm -rf. This is
    the retention contract — operators don't have to enumerate
    per-run subdirs."""
    from j1.providers.raganything._bridge import workspace_path_for_run

    settings = _settings(tmp_path)
    run1 = workspace_path_for_run(settings, ctx, "doc-a", "run-1")
    run2 = workspace_path_for_run(settings, ctx, "doc-a", "run-2")
    sibling_doc = workspace_path_for_run(settings, ctx, "doc-b", "run-1")
    assert run1 and run2 and sibling_doc
    run1.mkdir(parents=True, exist_ok=True)
    run2.mkdir(parents=True, exist_ok=True)
    sibling_doc.mkdir(parents=True, exist_ok=True)
    (run1 / "g.graphml").write_text("a")
    (run2 / "g.graphml").write_text("b")
    (sibling_doc / "g.graphml").write_text("c")

    # Prune doc-a's subtree.
    import shutil
    shutil.rmtree(run1.parent)

    # doc-a's runs are gone; doc-b's runs survive.
    assert not run1.exists()
    assert not run2.exists()
    assert sibling_doc.exists()
    assert (sibling_doc / "g.graphml").read_text() == "c"
