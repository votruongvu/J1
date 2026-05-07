"""Tests for `IngestionValidationService.run_manual_test_query`.

Phase 1 contract: stateless (no validation store), uses the existing
`HybridQueryEngine` with `RunScope` to filter retrieval, returns a
deterministic check report with split execution / validation status.

The service is exercised end-to-end against a real
`SqliteSearchIndexer`-backed engine so the run-scope plumbing is
verified all the way to the SQL layer. LLM calls are not made (the
default `KnowledgeQueryProvider` composes its answer from search hits
deterministically).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.ingestion_review.exceptions import ReviewNotFound
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.query.classifier import QueryIntentClassifier
from j1.query.engine import HybridQueryEngine
from j1.query.providers import (
    ConsistencyProvider,
    EvidenceProvider,
    GraphQueryProvider,
    KnowledgeQueryProvider,
    ReportGenerator,
)
from j1.runs import JsonlIngestionRunStore
from j1.runs.models import IngestionRun, RunStatus
from j1.search import SqliteSearchIndexer
from j1.validation import IngestionValidationService, ManualTestQueryRequest
from j1.workspace.layout import WorkspaceArea


# ---- Fixtures + helpers -------------------------------------------------


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def indexer(workspace, artifact_registry, registry) -> SqliteSearchIndexer:
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, indexer):
    """Real engine wired against the in-memory test workspace.

    `default_profile` would be a Profile fixture; ReportGenerator
    needs one but we're not driving the report mode here so a stub
    Profile suffices.
    """
    from types import SimpleNamespace
    profile_stub = SimpleNamespace(report_templates={})
    return HybridQueryEngine(
        classifier=QueryIntentClassifier(),
        knowledge_provider=KnowledgeQueryProvider(indexer),
        graph_provider=GraphQueryProvider(artifact_registry, workspace),
        evidence_provider=EvidenceProvider(indexer, registry),
        consistency_provider=ConsistencyProvider(artifact_registry, workspace),
        report_generator=ReportGenerator(indexer, profile_stub),
    )


@pytest.fixture
def audit_recorder(audit_sink) -> DefaultAuditRecorder:
    return DefaultAuditRecorder(audit_sink)


@pytest.fixture
def service(
    run_store, artifact_registry, query_engine, audit_recorder,
) -> IngestionValidationService:
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=audit_recorder,
    )


def _make_run(
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    status: RunStatus = RunStatus.SUCCEEDED,
) -> IngestionRun:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf",
        workflow_run_id="wfr",
        status=status,
        started_at=started,
        updated_at=started + timedelta(seconds=10),
        completed_at=started + timedelta(seconds=10),
        warning_count=0,
    )


def _index_chunk(
    workspace, ctx, artifact_registry, indexer,
    *, artifact_id: str, content: bytes, run_id: str, chunk_id: str,
) -> ArtifactRecord:
    """Stage a chunk-kind artifact + index it in one shot. Mirrors
    what the production compile pipeline produces (one chunk = one
    `ArtifactRecord` with `metadata.run_id` + `metadata.chunk_id`)."""
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[],
        source_artifact_ids=[],
        metadata={"run_id": run_id, "chunk_id": chunk_id},
    )
    artifact_registry.add(record)
    indexer.index(ctx, [artifact_id])
    return record


# ---- Happy path ---------------------------------------------------------


def test_manual_test_query_returns_run_scoped_results(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """End-to-end: a question against run-A gets only run-A's chunks
    in retrievedChunks + citations, and the deterministic checks
    aggregate to `passed`."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    run_store.upsert(ctx, _make_run(run_id="run-B"))
    _index_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-A1", content=b"shared keyword from run A",
        run_id="run-A", chunk_id="chunk-A1",
    )
    _index_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-B1", content=b"shared keyword from run B",
        run_id="run-B", chunk_id="chunk-B1",
    )

    response = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(question="shared keyword", top_k=5),
    )

    assert response.run_id == "run-A"
    # Server-derived run_id and chunk_id round-trip on every retrieved
    # chunk and citation.
    assert {c.run_id for c in response.retrieved_chunks} == {"run-A"}
    assert {c.chunk_id for c in response.retrieved_chunks} == {"chunk-A1"}
    assert all(c["runId"] == "run-A" for c in response.citations)
    assert all(c["chunkId"] == "chunk-A1" for c in response.citations)
    # Engine actually produced an answer, all required checks pass.
    assert response.answer
    assert response.validation_status == "passed"
    # Every check we ship in Phase 1 must be present (modulo the
    # conditional citation_present which is skipped when not required).
    names = {c.name for c in response.checks}
    assert "answer_non_empty" in names
    assert "retrieved_chunks_present" in names
    assert "retrieved_chunks_belong_to_run" in names
    assert "citations_belong_to_run" in names
    assert "no_cross_tenant_or_cross_project_leak" in names
    assert "citation_present" not in names  # not requested


def test_manual_test_query_request_id_round_trips_to_audit(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    audit_sink,
):
    """The `requestId` returned to the FE is the same id that lands
    in the audit log, so operators can correlate post-hoc."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _index_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world",
        run_id="run-A", chunk_id="c-1",
    )

    response = service.run_manual_test_query(
        ctx, "run-A", ManualTestQueryRequest(question="hello"),
    )

    audit_path = workspace.audit(ctx) / "events.jsonl"
    log_text = audit_path.read_text(encoding="utf-8")
    assert response.request_id in log_text
    assert "j1.validation.manual_query.completed" in log_text


# ---- Run ownership / cross-tenant -------------------------------------


def test_manual_test_query_unknown_run_raises_review_not_found(
    service, ctx,
):
    """Run that doesn't exist → ReviewNotFound (REST will map to 404).
    Identical message regardless of whether the run is missing or
    belongs to another tenant — existence must not be probeable."""
    with pytest.raises(ReviewNotFound):
        service.run_manual_test_query(
            ctx, "run-does-not-exist",
            ManualTestQueryRequest(question="hello"),
        )


def test_manual_test_query_cross_project_run_raises_review_not_found(
    service, run_store, ctx,
):
    """A run that exists in `(acme, alpha)` is invisible from
    `(acme, beta)` — cross-project access raises the same
    `ReviewNotFound` as a missing run."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    other_ctx = ProjectContext(tenant_id=ctx.tenant_id, project_id="some-other-project")

    with pytest.raises(ReviewNotFound):
        service.run_manual_test_query(
            other_ctx, "run-A",
            ManualTestQueryRequest(question="hello"),
        )


def test_manual_test_query_cross_tenant_run_raises_review_not_found(
    service, run_store, ctx,
):
    """Cross-tenant access — same shape as cross-project."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    other_ctx = ProjectContext(tenant_id="enemy-tenant", project_id=ctx.project_id)

    with pytest.raises(ReviewNotFound):
        service.run_manual_test_query(
            other_ctx, "run-A",
            ManualTestQueryRequest(question="hello"),
        )


# ---- Status / check aggregation ----------------------------------------


def test_manual_test_query_no_results_yields_failed_status(
    service, run_store, ctx,
):
    """A run with NO indexed artifacts → engine returns no sources →
    `retrieved_chunks_present` fails → validationStatus = failed.
    HTTP execution is still 200 (the engine ran successfully).
    This is the canonical 'split status' demonstration."""
    run_store.upsert(ctx, _make_run(run_id="run-empty"))

    response = service.run_manual_test_query(
        ctx, "run-empty",
        ManualTestQueryRequest(question="anything"),
    )

    assert response.validation_status == "failed"
    chunks_check = next(
        c for c in response.checks if c.name == "retrieved_chunks_present"
    )
    assert chunks_check.passed is False


def test_manual_test_query_citation_required_drives_check(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """When `citationRequired=true`, the citation_present check is
    added to the suite. With chunks indexed it should pass; we test
    the inclusion+pass shape here. The fail-shape is in the unit
    suite for the check."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _index_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world",
        run_id="run-A", chunk_id="c-1",
    )

    response = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(
            question="hello", citation_required=True,
        ),
    )

    names = {c.name for c in response.checks}
    assert "citation_present" in names
    assert response.validation_status == "passed"


# ---- Raw response toggle -----------------------------------------------


def test_manual_test_query_include_raw_attaches_debug_payload(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """`includeRaw=true` populates `rawResponse` with a JSON-friendly
    projection of the engine output. Off by default — raw payloads
    can be verbose and most testers only need the rendered view."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    _index_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"hello world",
        run_id="run-A", chunk_id="c-1",
    )

    on = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(question="hello", include_raw=True),
    )
    off = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(question="hello", include_raw=False),
    )

    assert on.raw_response is not None
    assert "answer" in on.raw_response
    assert "sources" in on.raw_response
    assert off.raw_response is None


# ---- top_k clamping ----------------------------------------------------


def test_manual_test_query_clamps_top_k(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Hard cap defends against a tester accidentally requesting
    10k chunks. We can't directly assert on the internal QueryRequest,
    but we can verify the response's chunk count is bounded by the
    cap (≤50 in Phase 1)."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    # Stage many chunks so the indexer would happily return more if
    # asked. Each gets its own artifact + chunk_id.
    for i in range(60):
        _index_chunk(
            workspace, ctx, artifact_registry, indexer,
            artifact_id=f"a-{i}", content=b"alpha",
            run_id="run-A", chunk_id=f"c-{i}",
        )

    response = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(question="alpha", top_k=999),
    )

    assert len(response.retrieved_chunks) <= 50


def test_manual_test_query_inconclusive_on_engine_failure(
    service, run_store, ctx, monkeypatch,
):
    """Engine raising mid-flight produces an `inconclusive`
    response, not a 500. The FE renders 'couldn't determine' so
    operators don't read it as a real validation fail."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))

    def _boom(self, ctx, request):
        raise RuntimeError("simulated engine failure")

    # Monkeypatch the live engine on the live service — a fresh
    # construction each test means this stays scoped to the test.
    monkeypatch.setattr(
        type(service._query_engine), "query", _boom,
    )

    response = service.run_manual_test_query(
        ctx, "run-A",
        ManualTestQueryRequest(question="hello"),
    )

    assert response.validation_status == "inconclusive"
    assert response.answer == ""
    assert any(
        c.name == "engine_invocation" and not c.passed
        for c in response.checks
    )
