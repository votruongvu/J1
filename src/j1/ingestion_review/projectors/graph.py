"""GraphSnapshotProjector — `kind="graph_json"` artifacts → neutral DTO.

The producer side (today: LightRAG via the RAGAnything bridge) writes
several JSON files into its storage area, all surfaced uniformly as
`kind="graph_json"` artifacts:

  * `vdb_entities.json` / `*entit*.json`     — entity records.
  * `vdb_relationships.json` / `*relation*`  — relation records.
  * `kv_store_*.json`                        — internal KV stores
                                                (text chunks, doc
                                                status, llm cache).

The projector classifies each artifact by filename + content shape,
extracts entities and relations, and emits a vendor-neutral DTO.
LightRAG-internal field names (`__id__`, `__entity_type__`,
`__source_id__`, `__src__`, `__tgt__`, `__weight__`) are mapped to
the neutral DTO field names; vendor-internal fields (`__vector__`,
`__embedding__`) are dropped.

Caps are applied per-list (entities / relations independently) so a
graph with 50k entities and 200 relations doesn't truncate the
relations just because the entities overflowed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review.dtos import (
    GraphEntityDTO,
    GraphRelationDTO,
    GraphSnapshotDTO,
    GraphStatsDTO,
    GraphTruncatedDTO,
    GraphTruncationLimitsDTO,
    GraphUnavailableDTO,
)

_log = logging.getLogger("j1.ingestion_review.graph")

GRAPH_KIND = "graph_json"

# Filename pattern hints. Matched case-insensitively against the
# `location` basename. Order matters when a file matches multiple
# patterns — entities + relations both have files containing
# "relation" (e.g. `graph_chunk_entity_relation.json`), so check the
# more-specific patterns first.
_ENTITY_PATTERNS = ("vdb_entit", "entit")
_RELATION_PATTERNS = ("vdb_relat", "relat", "edge")
_INTERNAL_KV_PATTERNS = (
    "kv_store_full_doc", "kv_store_text_chunk", "kv_store_doc_status",
    "kv_store_llm", "kv_store_chunk_entity",
)

# LightRAG's per-record source delimiter — semicolon is most common,
# `<SEP>` shows up in some versions. Both are accepted.
_SOURCE_DELIMITERS = (";", "<SEP>", "<sep>")

# Per-record vendor-internal fields we strip from the metadata
# passthrough — never useful to the FE, often huge.
_DROPPED_METADATA_KEYS = frozenset({
    "__vector__", "__embedding__", "vector", "embedding",
    "content_vector", "graph_node_data",
})


class GraphSnapshotProjector:
    """Projects `graph_json` artifacts into a neutral graph snapshot.

    Same construction pattern as the chunk + quality projectors —
    takes a `path_resolver` callable so it inherits the path-traversal
    guard from the caller's context."""

    def __init__(self, *, path_resolver) -> None:
        self._path_resolver = path_resolver

    def project(
        self,
        artifacts: list[ArtifactRecord],
        *,
        max_nodes: int = 5000,
        max_edges: int = 5000,
        unavailable_reason: str | None = None,
    ) -> GraphSnapshotDTO:
        """Build the snapshot.

        `unavailable_reason` is set by the caller when no graph data
        was produced for the run (skipped by policy, planner, or
        attempt failed). When set, the projector returns an empty
        snapshot with `unavailable` populated — even if there are
        graph artifacts present.

        `max_nodes` / `max_edges` are per-list caps. The projector
        truncates each list independently and reports per-list flags
        in `truncated`."""
        truncated = GraphTruncatedDTO(
            limits=GraphTruncationLimitsDTO(
                max_nodes=max_nodes,
                max_edges=max_edges,
            ),
        )

        if unavailable_reason is not None:
            return GraphSnapshotDTO(
                stats=GraphStatsDTO(),
                entities=[],
                relations=[],
                truncated=truncated,
                unavailable=GraphUnavailableDTO(reason=unavailable_reason),
            )

        graph_artifacts = [a for a in artifacts if a.kind == GRAPH_KIND]
        if not graph_artifacts:
            # Caller didn't pass a reason but there are no graph
            # artifacts — fall back to the generic copy. The service
            # always passes a reason in practice; this branch is
            # defense in depth for direct projector callers (tests).
            return GraphSnapshotDTO(
                stats=GraphStatsDTO(),
                entities=[],
                relations=[],
                truncated=truncated,
                unavailable=GraphUnavailableDTO(
                    reason="No graph snapshot was produced for this run.",
                ),
            )

        entity_records: list[GraphEntityDTO] = []
        relation_records: list[GraphRelationDTO] = []
        contributing_artifact_ids: list[str] = []

        for artifact in graph_artifacts:
            try:
                path = self._path_resolver(artifact)
            except Exception:  # noqa: BLE001 — projector must not crash
                _log.warning(
                    "graph artifact %s not readable; skipping",
                    artifact.artifact_id,
                )
                continue
            if not path.is_file():
                continue

            kind = _classify_artifact(artifact, path)
            if kind is None:
                continue

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning(
                    "graph artifact %s has invalid JSON: %s",
                    artifact.artifact_id, exc,
                )
                continue

            new_entities = 0
            new_relations = 0
            if kind == "entities":
                projected = list(_iter_entities(payload, artifact))
                entity_records.extend(projected)
                new_entities = len(projected)
            elif kind == "relations":
                projected = list(_iter_relations(payload, artifact))
                relation_records.extend(projected)
                new_relations = len(projected)
            elif kind == "mixed":
                # Some LightRAG variants put both entities + relations
                # in the same `graph_chunk_entity_relation.json` file
                # under top-level keys.
                ents = list(_iter_entities(payload, artifact))
                rels = list(_iter_relations(payload, artifact))
                entity_records.extend(ents)
                relation_records.extend(rels)
                new_entities = len(ents)
                new_relations = len(rels)

            if new_entities or new_relations:
                contributing_artifact_ids.append(artifact.artifact_id)

        # De-duplicate entities (LightRAG sometimes writes the same
        # entity into both vdb_entities and graph_chunk_entity_relation;
        # we don't want it counted twice). Prefer the first seen record
        # — vdb_entities tends to be more complete.
        entity_records = _dedupe_by_id(entity_records)
        relation_records = _dedupe_by_id(relation_records)

        stats = GraphStatsDTO(
            entity_count=len(entity_records),
            relation_count=len(relation_records),
            source_artifact_ids=contributing_artifact_ids,
        )

        # Apply caps per-list; record truncation flags.
        entities_kept = entity_records[:max_nodes]
        relations_kept = relation_records[:max_edges]
        truncated = GraphTruncatedDTO(
            entities=len(entity_records) > max_nodes,
            relations=len(relation_records) > max_edges,
            limits=truncated.limits,
        )

        return GraphSnapshotDTO(
            stats=stats,
            entities=entities_kept,
            relations=relations_kept,
            truncated=truncated,
            unavailable=None,
        )


# ---- Classification ----------------------------------------------


def _classify_artifact(
    artifact: ArtifactRecord, path: Path,
) -> str | None:
    """Decide which sub-projector to apply.

    Returns `"entities"`, `"relations"`, `"mixed"`, or None to skip.
    Decision uses (1) filename hints from the location, (2) explicit
    `metadata["filename"]` set by `_graph_drafts_from_storage`, (3)
    file extension (only `.json` is supported — `.graphml` files are
    skipped)."""
    suffix = PurePosixPath(path.name).suffix.lower()
    if suffix != ".json":
        return None

    name = (
        artifact.metadata.get("filename")
        or PurePosixPath(artifact.location).name
    ).lower()

    # Internal LightRAG KV stores carry no graph data — skip cleanly.
    for pat in _INTERNAL_KV_PATTERNS:
        if pat in name:
            return None

    has_relation_hint = any(p in name for p in _RELATION_PATTERNS)
    has_entity_hint = any(p in name for p in _ENTITY_PATTERNS)

    # `graph_chunk_entity_relation.json` matches BOTH — treat as mixed.
    if has_entity_hint and has_relation_hint:
        return "mixed"
    if has_relation_hint:
        return "relations"
    if has_entity_hint:
        return "entities"
    # Filename gave no hint. Skip rather than guess — the projector
    # should never invent entities out of an unknown JSON shape.
    return None


# ---- Entity projection -------------------------------------------


def _iter_entities(
    payload: Any, artifact: ArtifactRecord,
) -> Iterable[GraphEntityDTO]:
    """Yield neutral entity DTOs from one entity-bearing payload.

    Tolerates two top-level shapes:
      * Object keyed by entity id → `{ "ent-1": {...}, "ent-2": {...} }`
      * List of records → `[{...}, {...}]`
      * Object with `entities`/`nodes` key wrapping the above.
    """
    candidates = _entries(payload, "entities", "nodes")
    for index, (key, raw) in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        entity = _project_entity(raw, fallback_id=key, artifact=artifact, index=index)
        if entity is not None:
            yield entity


def _project_entity(
    raw: dict[str, Any],
    *,
    fallback_id: str | None,
    artifact: ArtifactRecord,
    index: int,
) -> GraphEntityDTO | None:
    """One LightRAG-style entity record → `GraphEntityDTO`.

    Field name precedence (snake_case + camelCase + LightRAG-internal):
      id   : __id__ / id / entity_name / __name__ / name / fallback_id
      type : __entity_type__ / entity_type / type
      desc : __description__ / description
      src  : __source_id__ / source_id  (split on common delimiters)
    """
    entity_id = (
        _str_field(raw, "__id__", "id", "entity_name", "__name__", "name")
        or fallback_id
    )
    if not entity_id:
        entity_id = f"{artifact.artifact_id}#node-{index}"

    label = (
        _str_field(raw, "__name__", "name", "entity_name", "label")
        or entity_id
    )
    return GraphEntityDTO(
        id=entity_id,
        label=label,
        type=_str_field(raw, "__entity_type__", "entity_type", "type"),
        description=_str_field(raw, "__description__", "description"),
        source_chunk_ids=_split_sources(
            _str_field(raw, "__source_id__", "source_id", "chunk_ids"),
        ),
        source_artifact_ids=[artifact.artifact_id],
        metadata=_strip_internal(raw),
    )


# ---- Relation projection -----------------------------------------


def _iter_relations(
    payload: Any, artifact: ArtifactRecord,
) -> Iterable[GraphRelationDTO]:
    candidates = _entries(payload, "relations", "edges", "relationships")
    for index, (key, raw) in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        relation = _project_relation(
            raw, fallback_id=key, artifact=artifact, index=index,
        )
        if relation is not None:
            yield relation


def _project_relation(
    raw: dict[str, Any],
    *,
    fallback_id: str | None,
    artifact: ArtifactRecord,
    index: int,
) -> GraphRelationDTO | None:
    src = _str_field(raw, "__src__", "src_id", "src", "source", "from")
    tgt = _str_field(raw, "__tgt__", "tgt_id", "tgt", "target", "to")
    if not src or not tgt:
        # Without endpoints there's no edge to render — drop.
        return None

    relation_id = (
        _str_field(raw, "__id__", "id", "rel_id")
        or fallback_id
        or f"{src}->{tgt}#{index}"
    )
    return GraphRelationDTO(
        id=relation_id,
        source_entity_id=src,
        target_entity_id=tgt,
        label=_str_field(raw, "__keywords__", "keywords", "label"),
        type=_str_field(raw, "__rel_type__", "rel_type", "type"),
        description=_str_field(raw, "__description__", "description"),
        weight=_float_field(raw, "__weight__", "weight"),
        source_chunk_ids=_split_sources(
            _str_field(raw, "__source_id__", "source_id", "chunk_ids"),
        ),
        source_artifact_ids=[artifact.artifact_id],
        metadata=_strip_internal(raw),
    )


# ---- Field helpers -----------------------------------------------


def _entries(
    payload: Any, *list_keys: str,
) -> Iterable[tuple[str | None, Any]]:
    """Yield `(key, value)` pairs from a payload that might be:

      * a top-level list                  → `(None, item)` per item
      * an object keyed by entity id      → `(key, value)` per pair
      * an object containing one of `list_keys` (e.g. `entities`)
        whose value is either of the above
    """
    if isinstance(payload, list):
        for entry in payload:
            yield None, entry
        return
    if not isinstance(payload, dict):
        return
    # Look for a wrapping key first.
    for key in list_keys:
        wrapped = payload.get(key)
        if isinstance(wrapped, list):
            for entry in wrapped:
                yield None, entry
            return
        if isinstance(wrapped, dict):
            for k, v in wrapped.items():
                yield k, v
            return
    # No wrapping key — payload itself is the keyed map.
    for k, v in payload.items():
        yield k, v


def _split_sources(value: str | None) -> list[str]:
    if not value:
        return []
    parts: list[str] = [value]
    for delim in _SOURCE_DELIMITERS:
        next_parts: list[str] = []
        for p in parts:
            next_parts.extend(p.split(delim))
        parts = next_parts
    return [p.strip() for p in parts if p.strip()]


def _strip_internal(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop vendor-internal fields from passthrough metadata."""
    return {
        k: v for k, v in raw.items()
        if k not in _DROPPED_METADATA_KEYS
    }


def _dedupe_by_id(records: list) -> list:
    """Keep first occurrence of each id — preserves order."""
    seen: set[str] = set()
    out = []
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        out.append(record)
    return out


def _str_field(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return None


def _float_field(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None
