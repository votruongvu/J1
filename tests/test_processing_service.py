import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.cost.sink import COST_LOG_FILENAME
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.results import (
    ArtifactDraft,
    ArtifactProcessingResult,
    CostBreakdown,
    ProcessingResult,
    QueryResult,
    ResultStatus,
)
from j1.projects.context import ProjectContext


def _document(ctx: ProjectContext) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        project=ctx,
        original_filename="paper.pdf",
        stored_filename="doc-1.pdf",
        mime_type="application/pdf",
        file_size=100,
        checksum="sha256:doc",
        status=ProcessingStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _artifact(ctx: ProjectContext, *, artifact_id: str = "art-1") -> ArtifactRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="compiled.text",
        location=f"compiled/{artifact_id}.txt",
        content_hash="sha256:abc",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
    )


def _read_audit(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_costs(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / COST_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# Mock processors — plain classes, no Protocol inheritance, prove structural typing.


class _MockCompiler:
    kind = "mock.compiler"

    def __init__(self, *, drafts=None, costs=None, raise_exc=None):
        self._drafts = drafts or []
        self._costs = costs or []
        self._exc = raise_exc
        self.calls: list[tuple] = []

    def compile(self, ctx, document_id):
        self.calls.append((ctx, document_id))
        if self._exc:
            raise self._exc
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED,
            drafts=self._drafts,
            cost_events=self._costs,
        )


class _MockEnricher:
    kind = "mock.enricher"

    def __init__(self, *, drafts=None, raise_exc=None):
        self._drafts = drafts or []
        self._exc = raise_exc

    def enrich(self, ctx, artifact_id):
        if self._exc:
            raise self._exc
        return ArtifactProcessingResult(
            status=ResultStatus.SUCCEEDED, drafts=self._drafts
        )


class _MockGraphBuilder:
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


class _MockIndexer:
    kind = "mock.index"

    def __init__(self, *, status=ResultStatus.SUCCEEDED, raise_exc=None):
        self._status = status
        self._exc = raise_exc

    def index(self, ctx, artifact_ids):
        if self._exc:
            raise self._exc
        return ProcessingResult(
            status=self._status, metadata={"indexed": len(artifact_ids)}
        )


class _MockQueryProvider:
    kind = "mock.query"

    def __init__(self, *, answer=None, citations=None, costs=None, raise_exc=None):
        self._answer = answer
        self._citations = citations or []
        self._costs = costs or []
        self._exc = raise_exc

    def query(self, ctx, question, *, max_results=None):
        if self._exc:
            raise self._exc
        return QueryResult(
            status=ResultStatus.SUCCEEDED,
            answer=self._answer,
            citations=self._citations,
            cost_events=self._costs,
        )


# Compile


def test_compile_registers_drafts_as_artifacts(processing_service, workspace, ctx):
    drafts = [
        ArtifactDraft(
            kind="compiled.text",
            content=b"hello world",
            suggested_extension=".txt",
        )
    ]
    compiler = _MockCompiler(drafts=drafts)
    result = processing_service.compile(ctx, compiler, _document(ctx))

    assert result.status is ResultStatus.SUCCEEDED
    assert len(result.artifacts) == 1
    record = result.artifacts[0]
    assert record.kind == "compiled.text"
    assert record.location.startswith("compiled/")
    assert record.content_hash.startswith("sha256:")
    assert record.byte_size == len(b"hello world")
    assert record.source_document_ids == ["doc-1"]

    stored = workspace.compiled(ctx) / record.location.split("/", 1)[1]
    assert stored.is_file()
    assert stored.read_bytes() == b"hello world"


def test_compile_audit_event_recorded(processing_service, workspace, ctx):
    drafts = [ArtifactDraft(kind="compiled.text", content=b"x", suggested_extension=".txt")]
    processing_service.compile(ctx, _MockCompiler(drafts=drafts), _document(ctx))
    events = _read_audit(workspace, ctx)
    assert len(events) == 1
    assert events[0]["action"] == "processing.compile.completed"
    assert events[0]["target_id"] == "doc-1"
    assert events[0]["payload"]["processor_kind"] == "mock.compiler"
    assert len(events[0]["payload"]["artifact_ids"]) == 1


def test_compile_failure_captured_consistently(
    processing_service, workspace, ctx, artifact_registry
):
    compiler = _MockCompiler(raise_exc=RuntimeError("boom"))
    result = processing_service.compile(ctx, compiler, _document(ctx))

    assert result.status is ResultStatus.FAILED
    assert result.error == "boom"
    assert result.message == "RuntimeError"
    # Nothing registered.
    assert artifact_registry.list_artifacts(ctx) == []
    # Failure was audited.
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "processing.compile.failed"
    assert events[0]["payload"]["error"] == "boom"
    assert events[0]["payload"]["error_type"] == "RuntimeError"


def test_compile_records_cost_events(processing_service, workspace, ctx):
    breakdown = CostBreakdown(
        vendor="anthropic",
        model="claude-sonnet-4-6",
        unit_kind="input_tokens",
        units=500,
        amount=Decimal("0.0050"),
    )
    drafts = [ArtifactDraft(kind="compiled.text", content=b"x", suggested_extension=".txt")]
    processing_service.compile(
        ctx, _MockCompiler(drafts=drafts, costs=[breakdown]), _document(ctx)
    )
    costs = _read_costs(workspace, ctx)
    assert len(costs) == 1
    assert costs[0]["vendor"] == "anthropic"
    assert costs[0]["units"] == 500


def test_compile_with_no_drafts_still_audits(processing_service, workspace, ctx):
    result = processing_service.compile(ctx, _MockCompiler(), _document(ctx))
    assert result.status is ResultStatus.SUCCEEDED
    assert result.artifacts == []
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "processing.compile.completed"
    assert events[0]["payload"]["artifact_ids"] == []


def test_compile_correlation_id_propagates(processing_service, workspace, ctx):
    drafts = [ArtifactDraft(kind="compiled.text", content=b"x")]
    processing_service.compile(
        ctx,
        _MockCompiler(drafts=drafts),
        _document(ctx),
        correlation_id="run-7",
    )
    assert _read_audit(workspace, ctx)[0]["correlation_id"] == "run-7"


# Enrich


def test_enrich_registers_into_enriched_area(processing_service, workspace, ctx):
    drafts = [ArtifactDraft(kind="enriched.entities", content=b'{"e":1}', suggested_extension=".json")]
    processor = _MockEnricher(drafts=drafts)
    result = processing_service.enrich(ctx, processor, _artifact(ctx))
    assert result.status is ResultStatus.SUCCEEDED
    assert result.artifacts[0].location.startswith("enriched/")
    assert result.artifacts[0].source_artifact_ids == ["art-1"]


def test_enrich_failure_audited(processing_service, workspace, ctx):
    processor = _MockEnricher(raise_exc=ValueError("bad"))
    result = processing_service.enrich(ctx, processor, _artifact(ctx))
    assert result.status is ResultStatus.FAILED
    assert _read_audit(workspace, ctx)[0]["action"] == "processing.enrich.failed"


# Graph


def test_build_graph_registers_into_graph_area(processing_service, workspace, ctx):
    drafts = [ArtifactDraft(kind="graph.entities", content=b"<graph/>", suggested_extension=".xml")]
    builder = _MockGraphBuilder(drafts=drafts)
    result = processing_service.build_graph(ctx, builder, ["art-1", "art-2"])
    assert result.status is ResultStatus.SUCCEEDED
    assert result.artifacts[0].location.startswith("graph/")
    assert result.artifacts[0].source_artifact_ids == ["art-1", "art-2"]


def test_build_graph_failure_audited(processing_service, workspace, ctx):
    builder = _MockGraphBuilder(raise_exc=RuntimeError("nope"))
    result = processing_service.build_graph(ctx, builder, ["a"])
    assert result.status is ResultStatus.FAILED
    assert _read_audit(workspace, ctx)[0]["action"] == "processing.graph.failed"


# Index


def test_index_returns_processing_result(processing_service, workspace, ctx):
    indexer = _MockIndexer()
    result = processing_service.index(ctx, indexer, ["a", "b"])
    assert isinstance(result, ProcessingResult)
    assert result.status is ResultStatus.SUCCEEDED
    assert result.metadata["indexed"] == 2
    events = _read_audit(workspace, ctx)
    assert events[0]["action"] == "processing.index.completed"


def test_index_failure_captured(processing_service, workspace, ctx):
    indexer = _MockIndexer(raise_exc=RuntimeError("offline"))
    result = processing_service.index(ctx, indexer, ["a"])
    assert result.status is ResultStatus.FAILED
    assert result.error == "offline"
    assert _read_audit(workspace, ctx)[0]["action"] == "processing.index.failed"


# Query


def test_query_returns_query_result_and_records_costs(
    processing_service, workspace, ctx
):
    cost = CostBreakdown(
        vendor="anthropic",
        model="claude-sonnet-4-6",
        unit_kind="input_tokens",
        units=42,
        amount=Decimal("0.0010"),
    )
    provider = _MockQueryProvider(
        answer="42", citations=["doc-1", "art-9"], costs=[cost]
    )
    result = processing_service.query(ctx, provider, "what is the answer?")
    assert isinstance(result, QueryResult)
    assert result.answer == "42"
    assert result.citations == ["doc-1", "art-9"]
    assert _read_costs(workspace, ctx)[0]["units"] == 42
    assert _read_audit(workspace, ctx)[0]["action"] == "processing.query.completed"


def test_query_failure_captured(processing_service, workspace, ctx):
    provider = _MockQueryProvider(raise_exc=RuntimeError("no llm"))
    result = processing_service.query(ctx, provider, "anything?")
    assert result.status is ResultStatus.FAILED
    assert result.error == "no llm"
    assert _read_audit(workspace, ctx)[0]["action"] == "processing.query.failed"


# Cross-cutting


def test_processor_passes_only_ids_not_records(processing_service, ctx):
    compiler = _MockCompiler()
    document = _document(ctx)
    processing_service.compile(ctx, compiler, document)
    received_ctx, received_id = compiler.calls[0]
    assert received_ctx is ctx
    assert received_id == document.document_id
    # The processor never sees a Path or DocumentRecord.
    assert isinstance(received_id, str)


def test_artifact_record_does_not_leak_absolute_paths(processing_service, ctx):
    drafts = [ArtifactDraft(kind="compiled.text", content=b"x", suggested_extension=".txt")]
    result = processing_service.compile(ctx, _MockCompiler(drafts=drafts), _document(ctx))
    record = result.artifacts[0]
    # location is workspace-relative, not absolute.
    assert not record.location.startswith("/")
    assert ".." not in record.location


@pytest.mark.parametrize(
    "method_name",
    ["compile", "enrich", "build_graph", "index", "query"],
)
def test_service_exposes_all_capability_methods(processing_service, method_name):
    assert callable(getattr(processing_service, method_name))
