import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from temporalio.exceptions import ApplicationError

from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.cost.breakdown import CostBreakdown
from j1.cost.sink import COST_LOG_FILENAME
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.knowledge import (
    ACTIVITY_PREPARE_GRAPH_CORPUS,
    ACTIVITY_REGISTER_COMPILED,
    ACTIVITY_REGISTER_GRAPH,
    ACTIVITY_RUN_COMPILATION,
    ACTIVITY_RUN_ENRICHMENT,
    ACTIVITY_RUN_GRAPH_BUILD,
    KnowledgeProcessingActivities,
)
from j1.orchestration.activities.payloads import (
    ArtifactEnrichmentInput,
    DraftPayload,
    GraphBuildInput,
    GraphCorpusInput,
    KnowledgeCompilationInput,
    ProjectScope,
    RegisterArtifactsInput,
)
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ResultStatus,
)


# Mocks


class _Compiler:
    kind = "mock.compiler"

    def __init__(self, *, drafts=None, costs=None, raise_exc=None):
        self._drafts = drafts or []
        self._costs = costs or []
        self._exc = raise_exc

    def compile(self, ctx, document_id):
        if self._exc:
            raise self._exc
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=self._drafts,
            cost_events=self._costs,
        )


class _Enricher:
    kind = "mock.enricher"

    def __init__(self, *, drafts=None, costs=None, raise_exc=None):
        self._drafts = drafts or []
        self._costs = costs or []
        self._exc = raise_exc

    def enrich(self, ctx, artifact_id):
        if self._exc:
            raise self._exc
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=self._drafts,
            cost_events=self._costs,
        )


class _GraphBuilder:
    kind = "mock.graph"

    def __init__(self, *, drafts=None, raise_exc=None):
        self._drafts = drafts or []
        self._exc = raise_exc

    def build(self, ctx, artifact_ids):
        if self._exc:
            raise self._exc
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=self._drafts
        )


# Helpers


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _document(ctx, *, doc_id="doc-1") -> DocumentRecord:
    return DocumentRecord(
        document_id=doc_id,
        project=ctx,
        original_filename=f"{doc_id}.pdf",
        stored_filename=f"{doc_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=f"sha256:{doc_id}",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )


def _artifact_record(ctx, *, artifact_id="art-1", kind="compiled.text") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"compiled/{artifact_id}.txt",
        content_hash=f"sha256:{artifact_id}",
        byte_size=5,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )


def _read_audit(workspace, ctx):
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_costs(workspace, ctx):
    path = workspace.audit(ctx) / COST_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def knowledge_activities(
    workspace,
    registry,
    artifact_registry,
    audit_recorder,
    cost_recorder,
    fixed_clock,
    id_factory,
):
    return KnowledgeProcessingActivities(
        workspace=workspace,
        sources=registry,
        artifacts=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
        compilers={"mock.compiler": _Compiler(drafts=[
            ArtifactDraft(
                kind="compiled.text",
                content=b"hello compiled",
                suggested_extension=".txt",
            )
        ])},
        enrichers={"mock.enricher": _Enricher(drafts=[
            ArtifactDraft(
                kind="enriched.entities",
                content=b'{"e":1}',
                suggested_extension=".json",
            )
        ])},
        graph_builders={"mock.graph": _GraphBuilder(drafts=[
            ArtifactDraft(
                kind="graph.entities",
                content=b"<g/>",
                suggested_extension=".xml",
            )
        ])},
        clock=fixed_clock,
        id_factory=id_factory,
    )


# Activity-defn metadata


def test_activity_names(knowledge_activities):
    names = [
        a.__temporal_activity_definition.name
        for a in knowledge_activities.all_activities()
    ]
    expected = {
        ACTIVITY_RUN_COMPILATION,
        ACTIVITY_REGISTER_COMPILED,
        ACTIVITY_RUN_ENRICHMENT,
        ACTIVITY_PREPARE_GRAPH_CORPUS,
        ACTIVITY_RUN_GRAPH_BUILD,
        ACTIVITY_REGISTER_GRAPH,
    }
    assert expected.issubset(set(names))


# run_knowledge_compilation


def test_run_compilation_returns_drafts_without_registering(
    knowledge_activities, registry, artifact_registry, ctx
):
    registry.add(_document(ctx))
    result = knowledge_activities.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "succeeded"
    assert len(result.drafts) == 1
    assert result.drafts[0].content == b"hello compiled"
    # Nothing registered yet — registration is a separate activity.
    assert artifact_registry.list_artifacts(ctx) == []


def test_run_compilation_records_cost(
    registry,
    artifact_registry,
    workspace,
    audit_recorder,
    cost_recorder,
    fixed_clock,
    id_factory,
    ctx,
):
    registry.add(_document(ctx))
    activities = KnowledgeProcessingActivities(
        workspace=workspace,
        sources=registry,
        artifacts=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
        compilers={
            "mock.compiler": _Compiler(
                drafts=[
                    ArtifactDraft(
                        kind="compiled.text",
                        content=b"x",
                        suggested_extension=".txt",
                    )
                ],
                costs=[
                    CostBreakdown(
                        vendor="anthropic",
                        model="m",
                        unit_kind="input_tokens",
                        units=42,
                        amount=Decimal("0.0010"),
                    )
                ],
            )
        },
        clock=fixed_clock,
        id_factory=id_factory,
    )
    activities.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    costs = _read_costs(workspace, ctx)
    assert len(costs) == 1
    assert costs[0]["units"] == 42


def test_run_compilation_unknown_kind_raises_non_retryable(
    knowledge_activities, registry, ctx
):
    registry.add(_document(ctx))
    with pytest.raises(ApplicationError) as exc:
        knowledge_activities.run_knowledge_compilation_activity(
            KnowledgeCompilationInput(
                scope=ProjectScope.from_context(ctx),
                document_id="doc-1",
                processor_kind="unknown",
            )
        )
    assert exc.value.non_retryable is True


def test_run_compilation_missing_document_raises_non_retryable(
    knowledge_activities, ctx
):
    with pytest.raises(ApplicationError) as exc:
        knowledge_activities.run_knowledge_compilation_activity(
            KnowledgeCompilationInput(
                scope=ProjectScope.from_context(ctx),
                document_id="missing",
                processor_kind="mock.compiler",
            )
        )
    assert exc.value.non_retryable is True


def test_run_compilation_failed_processor_returns_failed(
    workspace, registry, artifact_registry, audit_recorder, cost_recorder,
    fixed_clock, id_factory, ctx,
):
    registry.add(_document(ctx))
    activities = KnowledgeProcessingActivities(
        workspace=workspace,
        sources=registry,
        artifacts=artifact_registry,
        audit=audit_recorder,
        cost=cost_recorder,
        compilers={"mock.compiler": _Compiler(raise_exc=RuntimeError("boom"))},
        clock=fixed_clock,
        id_factory=id_factory,
    )
    result = activities.run_knowledge_compilation_activity(
        KnowledgeCompilationInput(
            scope=ProjectScope.from_context(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "failed"
    assert result.error == "boom"
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "j1.knowledge.compilation.failed"


# register_compiled_artifacts


def test_register_compiled_artifacts_writes_and_registers(
    knowledge_activities, workspace, artifact_registry, ctx
):
    drafts = [
        DraftPayload(
            kind="compiled.text",
            content=b"hello",
            suggested_extension=".txt",
        )
    ]
    result = knowledge_activities.register_compiled_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            source_document_ids=["doc-1"],
            # `compiled.text` is a lineage-required kind under the
            # fail-fast guard added in the lineage hardening round.
            # Real callers always pass correlation_id here (the
            # workflow's run id); the test mirrors that contract.
            correlation_id="run-test",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1
    record = artifact_registry.get(ctx, result.artifact_ids[0])
    assert record.location.startswith("compiled/")
    assert record.source_document_ids == ["doc-1"]

    stored = workspace.compiled(ctx) / record.location.split("/", 1)[1]
    assert stored.read_bytes() == b"hello"


def test_register_graph_artifact_stamps_run_id_from_correlation_id(
    knowledge_activities, artifact_registry, ctx
):
    """Regression test for the root cause of validation set
 vrun-b42ec7453865's 3/6 failures: graph artifacts produced by
 the orchestration activity weren't getting `run_id` stamped, so
 every validation check that scoped on `metadata.run_id == run_id`
 failed downstream. The activity must propagate
 `input.correlation_id` (= the ingestion run id) into the
 registered artifact's metadata."""
    drafts = [
        DraftPayload(
            kind="graph_json",
            content=b'{"nodes": [], "edges": []}',
            suggested_extension=".json",
        )
    ]
    result = knowledge_activities.register_graph_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            source_document_ids=["doc-1"],
            correlation_id="run-deadbeef",
        )
    )
    assert result.status == "succeeded"
    record = artifact_registry.get(ctx, result.artifact_ids[0])
    assert record.metadata.get("run_id") == "run-deadbeef", (
        "graph_json artifact missing run_id stamp — validation set "
        "checks like retrieved_chunks_belong_to_run will fail because "
        "the citation's run_id is None"
    )


def test_register_artifact_preserves_producer_supplied_run_id(
    knowledge_activities, artifact_registry, ctx
):
    """Producer-supplied `metadata.run_id` on the draft wins over the
 activity's correlation_id stamp. Lets a producer that already
 knows the run id (e.g. a back-fill path) opt out of the
 default behaviour."""
    drafts = [
        DraftPayload(
            kind="graph_json",
            content=b'{"nodes": [], "edges": []}',
            suggested_extension=".json",
            metadata={"run_id": "run-explicit"},
        )
    ]
    result = knowledge_activities.register_graph_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            source_document_ids=["doc-1"],
            correlation_id="run-default",
        )
    )
    record = artifact_registry.get(ctx, result.artifact_ids[0])
    assert record.metadata.get("run_id") == "run-explicit"


def test_register_compiled_artifacts_is_idempotent(knowledge_activities, ctx):
    drafts = [
        DraftPayload(
            kind="compiled.text",
            content=b"same content",
            suggested_extension=".txt",
        )
    ]
    first = knowledge_activities.register_compiled_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            correlation_id="run-idempotent",
        )
    )
    second = knowledge_activities.register_compiled_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            correlation_id="run-idempotent",
        )
    )
    assert first.artifact_ids
    assert second.artifact_ids == []
    assert second.reused_artifact_ids == first.artifact_ids


# run_artifact_enrichment


def test_run_enrichment_registers_into_enriched_area(
    knowledge_activities, workspace, artifact_registry, ctx
):
    artifact_registry.add(_artifact_record(ctx))
    result = knowledge_activities.run_artifact_enrichment_activity(
        ArtifactEnrichmentInput(
            scope=ProjectScope.from_context(ctx),
            artifact_id="art-1",
            processor_kind="mock.enricher",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1
    record = artifact_registry.get(ctx, result.artifact_ids[0])
    assert record.location.startswith("enriched/")
    assert record.source_artifact_ids == ["art-1"]


def test_run_enrichment_missing_artifact_raises_non_retryable(
    knowledge_activities, ctx
):
    with pytest.raises(ApplicationError) as exc:
        knowledge_activities.run_artifact_enrichment_activity(
            ArtifactEnrichmentInput(
                scope=ProjectScope.from_context(ctx),
                artifact_id="missing",
                processor_kind="mock.enricher",
            )
        )
    assert exc.value.non_retryable is True


def test_run_enrichment_unknown_kind_raises_non_retryable(
    knowledge_activities, artifact_registry, ctx
):
    artifact_registry.add(_artifact_record(ctx))
    with pytest.raises(ApplicationError) as exc:
        knowledge_activities.run_artifact_enrichment_activity(
            ArtifactEnrichmentInput(
                scope=ProjectScope.from_context(ctx),
                artifact_id="art-1",
                processor_kind="unknown",
            )
        )
    assert exc.value.non_retryable is True


# prepare_graph_corpus


def test_prepare_graph_corpus_no_filter(
    knowledge_activities, artifact_registry, ctx
):
    artifact_registry.add(_artifact_record(ctx, artifact_id="a", kind="compiled.text"))
    artifact_registry.add(
        _artifact_record(ctx, artifact_id="b", kind="enriched.entities")
    )
    result = knowledge_activities.prepare_graph_corpus_activity(
        GraphCorpusInput(scope=ProjectScope.from_context(ctx))
    )
    assert result.status == "succeeded"
    assert sorted(result.artifact_ids) == ["a", "b"]


def test_prepare_graph_corpus_include_kinds(
    knowledge_activities, artifact_registry, ctx
):
    artifact_registry.add(_artifact_record(ctx, artifact_id="a", kind="compiled.text"))
    artifact_registry.add(
        _artifact_record(ctx, artifact_id="b", kind="enriched.entities")
    )
    result = knowledge_activities.prepare_graph_corpus_activity(
        GraphCorpusInput(
            scope=ProjectScope.from_context(ctx),
            include_kinds=["enriched.entities"],
        )
    )
    assert result.artifact_ids == ["b"]


def test_prepare_graph_corpus_exclude_kinds(
    knowledge_activities, artifact_registry, ctx
):
    artifact_registry.add(_artifact_record(ctx, artifact_id="a", kind="compiled.text"))
    artifact_registry.add(
        _artifact_record(ctx, artifact_id="b", kind="enriched.entities")
    )
    result = knowledge_activities.prepare_graph_corpus_activity(
        GraphCorpusInput(
            scope=ProjectScope.from_context(ctx),
            exclude_kinds=["compiled.text"],
        )
    )
    assert result.artifact_ids == ["b"]


# run_graph_build


def test_run_graph_build_returns_drafts(knowledge_activities, ctx):
    result = knowledge_activities.run_graph_build_activity(
        GraphBuildInput(
            scope=ProjectScope.from_context(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.graph",
        )
    )
    assert result.status == "succeeded"
    assert len(result.drafts) == 1


def test_run_graph_build_unknown_kind_raises(knowledge_activities, ctx):
    with pytest.raises(ApplicationError) as exc:
        knowledge_activities.run_graph_build_activity(
            GraphBuildInput(
                scope=ProjectScope.from_context(ctx),
                artifact_ids=["a"],
                processor_kind="unknown",
            )
        )
    assert exc.value.non_retryable is True


# register_graph_artifacts


def test_register_graph_artifacts_writes_to_graph_area(
    knowledge_activities, workspace, artifact_registry, ctx
):
    drafts = [
        DraftPayload(
            kind="graph.entities",
            content=b"<g/>",
            suggested_extension=".xml",
        )
    ]
    result = knowledge_activities.register_graph_artifacts_activity(
        RegisterArtifactsInput(
            scope=ProjectScope.from_context(ctx),
            drafts=drafts,
            source_artifact_ids=["a", "b"],
        )
    )
    assert result.status == "succeeded"
    record = artifact_registry.get(ctx, result.artifact_ids[0])
    assert record.location.startswith("graph/")
    assert record.source_artifact_ids == ["a", "b"]
    assert (workspace.graph(ctx) / record.location.split("/", 1)[1]).is_file()
