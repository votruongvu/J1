"""Phase 5 service tests: tester verdict + report export.

Service-level semantics. REST envelope tests live in
`test_rest_validation_phase5.py`. Frontend tests cover the UI.
"""

from __future__ import annotations

import json
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
def vrun_store(workspace) -> JsonlValidationRunStore:
    return JsonlValidationRunStore(workspace)


@pytest.fixture
def service(
    run_store, artifact_registry, query_engine, audit_sink,
    workspace, vrun_store,
) -> IngestionValidationService:
    return IngestionValidationService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        query_engine=query_engine,
        audit=DefaultAuditRecorder(audit_sink),
        workspace=workspace,
        validation_set_store=JsonlValidationSetStore(workspace),
        validation_run_store=vrun_store,
        test_case_generator=DefaultTestCaseGenerator(),
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
    body: bytes,
    run_id: str = "run-1",
    chunk_id: str = "c-1",
):
    area = WorkspaceArea.COMPILED
    area_dir = workspace.area(ctx, area)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.json"
    payload = {"chunkId": chunk_id, "body": body.decode("utf-8")}
    (area_dir / stored).write_bytes(json.dumps(payload).encode("utf-8"))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{area.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(body),
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


def _seed_completed_run(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Helper: stage a run, generate a set, run validation. Returns
    the terminal vrun (used as starting state for verdict / report
    tests)."""
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    _stage_chunk(
        workspace, ctx, artifact_registry, indexer,
        artifact_id="a-1", body=b"alpha keyword in chunk",
        run_id="run-1", chunk_id="c-1",
    )
    vset = service.generate_validation_set(ctx, "run-1")
    return service.run_validation(ctx, "run-1", vset.validation_set_id)


# ---- record_tester_verdict ------------------------------------------


def test_verdict_persists_to_store(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    vrun_store,
):
    """Happy path: verdict + notes get written through the run
    store. Latest-snapshot semantics mean a re-fetch sees the
    updated record."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    assert vrun.results, "test fixture expected at least one result"
    target = vrun.results[0]

    updated = service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="pass", notes="Looked fine on manual review.",
        actor="alice@example.com",
    )

    fetched = vrun_store.get(ctx, vrun.validation_run_id)
    assert fetched is not None
    matched = next(
        r for r in fetched.results if r.result_id == target.result_id
    )
    assert matched.tester_verdict == "pass"
    assert matched.tester_notes == "Looked fine on manual review."
    # The returned DTO matches the persisted one.
    assert updated.results[0].tester_verdict == "pass"


def test_verdict_does_not_alter_automated_status(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    vrun_store,
):
    """Critical: tester verdict is INDEPENDENT of automated
    `status`. A failed result with `tester_verdict=pass` keeps
    `status=failed` — operators see both signals side-by-side."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]
    original_status = target.status

    service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="pass",
    )

    fetched = vrun_store.get(ctx, vrun.validation_run_id)
    matched = next(
        r for r in fetched.results if r.result_id == target.result_id
    )
    assert matched.status == original_status
    assert matched.tester_verdict == "pass"


def test_verdict_preserves_other_results(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    vrun_store,
):
    """Verdict on result A must not touch result B. Locks the
    field-by-field copy contract in `_replace_run_results`."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    if len(vrun.results) < 2:
        pytest.skip("fixture didn't produce >=2 results; check generator caps")
    target = vrun.results[0]
    other = vrun.results[1]

    service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="warning",
    )

    fetched = vrun_store.get(ctx, vrun.validation_run_id)
    other_after = next(
        r for r in fetched.results if r.result_id == other.result_id
    )
    assert other_after.tester_verdict is None
    assert other_after.status == other.status


def test_verdict_can_be_revised(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """A second verdict overwrites the first. JSONL latest-wins
    means appending a revised snapshot becomes the visible state."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]

    service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="warning", notes="initial",
    )
    final = service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="pass", notes="reviewed again — ok",
    )

    matched = next(
        r for r in final.results if r.result_id == target.result_id
    )
    assert matched.tester_verdict == "pass"
    assert matched.tester_notes == "reviewed again — ok"


def test_verdict_invalid_value_raises_value_error(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]
    with pytest.raises(ValueError, match="invalid tester verdict"):
        service.record_tester_verdict(
            ctx, "run-1", vrun.validation_run_id, target.result_id,
            verdict="approved",  # not in {pass, warning, fail}
        )


def test_verdict_unknown_result_raises_review_not_found(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    with pytest.raises(ReviewNotFound):
        service.record_tester_verdict(
            ctx, "run-1", vrun.validation_run_id, "vr-ghost",
            verdict="pass",
        )


def test_verdict_cross_tenant_raises_review_not_found(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Cross-tenant access must 404 — same uniform shape as the
    rest of the validation surface."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]
    other = ProjectContext(tenant_id="enemy", project_id=ctx.project_id)
    with pytest.raises(ReviewNotFound):
        service.record_tester_verdict(
            other, "run-1", vrun.validation_run_id, target.result_id,
            verdict="pass",
        )


def test_verdict_emits_audit_event(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]
    service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="warning", notes="x", actor="alice",
    )
    log = (workspace.audit(ctx) / "events.jsonl").read_text()
    assert "j1.validation.verdict_recorded" in log
    assert target.result_id in log
    assert "warning" in log


# ---- export_validation_run_report ----------------------------------


def test_report_markdown_contains_split_status(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """Wire-shape regression: the Markdown report MUST surface
    both `executionStatus` and `validationStatus`. Operators rely
    on the split being unambiguous in shareable artifacts."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )

    content, media = service.export_validation_run_report(
        ctx, "run-1", vrun.validation_run_id, format="markdown",
    )

    assert media == "text/markdown"
    assert "Validation Report" in content
    assert "Execution status" in content
    assert "Validation status" in content
    assert vrun.run_id in content
    assert vrun.validation_set_id in content


def test_report_markdown_renders_failed_results_first(
    service, run_store, ctx, workspace, artifact_registry, indexer,
    vrun_store,
):
    """Operator-readability: failed results come first in the
    per-case section. Locked here so a reordering refactor
    doesn't accidentally bury the things testers need to act on."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    # Hand-craft a vrun with a guaranteed mix of statuses so the
    # ordering test is deterministic, then upsert it back.
    if len(vrun.results) < 2:
        pytest.skip("fixture didn't produce >=2 results")
    from j1.validation.dtos import ValidationResultDTO
    failed = ValidationResultDTO(
        result_id="vr-failed",
        test_case_id="tc-failed",
        status="failed",
        question="failed-q",
        answer="",
        retrieved_chunks=[],
        citations=[],
        checks=[],
        failure_reason="forced",
    )
    passed = ValidationResultDTO(
        result_id="vr-passed",
        test_case_id="tc-passed",
        status="passed",
        question="passed-q",
        answer="ok",
        retrieved_chunks=[],
        citations=[],
        checks=[],
    )
    from j1.validation.service import _replace_run_results
    rebuilt = _replace_run_results(vrun, results=[passed, failed])
    vrun_store.upsert(ctx, rebuilt)

    content, _ = service.export_validation_run_report(
        ctx, "run-1", vrun.validation_run_id, format="markdown",
    )

    failed_idx = content.find("tc-failed")
    passed_idx = content.find("tc-passed")
    assert failed_idx > 0 and passed_idx > 0
    assert failed_idx < passed_idx, (
        "failed results must come before passed in the report"
    )


def test_report_markdown_shows_tester_override_when_disagrees(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """When `tester_verdict` disagrees with `status`, the report
    surfaces both. The contract: operators must see the override
    explicitly so the human signal isn't hidden under the auto
    badge."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    target = vrun.results[0]
    service.record_tester_verdict(
        ctx, "run-1", vrun.validation_run_id, target.result_id,
        verdict="pass" if target.status != "passed" else "fail",
    )

    content, _ = service.export_validation_run_report(
        ctx, "run-1", vrun.validation_run_id, format="markdown",
    )

    # When the tester verdict differs from auto, the section
    # heading should make it explicit.
    if target.status != "passed":
        assert "tester:" in content


def test_report_json_format_is_valid_json(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    """JSON format must round-trip through `json.loads` — caller
    should be able to feed it to downstream tooling without
    further manipulation."""
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    content, media = service.export_validation_run_report(
        ctx, "run-1", vrun.validation_run_id, format="json",
    )
    assert media == "application/json"
    parsed = json.loads(content)
    assert parsed["validation_run_id"] == vrun.validation_run_id


def test_report_unknown_format_raises_value_error(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    with pytest.raises(ValueError, match="unsupported report format"):
        service.export_validation_run_report(
            ctx, "run-1", vrun.validation_run_id, format="xml",
        )


def test_report_unknown_run_raises_review_not_found(service, run_store, ctx):
    run_store.upsert(ctx, _make_run(run_id="run-1"))
    with pytest.raises(ReviewNotFound):
        service.export_validation_run_report(
            ctx, "run-1", "vrun-ghost",
        )


def test_report_cross_tenant_raises_review_not_found(
    service, run_store, ctx, workspace, artifact_registry, indexer,
):
    vrun = _seed_completed_run(
        service, run_store, ctx, workspace, artifact_registry, indexer,
    )
    other = ProjectContext(tenant_id="enemy", project_id=ctx.project_id)
    with pytest.raises(ReviewNotFound):
        service.export_validation_run_report(
            other, "run-1", vrun.validation_run_id,
        )
