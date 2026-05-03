from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.payloads import (
    CompileActivityInput,
    EnrichActivityInput,
    GraphActivityInput,
    IndexActivityInput,
    ProjectScope,
    QueryActivityInput,
)
from j1.orchestration.activities.processing import (
    ACTIVITY_BUILD_GRAPH,
    ACTIVITY_COMPILE,
    ACTIVITY_ENRICH,
    ACTIVITY_INDEX,
    ACTIVITY_QUERY,
    ProcessingActivities,
    UnknownProcessorError,
)
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    ProcessingResult,
    QueryResult,
    ResultStatus,
)


# Mock processors


class _Compiler:
    kind = "mock.compiler"

    def compile(self, ctx, document_id):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="compiled.text",
                    content=b"hello",
                    suggested_extension=".txt",
                )
            ],
        )


class _Enricher:
    kind = "mock.enricher"

    def enrich(self, ctx, artifact_id):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="enriched.entities",
                    content=b'{"e":1}',
                    suggested_extension=".json",
                )
            ],
        )


class _GraphBuilder:
    kind = "mock.graph"

    def build(self, ctx, artifact_ids):
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=[
                ArtifactDraft(
                    kind="graph.entities",
                    content=b"<g/>",
                    suggested_extension=".xml",
                )
            ],
        )


class _Indexer:
    kind = "mock.index"

    def index(self, ctx, artifact_ids):
        return ProcessingResult(
            status=ResultStatus.SUCCEEDED,
            metadata={"indexed": str(len(artifact_ids))},
        )


class _QueryProvider:
    kind = "mock.query"

    def query(self, ctx, question, *, max_results=None):
        return QueryResult(
            status=ResultStatus.SUCCEEDED, answer="42", citations=["doc-1"]
        )


# Helpers


def _scope(ctx) -> ProjectScope:
    return ProjectScope.from_context(ctx)


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _document(ctx) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="paper.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="sha256:doc",
        status=ProcessingStatus.PENDING,
        created_at=_now(),
    )


def _artifact_record(ctx, *, artifact_id="art-1") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash="sha256:abc",
        byte_size=5,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )


@pytest.fixture
def activities(processing_service, registry, artifact_registry):
    return ProcessingActivities(
        processing=processing_service,
        sources=registry,
        artifacts=artifact_registry,
        compilers={"mock.compiler": _Compiler()},
        enrichers={"mock.enricher": _Enricher()},
        graph_builders={"mock.graph": _GraphBuilder()},
        indexers={"mock.index": _Indexer()},
        query_providers={"mock.query": _QueryProvider()},
    )


# Activity-defn metadata


def test_each_activity_has_temporal_marker(activities):
    for func in activities.all_activities():
        assert hasattr(func, "__temporal_activity_definition")


def test_activity_names_are_namespaced(activities):
    names = [
        a.__temporal_activity_definition.name for a in activities.all_activities()
    ]
    assert ACTIVITY_COMPILE in names
    assert ACTIVITY_ENRICH in names
    assert ACTIVITY_BUILD_GRAPH in names
    assert ACTIVITY_INDEX in names
    assert ACTIVITY_QUERY in names


# Compile


def test_compile_activity_invokes_processing_service(
    activities, ctx, registry, artifact_registry
):
    registry.add(_document(ctx))
    result = activities.compile(
        CompileActivityInput(
            scope=_scope(ctx),
            document_id="doc-1",
            processor_kind="mock.compiler",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1
    # The artifact ended up in the registry.
    assert artifact_registry.list_artifacts(ctx)[0].artifact_id == result.artifact_ids[0]


def test_compile_activity_unknown_processor(activities, ctx, registry):
    registry.add(_document(ctx))
    with pytest.raises(UnknownProcessorError):
        activities.compile(
            CompileActivityInput(
                scope=_scope(ctx),
                document_id="doc-1",
                processor_kind="unregistered",
            )
        )


# Enrich


def test_enrich_activity_invokes_processing_service(
    activities, ctx, artifact_registry
):
    artifact_registry.add(_artifact_record(ctx))
    result = activities.enrich(
        EnrichActivityInput(
            scope=_scope(ctx),
            artifact_id="art-1",
            processor_kind="mock.enricher",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1


# Graph


def test_build_graph_activity(activities, ctx):
    result = activities.build_graph(
        GraphActivityInput(
            scope=_scope(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.graph",
        )
    )
    assert result.status == "succeeded"
    assert len(result.artifact_ids) == 1


# Index


def test_index_activity(activities, ctx):
    result = activities.index(
        IndexActivityInput(
            scope=_scope(ctx),
            artifact_ids=["a", "b"],
            processor_kind="mock.index",
        )
    )
    assert result.status == "succeeded"


# Query


def test_query_activity(activities, ctx):
    result = activities.query(
        QueryActivityInput(
            scope=_scope(ctx),
            question="what?",
            processor_kind="mock.query",
        )
    )
    assert result.status == "succeeded"
    assert result.answer == "42"
    assert result.citations == ["doc-1"]


# Project scope round-trip (Temporal payload safety)


def test_project_scope_round_trips(ctx):
    scope = ProjectScope.from_context(ctx)
    rehydrated = scope.to_context()
    assert rehydrated == ctx
