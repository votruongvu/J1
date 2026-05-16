"""Persistent Knowledge Memory artifact — Phase 2.

Materialises a snapshot-scoped, query-ready memory layer from
compile output + post-compile domain enrichment artifacts +
domain pack hints. Built on demand by the
``ACTION_BUILD_KNOWLEDGE_MEMORY`` manual action; persisted as a
single ``knowledge_memory`` artifact per ``(document_id,
snapshot_id)`` pair.

Phase 2 boundary:

  * **Manual build only.** No automatic post-compile / post-
    enrichment build. Phase 3 will wire automatic rebuilds when
    the query side learns to prefer this artifact.
  * **Query routing unchanged.** Query continues to read legacy
    `enriched.*` artifacts directly. Phase 3 introduces a
    memory-preferred route.
  * **No LLM calls.** The builder is deterministic — it projects
    existing summaries / signals into typed entries. It never
    generates new prose. If a source artifact already carries a
    summary, the summary is projected verbatim; otherwise the
    entry surfaces the structured payload only.
  * **Bounded payload.** Entries are short by contract — long
    excerpts (chunk bodies, full image captions, large tables)
    are NOT replicated here. The artifact stays small enough to
    load into a query context window.

The on-disk shape mirrors `ArtifactDraft` conventions: a single
JSON blob ``knowledge_memory_<snapshot_id>.json`` in the
ENRICHED workspace area, registered with `ArtifactRegistry`
under ``kind="knowledge_memory"``. One ACTIVE artifact per
``(document_id, snapshot_id)``; rebuilding flips the previous
build's ``search_state`` to ``superseded``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from j1.processing.derived_enrichment import (
    DERIVED_ENRICHMENT_ARTIFACT_SCHEMA,
    DerivedEnrichmentArtifact,
    EnrichmentSourceRef,
    KNOWN_DERIVED_ENRICHMENT_KINDS,
    normalize_enrichment_artifact_payload,
)


_log = logging.getLogger(__name__)


# Stable schema marker — bump only on contract-breaking changes.
KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA = "knowledge_memory.v1"


# Builder identity — pinned strings so audit logs can attribute
# artifacts to a specific builder version.
KNOWLEDGE_MEMORY_BUILDER_NAME = "knowledge_memory_builder"
KNOWLEDGE_MEMORY_BUILDER_VERSION = "1"


# ---- Entry type vocabulary -----------------------------------------


# Stable set of memory-entry types. Add values, don't rename.
# Query / FE consumers filter on these strings.
MEMORY_ENTRY_TYPE_ENTITY = "entity"
MEMORY_ENTRY_TYPE_RELATIONSHIP = "relationship"
MEMORY_ENTRY_TYPE_ALIAS = "alias"
MEMORY_ENTRY_TYPE_RETRIEVAL_HINT = "retrieval_hint"
MEMORY_ENTRY_TYPE_TERMINOLOGY = "terminology"
MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT = "domain_insight"
MEMORY_ENTRY_TYPE_RISK = "risk"
MEMORY_ENTRY_TYPE_REQUIREMENT = "requirement"
MEMORY_ENTRY_TYPE_ACTION_ITEM = "action_item"
MEMORY_ENTRY_TYPE_VALIDATION_CHECK = "validation_check"
MEMORY_ENTRY_TYPE_TABLE_ROW = "table_row"
MEMORY_ENTRY_TYPE_TABLE_SUMMARY = "table_summary"
MEMORY_ENTRY_TYPE_VISUAL_SUMMARY = "visual_summary"
MEMORY_ENTRY_TYPE_FORMULA = "formula"
MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY = "document_summary"
MEMORY_ENTRY_TYPE_SECTION = "section"
MEMORY_ENTRY_TYPE_GRAPH_SUMMARY = "graph_summary"
MEMORY_ENTRY_TYPE_QUALITY_SUMMARY = "quality_summary"


# ---- Entry origin vocabulary ---------------------------------------


ENTRY_ORIGIN_COMPILE = "compile"
ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT = "post_compile_enrichment"
ENTRY_ORIGIN_DOMAIN_PACK = "domain_pack"
ENTRY_ORIGIN_MEMORY_RESOLVER = "memory_resolver"


# ---- Entry status vocabulary ---------------------------------------


ENTRY_STATUS_ACTIVE = "active"
ENTRY_STATUS_CONTEXTUAL = "contextual"  # No source refs; weak context.


# ---- Build warning codes -------------------------------------------


WARNING_NO_ACTIVE_SNAPSHOT = "no_active_snapshot"
WARNING_COMPILE_NOT_SUCCEEDED = "compile_not_succeeded"
WARNING_NO_ENRICHMENT_ARTIFACTS = "no_enrichment_artifacts"
WARNING_UNKNOWN_ENRICHMENT_KIND_SKIPPED = "unknown_enrichment_kind_skipped"
WARNING_DOMAIN_PACK_NOT_FOUND = "domain_pack_not_found"
WARNING_SUPERSEDED_ARTIFACT_SKIPPED = "superseded_artifact_skipped"


# Cap on entry `content` text — keep entries short so the whole
# memory payload stays in a single LLM context window in future
# Phase 3 query integration. Producers can put richer data in
# `structured_payload` without inflating `content`.
ENTRY_CONTENT_CAP_CHARS = 480


# Cap on the total number of entries per memory artifact. Domain
# enrichment can produce hundreds of requirements / risks /
# findings; the builder truncates with a `truncated` warning
# rather than letting the artifact balloon.
DEFAULT_MAX_ENTRIES_PER_KIND = 200


# ---- Source for the entry ------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryEntrySource:
    """Where this entry came from (origin + producer artifact).

    Distinct from `EnrichmentSourceRef` — that points back into
    COMPILE evidence (chunk / page / table). This points at the
    artifact OR rule that PRODUCED the entry (an enrichment
    artifact id, a domain pack id, etc.)."""

    origin: str = ENTRY_ORIGIN_COMPILE
    artifact_kind: str | None = None
    artifact_id: str | None = None
    producer: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "artifact_kind": self.artifact_kind,
            "artifact_id": self.artifact_id,
            "producer": self.producer,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KnowledgeMemoryEntrySource":
        return cls(
            origin=str(payload.get("origin") or ENTRY_ORIGIN_COMPILE),
            artifact_kind=_str_or_none(payload.get("artifact_kind")),
            artifact_id=_str_or_none(payload.get("artifact_id")),
            producer=_str_or_none(payload.get("producer")),
        )


# ---- Entry --------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryEntry:
    """One memory-entry row.

    Field semantics:
      * `memory_id` — caller-stable id when known; otherwise the
        builder synthesises `<type>:<index>:<short-hash>` (NOT
        a UUID — we want determinism so rebuilds produce the same
        ids when inputs match).
      * `memory_type` — vocabulary in `MEMORY_ENTRY_TYPE_*`.
      * `title` — short header (≤120 chars). Used as the FE row
        label.
      * `content` — human-readable text body (capped at
        `ENTRY_CONTENT_CAP_CHARS`). NEVER a full chunk; the
        builder truncates with an ellipsis.
      * `structured_payload` — opaque dict producers can fill with
        kind-specific data (requirement number, risk severity,
        table row values, etc.). Consumers iterate keys.
      * `source` — `(origin, artifact_kind, artifact_id, producer)`
        — where this entry came from.
      * `source_refs` — typed back-pointers into compile evidence
        via Phase 1 `EnrichmentSourceRef`. Empty when the entry
        is contextual-only (no specific source); the builder sets
        `status=contextual` in that case.
      * `confidence` — `low / medium / high / None`. Propagated
        from the source artifact when available.
      * `tags` — operator-readable labels (e.g. `["high_severity",
        "compliance"]`). Free-form; not part of any retrieval
        contract yet.
      * `status` — `active` or `contextual`. Phase 2 only uses
        these two; future phases may add `stale` / `superseded`.
    """

    memory_id: str
    memory_type: str
    domain_id: str | None = None
    title: str = ""
    content: str = ""
    structured_payload: Mapping[str, Any] = field(default_factory=dict)
    source: KnowledgeMemoryEntrySource = field(
        default_factory=KnowledgeMemoryEntrySource,
    )
    source_refs: tuple[EnrichmentSourceRef, ...] = ()
    confidence: str | None = None
    tags: tuple[str, ...] = ()
    status: str = ENTRY_STATUS_ACTIVE

    def to_payload(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "domain_id": self.domain_id,
            "title": self.title,
            "content": self.content,
            "structured_payload": dict(self.structured_payload),
            "source": self.source.to_payload(),
            "source_refs": [r.to_payload() for r in self.source_refs],
            "confidence": self.confidence,
            "tags": list(self.tags),
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KnowledgeMemoryEntry":
        refs_raw = payload.get("source_refs") or ()
        refs: tuple[EnrichmentSourceRef, ...]
        if isinstance(refs_raw, (list, tuple)):
            refs = tuple(
                EnrichmentSourceRef.from_payload(r)
                for r in refs_raw if isinstance(r, Mapping)
            )
        else:
            refs = ()
        return cls(
            memory_id=str(payload.get("memory_id") or ""),
            memory_type=str(payload.get("memory_type") or ""),
            domain_id=_str_or_none(payload.get("domain_id")),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or ""),
            structured_payload=dict(payload.get("structured_payload") or {}),
            source=KnowledgeMemoryEntrySource.from_payload(
                payload.get("source") or {},
            ),
            source_refs=refs,
            confidence=_str_or_none(payload.get("confidence")),
            tags=tuple(str(t) for t in (payload.get("tags") or ())),
            status=str(payload.get("status") or ENTRY_STATUS_ACTIVE),
        )


# ---- Payload-level shapes ------------------------------------------


@dataclass(frozen=True)
class KnowledgeMemoryBuiltFrom:
    """Provenance of the build itself — which artifacts were read."""

    compile_artifact_ids: tuple[str, ...] = ()
    enrichment_artifact_ids: tuple[str, ...] = ()
    domain_pack_id: str | None = None
    domain_pack_version: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "compile_artifact_ids": list(self.compile_artifact_ids),
            "enrichment_artifact_ids": list(self.enrichment_artifact_ids),
            "domain_pack_id": self.domain_pack_id,
            "domain_pack_version": self.domain_pack_version,
        }


@dataclass(frozen=True)
class KnowledgeMemoryBuilderId:
    """Builder identity stamped on the artifact."""

    name: str = KNOWLEDGE_MEMORY_BUILDER_NAME
    version: str = KNOWLEDGE_MEMORY_BUILDER_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class KnowledgeMemorySource:
    """Top-level `source` block on the artifact."""

    built_from: KnowledgeMemoryBuiltFrom = field(
        default_factory=KnowledgeMemoryBuiltFrom,
    )
    builder: KnowledgeMemoryBuilderId = field(
        default_factory=KnowledgeMemoryBuilderId,
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "built_from": self.built_from.to_payload(),
            "builder": self.builder.to_payload(),
        }


@dataclass(frozen=True)
class KnowledgeMemorySummary:
    """Lightweight counts for FE / audit / future projection."""

    document_type_hint: str | None = None
    entity_count: int = 0
    relationship_count: int = 0
    alias_count: int = 0
    domain_insight_count: int = 0
    source_ref_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_type_hint": self.document_type_hint,
            "entity_count": self.entity_count,
            "relationship_count": self.relationship_count,
            "alias_count": self.alias_count,
            "domain_insight_count": self.domain_insight_count,
            "source_ref_count": self.source_ref_count,
        }


@dataclass(frozen=True)
class KnowledgeMemoryPayload:
    """Top-level payload of the `knowledge_memory` artifact."""

    artifact_schema: str = KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA
    document_id: str = ""
    snapshot_id: str = ""
    run_id: str | None = None
    project_id: str | None = None
    domain_id: str | None = None
    status: str = "ready"
    created_at: str = ""  # ISO 8601 string
    source: KnowledgeMemorySource = field(
        default_factory=KnowledgeMemorySource,
    )
    summary: KnowledgeMemorySummary = field(
        default_factory=KnowledgeMemorySummary,
    )
    entries: tuple[KnowledgeMemoryEntry, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_schema": self.artifact_schema,
            "document_id": self.document_id,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "domain_id": self.domain_id,
            "status": self.status,
            "created_at": self.created_at,
            "source": self.source.to_payload(),
            "summary": self.summary.to_payload(),
            "entries": [e.to_payload() for e in self.entries],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "KnowledgeMemoryPayload":
        entries_raw = payload.get("entries") or ()
        entries: tuple[KnowledgeMemoryEntry, ...]
        if isinstance(entries_raw, (list, tuple)):
            entries = tuple(
                KnowledgeMemoryEntry.from_payload(e)
                for e in entries_raw if isinstance(e, Mapping)
            )
        else:
            entries = ()
        source = payload.get("source") or {}
        built_from = source.get("built_from") or {} if isinstance(source, Mapping) else {}
        builder = source.get("builder") or {} if isinstance(source, Mapping) else {}
        summary = payload.get("summary") or {}
        return cls(
            artifact_schema=str(
                payload.get("artifact_schema")
                or KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA
            ),
            document_id=str(payload.get("document_id") or ""),
            snapshot_id=str(payload.get("snapshot_id") or ""),
            run_id=_str_or_none(payload.get("run_id")),
            project_id=_str_or_none(payload.get("project_id")),
            domain_id=_str_or_none(payload.get("domain_id")),
            status=str(payload.get("status") or "ready"),
            created_at=str(payload.get("created_at") or ""),
            source=KnowledgeMemorySource(
                built_from=KnowledgeMemoryBuiltFrom(
                    compile_artifact_ids=tuple(
                        str(x) for x in (built_from.get("compile_artifact_ids") or ())
                    ),
                    enrichment_artifact_ids=tuple(
                        str(x) for x in (built_from.get("enrichment_artifact_ids") or ())
                    ),
                    domain_pack_id=_str_or_none(built_from.get("domain_pack_id")),
                    domain_pack_version=_str_or_none(
                        built_from.get("domain_pack_version"),
                    ),
                ),
                builder=KnowledgeMemoryBuilderId(
                    name=str(builder.get("name") or KNOWLEDGE_MEMORY_BUILDER_NAME),
                    version=str(builder.get("version") or KNOWLEDGE_MEMORY_BUILDER_VERSION),
                ),
            ),
            summary=KnowledgeMemorySummary(
                document_type_hint=_str_or_none(summary.get("document_type_hint")) if isinstance(summary, Mapping) else None,
                entity_count=_int(summary.get("entity_count")) if isinstance(summary, Mapping) else 0,
                relationship_count=_int(summary.get("relationship_count")) if isinstance(summary, Mapping) else 0,
                alias_count=_int(summary.get("alias_count")) if isinstance(summary, Mapping) else 0,
                domain_insight_count=_int(summary.get("domain_insight_count")) if isinstance(summary, Mapping) else 0,
                source_ref_count=_int(summary.get("source_ref_count")) if isinstance(summary, Mapping) else 0,
            ),
            entries=entries,
            warnings=tuple(str(w) for w in (payload.get("warnings") or ())),
        )


# ---- Builder inputs ------------------------------------------------


@dataclass
class KnowledgeMemoryBuildInputs:
    """Inputs the builder reads. Container for the dependencies so
    tests can inject fakes without monkey-patching the builder.

    All optional — when a dependency is `None` the builder skips
    the corresponding section with a warning instead of raising."""

    document_id: str
    snapshot_id: str
    run_id: str | None = None
    project_id: str | None = None
    domain_id: str | None = None
    domain_pack_version: str | None = None
    document_type_hint: str | None = None
    compile_artifact_ids: tuple[str, ...] = ()
    # Domain pack hints — optional iterables the caller resolves
    # from `DomainPack` at the call site. We accept these as
    # plain iterables of strings rather than importing the
    # `DomainPack` dataclass to keep this module decoupled from
    # the domain layer (and the no-LLM regression guard happy).
    aliases: Sequence[Mapping[str, Any]] = ()
    terminology_hints: Sequence[str] = ()
    retrieval_hints: Sequence[str] = ()
    # Enrichment artifacts the builder will project. Each entry
    # is `(artifact_id, artifact_kind, raw_payload_dict)`. The
    # caller is responsible for filtering to the active snapshot.
    enrichment_artifacts: Sequence[
        tuple[str, str, Mapping[str, Any]]
    ] = ()
    # Graph summary signals (cheap counts only). The builder
    # never opens `graph_json` artifacts — it reads counts the
    # caller already has in hand from compile metadata.
    graph_entity_count: int = 0
    graph_relationship_count: int = 0
    # Compile artifact ids in `built_from` — separate from
    # `compile_artifact_ids` above which is the lineage list used
    # for entries; both default to the same set in practice.


# ---- Builder ------------------------------------------------------


class KnowledgeMemoryBuilder:
    """Project compile + enrichment + domain pack inputs into a
    `KnowledgeMemoryPayload`.

    The builder is deterministic. Given the same inputs it
    produces the same artifact — entry ids are derived from the
    source artifact id + index, not a UUID. This matters for
    idempotency: rebuilding the artifact for an unchanged snapshot
    produces an artifact byte-identical (modulo `created_at`) to
    the previous build.

    Phase 2 boundary: no LLM calls. The builder reads existing
    payloads, normalises them via the Phase 1 helper, and projects
    selected fields into typed entries. It NEVER synthesises new
    prose.
    """

    def __init__(self, *, max_entries_per_kind: int = DEFAULT_MAX_ENTRIES_PER_KIND) -> None:
        self._max_entries_per_kind = max_entries_per_kind

    def build(self, inputs: KnowledgeMemoryBuildInputs) -> KnowledgeMemoryPayload:
        entries: list[KnowledgeMemoryEntry] = []
        warnings: list[str] = []
        enrichment_artifact_ids: list[str] = []

        # 1. Domain pack hints (aliases / terminology / retrieval
        #    hints). Origin = domain_pack. No source refs since
        #    these are static pack-level data; entries stamped
        #    `status=contextual`.
        entries.extend(
            self._project_domain_aliases(
                inputs.aliases, domain_id=inputs.domain_id,
            )
        )
        entries.extend(
            self._project_string_hints(
                inputs.terminology_hints,
                memory_type=MEMORY_ENTRY_TYPE_TERMINOLOGY,
                domain_id=inputs.domain_id,
            )
        )
        entries.extend(
            self._project_string_hints(
                inputs.retrieval_hints,
                memory_type=MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
                domain_id=inputs.domain_id,
            )
        )

        # 2. Compile signals — graph counts + document-type hint.
        entries.extend(
            self._project_compile_signals(inputs)
        )

        # 3. Enrichment artifacts via the Phase 1 normaliser. The
        #    builder iterates the caller-supplied (id, kind,
        #    payload) tuples; each is normalised, then projected
        #    per-kind.
        for artifact_id, kind, raw_payload in inputs.enrichment_artifacts:
            envelope = normalize_enrichment_artifact_payload(
                raw_payload,
                artifact_kind=kind,
                document_id=inputs.document_id,
                snapshot_id=inputs.snapshot_id,
                run_id=inputs.run_id,
                artifact_id=artifact_id,
                domain_id=inputs.domain_id,
            )
            enrichment_artifact_ids.append(artifact_id)
            if kind not in KNOWN_DERIVED_ENRICHMENT_KINDS:
                warnings.append(WARNING_UNKNOWN_ENRICHMENT_KIND_SKIPPED)
                continue
            projected = self._project_enrichment_envelope(
                envelope, artifact_id=artifact_id,
            )
            entries.extend(projected)

        if not enrichment_artifact_ids:
            warnings.append(WARNING_NO_ENRICHMENT_ARTIFACTS)

        # Compute summary counts.
        summary = self._build_summary(
            entries, document_type_hint=inputs.document_type_hint,
        )

        # Cap source_ref_count at the actual aggregated total.
        return KnowledgeMemoryPayload(
            artifact_schema=KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA,
            document_id=inputs.document_id,
            snapshot_id=inputs.snapshot_id,
            run_id=inputs.run_id,
            project_id=inputs.project_id,
            domain_id=inputs.domain_id,
            status="ready",
            created_at=_utc_iso(),
            source=KnowledgeMemorySource(
                built_from=KnowledgeMemoryBuiltFrom(
                    compile_artifact_ids=tuple(inputs.compile_artifact_ids),
                    enrichment_artifact_ids=tuple(enrichment_artifact_ids),
                    domain_pack_id=inputs.domain_id,
                    domain_pack_version=inputs.domain_pack_version,
                ),
                builder=KnowledgeMemoryBuilderId(),
            ),
            summary=summary,
            entries=tuple(entries),
            warnings=tuple(warnings),
        )

    # ---- Per-section projectors ------------------------------------

    def _project_domain_aliases(
        self, aliases: Sequence[Mapping[str, Any]],
        *, domain_id: str | None,
    ) -> Iterable[KnowledgeMemoryEntry]:
        for idx, alias in enumerate(aliases):
            if not isinstance(alias, Mapping):
                continue
            canonical = _str_or_none(alias.get("canonical_name") or alias.get("canonical"))
            if not canonical:
                continue
            alias_terms = alias.get("aliases") or ()
            if isinstance(alias_terms, (list, tuple)):
                alias_str = ", ".join(str(t) for t in alias_terms if t)
            else:
                alias_str = ""
            content = (
                f"{canonical}" + (f" — aka {alias_str}" if alias_str else "")
            )
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id("alias", idx, canonical),
                memory_type=MEMORY_ENTRY_TYPE_ALIAS,
                domain_id=domain_id,
                title=canonical,
                content=_cap(content),
                structured_payload={
                    "canonical_name": canonical,
                    "aliases": list(alias_terms) if isinstance(alias_terms, (list, tuple)) else [],
                    "entity_type": alias.get("entity_type"),
                    "confidence": alias.get("confidence"),
                },
                source=KnowledgeMemoryEntrySource(
                    origin=ENTRY_ORIGIN_DOMAIN_PACK,
                    artifact_kind="domain_pack",
                    producer="domain_pack",
                ),
                source_refs=(),
                status=ENTRY_STATUS_CONTEXTUAL,
            )

    def _project_string_hints(
        self, hints: Sequence[str], *,
        memory_type: str, domain_id: str | None,
    ) -> Iterable[KnowledgeMemoryEntry]:
        for idx, hint in enumerate(hints):
            if not hint:
                continue
            text = str(hint).strip()
            if not text:
                continue
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id(memory_type, idx, text),
                memory_type=memory_type,
                domain_id=domain_id,
                title=text[:120],
                content=_cap(text),
                source=KnowledgeMemoryEntrySource(
                    origin=ENTRY_ORIGIN_DOMAIN_PACK,
                    artifact_kind="domain_pack",
                    producer="domain_pack",
                ),
                source_refs=(),
                status=ENTRY_STATUS_CONTEXTUAL,
            )

    def _project_compile_signals(
        self, inputs: KnowledgeMemoryBuildInputs,
    ) -> Iterable[KnowledgeMemoryEntry]:
        # Single graph-summary entry capturing counts. Future
        # phases can add per-entity / per-relationship entries
        # when graph traversal is cheaper.
        if inputs.graph_entity_count or inputs.graph_relationship_count:
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id("graph_summary", 0, inputs.snapshot_id),
                memory_type=MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
                domain_id=inputs.domain_id,
                title="Graph summary",
                content=_cap(
                    f"{inputs.graph_entity_count} entities, "
                    f"{inputs.graph_relationship_count} relationships."
                ),
                structured_payload={
                    "entity_count": inputs.graph_entity_count,
                    "relationship_count": inputs.graph_relationship_count,
                },
                source=KnowledgeMemoryEntrySource(
                    origin=ENTRY_ORIGIN_COMPILE,
                    artifact_kind="graph_json",
                    producer="compile",
                ),
                source_refs=(),
                status=ENTRY_STATUS_CONTEXTUAL,
            )
        if inputs.document_type_hint:
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id(
                    "document_summary", 0, inputs.document_id,
                ),
                memory_type=MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY,
                domain_id=inputs.domain_id,
                title=f"Document type: {inputs.document_type_hint}",
                content=_cap(
                    f"Document classified as '{inputs.document_type_hint}' "
                    "by the active domain pack."
                ),
                structured_payload={
                    "document_type_hint": inputs.document_type_hint,
                },
                source=KnowledgeMemoryEntrySource(
                    origin=ENTRY_ORIGIN_COMPILE,
                    artifact_kind="compile_metadata",
                    producer="compile",
                ),
                source_refs=(),
                status=ENTRY_STATUS_CONTEXTUAL,
            )

    def _project_enrichment_envelope(
        self,
        envelope: DerivedEnrichmentArtifact,
        *,
        artifact_id: str,
    ) -> Iterable[KnowledgeMemoryEntry]:
        kind = envelope.artifact_kind
        payload = envelope.payload
        # Dispatch table — each branch maps the inner payload to
        # zero or more memory entries. Per-kind logic stays local
        # so adding a new kind doesn't change unrelated paths.
        if kind == "enriched.requirements":
            yield from self._project_per_item(
                payload, envelope,
                list_key="requirements",
                memory_type=MEMORY_ENTRY_TYPE_REQUIREMENT,
                artifact_id=artifact_id, artifact_kind=kind,
            )
        elif kind == "enriched.risks":
            yield from self._project_per_item(
                payload, envelope,
                list_key="risks",
                memory_type=MEMORY_ENTRY_TYPE_RISK,
                artifact_id=artifact_id, artifact_kind=kind,
            )
        elif kind == "enriched.consistency_findings":
            yield from self._project_per_item(
                payload, envelope,
                list_key="findings",
                memory_type=MEMORY_ENTRY_TYPE_VALIDATION_CHECK,
                artifact_id=artifact_id, artifact_kind=kind,
            )
        elif kind == "enriched.formulas":
            yield from self._project_per_item(
                payload, envelope,
                list_key="formulas",
                memory_type=MEMORY_ENTRY_TYPE_FORMULA,
                artifact_id=artifact_id, artifact_kind=kind,
            )
        elif kind == "enriched.tables":
            yield from self._project_per_item(
                payload, envelope,
                list_key="tables",
                memory_type=MEMORY_ENTRY_TYPE_TABLE_SUMMARY,
                artifact_id=artifact_id, artifact_kind=kind,
                title_keys=("title", "caption", "name"),
            )
        elif kind == "enriched.visuals":
            yield from self._project_per_item(
                payload, envelope,
                list_key="visuals",
                memory_type=MEMORY_ENTRY_TYPE_VISUAL_SUMMARY,
                artifact_id=artifact_id, artifact_kind=kind,
                title_keys=("caption", "title"),
                content_keys=("description", "caption"),
            )
        elif kind == "enriched.document_map":
            yield from self._project_document_map(
                payload, envelope,
                artifact_id=artifact_id,
            )
        elif kind == "enriched.confidence_assessment":
            yield from self._project_confidence_assessment(
                payload, envelope, artifact_id=artifact_id,
            )
        elif kind == "domain_enrichment_aliases":
            yield from self._project_alias_artifact(
                payload, envelope, artifact_id=artifact_id,
            )
        elif kind == "enriched.source_map":
            # Source-map artifacts trace WHERE compile evidence
            # lives — they're a useful audit signal but a noisy
            # query signal. Project a single summary entry rather
            # than one per source.
            count = len(payload.get("sources") or ())
            if count:
                yield KnowledgeMemoryEntry(
                    memory_id=_synth_id("source_map", 0, artifact_id),
                    memory_type=MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT,
                    domain_id=envelope.domain_id,
                    title="Source map",
                    content=_cap(f"Source map covers {count} artifacts."),
                    structured_payload={"source_count": count},
                    source=self._source_for_artifact(
                        artifact_id=artifact_id, kind=kind,
                    ),
                    source_refs=envelope.source_refs[:self._max_entries_per_kind],
                    status=ENTRY_STATUS_ACTIVE,
                )
        elif kind == "enrichment_result":
            # The composite overlay; project per-module-outcome
            # status as `domain_insight` entries so future query
            # can see "which enrichers ran".
            yield from self._project_enrichment_result(
                payload, envelope, artifact_id=artifact_id,
            )

    # ---- Per-item helpers --------------------------------------------

    def _project_per_item(
        self,
        payload: Mapping[str, Any],
        envelope: DerivedEnrichmentArtifact,
        *,
        list_key: str,
        memory_type: str,
        artifact_id: str,
        artifact_kind: str,
        title_keys: tuple[str, ...] = ("title", "text", "name"),
        content_keys: tuple[str, ...] = ("text", "description", "summary"),
    ) -> Iterable[KnowledgeMemoryEntry]:
        items = payload.get(list_key) or ()
        if not isinstance(items, (list, tuple)):
            return
        # Build a chunk_id / page → matching EnrichmentSourceRef map
        # so per-item entries pick up the right per-item ref.
        ref_index = self._index_source_refs(envelope.source_refs)
        for idx, item in enumerate(items):
            if idx >= self._max_entries_per_kind:
                break
            if not isinstance(item, Mapping):
                continue
            title = _first_str(item, title_keys) or f"{memory_type} #{idx + 1}"
            content = _first_str(item, content_keys) or title
            chunk_id = _str_or_none(item.get("chunk_id"))
            page = _int_or_none(item.get("page"))
            refs = self._refs_for_item(
                ref_index, chunk_id=chunk_id, page=page,
                fallback=envelope.source_refs,
            )
            status = ENTRY_STATUS_ACTIVE if refs else ENTRY_STATUS_CONTEXTUAL
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id(memory_type, idx, artifact_id),
                memory_type=memory_type,
                domain_id=envelope.domain_id,
                title=title[:120],
                content=_cap(content),
                structured_payload=dict(item),
                source=self._source_for_artifact(
                    artifact_id=artifact_id, kind=artifact_kind,
                ),
                source_refs=refs,
                confidence=_str_or_none(item.get("confidence")),
                tags=self._tags_for_item(item),
                status=status,
            )

    def _project_document_map(
        self,
        payload: Mapping[str, Any],
        envelope: DerivedEnrichmentArtifact,
        *,
        artifact_id: str,
    ) -> Iterable[KnowledgeMemoryEntry]:
        sections = payload.get("sections") or ()
        if not isinstance(sections, (list, tuple)):
            return
        ref_index = self._index_source_refs(envelope.source_refs)
        for idx, section in enumerate(sections):
            if idx >= self._max_entries_per_kind:
                break
            if not isinstance(section, Mapping):
                continue
            title = _str_or_none(section.get("title")) or f"Section #{idx + 1}"
            page_start = _int_or_none(section.get("page_start"))
            page_end = _int_or_none(section.get("page_end"))
            content_parts: list[str] = [title]
            if page_start is not None:
                if page_end is not None:
                    content_parts.append(f"pp. {page_start}–{page_end}")
                else:
                    content_parts.append(f"p. {page_start}")
            refs = self._refs_for_item(
                ref_index, chunk_id=None, page=page_start,
                fallback=envelope.source_refs,
            )
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id("section", idx, artifact_id),
                memory_type=MEMORY_ENTRY_TYPE_SECTION,
                domain_id=envelope.domain_id,
                title=title[:120],
                content=_cap(" ".join(content_parts)),
                structured_payload=dict(section),
                source=self._source_for_artifact(
                    artifact_id=artifact_id, kind="enriched.document_map",
                ),
                source_refs=refs,
                status=ENTRY_STATUS_ACTIVE if refs else ENTRY_STATUS_CONTEXTUAL,
            )

    def _project_confidence_assessment(
        self,
        payload: Mapping[str, Any],
        envelope: DerivedEnrichmentArtifact,
        *,
        artifact_id: str,
    ) -> Iterable[KnowledgeMemoryEntry]:
        overall = _str_or_none(payload.get("overall_confidence"))
        assessments = payload.get("assessments") or ()
        content_parts: list[str] = []
        if overall:
            content_parts.append(f"Overall confidence: {overall}")
        if isinstance(assessments, (list, tuple)) and assessments:
            content_parts.append(f"{len(assessments)} category assessments.")
        yield KnowledgeMemoryEntry(
            memory_id=_synth_id("quality_summary", 0, artifact_id),
            memory_type=MEMORY_ENTRY_TYPE_QUALITY_SUMMARY,
            domain_id=envelope.domain_id,
            title="Quality assessment",
            content=_cap(" — ".join(content_parts) or "No quality signals captured."),
            structured_payload={
                "overall_confidence": overall,
                "assessment_count": (
                    len(assessments) if isinstance(assessments, (list, tuple)) else 0
                ),
            },
            source=self._source_for_artifact(
                artifact_id=artifact_id, kind="enriched.confidence_assessment",
            ),
            source_refs=envelope.source_refs,
            confidence=overall,
            status=ENTRY_STATUS_ACTIVE if envelope.source_refs else ENTRY_STATUS_CONTEXTUAL,
        )

    def _project_alias_artifact(
        self,
        payload: Mapping[str, Any],
        envelope: DerivedEnrichmentArtifact,
        *,
        artifact_id: str,
    ) -> Iterable[KnowledgeMemoryEntry]:
        aliases = payload.get("aliases") or ()
        if not isinstance(aliases, (list, tuple)):
            return
        ref_index = self._index_source_refs(envelope.source_refs)
        for idx, alias in enumerate(aliases):
            if idx >= self._max_entries_per_kind:
                break
            if not isinstance(alias, Mapping):
                continue
            canonical = _str_or_none(alias.get("canonical")) or "unknown"
            alias_text = _str_or_none(alias.get("alias")) or canonical
            evidence = alias.get("evidence") or {}
            chunk_id = (
                _str_or_none(evidence.get("chunk_id"))
                if isinstance(evidence, Mapping) else None
            )
            page = (
                _int_or_none(evidence.get("page"))
                if isinstance(evidence, Mapping) else None
            )
            refs = self._refs_for_item(
                ref_index, chunk_id=chunk_id, page=page,
                fallback=envelope.source_refs,
            )
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id("alias_evidence", idx, artifact_id),
                memory_type=MEMORY_ENTRY_TYPE_ALIAS,
                domain_id=envelope.domain_id,
                title=alias_text,
                content=_cap(f"{alias_text} → {canonical}"),
                structured_payload={
                    "canonical": canonical,
                    "alias": alias_text,
                    "evidence": dict(evidence) if isinstance(evidence, Mapping) else {},
                },
                source=self._source_for_artifact(
                    artifact_id=artifact_id, kind="domain_enrichment_aliases",
                ),
                source_refs=refs,
                status=ENTRY_STATUS_ACTIVE if refs else ENTRY_STATUS_CONTEXTUAL,
            )

    def _project_enrichment_result(
        self,
        payload: Mapping[str, Any],
        envelope: DerivedEnrichmentArtifact,
        *,
        artifact_id: str,
    ) -> Iterable[KnowledgeMemoryEntry]:
        outcomes = payload.get("module_outcomes") or ()
        if not isinstance(outcomes, (list, tuple)):
            return
        for idx, outcome in enumerate(outcomes):
            if not isinstance(outcome, Mapping):
                continue
            module_id = _str_or_none(outcome.get("module_id")) or f"module_{idx}"
            status = _str_or_none(outcome.get("status")) or "unknown"
            yield KnowledgeMemoryEntry(
                memory_id=_synth_id("enrichment_module", idx, artifact_id),
                memory_type=MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT,
                domain_id=envelope.domain_id,
                title=f"{module_id} ({status})",
                content=_cap(
                    f"Enrichment module {module_id} reported status {status}."
                ),
                structured_payload={
                    "module_id": module_id,
                    "status": status,
                },
                source=self._source_for_artifact(
                    artifact_id=artifact_id, kind="enrichment_result",
                ),
                source_refs=envelope.source_refs[:8],
                tags=(status,),
                status=ENTRY_STATUS_ACTIVE,
            )

    # ---- Source-ref indexing ---------------------------------------

    def _index_source_refs(
        self, refs: tuple[EnrichmentSourceRef, ...],
    ) -> dict[tuple[str | None, int | None], EnrichmentSourceRef]:
        """Index `EnrichmentSourceRef`s by `(chunk_id, page)` so the
        per-item projector can look up the matching ref in O(1).
        Falls back to fuzzier match when only one key is present."""
        out: dict[tuple[str | None, int | None], EnrichmentSourceRef] = {}
        for ref in refs:
            out[(ref.chunk_id, ref.page)] = ref
        return out

    def _refs_for_item(
        self,
        ref_index: Mapping[tuple[str | None, int | None], EnrichmentSourceRef],
        *,
        chunk_id: str | None,
        page: int | None,
        fallback: tuple[EnrichmentSourceRef, ...],
    ) -> tuple[EnrichmentSourceRef, ...]:
        match = ref_index.get((chunk_id, page))
        if match is not None:
            return (match,)
        if chunk_id is not None:
            for (c, _), ref in ref_index.items():
                if c == chunk_id:
                    return (ref,)
        if page is not None:
            for (_, p), ref in ref_index.items():
                if p == page:
                    return (ref,)
        # No per-item match; if the envelope only carried a coarse
        # artifact-level ref we surface that as the item's ref so
        # the entry is at least traceable back to a compile artifact.
        if len(fallback) == 1 and not fallback[0].chunk_id and fallback[0].page is None:
            return fallback
        return ()

    def _source_for_artifact(
        self, *, artifact_id: str, kind: str,
    ) -> KnowledgeMemoryEntrySource:
        return KnowledgeMemoryEntrySource(
            origin=ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT,
            artifact_kind=kind,
            artifact_id=artifact_id,
            producer="enrichment_module",
        )

    def _tags_for_item(self, item: Mapping[str, Any]) -> tuple[str, ...]:
        tags: list[str] = []
        for key in ("severity", "category", "priority", "type"):
            value = _str_or_none(item.get(key))
            if value:
                tags.append(value)
        return tuple(tags)

    # ---- Summary --------------------------------------------------

    def _build_summary(
        self,
        entries: list[KnowledgeMemoryEntry],
        *,
        document_type_hint: str | None,
    ) -> KnowledgeMemorySummary:
        entity_count = sum(
            1 for e in entries if e.memory_type == MEMORY_ENTRY_TYPE_ENTITY
        )
        relationship_count = sum(
            1 for e in entries if e.memory_type == MEMORY_ENTRY_TYPE_RELATIONSHIP
        )
        alias_count = sum(
            1 for e in entries if e.memory_type == MEMORY_ENTRY_TYPE_ALIAS
        )
        # `domain_insight_count` covers everything that isn't
        # entity / relationship / alias — requirements, risks,
        # findings, tables, visuals, formulas, sections, graph_summary,
        # quality_summary, terminology, retrieval_hint, etc.
        insight_count = len(entries) - entity_count - relationship_count - alias_count
        source_ref_count = sum(len(e.source_refs) for e in entries)
        return KnowledgeMemorySummary(
            document_type_hint=document_type_hint,
            entity_count=entity_count,
            relationship_count=relationship_count,
            alias_count=alias_count,
            domain_insight_count=insight_count,
            source_ref_count=source_ref_count,
        )


# ---- Helpers ------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    return _int_or_none(value) or 0


def _first_str(
    item: Mapping[str, Any], keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = _str_or_none(item.get(key))
        if value:
            return value
    return None


def _cap(text: str) -> str:
    """Truncate to the configured entry-content cap. Producers
    that already know the text fits skip this; the builder applies
    it as a safety net."""
    if len(text) <= ENTRY_CONTENT_CAP_CHARS:
        return text
    return text[: ENTRY_CONTENT_CAP_CHARS - 1] + "…"


def _synth_id(prefix: str, index: int, seed: str) -> str:
    """Deterministic id derived from prefix + index + short hash of
    the seed string. Used for memory_id when the caller doesn't
    supply one. Determinism matters for idempotency: rebuilds with
    the same inputs produce identical ids so consumers can compare
    artifacts across builds.

    We use a short SHA1 hash truncated to 8 chars — collision
    probability per (prefix, seed) pair is negligible and the id
    stays human-readable."""
    import hashlib
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}:{index}:{digest}"
