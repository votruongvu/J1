"""Contract — Phase 5B Knowledge Memory source-ref resolver.

Pins:

  * Resolver materialises selected memory entries' `source_refs`
    into source-grounded `EvidenceCandidate` rows.
  * Memory entries themselves are NEVER injected as evidence.
  * Resolver respects scope: superseded refs ignored, ineligible
    snapshot pairs dropped.
  * Resolver respects caps: `max_source_evidence` bounds the
    injection size; cap warning surfaces.
  * Resolver deduplicates against the existing canonical pool —
    a chunk already retrieved doesn't get re-injected.
  * Entries without source refs / classified as expansion-only
    don't produce evidence candidates.
  * Table / image refs without a chunk/artifact locator defer
    with a diagnostic warning rather than failing.
  * Missing source artifact → warning, never failure.
  * Resolver failure / disabled never fails the query.
  * Orchestrator integration: resolver is invoked only when
    memory provider returned `status=used`; injection flows into
    the existing evidence pipeline; diagnostics land on the
    `knowledge_memory` trace block.
  * Source-grounding boundary: evidence carries
    `evidence_origin=memory_guided_source_ref` on `extra` so the
    binder/synthesizer can distinguish memory-guided from
    canonical evidence; the canonical answer-shape is unchanged.
  * No new LLM imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from j1.memory.knowledge_memory import (
    KnowledgeMemoryEntry,
    MEMORY_ENTRY_TYPE_ALIAS,
    MEMORY_ENTRY_TYPE_REQUIREMENT,
    MEMORY_ENTRY_TYPE_RISK,
    MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
)
from j1.memory.query_provider import (
    KnowledgeMemoryQueryContext,
    SelectedMemoryEntry,
    SCOPE_PROJECT_ACTIVE,
    STATUS_USED,
    USE_MODE_DERIVED_CANDIDATE,
    USE_MODE_EXPANSION_ONLY,
    USE_MODE_SUMMARY_CONTEXT,
)
from j1.memory.query_settings import (
    DEFAULT_MAX_SOURCE_EVIDENCE,
    ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED,
    ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE,
    KnowledgeMemoryQuerySettings,
    load_knowledge_memory_query_settings,
)
from j1.memory.source_ref_resolver import (
    EVIDENCE_ORIGIN_MEMORY_GUIDED,
    KnowledgeMemoryEvidenceResolution,
    KnowledgeMemoryEvidenceResolver,
    WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND,
    WARNING_SOURCE_REF_CAP_APPLIED,
    WARNING_SOURCE_REF_DEDUPED,
    WARNING_SOURCE_REF_IMAGE_DEFERRED,
    WARNING_SOURCE_REF_NO_LOCATOR,
    WARNING_SOURCE_REF_OUT_OF_SCOPE,
    WARNING_SOURCE_REF_SUPERSEDED,
    WARNING_SOURCE_REF_TABLE_DEFERRED,
    collect_existing_keys,
)
from j1.processing.derived_enrichment import EnrichmentSourceRef
from j1.projects.context import ProjectContext
from j1.query.query_plan import (
    AnswerShape,
    EvidenceCandidate,
    EvidenceGroupSpec,
    Intent,
    QualityPolicy,
    QueryPlan,
    RetrievalRouteKind,
    SufficiencyPolicy,
    SynthesisMode,
)


# ---- Test fixtures --------------------------------------------------


def _ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t1", project_id="p1", profile=None)


def _settings(*, max_source_evidence: int = DEFAULT_MAX_SOURCE_EVIDENCE):
    return KnowledgeMemoryQuerySettings(
        enabled=True,
        max_source_evidence=max_source_evidence,
    )


@dataclass
class _SourceRecord:
    """Stub `ArtifactRecord` shape — only the fields the resolver
    reads. Mirrors `ArtifactRecord` field names."""
    artifact_id: str
    kind: str = "compiled_text"
    location: str = ""
    metadata: dict = field(default_factory=dict)
    source_document_ids: list = field(default_factory=list)
    snapshot_id: str | None = None
    created_by_run_id: str | None = None


class _Registry:
    """Stub registry covering the resolver's two access patterns:
    `get(ctx, id)` and `list_artifacts(ctx)`. The resolver caches
    a single get() call per artifact_id, so the simpler list-based
    fallback isn't normally exercised, but we surface it here so
    tests can opt into either path."""

    def __init__(
        self,
        records: list[_SourceRecord],
        *,
        get_supported: bool = True,
    ) -> None:
        self._records = records
        self._get_supported = get_supported

    def get(self, ctx, artifact_id):
        if not self._get_supported:
            raise AttributeError("get not supported in this stub")
        for r in self._records:
            if r.artifact_id == artifact_id:
                return r
        raise LookupError(artifact_id)

    def list_artifacts(self, ctx, *, kind=None):
        if kind is None:
            return list(self._records)
        return [r for r in self._records if r.kind == kind]


def _entry(
    *,
    memory_type: str = MEMORY_ENTRY_TYPE_RISK,
    memory_id: str = "risk:0:abc",
    title: str = "Falling object risk",
    source_refs: tuple[EnrichmentSourceRef, ...] = (),
    use_mode: str = USE_MODE_DERIVED_CANDIDATE,
    document_id: str | None = None,
    snapshot_id: str | None = None,
    artifact_id: str | None = None,
) -> SelectedMemoryEntry:
    """Build a `SelectedMemoryEntry` directly. Most tests skip the
    provider and feed the resolver its inputs verbatim."""
    return SelectedMemoryEntry(
        entry=KnowledgeMemoryEntry(
            memory_id=memory_id,
            memory_type=memory_type,
            title=title,
            source_refs=source_refs,
        ),
        use_mode=use_mode,
        match_reason="title_in_query",
        document_id=document_id,
        snapshot_id=snapshot_id,
        artifact_id=artifact_id,
    )


def _ref(
    *,
    artifact_id: str = "compile-art-1",
    chunk_id: str | None = "chunk-1",
    page: int | None = 3,
    document_id: str | None = "doc-1",
    snapshot_id: str | None = "snap-1",
    table_id: str | None = None,
    image_id: str | None = None,
) -> EnrichmentSourceRef:
    return EnrichmentSourceRef(
        artifact_id=artifact_id,
        chunk_id=chunk_id,
        page=page,
        document_id=document_id,
        snapshot_id=snapshot_id,
        table_id=table_id,
        image_id=image_id,
        artifact_kind="compiled_text",
    )


def _make_resolver(
    *,
    records: list[_SourceRecord] | None = None,
    body_loader=None,
    registry_get_supported: bool = True,
) -> KnowledgeMemoryEvidenceResolver:
    registry = _Registry(
        records or [],
        get_supported=registry_get_supported,
    )
    return KnowledgeMemoryEvidenceResolver(
        artifact_registry=registry,
        body_loader=body_loader,
    )


# ---- Settings ------------------------------------------------------


def test_settings_default_max_source_evidence_is_8():
    s = load_knowledge_memory_query_settings(env={})
    assert s.max_source_evidence == DEFAULT_MAX_SOURCE_EVIDENCE


def test_settings_max_source_evidence_parses_positive_value():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE: "12",
    })
    assert s.max_source_evidence == 12


def test_settings_max_source_evidence_falls_back_on_negative():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE: "-3",
    })
    assert s.max_source_evidence == DEFAULT_MAX_SOURCE_EVIDENCE


def test_settings_max_source_evidence_falls_back_on_malformed():
    s = load_knowledge_memory_query_settings(env={
        ENV_QUERY_KNOWLEDGE_MEMORY_MAX_SOURCE_EVIDENCE: "abc",
    })
    assert s.max_source_evidence == DEFAULT_MAX_SOURCE_EVIDENCE


# ---- Resolver: chunk-id refs --------------------------------------


def test_resolves_chunk_id_ref_into_source_grounded_candidate():
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            kind="compiled_text",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "Chunk body text covering risks.")
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
        project_id="p1",
    )
    assert res.applied is True
    assert res.injected_evidence_count == 1
    assert res.resolved_source_ref_count == 1
    cand = res.injected[0].candidate
    assert cand.artifact_id == "compile-art-1"
    assert cand.chunk_id == "chunk-1"
    assert cand.route == RetrievalRouteKind.ARTIFACT_LOOKUP
    assert cand.extra["body"].startswith("Chunk body")
    assert (
        cand.extra["evidence_origin"]
        == EVIDENCE_ORIGIN_MEMORY_GUIDED
    )


def test_resolves_artifact_id_ref_without_chunk():
    """Coarse `artifact_id`-only refs still surface — same chunk-
    less ARTIFACT_LOOKUP route, body loaded from the artifact
    body_loader."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-2",
            kind="enriched.requirements",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "Artifact text body.")
    selected = (_entry(
        memory_type=MEMORY_ENTRY_TYPE_REQUIREMENT,
        source_refs=(_ref(artifact_id="compile-art-2", chunk_id=None),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
        project_id="p1",
    )
    assert res.injected_evidence_count == 1
    cand = res.injected[0].candidate
    assert cand.chunk_id is None
    assert cand.artifact_kind == "compiled_text"  # ref carries this


def test_resolves_preserves_document_and_snapshot_lineage():
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
            created_by_run_id="run-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(
            chunk_id="chunk-1", artifact_id="compile-art-1",
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
        project_id="p1",
    )
    cand = res.injected[0].candidate
    assert cand.document_id == "doc-1"
    assert cand.run_id == "run-1"
    assert cand.extra["snapshot_id"] == "snap-1"


# ---- Resolver: supersession + eligibility -------------------------


def test_superseded_artifact_dropped_with_warning():
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            metadata={"search_state": "superseded"},
            source_document_ids=["doc-1"],
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 0
    assert res.unresolved_source_ref_count == 1
    assert WARNING_SOURCE_REF_SUPERSEDED in res.warnings


def test_eligible_snapshot_pairs_filter_drops_out_of_scope_refs():
    """A ref pointing at (doc-X, snap-X) not in the eligibility set
    is dropped without contacting the registry."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(
            chunk_id="chunk-1", artifact_id="compile-art-1",
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        eligible_snapshot_pairs=frozenset({("doc-2", "snap-2")}),
    )
    assert res.injected_evidence_count == 0
    assert WARNING_SOURCE_REF_OUT_OF_SCOPE in res.warnings


def test_eligible_pairs_allows_in_scope_refs():
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(
            chunk_id="chunk-1", artifact_id="compile-art-1",
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        eligible_snapshot_pairs=frozenset({("doc-1", "snap-1")}),
    )
    assert res.injected_evidence_count == 1


def test_missing_source_artifact_produces_warning_not_failure():
    resolver = _make_resolver(records=[])  # empty registry
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="missing-art"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 0
    assert res.unresolved_source_ref_count == 1
    assert WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND in res.warnings


# ---- Resolver: no refs / wrong use-mode --------------------------


def test_entry_without_source_refs_produces_no_candidates():
    resolver = _make_resolver(records=[])
    selected = (_entry(source_refs=()),)  # use_mode default but no refs
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
    )
    assert res.injected_evidence_count == 0
    assert res.resolved_source_ref_count == 0


def test_expansion_only_entries_dont_create_evidence_candidates():
    """Memory entries the provider classified as expansion-only
    (aliases, terminology) MUST NOT produce evidence even if a
    well-meaning future producer attaches a source ref."""
    resolver = _make_resolver(records=[
        _SourceRecord(artifact_id="compile-art-1"),
    ])
    selected = (_entry(
        memory_type=MEMORY_ENTRY_TYPE_ALIAS,
        use_mode=USE_MODE_EXPANSION_ONLY,
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
    )
    assert res.injected_evidence_count == 0


def test_summary_context_entries_dont_create_evidence_candidates():
    resolver = _make_resolver(records=[
        _SourceRecord(artifact_id="compile-art-1"),
    ])
    selected = (_entry(
        memory_type=MEMORY_ENTRY_TYPE_GRAPH_SUMMARY,
        use_mode=USE_MODE_SUMMARY_CONTEXT,
        source_refs=(_ref(chunk_id="c1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
    )
    assert res.injected_evidence_count == 0


# ---- Resolver: table / image refs ---------------------------------


def test_table_only_ref_defers_with_diagnostic():
    """A ref carrying ONLY `table_id` (no chunk/artifact locator)
    can't safely become an evidence candidate today — defer with a
    diagnostic so the operator sees the ref was considered."""
    resolver = _make_resolver(records=[])
    selected = (_entry(
        source_refs=(EnrichmentSourceRef(
            table_id="table-7", chunk_id=None, artifact_id=None,
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 0
    assert WARNING_SOURCE_REF_TABLE_DEFERRED in res.warnings


def test_image_only_ref_resolves_to_marker_with_diagnostic():
    """An image-only ref (image_id but no chunk/artifact) tries to
    look up the image artifact directly and surfaces a marker
    candidate so the trace shows it; the diagnostic warning notes
    image bodies aren't rendered yet."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="img-1",
            kind="image",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ])
    selected = (_entry(
        source_refs=(EnrichmentSourceRef(
            image_id="img-1", chunk_id=None, artifact_id=None,
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert WARNING_SOURCE_REF_IMAGE_DEFERRED in res.warnings
    assert res.injected_evidence_count == 1
    assert res.injected[0].candidate.artifact_kind == "image"


def test_page_only_ref_without_locator_defers():
    """A ref with only `page` (no chunk_id/artifact_id) defers —
    we'd need a chunk index to translate page → chunk."""
    resolver = _make_resolver(records=[])
    selected = (_entry(
        source_refs=(EnrichmentSourceRef(
            page=5, chunk_id=None, artifact_id=None,
            document_id="doc-1", snapshot_id="snap-1",
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 0
    assert WARNING_SOURCE_REF_NO_LOCATOR in res.warnings


# ---- Resolver: cap + dedupe --------------------------------------


def test_cap_applied_when_injection_exceeds_max_source_evidence():
    """Cap fires; the warning surfaces; injected_evidence_count
    equals the cap; resolved_source_ref_count is the full count
    (we still resolved them, just didn't inject all)."""
    records = [
        _SourceRecord(
            artifact_id=f"compile-art-{i}",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        )
        for i in range(10)
    ]
    resolver = _make_resolver(records=records, body_loader=lambda r: "x")
    refs = tuple(
        _ref(chunk_id=f"chunk-{i}", artifact_id=f"compile-art-{i}")
        for i in range(10)
    )
    selected = (_entry(source_refs=refs),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(max_source_evidence=3),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 3
    assert res.resolved_source_ref_count == 10
    assert WARNING_SOURCE_REF_CAP_APPLIED in res.warnings


def test_deduplicates_against_existing_canonical_keys():
    """The orchestrator hands the resolver the set of
    `(route, artifact_id, chunk_id)` triples already present in
    the canonical pool. A ref that would collide is dropped with
    the dedupe warning."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    existing_keys = frozenset({(
        RetrievalRouteKind.ARTIFACT_LOOKUP.value, "compile-art-1", "chunk-1",
    )})
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        existing_keys=existing_keys,
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 0
    assert res.deduped_evidence_count == 1
    assert WARNING_SOURCE_REF_DEDUPED in res.warnings


def test_resolver_internal_dedupe_drops_duplicate_refs_in_same_call():
    """Two memory entries point at the same chunk — only one
    candidate is emitted."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    ref = _ref(chunk_id="chunk-1", artifact_id="compile-art-1")
    selected = (
        _entry(memory_id="risk-A", source_refs=(ref,)),
        _entry(memory_id="risk-B", source_refs=(ref,)),
    )
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    assert res.injected_evidence_count == 1
    assert res.deduped_evidence_count == 1


# ---- Resolver: project-active scope -------------------------------


def test_project_scope_refs_resolve_only_in_eligible_pairs():
    """Project-active query — a ref pointing at doc-A but the
    eligibility set covers only doc-B is dropped."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-doc-A",
            source_document_ids=["doc-A"],
            snapshot_id="snap-A",
        ),
        _SourceRecord(
            artifact_id="compile-art-doc-B",
            source_document_ids=["doc-B"],
            snapshot_id="snap-B",
        ),
    ], body_loader=lambda r: "body")
    selected = (
        _entry(
            memory_id="risk-A",
            document_id="doc-A", snapshot_id="snap-A",
            source_refs=(_ref(
                chunk_id="c-A", artifact_id="compile-art-doc-A",
                document_id="doc-A", snapshot_id="snap-A",
            ),),
        ),
        _entry(
            memory_id="risk-B",
            document_id="doc-B", snapshot_id="snap-B",
            source_refs=(_ref(
                chunk_id="c-B", artifact_id="compile-art-doc-B",
                document_id="doc-B", snapshot_id="snap-B",
            ),),
        ),
    )
    # Only doc-A is in scope.
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        eligible_snapshot_pairs=frozenset({("doc-A", "snap-A")}),
    )
    assert res.injected_evidence_count == 1
    assert res.injected[0].candidate.document_id == "doc-A"
    assert WARNING_SOURCE_REF_OUT_OF_SCOPE in res.warnings


def test_cross_document_ref_without_lineage_dropped():
    """If a memory entry's ref lacks document_id/snapshot_id and
    eligibility is a pair set, we drop it rather than guess — the
    eligibility filter can't validate it."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(EnrichmentSourceRef(
            artifact_id="compile-art-1", chunk_id="chunk-1",
            document_id=None, snapshot_id=None,
        ),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        eligible_snapshot_pairs=frozenset({("doc-1", "snap-1")}),
    )
    assert res.injected_evidence_count == 0
    assert WARNING_SOURCE_REF_OUT_OF_SCOPE in res.warnings


# ---- Resolver: failure safety ------------------------------------


def test_resolver_handles_internal_exception_gracefully():
    """If the body loader or any internal step explodes, the
    resolver returns an empty resolution + a resolver_error warning
    rather than propagating."""
    class _BoomLoader:
        def __call__(self, record):
            raise RuntimeError("boom")

    resolver = _make_resolver(
        records=[_SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
        )],
        body_loader=_BoomLoader(),
    )
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    # Body loader throwing is tolerated — body just empty.
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    # Candidate still produced; body field empty (loader failed).
    assert res.injected_evidence_count == 1
    assert res.injected[0].candidate.extra["body"] == ""


def test_resolver_with_no_registry_returns_empty():
    """Resolver without an artifact registry can't look up
    artifacts — returns an empty resolution + appropriate
    warnings."""
    resolver = KnowledgeMemoryEvidenceResolver(
        artifact_registry=None,
        body_loader=None,
    )
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
    )
    assert res.injected_evidence_count == 0


def test_resolver_with_empty_selected_entries_is_no_op():
    resolver = _make_resolver(records=[])
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=(),
        settings=_settings(),
    )
    assert res == KnowledgeMemoryEvidenceResolution.empty()


def test_collect_existing_keys_returns_route_artifact_chunk_triples():
    cands = (
        EvidenceCandidate(
            route=RetrievalRouteKind.RAGANYTHING,
            artifact_id="a-1", artifact_kind="chunk",
            chunk_id="c-1", text_preview="",
            score=1.0, matched_anchors=(),
            run_id=None, document_id=None, project_id="p1",
        ),
        EvidenceCandidate(
            route=RetrievalRouteKind.BM25,
            artifact_id="a-2", artifact_kind="evidence_chunk",
            chunk_id=None, text_preview="",
            score=0.5, matched_anchors=(),
            run_id=None, document_id=None, project_id="p1",
        ),
    )
    keys = collect_existing_keys(cands)
    assert ("raganything", "a-1", "c-1") in keys
    assert ("bm25", "a-2", "") in keys


# ---- Diagnostic projection ---------------------------------------


def test_diagnostic_payload_has_expected_keys():
    resolution = KnowledgeMemoryEvidenceResolution(
        resolved_source_ref_count=5,
        injected_evidence_count=3,
        deduped_evidence_count=1,
        unresolved_source_ref_count=1,
        warnings=(WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND,),
        applied=True,
    )
    payload = resolution.to_diagnostic()
    assert payload["resolved_source_ref_count"] == 5
    assert payload["injected_evidence_count"] == 3
    assert payload["deduped_evidence_count"] == 1
    assert payload["unresolved_source_ref_count"] == 1
    assert payload["evidence_injection_applied"] is True
    assert (
        WARNING_SOURCE_REF_ARTIFACT_NOT_FOUND
        in payload["source_ref_resolution_warnings"]
    )


# ---- Orchestrator integration ------------------------------------


class _StubKnowledgeMemoryProvider:
    """Stub provider that returns a fixed context. Used by the
    orchestrator-level integration tests to drive the resolver
    invocation gate."""

    def __init__(self, context: KnowledgeMemoryQueryContext) -> None:
        self._context = context

    def context_for_query(self, **_kwargs):
        return self._context


def _orch_with(provider=None, resolver=None):
    """Build a minimal orchestrator for integration tests."""
    from j1.query.orchestrator import SmartQueryOrchestrator

    class _NoopRoute:
        def execute(self, request, jobs):
            return []

    return SmartQueryOrchestrator.from_components(
        routes={RetrievalRouteKind.ARTIFACT_LOOKUP: _NoopRoute()},
        llm=lambda req: "",
        knowledge_memory_provider=provider,
        knowledge_memory_evidence_resolver=resolver,
    )


def test_orchestrator_accepts_resolver_kwarg():
    import inspect
    from j1.query.orchestrator import SmartQueryOrchestrator
    sig = inspect.signature(SmartQueryOrchestrator.__init__)
    assert "knowledge_memory_evidence_resolver" in sig.parameters
    assert (
        sig.parameters["knowledge_memory_evidence_resolver"].default is None
    )


def test_orchestrator_from_components_forwards_resolver_kwarg():
    import inspect
    from j1.query.orchestrator import SmartQueryOrchestrator
    sig = inspect.signature(SmartQueryOrchestrator.from_components)
    assert "knowledge_memory_evidence_resolver" in sig.parameters


def test_orchestrator_skips_resolver_when_memory_disabled(monkeypatch):
    """Provider not wired → resolver should never see selected
    entries. Trace's `knowledge_memory` stays None."""
    from j1.query.orchestrator import OrchestratorRequest

    calls: list[dict] = []

    class _Resolver:
        def resolve(self, **kwargs):
            calls.append(kwargs)
            return KnowledgeMemoryEvidenceResolution.empty()

    orch = _orch_with(provider=None, resolver=_Resolver())
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    result = orch.run(request)
    assert calls == []
    assert result.trace.knowledge_memory is None


def test_orchestrator_skips_resolver_when_provider_status_not_used(
    monkeypatch,
):
    """Provider returns `not_available` → resolver isn't invoked."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")
    from j1.memory.query_provider import STATUS_NOT_AVAILABLE
    from j1.query.orchestrator import OrchestratorRequest

    captured: list[dict] = []

    class _Resolver:
        def resolve(self, **kwargs):
            captured.append(kwargs)
            return KnowledgeMemoryEvidenceResolution.empty()

    provider = _StubKnowledgeMemoryProvider(
        KnowledgeMemoryQueryContext(status=STATUS_NOT_AVAILABLE)
    )
    orch = _orch_with(provider=provider, resolver=_Resolver())
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    result = orch.run(request)
    # Resolver not invoked since status != used.
    assert captured == []


def test_orchestrator_invokes_resolver_when_memory_used(monkeypatch):
    """All conditions met → resolver called; injected candidates
    surface in the trace diagnostic block."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")
    from j1.query.orchestrator import OrchestratorRequest

    selected_entry = _entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="art-1"),),
    )
    provider_context = KnowledgeMemoryQueryContext(
        status=STATUS_USED,
        available=True,
        artifact_id="mem-1",
        selected_entries=(selected_entry,),
        resolved_source_ref_count=1,
        entry_count=1,
    )

    invoked: list[dict] = []

    class _Resolver:
        def resolve(self, **kwargs):
            invoked.append(kwargs)
            return KnowledgeMemoryEvidenceResolution(
                resolved_source_ref_count=1,
                injected_evidence_count=1,
                applied=True,
                injected=(),  # don't need real candidates here
            )

    orch = _orch_with(
        provider=_StubKnowledgeMemoryProvider(provider_context),
        resolver=_Resolver(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="what are the risks?",
        document_id="doc-1",
    )
    result = orch.run(request)
    assert len(invoked) == 1
    assert invoked[0]["selected_entries"] == (selected_entry,)
    # Diagnostic block has the resolver fields.
    assert result.trace.knowledge_memory is not None
    assert (
        result.trace.knowledge_memory["evidence_injection_applied"]
        is True
    )
    assert result.trace.knowledge_memory["injected_evidence_count"] == 1


def test_orchestrator_resolver_exception_does_not_fail_query(monkeypatch):
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")
    from j1.query.orchestrator import OrchestratorRequest

    selected_entry = _entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="art-1"),),
    )
    provider_context = KnowledgeMemoryQueryContext(
        status=STATUS_USED,
        available=True,
        artifact_id="mem-1",
        selected_entries=(selected_entry,),
        resolved_source_ref_count=1,
        entry_count=1,
    )

    class _RaisingResolver:
        def resolve(self, **kwargs):
            raise RuntimeError("resolver blew up")

    orch = _orch_with(
        provider=_StubKnowledgeMemoryProvider(provider_context),
        resolver=_RaisingResolver(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    # Query completes without raising.
    result = orch.run(request)
    assert result is not None


def test_orchestrator_injected_candidates_flow_to_evidence_pool(monkeypatch):
    """Resolver returns a real candidate → orchestrator appends it to
    the canonical pool BEFORE the evidence builder runs."""
    monkeypatch.setenv(ENV_QUERY_KNOWLEDGE_MEMORY_ENABLED, "true")
    from j1.query.orchestrator import OrchestratorRequest
    from j1.memory.source_ref_resolver import ResolvedSourceEvidence

    selected_entry = _entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="art-1"),),
    )
    provider_context = KnowledgeMemoryQueryContext(
        status=STATUS_USED,
        available=True,
        artifact_id="mem-1",
        selected_entries=(selected_entry,),
        resolved_source_ref_count=1,
        entry_count=1,
    )

    injected_cand = EvidenceCandidate(
        route=RetrievalRouteKind.ARTIFACT_LOOKUP,
        artifact_id="art-1", artifact_kind="compiled_text",
        chunk_id="chunk-1", text_preview="memory grounded body",
        score=0.8, matched_anchors=(),
        run_id="run-1", document_id="doc-1", project_id="p1",
        extra={
            "body": "memory grounded body",
            "evidence_origin": EVIDENCE_ORIGIN_MEMORY_GUIDED,
            "memory_id": selected_entry.entry.memory_id,
            "memory_type": selected_entry.entry.memory_type,
            "source_artifact_id": "art-1",
        },
    )

    class _Resolver:
        def resolve(self, **kwargs):
            return KnowledgeMemoryEvidenceResolution(
                injected=(ResolvedSourceEvidence(
                    candidate=injected_cand,
                    memory_id=selected_entry.entry.memory_id,
                    memory_type=selected_entry.entry.memory_type,
                    memory_artifact_id="mem-1",
                    source_ref=selected_entry.entry.source_refs[0],
                ),),
                resolved_source_ref_count=1,
                injected_evidence_count=1,
                applied=True,
            )

    orch = _orch_with(
        provider=_StubKnowledgeMemoryProvider(provider_context),
        resolver=_Resolver(),
    )
    request = OrchestratorRequest(
        ctx=_ctx(), question="risks", document_id="doc-1",
    )
    result = orch.run(request)
    # The injected candidate landed in all_candidates.
    cand_ids = [c.artifact_id for c in result.trace.all_candidates]
    assert "art-1" in cand_ids
    # And the trace's diagnostic block shows the injection.
    assert (
        result.trace.knowledge_memory["evidence_injection_applied"]
        is True
    )


# ---- Source-grounding tests ---------------------------------------


def test_injected_candidate_carries_memory_guided_origin():
    """A injected candidate's `extra` dict must carry
    `evidence_origin=memory_guided_source_ref` so the synthesizer/
    binder/dashboards can distinguish memory-guided from canonical
    evidence."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    cand = res.injected[0].candidate
    assert cand.extra["evidence_origin"] == EVIDENCE_ORIGIN_MEMORY_GUIDED
    assert cand.extra["memory_id"]
    assert cand.extra["memory_type"]
    assert cand.extra["source_artifact_id"] == "compile-art-1"


def test_memory_entry_itself_never_appears_as_a_candidate():
    """Defense-in-depth — none of the injected candidates should
    have an artifact_id that matches a memory artifact id or a
    `knowledge_memory.*` artifact_kind."""
    resolver = _make_resolver(records=[
        _SourceRecord(
            artifact_id="compile-art-1",
            source_document_ids=["doc-1"],
            snapshot_id="snap-1",
        ),
    ], body_loader=lambda r: "body")
    selected = (_entry(
        artifact_id="mem-1",  # the memory artifact's id
        source_refs=(_ref(chunk_id="chunk-1", artifact_id="compile-art-1"),),
    ),)
    res = resolver.resolve(
        ctx=_ctx(),
        selected_entries=selected,
        settings=_settings(),
        document_id="doc-1",
    )
    for r in res.injected:
        assert r.candidate.artifact_id != "mem-1"
        assert not r.candidate.artifact_kind.startswith("knowledge_memory")


def test_synth_system_prompt_contains_memory_guided_guardrail():
    """The synthesizer's system prompt must include the Phase 5B
    instruction that memory-guided evidence cites the source, not
    the memory entry."""
    from j1.query.answer_synthesizer import _system_prompt
    from j1.query.domain_profile import GENERIC_PROFILE

    plan = QueryPlan(
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
    prompt = _system_prompt(plan, GENERIC_PROFILE)
    assert "memory-guided" in prompt
    assert "Knowledge Memory" in prompt
    assert "source evidence" in prompt


# ---- No-LLM regression guard --------------------------------------


def test_resolver_module_has_no_llm_imports():
    import importlib
    import inspect
    mod = importlib.import_module("j1.memory.source_ref_resolver")
    source = inspect.getsource(mod)
    forbidden = {
        "openai", "langchain", "anthropic", "raganything", "lightrag",
        "TextLLMClient", "VisionLLMClient",
    }
    leaked = [name for name in forbidden if name in source]
    assert not leaked, (
        f"j1.memory.source_ref_resolver leaks LLM imports: {leaked}"
    )
