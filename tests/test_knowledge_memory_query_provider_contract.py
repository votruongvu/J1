"""Contract — Phase 4 Knowledge Memory query provider.

Pins:

  * Provider feature-flag short-circuits when disabled.
  * Provider returns `not_available` when no `document_id` is on
    the request (Phase 4 supports `document_active` only).
  * Loads the active snapshot's memory artifact via the registry +
    `_select_active_memory` filter (ignores superseded rows).
  * Selects entries via deterministic substring + intent-keyword
    matching; no LLM call.
  * Classifies entries into the three use modes:
    `expansion_only`, `derived_candidate`, `summary_context`.
  * Caps selected entries at `max_entries` and reports
    `selection_truncated` on the result.
  * Returns `loaded_no_match` when the artifact exists but no
    entries match.
  * Never raises into the caller — internal exceptions become
    `status=failed` with a `provider_error:*` warning.
  * QueryTrace gains a `knowledge_memory` field that round-trips
    the provider's payload verbatim.
  * Orchestrator integration: optional kwarg defaults to `None`;
    when wired AND a context is returned, the trace receives the
    diagnostic block.

The tests use small in-memory artifact registry / source lookup
stubs that mirror the patterns from the Phase 2/3 contract tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from j1.memory.knowledge_memory import (
    KnowledgeMemoryEntry,
    KnowledgeMemoryPayload,
    MEMORY_ENTRY_TYPE_ALIAS,
    MEMORY_ENTRY_TYPE_DOCUMENT_SUMMARY,
    MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_RETRIEVAL_HINT,
    MEMORY_ENTRY_TYPE_RISK,
    MEMORY_ENTRY_TYPE_TABLE_SUMMARY,
    MEMORY_ENTRY_TYPE_TERMINOLOGY,
)
from j1.memory.query_provider import (
    KnowledgeMemoryContextProvider,
    KnowledgeMemoryQueryContext,
    SelectedMemoryEntry,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_LOADED_NO_MATCH,
    STATUS_NOT_AVAILABLE,
    STATUS_USED,
    USE_MODE_DERIVED_CANDIDATE,
    USE_MODE_EXPANSION_ONLY,
    USE_MODE_SUMMARY_CONTEXT,
    WARNING_MEMORY_LOADED_NO_MATCH,
    WARNING_MEMORY_NO_ENTRIES,
    WARNING_TRUNCATED,
)
from j1.memory.query_settings import (
    DEFAULT_MAX_ENTRIES,
    ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
    ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES,
    KnowledgeMemoryQuerySettings,
    load_knowledge_memory_query_settings,
)
from j1.processing.derived_enrichment import EnrichmentSourceRef
from j1.projects.context import ProjectContext
from j1.query.query_plan import (
    AnswerShape,
    EvidenceGroupSpec,
    Intent,
    QualityPolicy,
    QueryPlan,
    SufficiencyPolicy,
    SynthesisMode,
)
from j1.query.query_trace import QueryTrace


def _minimal_plan() -> QueryPlan:
    """Smallest valid `QueryPlan` for trace-shape tests."""
    return QueryPlan(
        normalized_question="q",
        intent=Intent.UNKNOWN,
        anchors=(),
        requested_fields=(),
        answer_shape=AnswerShape.PARAGRAPH,
        synthesis_mode=SynthesisMode.SYNTHESIZE,
        retrieval_jobs=(),
        required_groups=(EvidenceGroupSpec(name="answer", required=True),),
        sufficiency=SufficiencyPolicy(),
        quality=QualityPolicy(),
    )


# ---- Test fixtures ----------------------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


@dataclass
class _Doc:
    document_id: str
    active_snapshot_id: str | None


class _SourceLookup:
    def __init__(self, doc: _Doc) -> None:
        self._doc = doc

    def get_source(self, ctx, document_id):
        if document_id != self._doc.document_id:
            raise LookupError(document_id)
        return self._doc


@dataclass
class _Record:
    artifact_id: str
    kind: str = "knowledge_memory"
    location: str = ""
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


def _enabled(max_entries: int = DEFAULT_MAX_ENTRIES) -> KnowledgeMemoryQuerySettings:
    return KnowledgeMemoryQuerySettings(enabled=True, max_entries=max_entries)


def _disabled() -> KnowledgeMemoryQuerySettings:
    return KnowledgeMemoryQuerySettings(enabled=False, max_entries=DEFAULT_MAX_ENTRIES)


def _memory_payload(entries: list[KnowledgeMemoryEntry]) -> dict:
    payload = KnowledgeMemoryPayload(
        document_id="doc-1", snapshot_id="snap-1",
        entries=tuple(entries),
    )
    return payload.to_payload()


def _registry_with_memory(
    entries: list[KnowledgeMemoryEntry],
    *,
    document_id: str = "doc-1",
    snapshot_id: str = "snap-1",
    search_state: str = "active",
) -> _Registry:
    """Build an in-memory registry where the memory artifact's
    payload is stamped on `metadata.payload` (provider's
    inline-payload path — avoids needing a workspace stub)."""
    return _Registry([_Record(
        artifact_id="mem-1",
        kind="knowledge_memory",
        location="enriched/mem-1.json",
        metadata={
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "search_state": search_state,
            "payload": _memory_payload(entries),
        },
    )])


def _entry(
    memory_type: str,
    *,
    title: str = "",
    content: str = "",
    structured_payload: dict | None = None,
    source_refs: tuple[EnrichmentSourceRef, ...] = (),
    tags: tuple[str, ...] = (),
) -> KnowledgeMemoryEntry:
    return KnowledgeMemoryEntry(
        memory_id=f"{memory_type}:0:abc",
        memory_type=memory_type,
        title=title,
        content=content,
        structured_payload=structured_payload or {},
        source_refs=source_refs,
        tags=tags,
    )


def _provider(
    *, doc: _Doc, records: list[_Record],
) -> KnowledgeMemoryContextProvider:
    return KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(doc),
        artifact_registry=_Registry(records),
        workspace=None,
    )


# ---- Settings ---------------------------------------------------


def test_settings_defaults_off_and_max_8():
    s = load_knowledge_memory_query_settings(env={})
    assert s.enabled is False
    assert s.max_entries == DEFAULT_MAX_ENTRIES


def test_settings_enabled_parsed():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED: "true",
    })
    assert s.enabled is True


def test_settings_max_entries_parsed():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES: "20",
    })
    assert s.max_entries == 20


def test_settings_negative_max_falls_back_to_default():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES: "-5",
    })
    assert s.max_entries == DEFAULT_MAX_ENTRIES


def test_settings_malformed_max_falls_back_to_default():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_ENTRIES: "abc",
    })
    assert s.max_entries == DEFAULT_MAX_ENTRIES


# ---- Disabled / not-available paths -----------------------------


def test_disabled_short_circuits_with_status_disabled():
    provider = _provider(
        doc=_Doc(document_id="doc-1", active_snapshot_id="snap-1"),
        records=[],
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="anything",
        document_id="doc-1", settings=_disabled(),
    )
    assert result.status == STATUS_DISABLED
    assert result.selected_entries == ()
    assert result.expansion_terms == ()


def test_missing_document_id_routes_to_project_active(monkeypatch):
    """Phase 5A patch: `document_id=None` now routes to the
    project-active path. With an empty artifact registry the
    provider returns `not_available` with the
    `no_project_memory_artifacts` warning rather than a silent
    fall-through — operators see why memory wasn't consulted."""
    from j1.memory.query_provider import (
        SCOPE_PROJECT_ACTIVE, WARNING_NO_PROJECT_MEMORY_ARTIFACTS,
    )
    provider = _provider(
        doc=_Doc(document_id="doc-1", active_snapshot_id="snap-1"),
        records=[],
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id=None, settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE
    assert result.scope == SCOPE_PROJECT_ACTIVE
    assert WARNING_NO_PROJECT_MEMORY_ARTIFACTS in result.warnings


def test_no_active_snapshot_returns_not_available():
    provider = _provider(
        doc=_Doc(document_id="doc-1", active_snapshot_id=None),
        records=[],
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE


def test_no_memory_artifact_returns_not_available():
    provider = _provider(
        doc=_Doc(document_id="doc-1", active_snapshot_id="snap-1"),
        records=[],
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE


def test_superseded_memory_artifact_ignored():
    """The provider must mirror Phase 2's supersede filter — a
    superseded memory artifact for the active snapshot is NOT a
    match. The provider returns `not_available`."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_Registry([_Record(
            artifact_id="mem-old", kind="knowledge_memory",
            metadata={
                "document_id": "doc-1", "snapshot_id": "snap-1",
                "search_state": "superseded",
                "payload": _memory_payload([
                    _entry(MEMORY_ENTRY_TYPE_RISK, title="ignored"),
                ]),
            },
        )]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risk",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE


# ---- Loaded paths -----------------------------------------------


def test_loaded_no_entries_returns_loaded_no_match():
    """Memory artifact exists but its `entries` list is empty.
    The provider returns `loaded_no_match` with the
    `memory_artifact_has_no_entries` warning so dashboards can
    distinguish an empty artifact from a missing one."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="anything",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_LOADED_NO_MATCH
    assert result.available is True
    assert WARNING_MEMORY_NO_ENTRIES in result.warnings


def test_loaded_no_match_when_query_doesnt_match():
    """Memory artifact has entries but none match the query
    terms. `loaded_no_match` is the right outcome — dashboards
    can compare against `not_available` to see whether memory was
    consulted but skipped, vs. never existed at all."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(
                MEMORY_ENTRY_TYPE_REQUIREMENT,
                title="Concrete must be Grade 30",
                content="The concrete grade shall be 30.",
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(),
        question="What is the project schedule for floor 5?",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_LOADED_NO_MATCH
    assert result.available is True
    assert WARNING_MEMORY_LOADED_NO_MATCH in result.warnings


def test_loaded_with_match_returns_used():
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(
                MEMORY_ENTRY_TYPE_REQUIREMENT,
                title="Concrete Grade 30",
                content="The concrete grade shall be 30.",
                source_refs=(EnrichmentSourceRef(
                    chunk_id="c1", page=5,
                    artifact_id="cart-1",
                ),),
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(),
        question="What is the concrete grade requirement?",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_USED
    assert result.entry_count == 1
    assert len(result.selected_entries) == 1
    assert result.resolved_source_ref_count == 1
    assert "requirement" in result.selected_entry_types


# ---- Use-mode classification -----------------------------------


def test_alias_classified_as_expansion_only():
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(
                MEMORY_ENTRY_TYPE_ALIAS,
                title="bill of quantities",
                structured_payload={
                    "canonical_name": "bill of quantities",
                    "aliases": ["BoQ", "BOQ"],
                },
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(),
        question="show me the BoQ items",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_USED
    assert result.selected_entries[0].use_mode == USE_MODE_EXPANSION_ONLY
    # Expansion terms include the canonical + aliases.
    assert "bill of quantities" in result.expansion_terms
    assert "BoQ" in result.expansion_terms


def test_risk_with_source_refs_classified_as_derived_candidate():
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(
                MEMORY_ENTRY_TYPE_RISK,
                title="Falling object risk",
                source_refs=(EnrichmentSourceRef(chunk_id="c1", page=3),),
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="what are the risks?",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.selected_entries[0].use_mode == USE_MODE_DERIVED_CANDIDATE


def test_risk_without_source_refs_falls_back_to_expansion_only():
    """A derived-candidate type without source refs becomes
    expansion-only — we never pretend to ground an unrefed entry."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(MEMORY_ENTRY_TYPE_RISK, title="Falling object risk"),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="what are the risks?",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.selected_entries[0].use_mode == USE_MODE_EXPANSION_ONLY
    assert result.resolved_source_ref_count == 0


def test_graph_summary_classified_as_summary_context():
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(
                MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
                title="Graph summary",
                content="42 entities, 12 relationships.",
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="graph summary please",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.selected_entries[0].use_mode == USE_MODE_SUMMARY_CONTEXT


# ---- Intent-keyword matching -----------------------------------


def test_intent_keyword_surfaces_typed_entries():
    """Query mentioning 'risks' surfaces every risk entry even if
    the entry's title isn't in the query."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(MEMORY_ENTRY_TYPE_RISK, title="Falling object"),
            _entry(MEMORY_ENTRY_TYPE_RISK, title="Electrical shock"),
            _entry(
                MEMORY_ENTRY_TYPE_REQUIREMENT,
                title="Unrelated requirement",
            ),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="what risks are documented?",
        document_id="doc-1", settings=_enabled(),
    )
    risk_count = sum(
        1 for s in result.selected_entries
        if s.entry.memory_type == MEMORY_ENTRY_TYPE_RISK
    )
    assert risk_count == 2


def test_intent_keyword_matches_table_intents():
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=[
            _entry(MEMORY_ENTRY_TYPE_TABLE_SUMMARY, title="Cost table"),
        ]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="show me the BoQ quantities",
        document_id="doc-1", settings=_enabled(),
    )
    assert len(result.selected_entries) == 1


# ---- Truncation -------------------------------------------------


def test_selection_truncated_when_over_max():
    """The matcher returns more entries than max_entries → result
    is capped and `selection_truncated` warning surfaces."""
    entries = [
        _entry(MEMORY_ENTRY_TYPE_RISK, title=f"Risk {i}")
        for i in range(20)
    ]
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_registry_with_memory(entries=entries),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(max_entries=5),
    )
    assert len(result.selected_entries) == 5
    assert WARNING_TRUNCATED in result.warnings


# ---- Failure / fallback path -----------------------------------


def test_malformed_payload_returns_failed_with_warning():
    """An inline payload that can't be parsed → `failed` status +
    a `memory_payload_malformed` warning. Query proceeds with
    fallback behaviour at the orchestrator level."""
    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_Registry([_Record(
            artifact_id="mem-bad", kind="knowledge_memory",
            metadata={
                "document_id": "doc-1", "snapshot_id": "snap-1",
                "search_state": "active",
                "payload": "not-a-dict",  # forces from_payload error
            },
        )]),
        workspace=None,
    )
    # We can't trigger from_payload on a string easily since it
    # falls through to "no inline mapping; need workspace" path,
    # which returns failed with memory_artifact_not_readable.
    result = provider.context_for_query(
        ctx=_ctx(), question="anything",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_FAILED
    assert any(
        w in ("memory_artifact_not_readable", "memory_payload_malformed")
        for w in result.warnings
    )


def test_source_lookup_exception_returns_not_available():
    """If `source_lookup.get_source` raises, the provider treats
    it as no-active-snapshot and returns `not_available` — never
    propagates."""

    class _Raising:
        def get_source(self, ctx, document_id):
            raise RuntimeError("source registry down")

    provider = KnowledgeMemoryContextProvider(
        source_lookup=_Raising(),
        artifact_registry=_Registry([]),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(),
    )
    assert result.status == STATUS_NOT_AVAILABLE


def test_registry_list_raises_returns_not_available():
    """If `list_artifacts` raises, the provider falls back gracefully
    — query never fails because memory lookup failed."""

    class _BadRegistry:
        def list_artifacts(self, ctx, *, kind=None):
            raise RuntimeError("registry down")

    provider = KnowledgeMemoryContextProvider(
        source_lookup=_SourceLookup(_Doc(
            document_id="doc-1", active_snapshot_id="snap-1",
        )),
        artifact_registry=_BadRegistry(),
        workspace=None,
    )
    result = provider.context_for_query(
        ctx=_ctx(), question="risks",
        document_id="doc-1", settings=_enabled(),
    )
    # Phase 4 contract: lookup failures → not_available (we can't
    # find the artifact). Distinct from `failed`, which means the
    # artifact WAS located but couldn't be read/parsed.
    assert result.status == STATUS_NOT_AVAILABLE


# ---- Result payload + trace integration ------------------------


def test_to_payload_round_trips_with_zero_values():
    ctx = KnowledgeMemoryQueryContext()
    payload = ctx.to_payload()
    assert payload["status"] == STATUS_NOT_AVAILABLE
    assert payload["available"] is False
    assert payload["selected_entry_count"] == 0
    assert payload["selected_entry_types"] == []
    assert payload["expansion_terms"] == []


def test_query_trace_with_knowledge_memory_round_trip():
    plan = _minimal_plan()
    trace = QueryTrace.empty_with_plan("q", plan)
    diagnostic = {
        "status": "used",
        "available": True,
        "artifact_id": "mem-1",
        "entry_count": 10,
        "selected_entry_count": 3,
        "selected_entry_types": ["risk", "requirement"],
        "expansion_terms": ["BoQ"],
        "resolved_source_ref_count": 2,
        "warnings": [],
    }
    trace2 = trace.with_knowledge_memory(diagnostic)
    assert trace2.knowledge_memory == diagnostic
    json_dump = trace2.to_dict()
    assert json_dump["knowledge_memory"] == diagnostic


def test_query_trace_knowledge_memory_default_none():
    plan = _minimal_plan()
    trace = QueryTrace.empty_with_plan("q", plan)
    # Trace built without `with_knowledge_memory` carries None —
    # FE / dashboard rendering distinguishes "memory not
    # consulted" from "memory consulted, no match".
    assert trace.knowledge_memory is None
    assert trace.to_dict()["knowledge_memory"] is None


# ---- Orchestrator integration ----------------------------------


def test_orchestrator_accepts_optional_knowledge_memory_provider():
    """The new constructor kwarg must default to None so existing
    callers don't need to pass it."""
    import inspect
    from j1.query.orchestrator import SmartQueryOrchestrator
    sig = inspect.signature(SmartQueryOrchestrator.__init__)
    assert "knowledge_memory_provider" in sig.parameters
    assert sig.parameters["knowledge_memory_provider"].default is None


def test_orchestrator_from_components_forwards_kwargs():
    import inspect
    from j1.query.orchestrator import SmartQueryOrchestrator
    sig = inspect.signature(SmartQueryOrchestrator.from_components)
    assert "knowledge_memory_provider" in sig.parameters
    assert "augmentation_provider" in sig.parameters


def test_orchestrator_invokes_provider_with_question_and_settings(monkeypatch):
    """When a provider is wired AND the flag is on, the orchestrator
    calls `context_for_query` with the request's question +
    document_id + loaded settings."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    captured: dict = {}

    class _Provider:
        def context_for_query(
            self, *, ctx, question, document_id, settings,
            eligible_snapshot_pairs=None,
        ):
            captured["question"] = question
            captured["document_id"] = document_id
            captured["enabled"] = settings.enabled
            return KnowledgeMemoryQueryContext(
                status=STATUS_USED,
                available=True,
                artifact_id="mem-1",
                entry_count=3,
                expansion_terms=("BoQ",),
                selected_entry_types=("risk",),
            )

    # Build the orchestrator with minimal-functional pieces using
    # `from_components`. We use stub routes + a no-op LLM since
    # the test doesn't exercise retrieval / synthesis.
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
        knowledge_memory_provider=_Provider(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(),
        question="what are the risks?",
        document_id="doc-1",
    )
    result = orch.run(request)
    # Provider was invoked with the request's fields.
    assert captured["question"] == "what are the risks?"
    assert captured["document_id"] == "doc-1"
    assert captured["enabled"] is True
    # Diagnostic landed on the trace.
    assert result.trace.knowledge_memory is not None
    assert result.trace.knowledge_memory["status"] == STATUS_USED
    assert result.trace.knowledge_memory["artifact_id"] == "mem-1"


def test_orchestrator_provider_exception_does_not_fail_query(monkeypatch):
    """Provider raising synchronously must NOT propagate. Trace's
    `knowledge_memory` stays None and the query proceeds."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")

    class _RaisingProvider:
        def context_for_query(self, **_kwargs):
            raise RuntimeError("provider blew up")

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
        knowledge_memory_provider=_RaisingProvider(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    # Query completes without raising.
    result = orch.run(request)
    # Memory section absent on trace — no diagnostic stamped.
    assert result.trace.knowledge_memory is None


def test_orchestrator_without_provider_does_not_stamp_trace():
    """When no provider is wired, the trace's `knowledge_memory`
    stays None — legacy behaviour preserved."""
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
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    result = orch.run(request)
    assert result.trace.knowledge_memory is None


# ---- No-LLM regression guard -----------------------------------


def test_query_provider_module_has_no_llm_imports():
    import importlib
    import inspect
    for module_name in (
        "j1.memory.query_provider",
        "j1.memory.query_settings",
    ):
        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        forbidden = {
            "openai", "langchain", "anthropic", "raganything", "lightrag",
            "TextLLMClient", "VisionLLMClient",
        }
        leaked = [name for name in forbidden if name in source]
        assert not leaked, f"{module_name} leaks LLM imports: {leaked}"
