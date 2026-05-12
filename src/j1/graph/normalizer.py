"""Normalize graph artifacts into a stable entity/relationship schema.

Different graph producers emit different on-disk shapes:

 * **Canonical** (the first-party `graphify` provider):
   `{"nodes": [{"id": "..."}], "edges": [{"from": "...", "to": "...", "type": "..."}]}`
 * **LightRAG**: `{"entities": [{"name": "..."}], "relationships":
   [{"src": "...", "tgt": "...", "predicate": "..."}]}` (and variants)
 * **GraphML**: XML — `<graphml><graph><node id="..."/><edge source="..." target="..."/></graph></graphml>`

The previous code path in `GraphQueryProvider._read_graph` only knew
the canonical shape and silently returned `None` for any deviation —
causing graph QA to answer "No graph relationships found" even when
the artifact existed and contained valid relationships. This module
provides one entry point that recognises all three shapes and emits
a single `NormalizedGraph` dataclass for downstream consumers.

Unrecognised shapes log at WARNING level and return `None`; the
caller (query provider, validation check) decides whether to fail
loudly or fall back to a neutral message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

_log = logging.getLogger("j1.graph.normalizer")


@dataclass(frozen=True)
class GraphEntity:
    """One node in the normalized graph.

 `id` is the producer-supplied identifier (or a synthesized one
 when the source format only carries node labels). `label` is a
 human-readable display string; falls back to `id` when missing.
 `type` is the entity class (Person, Document, …) when the
 producer surfaces one — `None` when untyped."""

    id: str
    label: str
    type: str | None = None


@dataclass(frozen=True)
class GraphRelationship:
    """One edge in the normalized graph. `from_id`/`to_id` reference
 entity ids. `kind` is the relationship type/predicate
 (`mentions`, `located_in`, …); falls back to `related_to` when
 the source format doesn't tag the edge."""

    from_id: str
    to_id: str
    kind: str = "related_to"
    label: str | None = None


@dataclass(frozen=True)
class NormalizedGraph:
    """The unified shape downstream consumers reason about.

 `source_artifact_id` and `run_id` propagate the artifact's
 lineage so validation checks can verify the graph belongs to the
 current run without re-reading the artifact registry."""

    entities: tuple[GraphEntity, ...]
    relationships: tuple[GraphRelationship, ...]
    source_artifact_id: str
    source_format: str  # "canonical" | "lightrag" | "graphml"
    run_id: str | None = None


__all__ = [
    "GraphEntity",
    "GraphRelationship",
    "NormalizedGraph",
    "normalize_graph_bytes",
    "normalize_graph_text",
]


def normalize_graph_bytes(
    content: bytes,
    *,
    source_artifact_id: str,
    run_id: str | None = None,
) -> NormalizedGraph | None:
    """Decode + normalize. Returns `None` on unrecognised shapes;
 logs a WARNING with the artifact id so operators can find the
 offending file. UTF-8 errors are replaced rather than raising —
 a partially-corrupted graph still beats silently dropping the
 artifact at validation time."""
    text = content.decode("utf-8", errors="replace")
    return normalize_graph_text(
        text,
        source_artifact_id=source_artifact_id,
        run_id=run_id,
    )


def normalize_graph_text(
    text: str,
    *,
    source_artifact_id: str,
    run_id: str | None = None,
) -> NormalizedGraph | None:
    """Same as `normalize_graph_bytes` but takes a pre-decoded string.
 Useful for tests."""
    stripped = text.lstrip()
    if not stripped:
        return None
    # GraphML files always start with `<?xml` or `<graphml`. Try the
    # XML parser first so we don't have to wait for the JSON parser
    # to fail. Anything else: try JSON.
    if stripped.startswith("<"):
        graph = _parse_graphml(text)
        if graph is not None:
            return _build("graphml", graph, source_artifact_id, run_id)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            _log.warning(
                "graph artifact %s: not valid JSON or GraphML",
                source_artifact_id,
            )
            return None
        if not isinstance(data, dict):
            _log.warning(
                "graph artifact %s: top-level is %s, expected object",
                source_artifact_id, type(data).__name__,
            )
            return None
        canonical = _parse_canonical(data)
        if canonical is not None:
            return _build("canonical", canonical, source_artifact_id, run_id)
        lightrag = _parse_lightrag(data)
        if lightrag is not None:
            return _build("lightrag", lightrag, source_artifact_id, run_id)
    _log.warning(
        "graph artifact %s: unrecognised shape (no canonical/lightrag/"
        "graphml fields)",
        source_artifact_id,
    )
    return None


# ---- shape detectors ----------------------------------------------


_CanonicalParse = tuple[list[GraphEntity], list[GraphRelationship]]


def _parse_canonical(data: dict) -> _CanonicalParse | None:
    """First-party `graphify` output: `{"nodes":[...], "edges":[...]}`.
 Returns None if neither key is present — the LightRAG detector
 then gets a chance."""
    nodes_raw = data.get("nodes")
    edges_raw = data.get("edges")
    if nodes_raw is None and edges_raw is None:
        return None
    entities: list[GraphEntity] = []
    for n in _iter_dicts(nodes_raw):
        node_id = _str_or_none(
            n.get("id") or n.get("name") or n.get("label"),
        )
        if not node_id:
            continue
        entities.append(GraphEntity(
            id=node_id,
            label=_str_or_none(n.get("label")) or node_id,
            type=_str_or_none(n.get("type") or n.get("kind")),
        ))
    relationships: list[GraphRelationship] = []
    for e in _iter_dicts(edges_raw):
        # Canonical edge keys: `from`/`to`/`type`. Some early
        # variants used `source`/`target` — accept both so we don't
        # regress on legacy data.
        src = _str_or_none(e.get("from") or e.get("source") or e.get("src"))
        dst = _str_or_none(e.get("to") or e.get("target") or e.get("tgt"))
        if not src or not dst:
            continue
        relationships.append(GraphRelationship(
            from_id=src,
            to_id=dst,
            kind=_str_or_none(e.get("type") or e.get("kind")) or "related_to",
            label=_str_or_none(e.get("label")),
        ))
    if not entities and not relationships:
        return None
    return (entities, relationships)


def _parse_lightrag(data: dict) -> _CanonicalParse | None:
    """LightRAG output: `{"entities":[...], "relationships":[...]}`.
 Entity records can carry `name` instead of `id`; relationships
 use `src_id`/`tgt_id` or `source`/`target`. Permissive on key
 names so future LightRAG releases don't break us."""
    entities_raw = data.get("entities")
    rel_raw = (
        data.get("relationships")
        or data.get("relations")
        or data.get("edges_extracted")
    )
    if entities_raw is None and rel_raw is None:
        return None
    entities: list[GraphEntity] = []
    for n in _iter_dicts(entities_raw):
        ent_id = _str_or_none(
            n.get("id") or n.get("entity_name") or n.get("name"),
        )
        if not ent_id:
            continue
        entities.append(GraphEntity(
            id=ent_id,
            label=_str_or_none(n.get("name") or n.get("label")) or ent_id,
            type=_str_or_none(n.get("entity_type") or n.get("type")),
        ))
    relationships: list[GraphRelationship] = []
    for e in _iter_dicts(rel_raw):
        src = _str_or_none(
            e.get("src_id") or e.get("source") or e.get("src")
            or e.get("from") or e.get("source_entity"),
        )
        dst = _str_or_none(
            e.get("tgt_id") or e.get("target") or e.get("tgt")
            or e.get("to") or e.get("target_entity"),
        )
        if not src or not dst:
            continue
        relationships.append(GraphRelationship(
            from_id=src,
            to_id=dst,
            kind=_str_or_none(
                e.get("predicate") or e.get("type") or e.get("relation"),
            ) or "related_to",
            label=_str_or_none(e.get("description") or e.get("label")),
        ))
    if not entities and not relationships:
        return None
    return (entities, relationships)


def _parse_graphml(text: str) -> _CanonicalParse | None:
    """GraphML XML. Standard namespace is
 `http://graphml.graphdrawing.org/xmlns`; we strip namespaces
 from tag names for matching simplicity since GraphML's element
 vocabulary is fixed."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    entities: list[GraphEntity] = []
    relationships: list[GraphRelationship] = []
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "node":
            node_id = elem.get("id")
            if not node_id:
                continue
            entities.append(GraphEntity(
                id=node_id,
                label=node_id,
                type=None,
            ))
        elif tag == "edge":
            src = elem.get("source")
            dst = elem.get("target")
            if not src or not dst:
                continue
            relationships.append(GraphRelationship(
                from_id=src,
                to_id=dst,
                kind="related_to",
                label=elem.get("label"),
            ))
    if not entities and not relationships:
        return None
    return (entities, relationships)


# ---- helpers ------------------------------------------------------


def _build(
    source_format: str,
    parsed: _CanonicalParse,
    source_artifact_id: str,
    run_id: str | None,
) -> NormalizedGraph:
    entities, relationships = parsed
    return NormalizedGraph(
        entities=tuple(entities),
        relationships=tuple(relationships),
        source_artifact_id=source_artifact_id,
        source_format=source_format,
        run_id=run_id,
    )


def _iter_dicts(value: Any) -> Iterable[dict]:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strip_ns(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag
