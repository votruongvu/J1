"""End-to-end integration tests: enrichment alias producer +
consumer wire.

Covers the spec's required scenarios:

  3. Query flow can consume an enrichment alias.
  4. Missing alias artifact is safe.
  5. Scope safety (alias for run/snapshot A is invisible to B).

Plus diagnostics coverage:

  * ``memory_view.enrichment_aliases_available`` count surfaces.
  * ``memory_view.enrichment_aliases_matched`` lists the bundles
    whose forms appeared in the query (separable from static-pack
    aliases).
  * The orchestrator stamps both onto the trace.

The producer side (chunk scan → registered artifact) is covered
end-to-end by exercising the activity helper directly with a
seeded chunk-artifact registry. We don't run the full Temporal
workflow; the activity helper is the unit of behaviour the spec
cares about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.documents.service import DocumentLifecycleService
from j1.domains.models import (
    ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
    EntityAlias,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.processing import (
    _read_chunks_for_alias_extraction,
)
from j1.processing.enrichment_aliases import (
    ALIAS_ARTIFACT_KIND,
    extract_aliases_from_chunks,
    load_enrichment_aliases_for_snapshot,
    register_aliases_artifact,
)
from j1.query.orchestrator import (
    ENV_QUERY_EXPANSION_ENABLED,
    OrchestratorRequest,
)
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.store import JsonlIngestionRunStore
from j1.validation.dtos import ManualTestQueryRequest, QueryScopeDTO
from j1.validation.service import IngestionValidationService


_NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


# ---- Pipeline scaffolding ----------------------------------------


class _CapturingOrchestrator:
    """Stub orchestrator that records ``OrchestratorRequest`` so
    tests can assert on ``memory_view.expansions`` + the
    enrichment-alias diagnostics the validation service stamps."""

    def __init__(self) -> None:
        self.calls: list[OrchestratorRequest] = []

    def run(self, request: OrchestratorRequest):
        self.calls.append(request)
        trace_stub = SimpleNamespace(
            llm_evidence=(), to_dict=lambda: {},
        )
        return SimpleNamespace(
            answer="stub", final_status="passed",
            citations=(), gate_results=(),
            trace=trace_stub, message=None,
        )


@pytest.fixture
def orchestrator():
    return _CapturingOrchestrator()


@pytest.fixture
def run_store(workspace):
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def lifecycle_service(workspace, registry, artifact_registry):
    return DocumentLifecycleService(
        registry=registry, artifact_registry=artifact_registry,
        clock=lambda: _NOW,
    )


def _seed_doc_with_compile_artifact(
    registry, run_store, artifact_registry, ctx,
    *, document_id: str = "doc-1",
    snapshot_id: str = "snap-active",
    run_id: str = "r-baseline",
):
    """Minimum state for the validation service's queryability gate
    to accept the request — attached doc + active snapshot + a
    compile artifact for the snapshot."""
    registry.add(DocumentRecord(
        document_id=document_id, project=ctx,
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf", file_size=42,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED, created_at=_NOW,
        knowledge_state="attached",
        active_snapshot_id=snapshot_id,
    ))
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id, document_id=document_id,
        workflow_id=f"wf-{run_id}", workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW, updated_at=_NOW, completed_at=_NOW,
        metadata={}, run_type="initial",
        target_snapshot_id=snapshot_id,
    ))
    artifact_registry.add(ArtifactRecord(
        artifact_id=f"a-chunk-{document_id}", project=ctx,
        kind="compiled.text",
        location=f"compiled/{document_id}.txt",
        content_hash=f"sha256:a-chunk-{document_id}",
        byte_size=10, status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=[document_id],
        snapshot_id=snapshot_id,
        created_by_run_id=run_id,
    ))


def _seed_enrichment_aliases(
    artifact_registry, ctx, *,
    document_id: str = "doc-1",
    snapshot_id: str = "snap-active",
    run_id: str = "r-enrich",
    text: str,
):
    """Run the extractor over a single body of text and persist
    the resulting aliases as a ``domain_enrichment_aliases``
    artifact under the given snapshot."""
    chunks = [{
        "body": text,
        "artifact_id": "a-source",
        "chunk_id": "c-source",
        "page": 1,
    }]
    extracted = extract_aliases_from_chunks(
        chunks,
        run_id=run_id,
        snapshot_id=snapshot_id,
        document_id=document_id,
    )
    return register_aliases_artifact(
        ctx=ctx,
        artifact_registry=artifact_registry,
        run_id=run_id,
        document_id=document_id,
        snapshot_id=snapshot_id,
        aliases=extracted,
    )


def _build_service(
    *,
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, domain_pack_lookup=None,
):
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        audit=audit_recorder,
        workspace=workspace,
        source_registry=registry,
        smart_query_orchestrator=orchestrator,
        domain_pack_lookup=domain_pack_lookup,
    )


def _request(question: str, document_id: str = "doc-1"):
    return ManualTestQueryRequest(
        question=question,
        scope=QueryScopeDTO(type="document_active", document_id=document_id),
    )


# ---- Spec test 3: query flow consumes enrichment alias -----------


def test_query_flow_consumes_enrichment_alias(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """``BOQ (bill of quantities)`` lands in an enrichment artifact;
    a query mentioning ``BOQ`` produces an expansion variant
    containing ``bill of quantities`` AND the diagnostic counts
    show the alias matched via enrichment."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_doc_with_compile_artifact(
        registry, run_store, artifact_registry, ctx,
    )
    _seed_enrichment_aliases(
        artifact_registry, ctx,
        text="Reference the bill of quantities (BOQ) before each cycle.",
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        # No pack lookup wired — the enrichment loader supplies the
        # alias on its own.
        domain_pack_lookup=None,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _request("BOQ item summary"),
    )

    [request] = orchestrator.calls
    view = request.memory_view
    # Expansion populated from the enrichment alias.
    assert "bill of quantities" in view.expansions
    # Diagnostic counts surface the enrichment provenance.
    assert view.enrichment_aliases_available == 1
    assert ("bill of quantities", "BOQ") in view.enrichment_aliases_matched


# ---- Spec test 4: missing alias artifact is safe ----------------


def test_query_flow_works_when_no_enrichment_aliases_persisted(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """No alias artifact for the scope → expansions are empty,
    diagnostics show zero availability, retrieval runs normally.
    Pinned per spec rule: "missing aliases do not break the
    query flow"."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_doc_with_compile_artifact(
        registry, run_store, artifact_registry, ctx,
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=None,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _request("BOQ item summary"),
    )

    [request] = orchestrator.calls
    view = request.memory_view
    assert view.expansions == ()
    assert view.enrichment_aliases_available == 0
    assert view.enrichment_aliases_matched == ()


# ---- Spec test 5: scope safety ----------------------------------


def test_enrichment_aliases_do_not_leak_across_snapshots(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """``BOQ`` is defined in an alias artifact stamped under
    snapshot A. A query against a document whose active snapshot
    is B must NOT see the alias."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    # Document is active on snap-B; the alias artifact lives on
    # snap-A.
    _seed_doc_with_compile_artifact(
        registry, run_store, artifact_registry, ctx,
        document_id="doc-1", snapshot_id="snap-B",
    )
    # Persist an alias artifact under DIFFERENT snapshot.
    _seed_enrichment_aliases(
        artifact_registry, ctx,
        document_id="doc-1",
        snapshot_id="snap-A",  # ← different from active
        text="Reference the bill of quantities (BOQ) before each cycle.",
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=None,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _request("BOQ item summary"),
    )

    [request] = orchestrator.calls
    view = request.memory_view
    # The active snapshot is B → snap-A's alias must NOT leak.
    assert view.expansions == ()
    assert view.enrichment_aliases_available == 0


def test_enrichment_aliases_do_not_leak_across_documents(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """Same snapshot pointer (unusual but possible in tests) +
    different document → loader's document_id filter rejects."""
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")
    _seed_doc_with_compile_artifact(
        registry, run_store, artifact_registry, ctx,
        document_id="doc-target", snapshot_id="snap-shared",
        run_id="r-target",
    )
    # Alias artifact stamped under the same snapshot but for a
    # DIFFERENT document.
    _seed_enrichment_aliases(
        artifact_registry, ctx,
        document_id="doc-other",
        snapshot_id="snap-shared",
        text="Reference the bill of quantities (BOQ) before each cycle.",
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=None,
    )
    svc.run_document_test_query(
        ctx, "doc-target",
        _request("BOQ item summary", document_id="doc-target"),
    )

    [request] = orchestrator.calls
    assert request.memory_view.enrichment_aliases_available == 0


# ---- Activity-helper unit ---------------------------------------


def test_activity_chunk_reader_filters_by_snapshot_and_document(
    workspace, artifact_registry, ctx,
):
    """``_read_chunks_for_alias_extraction`` (used by
    ``ProcessingActivities._maybe_emit_enrichment_aliases``) must
    only surface chunks whose ``(snapshot_id, document_id)`` match
    the active scope. Pinned so the activity-layer producer can't
    accidentally read another document's bodies."""
    # Same-snapshot chunk for the target document — should be
    # picked up.
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-target", project=ctx,
        kind="chunk",
        location="chunks/target.json",
        content_hash="sha256:t", byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={
            "snapshot_id": "snap-active",
            "body": "RC (reinforced concrete) beams.",
        },
        snapshot_id="snap-active",
    ))
    # Same-snapshot chunk for a DIFFERENT document — must NOT be
    # picked up.
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-other-doc", project=ctx,
        kind="chunk",
        location="chunks/other-doc.json",
        content_hash="sha256:o", byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-other"],
        metadata={
            "snapshot_id": "snap-active",
            "body": "BOQ (bill of quantities).",
        },
        snapshot_id="snap-active",
    ))
    # Different-snapshot chunk for the target document — must NOT
    # be picked up.
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-stale-snap", project=ctx,
        kind="chunk",
        location="chunks/stale.json",
        content_hash="sha256:s", byte_size=42,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1, created_at=_NOW, updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata={
            "snapshot_id": "snap-stale",
            "body": "PDF (portable document format).",
        },
        snapshot_id="snap-stale",
    ))
    out = _read_chunks_for_alias_extraction(
        artifacts=artifact_registry,
        ctx=ctx,
        document_id="doc-1",
        snapshot_id="snap-active",
    )
    [picked] = out
    assert picked["artifact_id"] == "a-target"
    assert "reinforced concrete" in picked["body"]


# ---- Producer-side: chunks → emitted artifact -------------------


def test_register_aliases_artifact_round_trips_through_loader(
    workspace, artifact_registry, ctx,
):
    """The producer-side artifact written by
    ``register_aliases_artifact`` is readable by
    ``load_enrichment_aliases_for_snapshot`` end-to-end. Same
    snapshot stamp, same document_id, alias survives intact."""
    chunks = [{
        "body": "Issue an RFI (request for information) within 5 days.",
        "artifact_id": "a-source", "chunk_id": "c-source", "page": 7,
    }]
    extracted = extract_aliases_from_chunks(
        chunks,
        run_id="r-enrich", snapshot_id="snap-active",
        document_id="doc-1",
    )
    artifact_id = register_aliases_artifact(
        ctx=ctx, artifact_registry=artifact_registry,
        run_id="r-enrich", document_id="doc-1",
        snapshot_id="snap-active", aliases=extracted,
    )
    assert artifact_id is not None
    loaded = load_enrichment_aliases_for_snapshot(
        ctx=ctx, artifact_registry=artifact_registry,
        document_id="doc-1", snapshot_id="snap-active",
    )
    [bundle] = loaded
    assert bundle.canonical_name == "request for information"
    assert "RFI" in bundle.aliases
    assert bundle.source == ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT


def test_register_aliases_artifact_skips_when_input_empty(
    workspace, artifact_registry, ctx,
):
    """An enrichment run that finds no aliases must NOT persist an
    empty artifact — the loader treats absence as "no aliases"
    and a stub artifact would just clutter the registry."""
    result = register_aliases_artifact(
        ctx=ctx, artifact_registry=artifact_registry,
        run_id="r-enrich", document_id="doc-1",
        snapshot_id="snap-active", aliases=(),
    )
    assert result is None
    listed = artifact_registry.list_artifacts(
        ctx, kind=ALIAS_ARTIFACT_KIND,
    )
    assert listed == []


# ---- Static pack + enrichment compose -----------------------------


def test_pack_and_enrichment_aliases_compose_in_expansion(
    workspace, registry, run_store, artifact_registry, audit_recorder,
    orchestrator, ctx, monkeypatch,
):
    """Both sources active: pack ships ``RC``, enrichment ships
    ``BOQ``. A query mentioning both surfaces both expansions —
    the resolver's ``enrichment_aliases=...`` parameter is the
    glue."""
    from j1.domains.models import (
        DomainEnrichmentPolicy, DomainExtractionHints, DomainPack,
        DomainPromptPack, DomainValidationRules, EntityAlias,
    )
    monkeypatch.setenv(ENV_QUERY_EXPANSION_ENABLED, "true")

    pack = DomainPack(
        id="example.compose", display_name="Compose", version="1",
        extends_document_types=(), keyword_signals=(),
        extraction_targets=(), graph_entity_types=(),
        graph_relationship_types=(), prompt_addon="", overlays={},
        unsupported_capabilities=(),
        enrichment_policy=DomainEnrichmentPolicy(),
        extraction_hints=DomainExtractionHints(
            entity_aliases=(
                EntityAlias(
                    canonical_name="reinforced concrete",
                    aliases=("RC",),
                ),
            ),
        ),
        validation_rules=DomainValidationRules(),
        prompt_pack=DomainPromptPack(),
    )
    _seed_doc_with_compile_artifact(
        registry, run_store, artifact_registry, ctx,
    )
    _seed_enrichment_aliases(
        artifact_registry, ctx,
        text="Reference the bill of quantities (BOQ) before each cycle.",
    )

    svc = _build_service(
        workspace=workspace, registry=registry, run_store=run_store,
        artifact_registry=artifact_registry,
        audit_recorder=audit_recorder, orchestrator=orchestrator,
        domain_pack_lookup=lambda _did: pack,
    )
    svc.run_document_test_query(
        ctx, "doc-1", _request("RC and BOQ for the project"),
    )

    [request] = orchestrator.calls
    expansions = set(request.memory_view.expansions)
    assert "reinforced concrete" in expansions  # static pack
    assert "bill of quantities" in expansions   # enrichment
    # Only the enrichment one shows up in the matched-pairs
    # diagnostic — pack aliases are visible separately via the
    # provider's hints surface (covered in
    # ``test_query_augmentation_wiring``).
    matched = request.memory_view.enrichment_aliases_matched
    assert ("bill of quantities", "BOQ") in matched
