"""Tests for ``_graph_drafts_from_storage`` lineage stamping.

The producer-layer change addresses the production failure mode the
latest validation report flagged: graph_json artifacts reaching the
registry without ``metadata.run_id``. Earlier the function emitted
drafts with empty metadata and relied on the orchestration
registration to stamp run_id from ``correlation_id`` — that
silently lost the lineage when the producer was invoked outside
orchestration. The new contract: stamp at the draft layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1")


def _make_graph_file(storage_dir: Path, filename: str = "graph.json") -> Path:
    storage_dir.mkdir(parents=True, exist_ok=True)
    f = storage_dir / filename
    f.write_text('{"entities": [{"id": "e1"}]}', encoding="utf-8")
    return f


# ---- Happy path: full lineage stamping ---------------------------


def test_full_lineage_stamped_when_all_inputs_present(tmp_path: Path, ctx):
    """The headline contract: every emitted draft carries
    ``metadata.run_id`` + ``metadata.document_id`` + tenant/project
    fields. ``source_document_ids`` is set to ``[document_id]`` for
    document-scoped sweeps."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    _make_graph_file(tmp_path)
    drafts = _graph_drafts_from_storage(
        tmp_path,
        artifact_ids=["chunk-1", "chunk-2"],
        ctx=ctx,
        document_id="doc-a",
        run_id="run-1",
    )

    assert len(drafts) == 1
    d = drafts[0]
    assert d.kind == ARTIFACT_KIND_GRAPH_JSON
    # Lineage metadata stamped directly — independent of registration.
    assert d.metadata["run_id"] == "run-1"
    assert d.metadata["document_id"] == "doc-a"
    assert d.metadata["tenant_id"] == "t1"
    assert d.metadata["project_id"] == "p1"
    # Document binding for the document-scoped sweep.
    assert d.source_document_ids == ["doc-a"]
    # ``source_artifact_ids`` stays empty — the chunk-grounding
    # validator would strand non-chunk ids in this list. Run-scope
    # safety is provided by metadata.run_id instead.
    assert d.source_artifact_ids == []


def test_filename_and_relative_path_preserved_in_metadata(tmp_path: Path, ctx):
    """Existing metadata fields (filename, relative_path) keep
    working — the lineage fields are additive, not a replacement."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    _make_graph_file(tmp_path, filename="graph_chunk_entity_relation.json")
    drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=[], ctx=ctx,
        document_id="doc-a", run_id="run-1",
    )
    assert drafts[0].metadata["filename"] == "graph_chunk_entity_relation.json"
    assert drafts[0].metadata["relative_path"] == "graph_chunk_entity_relation.json"


# ---- Defensive: partial inputs ----------------------------------


def test_no_run_id_does_not_stamp_run_id_key(tmp_path: Path, ctx):
    """When the caller forgets to pass ``run_id``, the draft's
    metadata simply lacks the key. The orchestration / legacy
    registration path then fails the strict guard — that's the
    intended fail-fast behaviour. We do NOT silently drop the
    draft or stamp a placeholder, because both would hide the
    producer-side bug."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    _make_graph_file(tmp_path)
    drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=[], ctx=ctx,
        document_id="doc-a", run_id=None,
    )
    assert len(drafts) == 1
    assert "run_id" not in drafts[0].metadata
    # document_id IS stamped if available — it's independent of
    # run_id and useful for the document-scoped sweep.
    assert drafts[0].metadata["document_id"] == "doc-a"


def test_no_document_id_omits_document_binding(tmp_path: Path, ctx):
    """Missing document_id → no ``source_document_ids`` binding.
    The document-scoped sweep won't match, but the project-wide
    lineage sweep still catches the orphan via the run_id check."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    _make_graph_file(tmp_path)
    drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=[], ctx=ctx,
        document_id=None, run_id="run-1",
    )
    assert drafts[0].source_document_ids == []
    assert "document_id" not in drafts[0].metadata
    assert drafts[0].metadata["run_id"] == "run-1"


def test_no_ctx_omits_tenant_project_fields(tmp_path: Path):
    """No ctx → no tenant_id / project_id fields. Still works (the
    helper is defensive against partial inputs)."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    _make_graph_file(tmp_path)
    drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=[], ctx=None,
        document_id="doc-a", run_id="run-1",
    )
    assert "tenant_id" not in drafts[0].metadata
    assert "project_id" not in drafts[0].metadata
    assert drafts[0].metadata["run_id"] == "run-1"


# ---- Non-graph files are still excluded -------------------------


def test_kv_store_text_chunks_excluded(tmp_path: Path, ctx):
    """``kv_store_text_chunks.json`` is chunk data, not graph data —
    excluded by name pattern. Lineage stamping doesn't change the
    filter logic."""
    from j1.providers.raganything._bridge import _graph_drafts_from_storage

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "kv_store_text_chunks.json").write_text(
        '{"chunk_1": "text"}'
    )
    # Plus a real graph file.
    _make_graph_file(tmp_path, filename="kv_store_full_entities.json")

    drafts = _graph_drafts_from_storage(
        tmp_path, artifact_ids=[], ctx=ctx,
        document_id="doc-a", run_id="run-1",
    )
    # Only the entities file should be emitted.
    assert len(drafts) == 1
    assert drafts[0].metadata["filename"] == "kv_store_full_entities.json"
