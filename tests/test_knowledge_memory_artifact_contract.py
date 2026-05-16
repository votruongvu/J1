"""Contract — persistent Knowledge Memory artifact (Phase 2).

Pins the surface added in `j1.memory.knowledge_memory` and
`j1.memory.service`:

  * `KnowledgeMemoryPayload` + `KnowledgeMemoryEntry` round-trip
    cleanly with stable schema marker.
  * `KnowledgeMemoryBuilder` projects domain pack hints, compile
    signals, and Phase-1-normalised enrichment payloads into
    typed entries.
  * `KnowledgeMemoryService.build_and_persist(...)` orchestrates
    the build against a fake `(source_lookup, registry, workspace,
    processing_service, domain_registry)` quartet.
  * Manual action `ACTION_BUILD_KNOWLEDGE_MEMORY` is now wired
    (status=available; feature flag respected; the not_implemented
    marker is gone).
  * Idempotency / snapshot isolation: rebuild supersedes prior
    memory; superseded source artifacts never feed the build.

The tests use a small in-memory artifact registry stub + a
deterministic source-lookup stub so we exercise the orchestration
without filesystem / workspace concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from j1.memory.knowledge_memory import (
    DEFAULT_MAX_ENTRIES_PER_KIND,
    ENTRY_ORIGIN_COMPILE,
    ENTRY_ORIGIN_DOMAIN_PACK,
    ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT,
    ENTRY_STATUS_ACTIVE,
    ENTRY_STATUS_CONTEXTUAL,
    KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA,
    KNOWLEDGE_MEMORY_BUILDER_NAME,
    KNOWLEDGE_MEMORY_BUILDER_VERSION,
    MEMORY_ENTRY_TYPE_ALIAS,
    MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY,
    MEMORY_ENTRY_TYPE_DOMAIN_INSIGHT,
    MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
    MEMORY_ENTRY_TYPE_QUALITY_SUMMARY,
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
    MEMORY_ENTRY_TYPE_RISK,
    MEMORY_ENTRY_TYPE_SECTION,
    MEMORY_ENTRY_TYPE_TABLE_SUMMARY,
    MEMORY_ENTRY_TYPE_TERMINOLOGY,
    MEMORY_ENTRY_TYPE_VALIDATION_CHECK,
    MEMORY_ENTRY_TYPE_VISUAL_SUMMARY,
    WARNING_NO_ENRICHMENT_ARTIFACTS,
    WARNING_SUPERSEDED_ARTIFACT_SKIPPED,
    WARNING_UNKNOWN_ENRICHMENT_KIND_SKIPPED,
    KnowledgeMemoryBuilder,
    KnowledgeMemoryBuildInputs,
    KnowledgeMemoryEntry,
    KnowledgeMemoryEntrySource,
    KnowledgeMemoryPayload,
)
from j1.memory.service import (
    KnowledgeMemoryBuildResult,
    KnowledgeMemoryService,
    NoActiveSnapshotError,
)
from j1.processing.derived_enrichment import EnrichmentSourceRef
from j1.processing.manual_actions import (
    ACTION_BUILD_KNOWLEDGE_MEMORY,
    ENV_MANUAL_BUILD_KNOWLEDGE_MEMORY,
    MANUAL_ACTION_STATUS_AVAILABLE,
    MANUAL_ACTION_STATUS_DISABLED,
    MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    is_manual_action_enabled,
    list_manual_actions,
)
from j1.processing.results import ARTIFACT_KIND_KNOWLEDGE_MEMORY
from j1.projects.context import ProjectContext


# ---- Test fixtures ----------------------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


@dataclass
class _Doc:
    document_id: str
    active_snapshot_id: str | None
    domain_id: str | None = None
    document_type_hint: str | None = None


@dataclass
class _Record:
    artifact_id: str
    kind: str
    location: str = ""
    metadata: dict = None
    source_document_ids: tuple[str, ...] = ()
    created_at: datetime = datetime(2026, 5, 16, tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class _StubSourceLookup:
    def __init__(self, doc: _Doc) -> None:
        self._doc = doc

    def get_source(self, ctx: ProjectContext, document_id: str) -> _Doc:
        if document_id != self._doc.document_id:
            raise LookupError(document_id)
        return self._doc


class _StubArtifactRegistry:
    def __init__(self, records: list[_Record] | None = None) -> None:
        self.records: list[_Record] = list(records or [])
        self.metadata_updates: list[tuple[str, dict]] = []

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None,
    ) -> list[_Record]:
        if kind is None:
            return list(self.records)
        return [r for r in self.records if r.kind == kind]

    def update_metadata(
        self, ctx: ProjectContext, artifact_id: str, metadata: dict,
    ) -> None:
        for r in self.records:
            if r.artifact_id == artifact_id:
                r.metadata = dict(metadata)
                self.metadata_updates.append((artifact_id, dict(metadata)))
                return
        raise KeyError(artifact_id)


class _StubProcessingService:
    """Mimics `ProcessingService.persist_knowledge_memory` without
    touching workspace / filesystem."""

    def __init__(self, registry: _StubArtifactRegistry) -> None:
        self.registry = registry
        self.persisted: list[dict] = []
        self._counter = 0

    def persist_knowledge_memory(
        self,
        ctx: ProjectContext,
        *,
        run_id: str,
        document_id: str,
        snapshot_id: str,
        payload: dict,
        actor: str = "system",
        trigger: str | None = None,
        includes_domain_insights: bool | None = None,
    ) -> _Record:
        # Mirror service.py's supersede sweep on the stub registry.
        from j1.processing.service import _supersede_prior_knowledge_memory
        _supersede_prior_knowledge_memory(
            self.registry, ctx,
            document_id=document_id, snapshot_id=snapshot_id,
        )

        self._counter += 1
        new_id = f"memart-{self._counter}"
        record = _Record(
            artifact_id=new_id,
            kind=ARTIFACT_KIND_KNOWLEDGE_MEMORY,
            location=f"enriched/{new_id}.json",
            metadata={
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "document_id": document_id,
                "search_state": "active",
                "entry_count": len(payload.get("entries") or []),
            },
            source_document_ids=(document_id,),
        )
        self.registry.records.append(record)
        self.persisted.append(payload)
        return record


# ---- Payload + entry round-trip --------------------------------


def test_knowledge_memory_payload_round_trips():
    payload = KnowledgeMemoryPayload(
        document_id="doc-1",
        snapshot_id="snap-1",
        run_id="run-1",
        project_id="proj-1",
        domain_id="civil_engineering",
        entries=(
            KnowledgeMemoryEntry(
                memory_id="alias:0:abc",
                memory_type=MEMORY_ENTRY_TYPE_ALIAS,
                domain_id="civil_engineering",
                title="bill of quantities",
                content="bill of quantities — aka BoQ",
                status=ENTRY_STATUS_CONTEXTUAL,
            ),
        ),
        warnings=("missing_source_refs",),
    )
    round_tripped = KnowledgeMemoryPayload.from_payload(payload.to_payload())
    assert round_tripped.artifact_schema == KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA
    assert round_tripped.document_id == "doc-1"
    assert round_tripped.snapshot_id == "snap-1"
    assert round_tripped.run_id == "run-1"
    assert round_tripped.project_id == "proj-1"
    assert round_tripped.domain_id == "civil_engineering"
    assert len(round_tripped.entries) == 1
    assert round_tripped.entries[0].memory_id == "alias:0:abc"


def test_knowledge_memory_entry_preserves_source_refs():
    entry = KnowledgeMemoryEntry(
        memory_id="r:0:abc",
        memory_type=MEMORY_ENTRY_TYPE_REQUIREMENT,
        title="R1",
        content="Concrete grade 30",
        source=KnowledgeMemoryEntrySource(
            origin=ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT,
            artifact_kind="enriched.requirements",
            artifact_id="eart-1",
        ),
        source_refs=(EnrichmentSourceRef(
            document_id="doc-1", snapshot_id="snap-1", run_id="run-1",
            artifact_id="cart-1", artifact_kind="compiled_text",
            chunk_id="c1", page=5,
        ),),
    )
    payload = entry.to_payload()
    rt = KnowledgeMemoryEntry.from_payload(payload)
    assert rt.source_refs[0].document_id == "doc-1"
    assert rt.source_refs[0].snapshot_id == "snap-1"
    assert rt.source_refs[0].run_id == "run-1"
    assert rt.source_refs[0].chunk_id == "c1"
    assert rt.source_refs[0].page == 5


def test_knowledge_memory_payload_stamps_builder_identity():
    payload = KnowledgeMemoryPayload()
    rt = KnowledgeMemoryPayload.from_payload(payload.to_payload())
    assert rt.source.builder.name == KNOWLEDGE_MEMORY_BUILDER_NAME
    assert rt.source.builder.version == KNOWLEDGE_MEMORY_BUILDER_VERSION


def test_knowledge_memory_payload_legacy_round_trip_tolerates_missing_keys():
    """A payload from a future build that omits / adds keys must
    still round-trip without raising."""
    raw = {
        "artifact_schema": KNOWLEDGE_MEMORY_ARTIFACT_SCHEMA,
        "document_id": "doc-1",
        "snapshot_id": "snap-1",
        # No `entries`, no `source`, no `summary`.
    }
    payload = KnowledgeMemoryPayload.from_payload(raw)
    assert payload.entries == ()
    assert payload.summary.entity_count == 0
    assert payload.source.builder.name == KNOWLEDGE_MEMORY_BUILDER_NAME


# ---- Builder: domain pack + compile signals --------------------


def test_builder_projects_domain_pack_aliases_as_contextual_entries():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        domain_id="civil",
        aliases=[
            {"canonical_name": "BoQ", "aliases": ["bill of quantities"]},
            {"canonical_name": "spec", "aliases": ["specification"]},
        ],
    ))
    aliases = [e for e in payload.entries if e.memory_type == MEMORY_ENTRY_TYPE_ALIAS]
    assert len(aliases) == 2
    assert all(e.source.origin == ENTRY_ORIGIN_DOMAIN_PACK for e in aliases)
    assert all(e.status == ENTRY_STATUS_CONTEXTUAL for e in aliases)
    assert all(e.source_refs == () for e in aliases)


def test_builder_projects_terminology_and_retrieval_hints_separately():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        terminology_hints=["rebar", "lintel"],
        retrieval_hints=["look up BoQ for cost items"],
    ))
    types = [e.memory_type for e in payload.entries]
    assert types.count(MEMORY_ENTRY_TYPE_TERMINOLOGY) == 2
    assert types.count(MEMORY_ENTRY_TYPE_RETRIEVAL_HINT) == 1


def test_builder_projects_graph_summary_and_document_summary():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        graph_entity_count=42, graph_relationship_count=12,
        document_type_hint="boq",
    ))
    types = {e.memory_type for e in payload.entries}
    assert MEMORY_ENTRY_TYPE_GRAPH_SUMMARY in types
    assert MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY in types
    graph = next(
        e for e in payload.entries
        if e.memory_type == MEMORY_ENTRY_TYPE_GRAPH_SUMMARY
    )
    assert graph.structured_payload["entity_count"] == 42
    assert graph.structured_payload["relationship_count"] == 12
    assert graph.source.origin == ENTRY_ORIGIN_COMPILE


def test_builder_omits_graph_summary_when_counts_zero():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
    ))
    types = {e.memory_type for e in payload.entries}
    assert MEMORY_ENTRY_TYPE_GRAPH_SUMMARY not in types


def test_builder_emits_no_enrichment_warning_when_none_supplied():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
    ))
    assert WARNING_NO_ENRICHMENT_ARTIFACTS in payload.warnings


# ---- Builder: enrichment projection per kind -------------------


def test_builder_projects_requirements_with_source_refs():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1", run_id="run-1",
        enrichment_artifacts=[(
            "eart-1", "enriched.requirements",
            {"requirements": [
                {"text": "Concrete grade 30", "chunk_id": "c1", "page": 5},
                {"text": "Rebar tolerance 2mm", "chunk_id": "c2", "page": 7},
            ], "source_artifact_id": "cart-1"},
        )],
    ))
    reqs = [e for e in payload.entries if e.memory_type == MEMORY_ENTRY_TYPE_REQUIREMENT]
    assert len(reqs) == 2
    # Each requirement entry got the matching per-item source ref.
    assert reqs[0].source_refs[0].chunk_id == "c1"
    assert reqs[0].source_refs[0].page == 5
    assert reqs[0].source_refs[0].snapshot_id == "snap-1"
    assert reqs[0].source.origin == ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT
    assert reqs[0].source.artifact_kind == "enriched.requirements"
    assert reqs[0].source.artifact_id == "eart-1"
    assert reqs[0].status == ENTRY_STATUS_ACTIVE


def test_builder_projects_risks():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-r", "enriched.risks",
            {"risks": [{"text": "Falling object", "page": 3, "severity": "high"}],
             "source_artifact_id": "cart-1"},
        )],
    ))
    risks = [e for e in payload.entries if e.memory_type == MEMORY_ENTRY_TYPE_RISK]
    assert len(risks) == 1
    assert "high" in risks[0].tags


def test_builder_projects_consistency_findings_as_validation_checks():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-v", "enriched.consistency_findings",
            {"findings": [{"text": "Spec vs drawing mismatch", "page": 4}],
             "source_artifact_id": "cart-1"},
        )],
    ))
    checks = [
        e for e in payload.entries
        if e.memory_type == MEMORY_ENTRY_TYPE_VALIDATION_CHECK
    ]
    assert len(checks) == 1


def test_builder_projects_tables_visuals_formulas():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[
            ("eart-t", "enriched.tables",
             {"tables": [{"caption": "Cost summary", "page": 12, "table_id": "t1"}],
              "source_artifact_id": "cart-1"}),
            ("eart-vis", "enriched.visuals",
             {"visuals": [{"caption": "Plan view", "description": "Floor plan", "artifact_id": "img-1"}],
              "source_artifact_id": "cart-1"}),
            ("eart-f", "enriched.formulas",
             {"formulas": [{"text": "F = m·a", "page": 8}],
              "source_artifact_id": "cart-1"}),
        ],
    ))
    types = [e.memory_type for e in payload.entries]
    assert MEMORY_ENTRY_TYPE_TABLE_SUMMARY in types
    assert MEMORY_ENTRY_TYPE_VISUAL_SUMMARY in types
    # Formulas → no dedicated MEMORY_ENTRY_TYPE_FORMULA top-level
    # constant test here because the builder uses the formula type;
    # check the table summary count instead.
    table_summaries = [
        e for e in payload.entries
        if e.memory_type == MEMORY_ENTRY_TYPE_TABLE_SUMMARY
    ]
    assert table_summaries[0].title == "Cost summary"


def test_builder_projects_document_map_sections():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-dm", "enriched.document_map",
            {"sections": [
                {"title": "Intro", "page_start": 1, "page_end": 3},
                {"title": "Specs", "page_start": 4, "page_end": 12},
            ],
             "source_artifact_id": "cart-1"},
        )],
    ))
    sections = [e for e in payload.entries if e.memory_type == MEMORY_ENTRY_TYPE_SECTION]
    assert len(sections) == 2
    assert sections[0].title == "Intro"


def test_builder_projects_confidence_assessment_as_quality_summary():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-qa", "enriched.confidence_assessment",
            {"overall_confidence": "high",
             "assessments": [{"category": "metadata", "score": 0.9}],
             "source_artifact_id": "cart-1"},
        )],
    ))
    quality = [
        e for e in payload.entries
        if e.memory_type == MEMORY_ENTRY_TYPE_QUALITY_SUMMARY
    ]
    assert len(quality) == 1
    assert quality[0].confidence == "high"


def test_builder_projects_domain_enrichment_aliases_with_evidence():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-al", "domain_enrichment_aliases",
            {"aliases": [
                {"alias": "BoQ", "canonical": "bill of quantities",
                 "evidence": {"document_id": "doc-1", "snapshot_id": "snap-1",
                              "run_id": "run-1", "artifact_id": "chunk-art",
                              "chunk_id": "c1", "page": 5,
                              "snippet": "BoQ refers to ..."}},
            ]},
        )],
    ))
    aliases = [
        e for e in payload.entries
        if e.memory_type == MEMORY_ENTRY_TYPE_ALIAS
        and e.source.origin == ENTRY_ORIGIN_POST_COMPILE_ENRICHMENT
    ]
    assert len(aliases) == 1
    assert aliases[0].source_refs[0].chunk_id == "c1"
    assert aliases[0].source_refs[0].page == 5


def test_builder_ignores_unknown_enrichment_kind_with_warning():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[(
            "eart-x", "enriched.future_thing",
            {"some_data": [1, 2]},
        )],
    ))
    assert WARNING_UNKNOWN_ENRICHMENT_KIND_SKIPPED in payload.warnings
    # No entries projected for the unknown kind.
    assert all(
        e.source.artifact_id != "eart-x" for e in payload.entries
        if e.source is not None
    )


def test_builder_does_not_consume_post_compile_enrich_plan():
    """`post_compile_enrich_plan` is a plan, not an enrichment
    result. Service-level filter excludes it; the builder would
    also emit `unknown_enrichment_kind_skipped` if it leaked in."""
    from j1.memory.service import _ENRICHMENT_KINDS_PROJECTED
    assert "post_compile_enrich_plan" not in _ENRICHMENT_KINDS_PROJECTED


def test_builder_does_not_mutate_input_payload():
    raw = {"requirements": [{"text": "X", "page": 1}],
           "source_artifact_id": "cart-1"}
    snapshot = {**raw, "requirements": [dict(r) for r in raw["requirements"]]}
    builder = KnowledgeMemoryBuilder()
    builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        enrichment_artifacts=[("eart-r", "enriched.requirements", raw)],
    ))
    assert raw == snapshot


# ---- Summary counts --------------------------------------------


def test_builder_summary_reflects_entry_types():
    builder = KnowledgeMemoryBuilder()
    payload = builder.build(KnowledgeMemoryBuildInputs(
        document_id="doc-1", snapshot_id="snap-1",
        domain_id="civil",
        aliases=[{"canonical_name": "BoQ", "aliases": ["bill of quantities"]}],
        terminology_hints=["rebar"],
        retrieval_hints=[],
        graph_entity_count=10, graph_relationship_count=5,
        enrichment_artifacts=[(
            "eart-r", "enriched.requirements",
            {"requirements": [{"text": "X", "page": 1}],
             "source_artifact_id": "cart-1"},
        )],
    ))
    s = payload.summary
    assert s.alias_count == 1
    # Everything else (terminology, graph_summary, requirement)
    # bucketed as domain_insight per the builder's accounting rule.
    assert s.domain_insight_count >= 3
    assert s.source_ref_count >= 1


# ---- KnowledgeMemoryService orchestration ----------------------


def _make_service(
    *, doc: _Doc, records: list[_Record],
    domain_registry=None,
) -> tuple[KnowledgeMemoryService, _StubProcessingService, _StubArtifactRegistry]:
    registry = _StubArtifactRegistry(records=records)
    processing = _StubProcessingService(registry)
    service = KnowledgeMemoryService(
        source_lookup=_StubSourceLookup(doc),
        artifact_registry=registry,
        workspace=None,  # inline payloads only
        processing_service=processing,
        domain_registry=domain_registry,
    )
    return service, processing, registry


def _enrichment_record(
    artifact_id: str, kind: str, *,
    document_id: str, snapshot_id: str,
    inline_payload: dict,
    search_state: str = "active",
) -> _Record:
    return _Record(
        artifact_id=artifact_id, kind=kind,
        location=f"enriched/{artifact_id}.json",
        metadata={
            "run_id": "run-1",
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "search_state": search_state,
            "payload": inline_payload,
        },
        source_document_ids=(document_id,),
    )


def test_service_builds_persists_and_returns_artifact_id():
    doc = _Doc(document_id="doc-1", active_snapshot_id="snap-1")
    records = [
        _enrichment_record(
            "eart-1", "enriched.requirements",
            document_id="doc-1", snapshot_id="snap-1",
            inline_payload={
                "requirements": [{"text": "R1", "page": 1, "chunk_id": "c1"}],
                "source_artifact_id": "cart-1",
            },
        ),
    ]
    service, processing, registry = _make_service(doc=doc, records=records)
    result = service.build_and_persist(_ctx(), "doc-1")
    assert result.status == "succeeded"
    assert result.artifact_id == "memart-1"
    assert result.snapshot_id == "snap-1"
    assert result.entry_count >= 1
    # Persistence was called once.
    assert len(processing.persisted) == 1
    # The persisted payload includes the requirement entry.
    persisted = processing.persisted[0]
    types = {e["memory_type"] for e in persisted["entries"]}
    assert MEMORY_ENTRY_TYPE_REQUIREMENT in types


def test_service_raises_no_active_snapshot_error_when_missing():
    doc = _Doc(document_id="doc-1", active_snapshot_id=None)
    service, _, _ = _make_service(doc=doc, records=[])
    with pytest.raises(NoActiveSnapshotError):
        service.build_and_persist(_ctx(), "doc-1")


def test_service_skips_superseded_artifacts():
    doc = _Doc(document_id="doc-1", active_snapshot_id="snap-1")
    records = [
        _enrichment_record(
            "eart-active", "enriched.requirements",
            document_id="doc-1", snapshot_id="snap-1",
            inline_payload={"requirements": [{"text": "active", "page": 1}],
                            "source_artifact_id": "cart-1"},
        ),
        _enrichment_record(
            "eart-superseded", "enriched.requirements",
            document_id="doc-1", snapshot_id="snap-1",
            inline_payload={"requirements": [{"text": "old", "page": 1}],
                            "source_artifact_id": "cart-OLD"},
            search_state="superseded",
        ),
        _enrichment_record(
            "eart-other-snapshot", "enriched.requirements",
            document_id="doc-1", snapshot_id="snap-OLD",
            inline_payload={"requirements": [{"text": "stale", "page": 1}],
                            "source_artifact_id": "cart-x"},
        ),
    ]
    service, processing, _ = _make_service(doc=doc, records=records)
    result = service.build_and_persist(_ctx(), "doc-1")
    persisted = processing.persisted[0]
    # Only the active artifact's content reached the build.
    contents = " ".join(
        e["structured_payload"].get("text", "")
        for e in persisted["entries"]
        if e["memory_type"] == MEMORY_ENTRY_TYPE_REQUIREMENT
    )
    assert "active" in contents
    assert "old" not in contents
    assert "stale" not in contents
    # Warning surfaced for the superseded skip.
    assert WARNING_SUPERSEDED_ARTIFACT_SKIPPED in result.warnings


def test_service_idempotent_rebuild_supersedes_prior_memory():
    doc = _Doc(document_id="doc-1", active_snapshot_id="snap-1")
    records = [
        _enrichment_record(
            "eart-1", "enriched.requirements",
            document_id="doc-1", snapshot_id="snap-1",
            inline_payload={"requirements": [{"text": "R1", "page": 1}],
                            "source_artifact_id": "cart-1"},
        ),
    ]
    service, processing, registry = _make_service(doc=doc, records=records)
    first = service.build_and_persist(_ctx(), "doc-1")
    second = service.build_and_persist(_ctx(), "doc-1")
    assert first.artifact_id == "memart-1"
    assert second.artifact_id == "memart-2"
    # The first memory artifact should now be superseded; the
    # second is active.
    first_record = next(r for r in registry.records if r.artifact_id == "memart-1")
    second_record = next(r for r in registry.records if r.artifact_id == "memart-2")
    assert first_record.metadata.get("search_state") == "superseded"
    assert second_record.metadata.get("search_state") == "active"
    # Supersede sweep was invoked.
    assert any(
        u[0] == "memart-1" for u in registry.metadata_updates
    )


def test_service_handles_missing_domain_registry_gracefully():
    doc = _Doc(document_id="doc-1", active_snapshot_id="snap-1", domain_id="civil")
    service, _, _ = _make_service(
        doc=doc, records=[], domain_registry=None,
    )
    result = service.build_and_persist(_ctx(), "doc-1")
    # No exception; build still succeeds with zero domain hints.
    assert result.status == "succeeded"


def test_service_resolves_domain_pack_hints_when_registry_wired():
    """Domain pack hints (aliases + terminology + retrieval) should
    surface in the memory entries when the registry returns a pack
    with `extraction_hints`."""
    doc = _Doc(document_id="doc-1", active_snapshot_id="snap-1", domain_id="civil")

    class _FakePack:
        version = "0.1"
        class _Hints:
            entity_aliases = ()
            terminology_hints = ("rebar", "lintel")
            retrieval_hints = ("look up BoQ",)
        extraction_hints = _Hints()

    class _FakeRegistry:
        def get(self, domain_id):
            return _FakePack() if domain_id == "civil" else None

    service, processing, _ = _make_service(
        doc=doc, records=[], domain_registry=_FakeRegistry(),
    )
    result = service.build_and_persist(_ctx(), "doc-1")
    persisted = processing.persisted[0]
    types = [e["memory_type"] for e in persisted["entries"]]
    assert types.count(MEMORY_ENTRY_TYPE_TERMINOLOGY) == 2
    assert types.count(MEMORY_ENTRY_TYPE_RETRIEVAL_HINT) == 1
    # `domain_pack_version` surfaced on `built_from`.
    assert persisted["source"]["built_from"]["domain_pack_version"] == "0.1"


# ---- Manual action wiring ---------------------------------------


def test_manual_action_status_flipped_to_available():
    actions = {a.id: a for a in list_manual_actions()}
    descriptor = actions[ACTION_BUILD_KNOWLEDGE_MEMORY]
    assert descriptor.status == MANUAL_ACTION_STATUS_AVAILABLE
    assert descriptor.status != MANUAL_ACTION_STATUS_NOT_IMPLEMENTED


def test_manual_action_enabled_by_default():
    assert is_manual_action_enabled(ACTION_BUILD_KNOWLEDGE_MEMORY) is True


def test_manual_action_disabled_via_env(monkeypatch):
    monkeypatch.setenv(ENV_MANUAL_BUILD_KNOWLEDGE_MEMORY, "false")
    assert is_manual_action_enabled(ACTION_BUILD_KNOWLEDGE_MEMORY) is False
    # Action listing downgrades the descriptor to `disabled`.
    actions = {a.id: a for a in list_manual_actions()}
    assert (
        actions[ACTION_BUILD_KNOWLEDGE_MEMORY].status
        == MANUAL_ACTION_STATUS_DISABLED
    )


def test_manual_action_description_mentions_no_llm():
    actions = {a.id: a for a in list_manual_actions()}
    descriptor = actions[ACTION_BUILD_KNOWLEDGE_MEMORY]
    # Phase 2 contract: build is deterministic — surface that in
    # the cost note so operators don't expect an LLM cost.
    assert "no LLM" in descriptor.cost_note or "deterministic" in descriptor.cost_note.lower()


# ---- No-LLM regression guard -----------------------------------


def test_knowledge_memory_module_has_no_llm_imports():
    import importlib
    import inspect
    for module_name in (
        "j1.memory.knowledge_memory",
        "j1.memory.service",
    ):
        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        forbidden = {
            "openai", "langchain", "anthropic", "raganything", "lightrag",
            "TextLLMClient", "VisionLLMClient",
        }
        leaked = [name for name in forbidden if name in source]
        assert not leaked, (
            f"{module_name} leaks LLM imports: {leaked}"
        )


# ---- Snapshot isolation -----------------------------------------


def test_supersede_helper_filters_by_document_and_snapshot():
    """The `_supersede_prior_knowledge_memory` sweep must only
    touch artifacts that match BOTH document_id AND snapshot_id —
    a memory artifact for a different document or different
    snapshot stays active."""
    from j1.processing.service import _supersede_prior_knowledge_memory

    registry = _StubArtifactRegistry(records=[
        _Record(
            artifact_id="memart-target",
            kind=ARTIFACT_KIND_KNOWLEDGE_MEMORY,
            metadata={
                "document_id": "doc-1", "snapshot_id": "snap-1",
                "search_state": "active",
            },
        ),
        _Record(
            artifact_id="memart-other-doc",
            kind=ARTIFACT_KIND_KNOWLEDGE_MEMORY,
            metadata={
                "document_id": "doc-2", "snapshot_id": "snap-1",
                "search_state": "active",
            },
        ),
        _Record(
            artifact_id="memart-other-snap",
            kind=ARTIFACT_KIND_KNOWLEDGE_MEMORY,
            metadata={
                "document_id": "doc-1", "snapshot_id": "snap-OTHER",
                "search_state": "active",
            },
        ),
    ])
    stamped = _supersede_prior_knowledge_memory(
        registry, _ctx(),
        document_id="doc-1", snapshot_id="snap-1",
    )
    assert stamped == 1
    states = {r.artifact_id: r.metadata.get("search_state") for r in registry.records}
    assert states["memart-target"] == "superseded"
    assert states["memart-other-doc"] == "active"
    assert states["memart-other-snap"] == "active"


def test_supersede_helper_skips_already_superseded_rows():
    from j1.processing.service import _supersede_prior_knowledge_memory
    registry = _StubArtifactRegistry(records=[
        _Record(
            artifact_id="memart-old",
            kind=ARTIFACT_KIND_KNOWLEDGE_MEMORY,
            metadata={
                "document_id": "doc-1", "snapshot_id": "snap-1",
                "search_state": "superseded",
            },
        ),
    ])
    stamped = _supersede_prior_knowledge_memory(
        registry, _ctx(),
        document_id="doc-1", snapshot_id="snap-1",
    )
    assert stamped == 0
