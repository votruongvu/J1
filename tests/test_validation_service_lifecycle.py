"""Tests for Phase 2 service methods on `IngestionValidationService`.

Covers the generate / list / get / run methods. The Phase 1 manual-
test-query tests live in `test_validation_service.py`; the runner
+ generator + check primitives have their own focused suites.
These tests verify the service-level wiring:

  * ownership gates fire on every method
  * idempotency on regeneration
  * lifecycle persistence (pending → running → completed snapshots
    all land in the run store)
  * audit events fire
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.recorder import DefaultAuditRecorder
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
from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus
from j1.search import SqliteSearchIndexer
from j1.validation import (
    DefaultTestCaseGenerator,
    IngestionValidationService,
    JsonlValidationRunStore,
    JsonlValidationSetStore,
)
from j1.workspace.layout import WorkspaceArea


# ---- Fixtures -------------------------------------------------------


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def indexer(workspace, artifact_registry, registry):
    return SqliteSearchIndexer(workspace, artifact_registry, registry)


@pytest.fixture
def query_engine(workspace, artifact_registry, registry, indexer):
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
def set_store(workspace) -> JsonlValidationSetStore:
    return JsonlValidationSetStore(workspace)


@pytest.fixture
def vrun_store(workspace) -> JsonlValidationRunStore:
    return JsonlValidationRunStore(workspace)


@pytest.fixture
def audit_recorder(audit_sink) -> DefaultAuditRecorder:
    return DefaultAuditRecorder(audit_sink)


@pytest.fixture
def service(
    run_store, artifact_registry, query_engine, audit_recorder,
    workspace, set_store, vrun_store,
) -> IngestionValidationService:
    """Phase-2-complete service: every Phase 2 dependency wired so
    generate / list / run all work."""
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=audit_recorder,
        workspace=workspace,
        validation_set_store=set_store,
        validation_run_store=vrun_store,
        test_case_generator=DefaultTestCaseGenerator(),  # heuristic, no LLM
    )


# ---- Helpers --------------------------------------------------------


def _make_run(
    *, run_id: str = "run-1", document_id: str = "doc-1",
) -> IngestionRun:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf",
        workflow_run_id="wfr",
        status=RunStatus.SUCCEEDED,
        started_at=started,
        updated_at=started + timedelta(seconds=5),
        completed_at=started + timedelta(seconds=5),
    )


def _stage_chunk(
    workspace, ctx, artifact_registry, indexer,
    *,
    artifact_id: str,
    content: bytes,
    run_id: str,
    chunk_id: str,
):
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.json"
    # Generator reads chunk *body* via the projector. Producer
    # convention: a `kind="chunk"` artifact is JSON `{"chunkId",
    # "body", ...}`. Mirror that here.
    import json
    payload = {"chunkId": chunk_id, "body": content.decode("utf-8")}
    (area_dir / stored).write_bytes(json.dumps(payload).encode("utf-8"))
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
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={"run_id": run_id, "chunk_id": chunk_id},
    )
    artifact_registry.add(record)
    indexer.index(ctx, [artifact_id])
    return record


# ---- generate_validation_set ----------------------------------------


def test_generate_set_persists_and_returns_dto(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    set_store,
):
    """Happy path: generate writes the set to the store and returns
    the same DTO the FE will receive on the response."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"The proposal is due 20 May 2026.",
        run_id="run-1", chunk_id="chunk-A",
    )

    vset = service.generate_validation_set(ctx, "run-1", actor="tester")

    assert vset.run_id == "run-1"
    assert vset.source == "generated"
    assert vset.status == "draft"
    assert vset.created_by == "tester"
    # Smoke + chunk-derived case at minimum.
    assert len(vset.test_cases) >= 2
    # Persisted: a fetch by id round-trips.
    fetched = set_store.get(ctx, vset.validation_set_id)
    assert fetched is not None
    assert fetched.validation_set_id == vset.validation_set_id


def test_generate_set_is_idempotent_on_same_artifacts(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Re-generating with the same chunks reuses the existing set
    (hash match). This keeps the FE's "Generate" button safe to
    click twice without producing a confusing duplicate set."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="chunk-A",
    )

    first = service.generate_validation_set(ctx, "run-1")
    second = service.generate_validation_set(ctx, "run-1")

    assert first.validation_set_id == second.validation_set_id


def test_generate_set_force_bypasses_cache(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """`force=True` short-circuits the idempotency cache so a tester
    can explicitly request a fresh generation (e.g. after editing
    the prompt). New set id, same content."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="chunk-A",
    )

    first = service.generate_validation_set(ctx, "run-1")
    forced = service.generate_validation_set(ctx, "run-1", force=True)

    assert first.validation_set_id != forced.validation_set_id


def test_generate_set_caps_max_cases(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Service-side cap matches the runner's MAX_CASES_PER_RUN.
    Defends against a tester accidentally requesting 1000 cases."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    for i in range(20):
        _stage_chunk(
            workspace, ctx, artifact_registry, indexer,
            artifact_id=f"a-{i}", content=f"chunk content {i}".encode("utf-8"),
            run_id="run-1", chunk_id=f"chunk-{i}",
        )

    vset = service.generate_validation_set(ctx, "run-1", max_cases=999)

    # MAX_CASES_PER_RUN = 50 — service must clamp.
    assert len(vset.test_cases) <= 50


def test_generate_set_unknown_run_raises_review_not_found(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.generate_validation_set(ctx, "ghost")


def test_generate_set_cross_tenant_raises_review_not_found(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    other = ProjectContext(tenant_id="enemy", project_id=ctx.project_id)
    with pytest.raises(ReviewNotFound):
        service.generate_validation_set(other, "run-1")


def test_generate_set_emits_audit_event(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Tester actions must surface in the audit log so the run's
    /events SSE stream picks them up. Locks the action name + the
    targetId binding."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="chunk-A",
    )

    vset = service.generate_validation_set(ctx, "run-1", actor="tester")

    audit_path = workspace.audit(ctx) / "events.jsonl"
    log = audit_path.read_text(encoding="utf-8")
    assert "j1.validation.set_generated" in log
    assert vset.validation_set_id in log


# ---- list / get sets ------------------------------------------------


def test_list_validation_sets_orders_recent_first(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    set_store,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="c-1",
    )
    older = service.generate_validation_set(ctx, "run-1", force=True)
    newer = service.generate_validation_set(ctx, "run-1", force=True)

    sets = service.list_validation_sets(ctx, "run-1")
    ids = [v.validation_set_id for v in sets]
    # Newer first (most-recent wins on the FE list view).
    assert ids[0] == newer.validation_set_id
    assert older.validation_set_id in ids


def test_list_validation_sets_unknown_run_raises_review_not_found(service, ctx):
    """Missing run must 404 — never return [] (which would leak
    existence: 'this run isn't yours' vs. 'this run has no sets')."""
    with pytest.raises(ReviewNotFound):
        service.list_validation_sets(ctx, "ghost")


def test_get_validation_set_unknown_set_raises(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    with pytest.raises(ReviewNotFound):
        service.get_validation_set(ctx, "run-1", "vs-does-not-exist")


def test_get_validation_set_for_wrong_run_raises(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """A set written for run-A must not be fetchable via run-B's
    URL — defense-in-depth on top of run ownership."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    run_store.upsert(ctx, _make_run(run_id="run-B"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-A", chunk_id="c-1",
    )

    vset = service.generate_validation_set(ctx, "run-A")

    with pytest.raises(ReviewNotFound):
        service.get_validation_set(ctx, "run-B", vset.validation_set_id)


# ---- run_validation -------------------------------------------------


def test_run_validation_persists_lifecycle_snapshots(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    vrun_store,
):
    """The runner emits three lifecycle states; the store should
    have all three written. A FE polling /list_validation_runs
    after the call sees the terminal record (latest wins)."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha keyword in chunk",
        run_id="run-1", chunk_id="c-1",
    )
    vset = service.generate_validation_set(ctx, "run-1")

    vrun = service.run_validation(ctx, "run-1", vset.validation_set_id)

    # Terminal snapshot returned to caller.
    assert vrun.execution_status == "completed"
    # Stored snapshot reflects the same terminal state (latest wins).
    fetched = vrun_store.get(ctx, vrun.validation_run_id)
    assert fetched is not None
    assert fetched.execution_status == "completed"

    # The JSONL file should have multiple snapshots — one per
    # lifecycle transition. We assert on line count to lock the
    # contract that persistence happens at every transition.
    path = workspace.validation(ctx) / "validation_runs.jsonl"
    line_count = sum(1 for _ in path.read_text().splitlines() if _.strip())
    assert line_count >= 2  # at least pending + terminal


def test_run_validation_unknown_set_raises_review_not_found(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    with pytest.raises(ReviewNotFound):
        service.run_validation(ctx, "run-1", "vs-ghost")


def test_run_validation_cross_tenant_raises_review_not_found(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="c-1",
    )
    vset = service.generate_validation_set(ctx, "run-1")

    other = ProjectContext(tenant_id="enemy", project_id=ctx.project_id)
    with pytest.raises(ReviewNotFound):
        service.run_validation(other, "run-1", vset.validation_set_id)


def test_run_validation_emits_completion_audit_event(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", content=b"alpha",
        run_id="run-1", chunk_id="c-1",
    )
    vset = service.generate_validation_set(ctx, "run-1")

    vrun = service.run_validation(ctx, "run-1", vset.validation_set_id)

    audit_log = (workspace.audit(ctx) / "events.jsonl").read_text()
    assert "j1.validation.run_completed" in audit_log
    assert vrun.validation_run_id in audit_log


# ---- list / get validation runs ------------------------------------


def test_list_validation_runs_filters_to_run(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Listing under run-A must not surface validation runs that
    belong to run-B, even within the same project."""
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    run_store.upsert(ctx, _make_run(run_id="run-B"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-A", content=b"alpha",
        run_id="run-A", chunk_id="c-A",
    )
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-B", content=b"alpha",
        run_id="run-B", chunk_id="c-B",
    )
    vset_a = service.generate_validation_set(ctx, "run-A")
    vset_b = service.generate_validation_set(ctx, "run-B")
    service.run_validation(ctx, "run-A", vset_a.validation_set_id)
    service.run_validation(ctx, "run-B", vset_b.validation_set_id)

    runs_for_a = service.list_validation_runs(ctx, "run-A")
    runs_for_b = service.list_validation_runs(ctx, "run-B")

    assert len(runs_for_a) == 1
    assert runs_for_a[0].run_id == "run-A"
    assert len(runs_for_b) == 1
    assert runs_for_b[0].run_id == "run-B"


def test_get_validation_run_for_wrong_run_raises(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    run_store.upsert(ctx, _make_run(run_id="run-A"))
    run_store.upsert(ctx, _make_run(run_id="run-B"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-A", content=b"alpha",
        run_id="run-A", chunk_id="c-A",
    )
    vset_a = service.generate_validation_set(ctx, "run-A")
    vrun_a = service.run_validation(ctx, "run-A", vset_a.validation_set_id)

    with pytest.raises(ReviewNotFound):
        service.get_validation_run(ctx, "run-B", vrun_a.validation_run_id)


# ---- Phase 1 / Phase 2 deps independence ---------------------------


def test_phase_1_only_construction_still_works(
    run_store, artifact_registry, query_engine, audit_recorder, ctx,
):
    """A Phase 1-only deployment (no validation_set_store /
    validation_run_store / generator) must still ship — manual
    test query is the only surface available, and that's the
    Phase 1 contract."""
    svc = IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=audit_recorder,
    )
    run_store.upsert(ctx, _make_run(run_id="run-1"))

    # Manual query still works.
    from j1.validation import ManualTestQueryRequest
    response = svc.run_manual_test_query(
        ctx, "run-1", ManualTestQueryRequest(question="anything"),
    )
    assert response is not None

    # Phase 2 generation 503-equivalent: explicit RuntimeError.
    with pytest.raises(RuntimeError, match="not configured"):
        svc.generate_validation_set(ctx, "run-1")
    with pytest.raises(RuntimeError, match="not configured"):
        svc.run_validation(ctx, "run-1", "vs-1")

    # Phase 2 read methods degrade gracefully (empty list).
    assert svc.list_validation_sets(ctx, "run-1") == []
    assert svc.list_validation_runs(ctx, "run-1") == []
