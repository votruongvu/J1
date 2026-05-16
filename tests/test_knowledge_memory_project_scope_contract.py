"""Contract — Phase 5A patch: project-active scope for the
Knowledge Memory query provider.

Pins:

  * `document_active` scope still works (Phase 4 + Phase 5A
    behavior preserved).
  * `project_active` scope (document_id=None) walks the registry
    for active `knowledge_memory` artifacts.
  * `eligible_snapshot_pairs` filter narrows the project walk.
  * Superseded artifacts ignored.
  * Per-(document, snapshot) lineage check skips records missing
    metadata.
  * Caps: `max_project_documents` + `max_project_artifacts`
    bound the walk; cap warnings surface.
  * Partial-coverage warning when some eligibility pairs lack
    memory.
  * Selected entries preserve `document_id` / `snapshot_id` /
    `artifact_id` lineage in project mode.
  * No project memory → `status=not_available` with
    `no_project_memory_artifacts` warning; query proceeds.
  * Orchestrator passes `eligible_snapshot_pairs` to the provider.
  * Diagnostic block carries `scope`, `project_id`,
    `document_count`, `memory_artifact_count`.
  * No new LLM imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.memory.knowledge_memory import (
    KnowledgeMemoryEntry,
    KnowledgeMemoryPayload,
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_RISK,
)
from j1.memory.query_provider import (
    KnowledgeMemoryContextProvider,
    KnowledgeMemoryQueryContext,
    SCOPE_DOCUMENT_ACTIVE,
    SCOPE_PROJECT_ACTIVE,
    STATUS_LOADED_NO_MATCH,
    STATUS_NOT_AVAILABLE,
    STATUS_USED,
    SelectedMemoryEntry,
    WARNING_NO_PROJECT_MEMORY_ARTIFACTS,
    WARNING_PROJECT_MEMORY_ARTIFACT_CAP_APPLIED,
    WARNING_PROJECT_MEMORY_DOCUMENT_CAP_APPLIED,
    WARNING_PROJECT_MEMORY_PARTIAL,
)
from j1.memory.query_settings import (
    DEFAULT_MAX_PROJECT_ARTIFACTS,
    DEFAULT_MAX_PROJECT_DOCUMENTS,
    ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
    ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS,
    ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS,
    KnowledgeMemoryQuerySettings,
    load_knowledge_memory_query_settings,
)
from j1.processing.derived_enrichment import EnrichmentSourceRef
from j1.projects.context import ProjectContext


# ---- Fixtures ---------------------------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


@dataclass
class _Record:
    artifact_id: str
    kind: str = "knowledge_memory"
    metadata: dict = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class _Registry:
    def __init__(self, records: list[_Record]) -> None:
        self.records = records

    def list_artifacts(self, ctx, *, kind: str | None = None):
        if kind is None:
            return list(self.records)
        return [r for r in self.records if r.kind == kind]


def _entry(
    memory_type: str = MEMORY_ENTRY_TYPE_RISK,
    *,
    title: str = "",
    content: str = "",
    source_refs: tuple[EnrichmentSourceRef, ...] = (),
) -> KnowledgeMemoryEntry:
    return KnowledgeMemoryEntry(
        memory_id=f"{memory_type}:0:abc",
        memory_type=memory_type,
        title=title,
        content=content,
        source_refs=source_refs,
    )


def _memory_record(
    artifact_id: str,
    *,
    document_id: str,
    snapshot_id: str,
    entries: list[KnowledgeMemoryEntry],
    search_state: str = "active",
) -> _Record:
    return _Record(
        artifact_id=artifact_id,
        kind="knowledge_memory",
        metadata={
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "search_state": search_state,
            "payload": KnowledgeMemoryPayload(
                document_id=document_id, snapshot_id=snapshot_id,
                entries=tuple(entries),
            ).to_payload(),
        },
    )


def _provider(records: list[_Record]) -> KnowledgeMemoryContextProvider:
    class _SourceLookup:
        def get_source(self, ctx, document_id):
            raise LookupError("project mode doesn't use source_lookup")

    return KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(),
        artifact_registry=_Registry(records),
        workspace=None,
    )


def _enabled(**over) -> KnowledgeMemoryQuerySettings:
    return KnowledgeMemoryQuerySettings(enabled=True, **over)


# ---- Settings: new project caps --------------------------------


def test_settings_default_project_caps():
    s = load_knowledge_memory_query_settings(env={})
    assert s.max_project_documents == DEFAULT_MAX_PROJECT_DOCUMENTS == 20
    assert s.max_project_artifacts == DEFAULT_MAX_PROJECT_ARTIFACTS == 20


def test_settings_project_documents_env_parsed():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS: "50",
    })
    assert s.max_project_documents == 50


def test_settings_project_artifacts_env_parsed():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS: "30",
    })
    assert s.max_project_artifacts == 30


def test_settings_invalid_project_caps_fall_back_to_default():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_DOCUMENTS: "-5",
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_PROJECT_ARTIFACTS: "abc",
    })
    assert s.max_project_documents == DEFAULT_MAX_PROJECT_DOCUMENTS
    assert s.max_project_artifacts == DEFAULT_MAX_PROJECT_ARTIFACTS


# ---- Project-active path: not-available cases ------------------


def test_project_no_memory_artifacts_returns_not_available():
    provider = _provider(records=[])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE
    assert result.scope == SCOPE_PROJECT_ACTIVE
    assert WARNING_NO_PROJECT_MEMORY_ARTIFACTS in result.warnings


def test_project_only_superseded_artifacts_returns_not_available():
    provider = _provider(records=[
        _memory_record(
            "mem-old-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="Falling object")],
            search_state="superseded",
        ),
        _memory_record(
            "mem-old-2", document_id="doc-2", snapshot_id="snap-2",
            entries=[_entry(title="Electrical risk")],
            search_state="superseded",
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE
    assert result.scope == SCOPE_PROJECT_ACTIVE


def test_project_disabled_short_circuits():
    provider = _provider(records=[
        _memory_record("mem-1", document_id="doc-1", snapshot_id="snap-1",
                       entries=[_entry(title="x")]),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="anything",
        document_id=None,
        settings=KnowledgeMemoryQuerySettings(enabled=False),
    )
    assert result.status == "disabled"


# ---- Project-active path: matching --------------------------


def test_project_single_artifact_matches():
    provider = _provider(records=[
        _memory_record(
            "mem-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[
                _entry(title="Falling object risk"),
                _entry(title="Unrelated requirement",
                       memory_type=MEMORY_ENTRY_TYPE_REQUIREMENT),
            ],
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="What risks are documented?",
        document_id=None, settings=_enabled(),
    )
    assert result.status == STATUS_USED
    assert result.scope == SCOPE_PROJECT_ACTIVE
    assert result.project_id == "p1"
    assert result.document_count == 1
    assert result.memory_artifact_count == 1
    # Both entries surface — the risk via intent-keyword,
    # the requirement is skipped (no match).
    selected_types = {s.entry.memory_type for s in result.selected_entries}
    assert MEMORY_ENTRY_TYPE_RISK in selected_types


def test_project_multiple_artifacts_aggregate_across_documents():
    provider = _provider(records=[
        _memory_record(
            "mem-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="NCR-001 spotted")],
        ),
        _memory_record(
            "mem-2", document_id="doc-2", snapshot_id="snap-2",
            entries=[_entry(title="NCR-002 follow-up")],
        ),
        _memory_record(
            "mem-3", document_id="doc-3", snapshot_id="snap-3",
            entries=[_entry(title="Concrete grade",
                            memory_type=MEMORY_ENTRY_TYPE_REQUIREMENT)],
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="NCR issues",
        document_id=None, settings=_enabled(),
    )
    assert result.status == STATUS_USED
    assert result.document_count == 3
    assert result.memory_artifact_count == 3
    selected_doc_ids = {s.document_id for s in result.selected_entries}
    # Risk entries from doc-1 and doc-2 match (intent keyword
    # + title); doc-3 requirement doesn't match this query.
    assert "doc-1" in selected_doc_ids
    assert "doc-2" in selected_doc_ids


def test_project_selected_entries_carry_lineage():
    provider = _provider(records=[
        _memory_record(
            "mem-A", document_id="doc-A", snapshot_id="snap-A",
            entries=[_entry(title="Risk one")],
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks please",
        document_id=None, settings=_enabled(),
    )
    selected = result.selected_entries[0]
    assert selected.document_id == "doc-A"
    assert selected.snapshot_id == "snap-A"
    assert selected.artifact_id == "mem-A"


def test_project_ignores_superseded_alongside_active():
    """Active + superseded for the same project: only active
    counts."""
    provider = _provider(records=[
        _memory_record(
            "mem-active", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="active risk")],
        ),
        _memory_record(
            "mem-stale", document_id="doc-2", snapshot_id="snap-OLD",
            entries=[_entry(title="stale risk")],
            search_state="superseded",
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    assert result.memory_artifact_count == 1
    assert all(
        s.document_id == "doc-1" for s in result.selected_entries
    )


def test_project_skips_records_missing_lineage_metadata():
    """A memory artifact without `document_id` / `snapshot_id` in
    its metadata can't be safely matched. The provider skips it
    rather than risking cross-document leakage."""
    provider = _provider(records=[
        _memory_record(
            "mem-ok", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="risk one")],
        ),
        _Record(
            artifact_id="mem-bad",
            kind="knowledge_memory",
            metadata={
                # Missing document_id + snapshot_id.
                "search_state": "active",
                "payload": KnowledgeMemoryPayload(
                    entries=(_entry(title="orphan risk"),),
                ).to_payload(),
            },
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    # Only the well-formed record contributed.
    assert result.memory_artifact_count == 1


# ---- Eligibility filter --------------------------------------


def test_project_filters_by_eligible_snapshot_pairs():
    """When the caller supplies an allowlist of (doc, snap)
    pairs, the provider only walks artifacts matching one of
    them. Defends the production path where the validation
    service resolves a per-request pair set."""
    provider = _provider(records=[
        _memory_record(
            "mem-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="doc-1 risk")],
        ),
        _memory_record(
            "mem-2", document_id="doc-2", snapshot_id="snap-2",
            entries=[_entry(title="doc-2 risk")],
        ),
        _memory_record(
            "mem-3", document_id="doc-3", snapshot_id="snap-3",
            entries=[_entry(title="doc-3 risk")],
        ),
    ])
    eligible = frozenset({("doc-1", "snap-1"), ("doc-3", "snap-3")})
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None,
        eligible_snapshot_pairs=eligible,
        settings=_enabled(),
    )
    selected_doc_ids = {s.document_id for s in result.selected_entries}
    assert "doc-1" in selected_doc_ids
    assert "doc-3" in selected_doc_ids
    assert "doc-2" not in selected_doc_ids


def test_project_partial_coverage_emits_warning():
    """3 eligible pairs but only 1 has a memory artifact →
    `project_memory_partial` warning fires."""
    provider = _provider(records=[
        _memory_record(
            "mem-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(title="risk one")],
        ),
    ])
    eligible = frozenset({
        ("doc-1", "snap-1"),
        ("doc-2", "snap-2"),
        ("doc-3", "snap-3"),
    })
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None,
        eligible_snapshot_pairs=eligible,
        settings=_enabled(),
    )
    assert WARNING_PROJECT_MEMORY_PARTIAL in result.warnings
    assert result.memory_artifact_count == 1


# ---- Caps -----------------------------------------------------


def test_project_document_cap_truncates_and_warns():
    # 5 artifacts; cap at 2.
    records = [
        _memory_record(
            f"mem-{i}", document_id=f"doc-{i}", snapshot_id=f"snap-{i}",
            entries=[_entry(title=f"risk {i}")],
        )
        for i in range(5)
    ]
    provider = _provider(records=records)
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None,
        settings=_enabled(max_project_documents=2),
    )
    assert WARNING_PROJECT_MEMORY_DOCUMENT_CAP_APPLIED in result.warnings
    assert result.document_count == 2


def test_project_artifact_cap_applies_after_document_cap():
    """The artifact cap is defence-in-depth. With document_cap=5 +
    artifact_cap=2, the artifact cap fires and trims the load to 2."""
    records = [
        _memory_record(
            f"mem-{i}", document_id=f"doc-{i}", snapshot_id=f"snap-{i}",
            entries=[_entry(title=f"risk {i}")],
        )
        for i in range(5)
    ]
    provider = _provider(records=records)
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None,
        settings=_enabled(
            max_project_documents=10, max_project_artifacts=2,
        ),
    )
    assert WARNING_PROJECT_MEMORY_ARTIFACT_CAP_APPLIED in result.warnings
    assert result.memory_artifact_count == 2


# ---- Document-active path still works -------------------------


def test_document_active_path_unchanged():
    """Phase 4 + Phase 5A behaviour must be preserved when
    `document_id` is supplied. Lineage fields populate on the
    diagnostic block (Phase 5A patch addition); the existing
    selection + matching behaviour is unchanged."""

    @dataclass
    class _Doc:
        document_id: str
        active_snapshot_id: str | None

    class _SourceLookup:
        def __init__(self, doc):
            self._doc = doc

        def get_source(self, ctx, document_id):
            return self._doc

    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(
            _Doc(document_id="doc-1", active_snapshot_id="snap-1"),
        ),
        artifact_registry=_Registry([
            _memory_record(
                "mem-1", document_id="doc-1", snapshot_id="snap-1",
                entries=[_entry(title="Falling risk")],
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_USED
    assert result.scope == SCOPE_DOCUMENT_ACTIVE
    assert result.document_count == 1
    assert result.memory_artifact_count == 1


# ---- Source-grounding boundary --------------------------------


def test_project_never_injects_memory_as_evidence():
    """Provider returns selected entries with lineage; the
    orchestrator's evidence pipeline still drives candidates
    via routes. Phase 5A guardrail preserved."""
    provider = _provider(records=[
        _memory_record(
            "mem-1", document_id="doc-1", snapshot_id="snap-1",
            entries=[_entry(
                title="risk",
                source_refs=(EnrichmentSourceRef(
                    chunk_id="c1", page=3, artifact_id="cart-1",
                ),),
            )],
        ),
    ])
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    payload = result.to_payload()
    # Diagnostic surface — no raw entry payloads / content leakage.
    forbidden = {"content", "raw_content", "evidence_text", "body"}
    assert not (set(payload.keys()) & forbidden)


# ---- Orchestrator integration ---------------------------------


def test_orchestrator_passes_eligible_pairs_to_provider(monkeypatch):
    """The orchestrator forwards `request.eligible_snapshot_pairs`
    so the provider's project path can apply the filter."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    captured: dict = {}

    class _RecordingProvider:
        def context_for_query(
            self, *, ctx, question, document_id, settings,
            eligible_snapshot_pairs=None,
        ):
            captured["document_id"] = document_id
            captured["eligible"] = eligible_snapshot_pairs
            return KnowledgeMemoryQueryContext(
                status=STATUS_USED,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id="p1",
            )

    from j1.query.orchestrator import (
        SmartQueryOrchestrator, OrchestratorRequest,
    )
    from j1.query.retrieval_routes import RetrievalRouteKind

    class _Route:
        def execute(self, request, jobs):
            return []

    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.ARTIFACT_LOOKUP: _Route()},
        llm=lambda req: "",
        knowledge_memory_provider=_RecordingProvider(),
    )
    pairs = frozenset({("doc-1", "snap-1"), ("doc-2", "snap-2")})
    request = OrchestratorRequest(
        ctx=_ctx(),
        question="project-wide risks",
        document_id=None,
        eligible_snapshot_pairs=pairs,
    )
    result = orch.run(request)
    assert captured["document_id"] is None
    assert captured["eligible"] == pairs
    # Diagnostic block on the trace surfaces project scope.
    assert result.trace.knowledge_memory is not None
    assert result.trace.knowledge_memory["scope"] == SCOPE_PROJECT_ACTIVE
    assert result.trace.knowledge_memory["project_id"] == "p1"


def test_orchestrator_project_query_falls_back_gracefully(monkeypatch):
    """No memory provider returns `status=not_available` for a
    project query. Query proceeds with existing routes; trace
    records the not-available diagnostic."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    class _EmptyProvider:
        def context_for_query(self, **_kw):
            return KnowledgeMemoryQueryContext(
                status=STATUS_NOT_AVAILABLE,
                scope=SCOPE_PROJECT_ACTIVE,
                project_id="p1",
                warnings=(WARNING_NO_PROJECT_MEMORY_ARTIFACTS,),
            )

    from j1.query.orchestrator import (
        SmartQueryOrchestrator, OrchestratorRequest,
    )
    from j1.query.retrieval_routes import RetrievalRouteKind

    class _Route:
        def execute(self, request, jobs):
            return []

    orch = SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.ARTIFACT_LOOKUP: _Route()},
        llm=lambda req: "",
        knowledge_memory_provider=_EmptyProvider(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="anything", document_id=None,
    )
    result = orch.run(request)
    assert result.trace.knowledge_memory["status"] == STATUS_NOT_AVAILABLE


# ---- No-LLM regression ----------------------------------------


def test_provider_module_still_has_no_llm_imports():
    import importlib
    import inspect
    mod = importlib.import_module("j1.memory.query_provider")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked
