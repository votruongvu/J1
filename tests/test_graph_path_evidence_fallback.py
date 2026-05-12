"""Tests for the graph-paths-to-evidence fallback path.

Bug fixed:
   When the engine returned a graph-only result (e.g. for a
   question routed to ``GraphQueryProvider``), every retrieved
   source was ``kind="graph_json"``. The evidence builder
   correctly dropped them via ``_SKIP_KINDS`` (graph JSON blobs
   are not textual prose). But the engine ALSO surfaced parsed
   ``graph_paths`` in ``response.graph_paths`` — which ARE prose,
   just not loaded via the textual evidence builder. With no
   evidence to ground on, the synthesizer returned
   ``error="no_evidence"`` even though the retrieval preview in
   the FE showed real graph relationships.

   The fix: when textual evidence is empty AND
   ``response.graph_paths`` is non-empty, the service
   synthesises a single ``artifact_type="graph_paths"``
   ``EvidenceBlockDTO`` rendering the paths as a bullet list so
   the synthesizer has prose to work with.
"""

from __future__ import annotations

import pytest

from j1.query.models import GraphPath, SourceReference
from j1.validation.dtos import EvidenceBlockDTO
from j1.validation.evidence import build_graph_path_evidence


# ---- Headline regression -----------------------------------------


def test_graph_paths_render_as_evidence_bullets():
    """Multiple paths → one evidence block with a bullet-list text
    body. Each path becomes a line like '- A → B (rel)'."""
    paths = [
        GraphPath(nodes=["J1 Platform", "MinerU"], edges=["uses"]),
        GraphPath(nodes=["J1 Platform", "RAGAnything"], edges=["uses"]),
        GraphPath(nodes=["Civil Engineering", "Domain Enrichment"], edges=["related_to"]),
    ]
    blocks = build_graph_path_evidence(paths)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.artifact_type == "graph_paths"
    # All three rendered.
    assert "J1 Platform → MinerU (uses)" in block.text
    assert "J1 Platform → RAGAnything (uses)" in block.text
    assert "Civil Engineering → Domain Enrichment (related_to)" in block.text
    # Header line gives the LLM context.
    assert "Graph relationships" in block.text


def test_description_overrides_default_rendering():
    """When a ``GraphPath.description`` is set, the block uses it
    verbatim rather than the arrow-chain default. Useful for
    domain-specific edge labels."""
    paths = [
        GraphPath(
            nodes=["A", "B"], edges=["x"],
            description="A leads to B via approval workflow",
        ),
    ]
    blocks = build_graph_path_evidence(paths)
    assert "A leads to B via approval workflow" in blocks[0].text
    # Arrow form omitted when description present.
    assert "A → B" not in blocks[0].text


def test_multi_hop_path_renders_arrow_chain():
    """A 3-node path renders 'A → B → C'."""
    paths = [
        GraphPath(nodes=["A", "B", "C"], edges=["rel1", "rel2"]),
    ]
    blocks = build_graph_path_evidence(paths)
    assert "A → B → C" in blocks[0].text


def test_empty_graph_paths_returns_no_blocks():
    """No paths → no synthetic evidence. The caller falls through
    to its existing no_evidence flow."""
    assert build_graph_path_evidence([]) == []
    assert build_graph_path_evidence(None) == []  # type: ignore[arg-type]


def test_single_node_path_skipped():
    """Paths with <2 nodes can't be rendered as a relationship —
    silently skipped. (Defensive: a malformed engine emitting a
    one-node 'path' won't crash the fallback.)"""
    paths = [GraphPath(nodes=["loner"], edges=[])]
    assert build_graph_path_evidence(paths) == []


def test_anchors_to_first_graph_json_source(monkeypatch):
    """The synthetic block's ``artifact_id`` matches the first
    graph_json source's id — so the grounding judge can trace the
    evidence back to a real artifact in the registry."""
    paths = [GraphPath(nodes=["A", "B"], edges=["x"])]
    sources = [
        SourceReference(
            artifact_id="chunk-1", artifact_type="chunk", title="chunk-1",
        ),
        SourceReference(
            artifact_id="graph-real-id", artifact_type="graph_json",
            title="graph-real-id",
        ),
    ]
    blocks = build_graph_path_evidence(paths, sources=sources)
    assert blocks[0].artifact_id == "graph-real-id"


def test_falls_back_to_synthetic_id_when_no_graph_source():
    """When the engine surfaces graph_paths but no graph_json
    sources (rare — usually only happens in tests), the synthetic
    block uses a marker id so it's clearly synthesized."""
    paths = [GraphPath(nodes=["A", "B"], edges=["x"])]
    blocks = build_graph_path_evidence(paths)
    assert blocks[0].artifact_id.startswith("graph_paths:synthetic")


def test_text_cap_truncates_overlong_paths():
    """Many paths → text capped to ``_GRAPH_PATHS_TEXT_CAP`` so the
    fallback can't blow the synthesizer's context budget alone."""
    from j1.validation.evidence import _GRAPH_PATHS_TEXT_CAP

    # 50 paths × ~30 chars each → would be ~1500 chars without the
    # max_lines limit; cap also bounds total length.
    paths = [
        GraphPath(nodes=[f"Entity{i}", f"Other{i}"], edges=["rel"])
        for i in range(50)
    ]
    blocks = build_graph_path_evidence(paths)
    # Text capped (either by max_lines or by the char cap).
    assert len(blocks[0].text) <= _GRAPH_PATHS_TEXT_CAP + 1  # +1 for trailing "…"


def test_evidence_block_has_no_chunk_id_no_score():
    """Synthetic evidence carries no chunk_id (the relationships
    span the document, not a single chunk) and a neutral score."""
    paths = [GraphPath(nodes=["A", "B"], edges=["x"])]
    block = build_graph_path_evidence(paths)[0]
    assert block.chunk_id is None
    assert block.score == 0.0
