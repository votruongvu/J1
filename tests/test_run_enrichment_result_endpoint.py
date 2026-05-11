"""Wave 6 REST + service-layer tests for the enrichment-result
endpoint. Mirrors the shape of the other artifact-overlay
endpoint tests (initial_execution_plan, compile_result, etc.):
completed / unavailable / 404 / cross-project / replay-latest."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.ingestion_review import (
    IngestionResultReviewService,
    ReviewNotFound,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.processing.results import ARTIFACT_KIND_ENRICHMENT_RESULT
from j1.runs import JsonlIngestionRunStore
from j1.runs.models import IngestionRun, RunStatus
from j1.workspace.layout import WorkspaceArea


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def service(run_store, artifact_registry, workspace) -> IngestionResultReviewService:
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


def _payload() -> dict:
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "status": "succeeded",
        "module_outcomes": [
            {
                "module_id": "metadata_enrichment",
                "status": "run",
                "reason": "extracted 4 fields",
                "duration_ms": 1500,
                "output_artifact_refs": ["m-1"],
                "source_refs": [
                    {"source_artifact_id": "compile-1", "relation": "extracted_from"}
                ],
                "model_usage": {"model": "fast", "duration_ms": 1500},
                "warnings": [],
                "errors": [],
            },
        ],
        "document_metadata_overlay": {
            "fields": {"project_number": "ABC-2025"},
            "missing_required_fields": [],
            "extras": {},
            "provenance": [],
        },
        "terminology_map": [
            {"term": "RFI", "definition": "Request for Information"},
        ],
        "classification_result": None,
        "table_summaries": [],
        "image_summaries": [],
        "validation_result": None,
        "retrieval_hints": [],
        "confidence_notes": [],
        "warnings": [],
        "errors": [],
        "model_usage": {},
        "duration_ms": 1500,
        "domain_id": "civil_engineering",
    }


def _write_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    payload: dict | None = None,
    artifact_id: str = "a-enrichment",
    updated_at: datetime | None = None,
) -> dict:
    if payload is None:
        payload = _payload()
    filename = f"enrichment_result_{run_id}_{artifact_id}.json"
    location = f"enriched/{filename}"
    full = workspace.area(ctx, WorkspaceArea.ENRICHED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload), encoding="utf-8")
    ts = updated_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id, project=ctx,
        kind=ARTIFACT_KIND_ENRICHMENT_RESULT,
        location=location, content_hash=f"hash-{artifact_id}",
        byte_size=len(json.dumps(payload).encode("utf-8")),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=ts, updated_at=ts,
        source_document_ids=[document_id], source_artifact_ids=[],
        metadata={"run_id": run_id},
    ))
    return payload


def test_returns_completed_with_payload_when_artifact_exists(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    payload = _write_artifact(workspace, artifact_registry, ctx)
    result = service.get_run_enrichment_result(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["plan"] == payload
    assert result["plan"]["status"] == "succeeded"
    assert result["plan"]["module_outcomes"][0]["module_id"] == "metadata_enrichment"


def test_returns_unavailable_when_no_artifact(service, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    result = service.get_run_enrichment_result(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert (
        "enrichment" in result["unavailableReason"].lower()
        or "skipped" in result["unavailableReason"].lower()
    )


def test_returns_unavailable_when_payload_malformed(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    filename = "enrichment_result_run-1_a-bad.json"
    full = workspace.area(ctx, WorkspaceArea.ENRICHED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-bad", project=ctx,
        kind=ARTIFACT_KIND_ENRICHMENT_RESULT,
        location=f"enriched/{filename}", content_hash="hash-bad",
        byte_size=full.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"run_id": "run-1"},
    ))
    result = service.get_run_enrichment_result(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert "unexpected shape" in result["unavailableReason"].lower()


def test_404s_for_unknown_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_enrichment_result(ctx, "missing-run")


def test_does_not_leak_cross_project(
    service, run_store, artifact_registry, workspace, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run())
    _write_artifact(workspace, artifact_registry, ctx)
    with pytest.raises(ReviewNotFound):
        service.get_run_enrichment_result(other_ctx, "run-1")


def test_picks_latest_artifact_when_multiple_exist(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    old = _payload()
    old["status"] = "failed"
    _write_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-old", payload=old,
        updated_at=datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc),
    )
    new = _payload()
    new["status"] = "succeeded"
    _write_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-new", payload=new,
        updated_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
    )
    result = service.get_run_enrichment_result(ctx, "run-1")
    assert result["artifactId"] == "a-new"
    assert result["plan"]["status"] == "succeeded"
