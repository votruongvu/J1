"""Unit tests for GraphSnapshotProjector — no service, no REST.

Exercises the LightRAG-format → neutral DTO mapping, per-list caps,
skipped-reason handling, vendor-internal field stripping, and shape
tolerance (top-level list / keyed dict / wrapped object).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.projectors import GraphSnapshotProjector
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


def _record(
    artifact_id: str,
    location: str,
    *,
    kind: str = "graph_json",
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ProjectContext(tenant_id="acme", project_id="alpha"),
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        metadata=metadata or {},
    )


def _resolver(mapping: dict[str, Path]):
    def _resolve(record: ArtifactRecord) -> Path:
        return mapping[record.artifact_id]
    return _resolve


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_GRAPHML_SAMPLE = """\
<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="d0" for="node" attr.name="entity_type" attr.type="string"/>
  <key id="d1" for="node" attr.name="description" attr.type="string"/>
  <key id="d2" for="node" attr.name="source_id" attr.type="string"/>
  <key id="d3" for="edge" attr.name="weight" attr.type="double"/>
  <key id="d4" for="edge" attr.name="description" attr.type="string"/>
  <key id="d5" for="edge" attr.name="keywords" attr.type="string"/>
  <graph edgedefault="undirected">
    <node id="Acme Corp">
      <data key="d0">ORGANIZATION</data>
      <data key="d1">A logistics company.</data>
      <data key="d2">chunk-1;chunk-2</data>
    </node>
    <node id="Jane Doe">
      <data key="d0">PERSON</data>
      <data key="d1">CEO of Acme.</data>
      <data key="d2">chunk-1</data>
    </node>
    <edge source="Acme Corp" target="Jane Doe">
      <data key="d3">8.5</data>
      <data key="d4">Jane is CEO of Acme.</data>
      <data key="d5">leadership, role</data>
    </edge>
  </graph>
</graphml>
"""


# ---- Unavailable path -----------------------------------------------


def test_project_returns_unavailable_when_caller_supplies_reason():
    """Service-supplied reason wins — projector doesn't fight it."""
    projector = GraphSnapshotProjector(path_resolver=lambda _r: Path("/nope"))
    snapshot = projector.project(
        artifacts=[],
        unavailable_reason="Graph generation was skipped by policy.",
    )
    assert snapshot.unavailable.reason == "Graph generation was skipped by policy."
    assert snapshot.entities == []
    assert snapshot.relations == []
    assert snapshot.stats.entity_count == 0


def test_project_returns_generic_unavailable_when_no_artifacts():
    """No graph artifacts AND no caller-supplied reason → generic
    fallback. This is the defense-in-depth branch for direct
    projector callers (tests); the service always passes a reason."""
    projector = GraphSnapshotProjector(path_resolver=lambda _r: Path("/nope"))
    snapshot = projector.project(artifacts=[])
    assert snapshot.unavailable is not None
    assert "graph" in snapshot.unavailable.reason.lower()


def test_project_skips_non_graph_artifacts(tmp_path):
    p = tmp_path / "x.json"
    _write_json(p, {"chunks": []})
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": p}))
    snapshot = projector.project(
        artifacts=[_record("a1", "compiled/x.json", kind="chunk")],
    )
    # Wrong kind → treated as if no graph artifacts present.
    assert snapshot.unavailable is not None


# ---- Entity projection ----------------------------------------------


def test_project_extracts_entities_from_keyed_dict_lightrag_shape(tmp_path):
    """LightRAG's `vdb_entities.json` is typically a top-level dict
    keyed by entity id with `__id__` / `__entity_type__` /
    `__source_id__` fields per record."""
    path = tmp_path / "vdb_entities.json"
    _write_json(path, {
        "person:alice": {
            "__id__": "person:alice",
            "__name__": "Alice",
            "__entity_type__": "PERSON",
            "__description__": "Alice from chapter 1",
            "__source_id__": "chunk-1;chunk-2",
            "__vector__": [0.1, 0.2],  # vendor-internal — must be stripped
        },
        "place:bob": {
            "__id__": "place:bob",
            "__entity_type__": "PLACE",
            "__source_id__": "chunk-3",
        },
    })
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert snapshot.unavailable is None
    assert snapshot.stats.entity_count == 2

    by_id = {e.id: e for e in snapshot.entities}
    alice = by_id["person:alice"]
    assert alice.label == "Alice"
    assert alice.type == "PERSON"
    assert alice.description == "Alice from chapter 1"
    assert alice.source_chunk_ids == ["chunk-1", "chunk-2"]
    assert alice.source_artifact_ids == ["a1"]
    # Vendor-internal vector dropped.
    assert "__vector__" not in alice.metadata


def test_project_accepts_top_level_array_of_entities(tmp_path):
    path = tmp_path / "vdb_entities.json"
    _write_json(path, [
        {"id": "e1", "name": "First", "entity_type": "X"},
        {"id": "e2", "name": "Second", "entity_type": "Y"},
    ])
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert [e.id for e in snapshot.entities] == ["e1", "e2"]


def test_project_accepts_wrapped_entities_key(tmp_path):
    path = tmp_path / "vdb_entities.json"
    _write_json(path, {"entities": [
        {"id": "e1", "name": "wrapped"},
    ]})
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert len(snapshot.entities) == 1
    assert snapshot.entities[0].id == "e1"


def test_project_synthesises_entity_id_when_no_identifying_fields(tmp_path):
    """No `id` / `name` / `entity_name` AND no dict key → projector
    fabricates `<artifact_id>#node-<index>` so the FE can still
    address the node."""
    path = tmp_path / "vdb_entities.json"
    # Only carries a description; nothing usable as an id.
    _write_json(path, [{"description": "anonymous record"}])
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert snapshot.entities[0].id == "a1#node-0"


def test_project_uses_name_field_as_id_when_no_explicit_id(tmp_path):
    """`name` / `entity_name` / `__name__` is in the id priority
    chain — a record with only a name still gets a sensible id."""
    path = tmp_path / "vdb_entities.json"
    _write_json(path, [{"name": "Alice"}])
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert snapshot.entities[0].id == "Alice"
    assert snapshot.entities[0].label == "Alice"


def test_project_splits_source_ids_on_sep_delimiter(tmp_path):
    """LightRAG variants delimit `source_id` with `<SEP>` instead of
    semicolons. Projector handles both."""
    path = tmp_path / "vdb_entities.json"
    _write_json(path, {
        "e1": {"__source_id__": "chunk-a<SEP>chunk-b<SEP>chunk-c"},
    })
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert snapshot.entities[0].source_chunk_ids == [
        "chunk-a", "chunk-b", "chunk-c",
    ]


# ---- Relation projection --------------------------------------------


def test_project_extracts_relations_with_lightrag_field_names(tmp_path):
    path = tmp_path / "vdb_relationships.json"
    _write_json(path, [
        {
            "__src__": "alice",
            "__tgt__": "bob",
            "__keywords__": "knows",
            "__description__": "Alice knows Bob",
            "__weight__": 0.85,
            "__source_id__": "chunk-1;chunk-2",
        },
    ])
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_relationships.json")],
    )
    assert len(snapshot.relations) == 1
    rel = snapshot.relations[0]
    assert rel.source_entity_id == "alice"
    assert rel.target_entity_id == "bob"
    assert rel.label == "knows"
    assert rel.description == "Alice knows Bob"
    assert rel.weight == 0.85
    assert rel.source_chunk_ids == ["chunk-1", "chunk-2"]


def test_project_drops_relation_without_endpoints(tmp_path):
    """A relation record missing src or tgt is undrawable — drop it
    silently rather than crash the snapshot."""
    path = tmp_path / "vdb_relationships.json"
    _write_json(path, [
        {"src": "a", "tgt": "b", "label": "ok"},
        {"src": "a", "label": "bad-no-tgt"},
        {"label": "bad-no-src-or-tgt"},
    ])
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_relationships.json")],
    )
    assert len(snapshot.relations) == 1
    assert snapshot.relations[0].label == "ok"


# ---- Mixed entities + relations file -------------------------------


def test_project_handles_mixed_entity_relation_file(tmp_path):
    """`graph_chunk_entity_relation.json` carries both entities AND
    relations under their own keys. Projector must surface both."""
    path = tmp_path / "graph_chunk_entity_relation.json"
    _write_json(path, {
        "entities": [{"id": "e1", "name": "X"}],
        "relations": [{"src": "e1", "tgt": "e2"}],
    })
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/graph_chunk_entity_relation.json")],
    )
    assert snapshot.stats.entity_count == 1
    assert snapshot.stats.relation_count == 1


# ---- Internal KV stores skipped ------------------------------------


def test_project_skips_internal_kv_store_files(tmp_path):
    """LightRAG's `kv_store_*.json` files (text chunks, doc status,
    LLM cache) carry no graph data — projector must skip cleanly."""
    paths = {
        "k1": tmp_path / "kv_store_text_chunks.json",
        "k2": tmp_path / "kv_store_full_docs.json",
        "k3": tmp_path / "kv_store_llm_response_cache.json",
    }
    for p in paths.values():
        _write_json(p, {"some": "data"})

    projector = GraphSnapshotProjector(path_resolver=_resolver(paths))
    snapshot = projector.project(
        artifacts=[
            _record("k1", "graph/kv_store_text_chunks.json"),
            _record("k2", "graph/kv_store_full_docs.json"),
            _record("k3", "graph/kv_store_llm_response_cache.json"),
        ],
    )
    # Graph artifacts EXIST (so unavailable is None) but nothing
    # graph-shaped was found.
    assert snapshot.unavailable is None
    assert snapshot.stats.entity_count == 0
    assert snapshot.stats.relation_count == 0


# ---- Caps ----------------------------------------------------------


def test_project_truncates_entities_at_max_nodes(tmp_path):
    payload = {
        f"e{i}": {"id": f"e{i}", "name": f"Entity {i}"}
        for i in range(10)
    }
    path = tmp_path / "vdb_entities.json"
    _write_json(path, payload)
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))

    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
        max_nodes=3, max_edges=5000,
    )

    assert snapshot.stats.entity_count == 10  # full count BEFORE truncation
    assert len(snapshot.entities) == 3
    assert snapshot.truncated.entities is True
    assert snapshot.truncated.relations is False
    assert snapshot.truncated.limits.max_nodes == 3


def test_project_truncates_relations_independently(tmp_path):
    """Per-list caps: a graph with 10 nodes and 100 edges + caps
    (50 nodes / 50 edges) must truncate ONLY relations — not
    entities."""
    e_path = tmp_path / "vdb_entities.json"
    _write_json(e_path, [{"id": f"e{i}"} for i in range(10)])
    r_path = tmp_path / "vdb_relationships.json"
    _write_json(r_path, [
        {"src": f"e{i % 10}", "tgt": f"e{(i + 1) % 10}"}
        for i in range(100)
    ])
    projector = GraphSnapshotProjector(path_resolver=_resolver({
        "ae": e_path, "ar": r_path,
    }))

    snapshot = projector.project(
        artifacts=[
            _record("ae", "graph/vdb_entities.json"),
            _record("ar", "graph/vdb_relationships.json"),
        ],
        max_nodes=50, max_edges=50,
    )

    assert snapshot.truncated.entities is False
    assert snapshot.truncated.relations is True
    assert len(snapshot.entities) == 10
    assert len(snapshot.relations) == 50


# ---- De-duplication ------------------------------------------------


def test_project_dedupes_entities_across_artifacts(tmp_path):
    """Same entity id appearing in two artifacts is counted once."""
    p1 = tmp_path / "vdb_entities.json"
    _write_json(p1, [{"id": "shared", "name": "first occurrence"}])
    p2 = tmp_path / "graph_chunk_entity_relation.json"
    _write_json(p2, {
        "entities": [{"id": "shared", "name": "second occurrence"}],
        "relations": [],
    })

    projector = GraphSnapshotProjector(path_resolver=_resolver({
        "a1": p1, "a2": p2,
    }))
    snapshot = projector.project(
        artifacts=[
            _record("a1", "graph/vdb_entities.json"),
            _record("a2", "graph/graph_chunk_entity_relation.json"),
        ],
    )

    assert snapshot.stats.entity_count == 1
    assert snapshot.entities[0].label == "first occurrence"  # first wins


# ---- Stats lineage -------------------------------------------------


def test_project_stats_carry_contributing_artifact_ids(tmp_path):
    """`stats.source_artifact_ids` must list the artifacts that
    actually contributed records — empty / KV-only files don't
    appear there."""
    e_path = tmp_path / "vdb_entities.json"
    _write_json(e_path, [{"id": "e1"}])
    kv_path = tmp_path / "kv_store_text_chunks.json"
    _write_json(kv_path, {"x": "y"})

    projector = GraphSnapshotProjector(path_resolver=_resolver({
        "ent": e_path, "kv": kv_path,
    }))
    snapshot = projector.project(
        artifacts=[
            _record("ent", "graph/vdb_entities.json"),
            _record("kv", "graph/kv_store_text_chunks.json"),
        ],
    )

    assert snapshot.stats.source_artifact_ids == ["ent"]


# ---- Resilience ----------------------------------------------------


def test_project_skips_artifact_when_path_resolver_raises(tmp_path):
    """Path-resolver failure (e.g. path-traversal guard rejected a
    tampered location) is a degraded state — the artifact EXISTS so
    `unavailable` stays None (the FE shows empty tables, not the
    skipped-graph empty state). Reserve `unavailable` for "the run
    never produced a graph at all" semantics."""
    def _bad(_r):
        raise RuntimeError("blocked")
    projector = GraphSnapshotProjector(path_resolver=_bad)
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    assert snapshot.unavailable is None
    assert snapshot.stats.entity_count == 0
    assert snapshot.stats.relation_count == 0
    assert snapshot.entities == []
    assert snapshot.relations == []


def test_project_skips_invalid_json(tmp_path):
    bad = tmp_path / "vdb_entities.json"
    bad.write_text("{not json", encoding="utf-8")
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": bad}))
    snapshot = projector.project(
        artifacts=[_record("a1", "graph/vdb_entities.json")],
    )
    # Artifact existed but was unparseable → empty graph, but
    # `unavailable` stays None because the artifact WAS present.
    assert snapshot.unavailable is None
    assert snapshot.stats.entity_count == 0


def test_project_handles_empty_graphml_gracefully(tmp_path):
    """Empty `.graphml` (no <node>/<edge>) yields 0 entities + 0
    relations rather than crashing the projector."""
    path = tmp_path / "graph_chunk_entity_relation.graphml"
    path.write_text("<graphml/>", encoding="utf-8")
    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record(
            "a1", "graph/graph_chunk_entity_relation.graphml",
        )],
    )
    assert snapshot.stats.entity_count == 0
    assert snapshot.stats.relation_count == 0


def test_project_extracts_entities_and_relations_from_graphml(tmp_path):
    """LightRAG's canonical entity-relation graph lives in
    `graph_chunk_entity_relation.graphml`. The projector must read
    `<node>` elements as entities and `<edge>` elements as relations,
    pulling attributes from `<data key="dN">` children whose `dN`
    references the corresponding `<key attr.name=...>` declaration."""
    path = tmp_path / "graph_chunk_entity_relation.graphml"
    _write_text(path, _GRAPHML_SAMPLE)

    projector = GraphSnapshotProjector(path_resolver=_resolver({"a1": path}))
    snapshot = projector.project(
        artifacts=[_record(
            "a1", "graph/graph_chunk_entity_relation.graphml",
            metadata={"filename": "graph_chunk_entity_relation.graphml"},
        )],
    )

    assert snapshot.stats.entity_count == 2
    assert snapshot.stats.relation_count == 1

    entities_by_id = {e.id: e for e in snapshot.entities}
    assert "Acme Corp" in entities_by_id
    assert "Jane Doe" in entities_by_id
    assert entities_by_id["Acme Corp"].type == "ORGANIZATION"
    assert entities_by_id["Acme Corp"].description == "A logistics company."
    # `source_id` semicolon-separated values get split into a list.
    assert "chunk-1" in entities_by_id["Acme Corp"].source_chunk_ids
    assert "chunk-2" in entities_by_id["Acme Corp"].source_chunk_ids

    rel = snapshot.relations[0]
    assert rel.source_entity_id == "Acme Corp"
    assert rel.target_entity_id == "Jane Doe"
    assert rel.weight == 8.5
    assert rel.label == "leadership, role"
    assert rel.description == "Jane is CEO of Acme."
