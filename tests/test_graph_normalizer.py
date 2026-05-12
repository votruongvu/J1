"""Tests for `j1.graph.normalizer`.

The normalizer is the layer that lets `GraphQueryProvider` parse
graph artifacts in any of the three on-disk shapes the codebase
might encounter:

 * Canonical first-party `{nodes: [...], edges: [...]}` from
   `j1.providers.graphify`.
 * LightRAG `{entities: [...], relationships: [...]}` (and its
   minor field-name variants).
 * GraphML XML.

Failure mode: unrecognised shapes log + return `None` so the
caller can produce an actionable "graph artifact present but
unparseable" warning instead of the misleading "no graph
relationships found".
"""

from __future__ import annotations

import json

import pytest

from j1.graph import (
    GraphEntity,
    GraphRelationship,
    NormalizedGraph,
    normalize_graph_bytes,
    normalize_graph_text,
)


# ---- Canonical shape (first-party graphify) -----------------------


def test_canonical_nodes_and_edges_parse():
    data = {
        "nodes": [{"id": "Alice"}, {"id": "Bob"}],
        "edges": [{"from": "Alice", "to": "Bob", "type": "knows"}],
    }
    g = normalize_graph_text(
        json.dumps(data),
        source_artifact_id="art-1",
        run_id="run-1",
    )
    assert g is not None
    assert g.source_format == "canonical"
    assert g.source_artifact_id == "art-1"
    assert g.run_id == "run-1"
    assert g.entities == (
        GraphEntity(id="Alice", label="Alice"),
        GraphEntity(id="Bob", label="Bob"),
    )
    assert g.relationships == (
        GraphRelationship(from_id="Alice", to_id="Bob", kind="knows"),
    )


def test_canonical_accepts_legacy_source_target_edge_fields():
    """Some early graphify versions used `source`/`target` instead
 of `from`/`to`. The normalizer must accept both so older
 replayed runs don't regress."""
    data = {
        "nodes": [{"id": "X"}, {"id": "Y"}],
        "edges": [{"source": "X", "target": "Y"}],
    }
    g = normalize_graph_text(
        json.dumps(data), source_artifact_id="art-1",
    )
    assert g is not None
    assert g.relationships[0].from_id == "X"
    assert g.relationships[0].to_id == "Y"
    # Edge type missing → default "related_to" so the runner's
    # `_check_expected_graph_evidence` still has a non-empty edge
    # list to match against.
    assert g.relationships[0].kind == "related_to"


# ---- LightRAG shape -----------------------------------------------


def test_lightrag_entities_and_relationships_parse():
    data = {
        "entities": [
            {"entity_name": "Alice", "entity_type": "Person"},
            {"entity_name": "Bob", "entity_type": "Person"},
        ],
        "relationships": [
            {
                "src_id": "Alice", "tgt_id": "Bob",
                "predicate": "knows",
                "description": "long-standing colleague",
            },
        ],
    }
    g = normalize_graph_text(json.dumps(data), source_artifact_id="art-2")
    assert g is not None
    assert g.source_format == "lightrag"
    assert [e.id for e in g.entities] == ["Alice", "Bob"]
    assert g.entities[0].type == "Person"
    assert g.relationships[0].kind == "knows"
    assert g.relationships[0].label == "long-standing colleague"


def test_lightrag_accepts_source_target_alias():
    """LightRAG itself isn't consistent about field names across
 versions — accept the common aliases so we don't have to keep
 chasing changes."""
    data = {
        "entities": [{"name": "X"}, {"name": "Y"}],
        "relations": [{"source": "X", "target": "Y", "type": "links_to"}],
    }
    g = normalize_graph_text(json.dumps(data), source_artifact_id="art-2")
    assert g is not None
    assert g.source_format == "lightrag"
    assert g.relationships[0].from_id == "X"
    assert g.relationships[0].kind == "links_to"


# ---- GraphML XML --------------------------------------------------


def test_graphml_parses():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <graph id="G" edgedefault="directed">
    <node id="A"/>
    <node id="B"/>
    <edge source="A" target="B" label="links"/>
  </graph>
</graphml>"""
    g = normalize_graph_text(xml, source_artifact_id="art-3")
    assert g is not None
    assert g.source_format == "graphml"
    assert {e.id for e in g.entities} == {"A", "B"}
    assert g.relationships[0].from_id == "A"
    assert g.relationships[0].to_id == "B"
    assert g.relationships[0].label == "links"


def test_graphml_with_no_namespace_still_parses():
    """GraphML can be authored without the xmlns. The tag-stripper
 must handle both."""
    xml = "<graphml><node id=\"A\"/><node id=\"B\"/>" \
          "<edge source=\"A\" target=\"B\"/></graphml>"
    g = normalize_graph_text(xml, source_artifact_id="art-3")
    assert g is not None
    assert g.source_format == "graphml"


# ---- Failure modes ------------------------------------------------


def test_unknown_json_shape_returns_none(caplog):
    """Top-level object with neither nodes/edges nor entities/
 relationships → unrecognised. None signals the caller to
 surface a "format unknown" warning."""
    data = {"foo": "bar", "items": [1, 2, 3]}
    g = normalize_graph_text(json.dumps(data), source_artifact_id="art-x")
    assert g is None


def test_invalid_json_returns_none():
    g = normalize_graph_text("{not: valid json", source_artifact_id="art-x")
    assert g is None


def test_invalid_xml_falls_back_to_none():
    g = normalize_graph_text("<graphml><incomplete", source_artifact_id="art-x")
    assert g is None


def test_empty_input_returns_none():
    assert normalize_graph_text("", source_artifact_id="art-x") is None
    assert normalize_graph_text("   \n\n  ", source_artifact_id="art-x") is None


def test_bytes_entry_point_handles_utf8():
    """Real artifacts are bytes; entry point should decode + dispatch."""
    raw = json.dumps({
        "nodes": [{"id": "n"}],
        "edges": [{"from": "n", "to": "n"}],
    }).encode("utf-8")
    g = normalize_graph_bytes(raw, source_artifact_id="art-b")
    assert g is not None
    assert g.entities[0].id == "n"


def test_bytes_entry_point_recovers_from_invalid_utf8():
    """Partially-corrupted bytes shouldn't raise — replace errors
 so we still try to parse what's there."""
    raw = b"\x80\x81" + json.dumps({"nodes": [], "edges": []}).encode("utf-8")
    # First bytes are invalid UTF-8; replace policy means decode
    # succeeds but the JSON parser will reject the prefix. Should
    # return None cleanly, not raise.
    g = normalize_graph_bytes(raw, source_artifact_id="art-b")
    assert g is None
