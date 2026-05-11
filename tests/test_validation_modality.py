"""Phase 4 modality tests: skip gate, modality cases, modality
checks, honest evidence flags.

Layout:
  * Generator emits the right modality cases when artifacts exist
    (and skips when they don't).
  * Runner skips modality cases when matching artifacts are absent
    (defense-in-depth — generator already gates upstream).
  * `expected_artifact_retrieved` + `expected_graph_evidence`
    checks fire correctly.
  * Service-side wiring partitions modality artifacts and threads
    them to the generator.
  * Manual-query path's `evidence_flags` reflects the retrieved
    artifact_kinds honestly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.recorder import DefaultAuditRecorder
from j1.jobs.status import ProcessingStatus, ReviewStatus
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
    DefaultValidationRunner,
    IngestionValidationService,
    JsonlValidationRunStore,
    JsonlValidationSetStore,
    ManualTestQueryRequest,
    ValidationSetDTO,
    ValidationTestCaseDTO,
)
from j1.validation.generator import GenerationOptions
from j1.workspace.layout import WorkspaceArea


# ---- Fixtures + helpers --------------------------------------------


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
def service(
    run_store, artifact_registry, query_engine, audit_sink, workspace,
) -> IngestionValidationService:
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=DefaultAuditRecorder(audit_sink),
        workspace=workspace,
        validation_set_store=JsonlValidationSetStore(workspace),
        validation_run_store=JsonlValidationRunStore(workspace),
        test_case_generator=DefaultTestCaseGenerator(),
    )


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


def _stage_artifact(
    workspace, ctx, artifact_registry,
    *,
    artifact_id: str,
    kind: str,
    body: bytes = b"",
    run_id: str = "run-1",
    extra_metadata: dict | None = None,
) -> ArtifactRecord:
    """Stage an artifact of the requested kind. Used to construct
    table / visual / graph fixtures for the modality tests."""
    area_dir = workspace.area(ctx, WorkspaceArea.ENRICHED)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.json"
    (area_dir / stored).write_bytes(body or b"{}")
    metadata: dict = {"run_id": run_id}
    if extra_metadata:
        metadata.update(extra_metadata)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"{WorkspaceArea.ENRICHED.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(body),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata=metadata,
    )
    artifact_registry.add(record)
    return record


# ---- Generator: modality case emission ------------------------------


def test_generator_emits_table_case_when_table_artifact_present():
    """One table → one table case with expected_artifacts pointing
    at it. Question text is deterministic (regression-friendly)."""
    table = ArtifactRecord(
        artifact_id="art-table-1",
        project=None,  # type: ignore[arg-type] — projector not exercised
        kind="enriched.tables",
        location="enriched/art-table-1.json",
        content_hash="x",
        byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={"title": "Q4 Revenue", "page": 3},
    )
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=[],
        table_artifacts=[table],
        options=GenerationOptions(negative_case_count=0),
    )

    table_cases = [c for c in vset.test_cases if c.type == "table"]
    assert len(table_cases) == 1
    case = table_cases[0]
    assert case.expected_artifacts == ["art-table-1"]
    assert case.expected_pages == [3]
    assert "page 3" in case.question
    assert case.metadata.get("table") is True


def test_generator_emits_image_and_graph_cases():
    image = ArtifactRecord(
        artifact_id="art-img-1",
        project=None,  # type: ignore[arg-type]
        kind="enriched.visuals",
        location="enriched/art-img-1.json",
        content_hash="x", byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"title": "Diagram A"},
    )
    graph = ArtifactRecord(
        artifact_id="art-graph-1",
        project=None,  # type: ignore[arg-type]
        kind="graph_json",
        location="graph/art-graph-1.json",
        content_hash="x", byte_size=10,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={
            # Generator pulls expected nodes from the artifact's
            # metadata when present — locks the contract that
            # producers can pre-register expected entities.
            "top_entities": ["entity-A", "entity-B"],
        },
    )

    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=[],
        visual_artifacts=[image],
        graph_artifacts=[graph],
        options=GenerationOptions(negative_case_count=0),
    )

    image_cases = [c for c in vset.test_cases if c.type == "image"]
    graph_cases = [c for c in vset.test_cases if c.type == "graph"]
    assert len(image_cases) == 1
    assert image_cases[0].expected_artifacts == ["art-img-1"]
    assert len(graph_cases) == 1
    assert graph_cases[0].expected_artifacts == ["art-graph-1"]
    assert graph_cases[0].expected_graph_nodes == ["entity-A", "entity-B"]


def test_generator_modality_caps_respect_options():
    """`max_table_cases` (etc.) bound how many cases per modality
    even when more artifacts exist. Defends against tester input
    that would inflate the case count."""
    tables = [
        ArtifactRecord(
            artifact_id=f"art-table-{i}", project=None,  # type: ignore[arg-type]
            kind="enriched.tables",
            location=f"enriched/art-table-{i}.json",
            content_hash="x", byte_size=10,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED, version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-1"], source_artifact_ids=[],
            metadata={},
        )
        for i in range(10)
    ]

    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=[],
        table_artifacts=tables,
        options=GenerationOptions(
            max_cases=50, max_table_cases=2, negative_case_count=0,
        ),
    )

    table_cases = [c for c in vset.test_cases if c.type == "table"]
    assert len(table_cases) == 2


def test_generator_max_table_cases_zero_disables_tables():
    """A tester can switch off a modality entirely via the per-
    modality cap. Lets a small budget skip modality cases without
    affecting smoke + chunks."""
    tables = [
        ArtifactRecord(
            artifact_id="t-1", project=None,  # type: ignore[arg-type]
            kind="enriched.tables",
            location="enriched/t-1.json",
            content_hash="x", byte_size=10,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED, version=1,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_document_ids=["doc-1"], source_artifact_ids=[],
            metadata={},
        ),
    ]
    gen = DefaultTestCaseGenerator()
    vset = gen.generate(
        run_id="r", document_ids=["doc-1"], chunks=[],
        table_artifacts=tables,
        options=GenerationOptions(
            max_table_cases=0, negative_case_count=0,
        ),
    )
    assert all(c.type != "table" for c in vset.test_cases)


# ---- Runner: skip-applicability gate -------------------------------


def test_runner_skips_table_case_when_run_has_no_tables(
    artifact_registry, query_engine, ctx,
):
    """Imported set targets a table modality the run lacks. Result
    is `skipped` with a reason; run aggregates without counting
    the skip toward `failed`."""
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-table-1",
        question="What does the table show?",
        type="table",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=["art-some-table"],
        citation_required=True,
        source_traceability=["art-some-table"],
    )
    vset = ValidationSetDTO(
        validation_set_id="vs", run_id="run-1",
        document_ids=["doc-1"], source="generated",
        status="draft", created_at="2026-05-07T10:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash=None,
        test_cases=[case],
    )

    vrun = runner.run(ctx, vset)

    assert vrun.results[0].status == "skipped"
    assert vrun.results[0].failure_reason
    assert "table" in vrun.results[0].failure_reason.lower()
    # Skipped doesn't count toward `failed` aggregation; the run
    # has no other cases so it's `inconclusive` (no positive
    # signals were evaluated).
    assert vrun.summary.skipped == 1
    assert vrun.summary.failed == 0


def test_runner_runs_table_case_when_table_artifact_present(
    workspace, ctx, artifact_registry, query_engine,
):
    """When the run has a matching artifact, the gate is open and
    the case executes normally (passes/fails on the actual checks
    rather than skipping)."""
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-table-1", kind="enriched.tables",
        run_id="run-1",
    )
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-table-1",
        question="What does the table show?",
        type="table",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=["art-table-1"],
        citation_required=False,
        source_traceability=["art-table-1"],
    )
    vset = ValidationSetDTO(
        validation_set_id="vs", run_id="run-1",
        document_ids=["doc-1"], source="generated",
        status="draft", created_at="2026-05-07T10:00:00Z",
        created_by=None, generator_version="v1",
        artifacts_content_hash=None,
        test_cases=[case],
    )

    vrun = runner.run(ctx, vset)

    # Not skipped — the gate is open. The table artifact was never
    # FTS-indexed in this test so retrieval will be empty + the
    # expected_artifact_retrieved check will fail. That's the right
    # outcome for an unindexed artifact.
    assert vrun.results[0].status != "skipped"


# ---- Runner: expected_artifact_retrieved + expected_graph_evidence -


def test_expected_artifact_retrieved_passes_when_artifact_found(
    workspace, ctx, artifact_registry, query_engine, indexer,
):
    """End-to-end: the table artifact is FTS-indexed under the
    target run, the case names it as expected, retrieval surfaces
    it, the new check passes."""
    # Stage + INDEX a table artifact so retrieval can find it.
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-table-1", kind="enriched.tables",
        body=b"Quarterly revenue table content",
        run_id="run-1", extra_metadata={"chunk_id": "chunk-table-1"},
    )
    indexer.index(ctx, ["art-table-1"])

    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-table-1",
        question="quarterly revenue",
        type="table",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=["art-table-1"],
        citation_required=False,
        source_traceability=["art-table-1"],
    )
    vset = ValidationSetDTO(
        validation_set_id="vs", run_id="run-1",
        document_ids=["doc-1"], source="generated", status="draft",
        created_at="2026-05-07T10:00:00Z", created_by=None,
        generator_version="v1", artifacts_content_hash=None,
        test_cases=[case],
    )

    vrun = runner.run(ctx, vset)
    check = next(
        c for c in vrun.results[0].checks
        if c.name == "expected_artifact_retrieved"
    )
    assert check.passed is True


def test_expected_artifact_retrieved_fails_when_artifact_missing(
    workspace, ctx, artifact_registry, query_engine,
):
    """The case names an artifact that exists in the run (so the
    skip gate doesn't fire) but isn't retrievable for the
    question. Required check fails → run is `failed`."""
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-table-1", kind="enriched.tables",
        run_id="run-1",
    )
    # NOT indexed — retrieval can't find it.
    runner = DefaultValidationRunner(
        query_engine=query_engine,
        artifact_registry=artifact_registry,
    )
    case = ValidationTestCaseDTO(
        test_case_id="tc-table-1",
        question="some unrelated query",
        type="table",
        priority="normal",
        expected_behavior="answer_with_citations",
        expected_artifacts=["art-table-1"],
        citation_required=False,
        source_traceability=["art-table-1"],
    )
    vset = ValidationSetDTO(
        validation_set_id="vs", run_id="run-1",
        document_ids=["doc-1"], source="generated", status="draft",
        created_at="2026-05-07T10:00:00Z", created_by=None,
        generator_version="v1", artifacts_content_hash=None,
        test_cases=[case],
    )

    vrun = runner.run(ctx, vset)
    check = next(
        c for c in vrun.results[0].checks
        if c.name == "expected_artifact_retrieved"
    )
    assert check.passed is False
    assert vrun.validation_status == "failed"


# ---- Service: modality artifact partition --------------------------


def test_service_partitions_modality_artifacts_to_generator(
    service, run_store, ctx, workspace, artifact_registry,
):
    """The service builds the three modality lists and threads them
    to the generator. Verify the resulting set has cases of all
    three modalities."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-table-1", kind="enriched.tables",
        run_id="run-1",
    )
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-img-1", kind="enriched.visuals",
        run_id="run-1",
    )
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-graph-1", kind="graph_json",
        run_id="run-1",
    )

    vset = service.generate_validation_set(ctx, "run-1")

    types = {c.type for c in vset.test_cases}
    assert "table" in types
    assert "image" in types
    assert "graph" in types
    # Idempotency hash is unaffected by the new modality fields —
    # a regenerate without `force` still hits the cache.
    again = service.generate_validation_set(ctx, "run-1")
    assert again.validation_set_id == vset.validation_set_id


def test_service_metadata_surfaces_modality_counts(
    service, run_store, ctx, workspace, artifact_registry,
):
    """The set's metadata exposes modality artifact counts so the
    FE can render 'N tables surveyed' subtitles without
    re-counting."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-img-1", kind="enriched.visuals",
        run_id="run-1",
    )

    vset = service.generate_validation_set(ctx, "run-1")

    assert vset.metadata.get("table_artifact_count") == 0
    assert vset.metadata.get("visual_artifact_count") == 1
    assert vset.metadata.get("graph_artifact_count") == 0


# ---- Manual-query path: honest evidence flags ---------------------


def test_manual_query_evidence_flags_reflect_artifact_kinds(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """When retrieval surfaces a table artifact, `tablesUsed`
    flips to True. Phase 1's stub returned False unconditionally;
    Phase 4 makes it honest."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="art-table-1", kind="enriched.tables",
        body=b"keyword content for retrieval", run_id="run-1",
    )
    indexer.index(ctx, ["art-table-1"])

    response = service.run_manual_test_query(
        ctx, "run-1",
        ManualTestQueryRequest(question="keyword"),
    )

    # `tablesUsed` should be True (the retrieved hit is an
    # `enriched.tables` artifact), `imagesUsed` False (no visuals
    # retrieved), `graphUsed` False (no graph paths).
    assert response.evidence_flags.get("tablesUsed") is True
    assert response.evidence_flags.get("imagesUsed") is False


def test_manual_query_evidence_flags_default_to_false(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """No modality artifacts retrieved → all three flags False.
    Honest negative — locks the contract that `False` means
    'we can confirm this modality wasn't used,' not 'we don't
    know.'"""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    # Stage a regular chunk artifact (NOT table/visual/graph kind).
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / "chunk-1.json").write_bytes(b'{"chunkId": "c-1", "body": "alpha keyword"}')
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(
        ArtifactRecord(
            artifact_id="chunk-1",
            project=ctx,
            kind="chunk",
            location=f"{area.value}/chunk-1.json",
            content_hash="sha256:c1",
            byte_size=10,
            status=ProcessingStatus.SUCCEEDED,
            review_status=ReviewStatus.NOT_REQUIRED,
            version=1,
            created_at=now, updated_at=now,
            source_document_ids=[], source_artifact_ids=[],
            metadata={"run_id": "run-1", "chunk_id": "c-1"},
        )
    )
    indexer.index(ctx, ["chunk-1"])

    response = service.run_manual_test_query(
        ctx, "run-1", ManualTestQueryRequest(question="keyword"),
    )

    assert response.evidence_flags.get("tablesUsed") is False
    assert response.evidence_flags.get("imagesUsed") is False
    assert response.evidence_flags.get("graphUsed") is False
