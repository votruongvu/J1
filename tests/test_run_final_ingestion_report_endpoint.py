"""REST + service-layer tests for the
`final_ingestion_report` endpoint. Mirrors the shape of the other
artifact-overlay endpoint tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review import IngestionResultReviewService, ReviewNotFound
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.final_ingestion_report import (
    FINAL_INGESTION_REPORT_SCHEMA_VERSION,
)
from j1.processing.results import ARTIFACT_KIND_FINAL_INGESTION_REPORT
from j1.runs import JsonlIngestionRunStore
from j1.runs.models import IngestionRun, RunStatus
from j1.workspace.layout import WorkspaceArea


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def service(
    run_store, artifact_registry, workspace,
) -> IngestionResultReviewService:
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


def _make_run(run_id: str = "run-1") -> IngestionRun:
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id, document_id="doc-1",
        workflow_id="wf-1", workflow_run_id="wfr-1",
        status=RunStatus.SUCCEEDED,
        started_at=now, updated_at=now, completed_at=now,
        warning_count=0,
        metadata={"document_name": "spec.pdf"},
    )


def _report_payload(
    *, final_status: str = "completed_with_enrichment",
) -> dict:
    """Minimal `FinalIngestionReport.to_dict` shape — enough to
 test the wire envelope without re-running the builder."""
    return {
        "schema_version": FINAL_INGESTION_REPORT_SCHEMA_VERSION,
        "run_id": "run-1",
        "document_id": "doc-1",
        "document_name": "spec.pdf",
        "tenant_id": "acme",
        "project_id": "alpha",
        "domain_profile_id": "civil_engineering",
        "started_at": "2026-05-11T12:00:00+00:00",
        "completed_at": "2026-05-11T12:01:00+00:00",
        "duration_ms": 60000,
        "final_status": final_status,
        "final_status_reason": "enrichment overlay produced",
        "stages": [],
        "compile_summary": {
            "compile_engine": "mineru",
            "compile_status": "succeeded",
            "chunks_count": 42,
            "page_count": 10,
            "extracted_text_chars": 15000,
            "detected_tables_count": 2,
            "detected_images_count": 1,
            "quality_verdict": "good",
            "warnings": [],
            "errors": [],
            "retry_count": 0,
            "artifact_refs": ["raw-1"],
        },
        "enrichment_summary": {
            "should_enrich": True,
            "enrichment_status": "succeeded",
            "policy": "auto",
            "require_enrichment_success": False,
            "selected_modules": ["metadata_enrichment"],
            "skipped_modules": [],
            "module_outcomes": [],
            "what_enrichment_added": ["Document metadata: 3 fields"],
            "warnings": [],
            "errors": [],
            "retry_count": 0,
            "skipped_reason": None,
            "artifact_refs": [],
        },
        "artifact_refs": {
            "initial_execution_plan": "art-init-1",
            "compile_result_summary": "art-cmp-1",
            "enrichment_result": "art-enr-1",
            "final_summary": "art-fs-1",
        },
        "warnings": [],
        "errors": [],
        "retry_counts": {"compile": 0, "enrichment": 0},
        "operator_notes": [],
    }


def _write_report_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    payload: dict | None = None,
    artifact_id: str = "a-fir",
    updated_at: datetime | None = None,
) -> dict:
    if payload is None:
        payload = _report_payload()
    filename = f"final_ingestion_report_{run_id}_{artifact_id}.json"
    location = f"compiled/{filename}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload), encoding="utf-8")
    ts = updated_at or datetime(2026, 5, 11, 12, 1, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id, project=ctx,
        kind=ARTIFACT_KIND_FINAL_INGESTION_REPORT,
        location=location, content_hash=f"hash-{artifact_id}",
        byte_size=len(json.dumps(payload).encode("utf-8")),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=ts, updated_at=ts,
        source_document_ids=[document_id], source_artifact_ids=[],
        metadata={"run_id": run_id, "final_status": payload["final_status"]},
    ))
    return payload


# ---- 1. Available path ---------------------------------------------


def test_returns_completed_with_payload_when_artifact_exists(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    payload = _write_report_artifact(workspace, artifact_registry, ctx)
    result = service.get_run_final_ingestion_report(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["report"] == payload
    assert result["report"]["final_status"] == "completed_with_enrichment"
    assert result["report"]["schema_version"] == (
        FINAL_INGESTION_REPORT_SCHEMA_VERSION
    )
    assert result["artifactId"] == "a-fir"


# ---- 2. Unavailable / pre- paths ----------------------------


def test_returns_unavailable_for_legacy_run(service, run_store, ctx):
    """Old runs that completed won't have the report
 artifact — the endpoint must return the documented
 `final_ingestion_report_not_available` sentinel so the FE can
 fall back."""
    run_store.upsert(ctx, _make_run())
    result = service.get_run_final_ingestion_report(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["report"] is None
    assert (
        "final_ingestion_report_not_available"
        in (result["unavailableReason"] or "").lower()
    )


def test_returns_unavailable_when_payload_malformed(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Artifact exists on the registry but the on-disk JSON is not a
 dict — endpoint must surface the unavailable sentinel rather than
 crash the FE consumer."""
    run_store.upsert(ctx, _make_run())
    filename = "final_ingestion_report_run-1_a-bad.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-bad", project=ctx,
        kind=ARTIFACT_KIND_FINAL_INGESTION_REPORT,
        location=f"compiled/{filename}", content_hash="hash-bad",
        byte_size=full.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"run_id": "run-1"},
    ))
    result = service.get_run_final_ingestion_report(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert "unexpected shape" in (result["unavailableReason"] or "").lower()


# ---- 3. Run scoping ------------------------------------------------


def test_404s_for_unknown_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_final_ingestion_report(ctx, "missing-run")


def test_does_not_leak_cross_project(
    service, run_store, artifact_registry, workspace, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run())
    _write_report_artifact(workspace, artifact_registry, ctx)
    with pytest.raises(ReviewNotFound):
        service.get_run_final_ingestion_report(other_ctx, "run-1")


# ---- 4. Latest-wins selection --------------------------------------


def test_picks_latest_artifact_when_multiple_exist(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    old = _report_payload(final_status="completed_without_enrichment")
    _write_report_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-old", payload=old,
        updated_at=datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc),
    )
    new = _report_payload(final_status="completed_with_enrichment")
    _write_report_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-new", payload=new,
        updated_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
    )
    result = service.get_run_final_ingestion_report(ctx, "run-1")
    assert result["artifactId"] == "a-new"
    assert result["report"]["final_status"] == "completed_with_enrichment"


# ---- 5. Report content guards --------------------------------------


def test_report_payload_has_no_split_mode_vocabulary(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Operator/FE wire format must stay free of the legacy
 pre-compile gating vocabulary."""
    run_store.upsert(ctx, _make_run())
    _write_report_artifact(workspace, artifact_registry, ctx)
    result = service.get_run_final_ingestion_report(ctx, "run-1")
    blob = json.dumps(result)
    for forbidden in (
        "split_mode", "SplitMode", "split mode",
        "insert_content",
        "pre_compile_gating", "pre-compile gating",
        "graph gating", "index gating",
    ):
        assert forbidden not in blob, (
            f"forbidden token {forbidden!r} leaked through the read service"
        )
