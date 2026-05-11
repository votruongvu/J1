"""Tests for `IngestionResultReviewService.get_run_compile_result`.

Mirrors the enrich-plan / initial-execution-plan endpoint tests:
artifact present → completed; missing → unavailable; malformed →
unavailable; unknown run → ReviewNotFound; cross-project isolation
preserved; replay duplicates resolve to latest."""

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
from j1.processing.results import ARTIFACT_KIND_COMPILE_RESULT_SUMMARY
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
        status=RunStatus.SUCCEEDED, started_at=now, updated_at=now,
        completed_at=now, warning_count=0,
        metadata={"document_name": "spec.pdf"},
    )


def _make_payload() -> dict:
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "compile_engine": "raganything",
        "engine_version": None,
        "status": "succeeded",
        "raw_artifact_refs": ["a-1", "a-2"],
        "artifact_kinds": ["chunk", "parsed_content_manifest"],
        "chunks_count": 2,
        "extracted_text_chars": 8000,
        "page_count": 5,
        "text_block_count": 30,
        "detected_tables": [],
        "detected_images": [
            {"image_id": "img-1", "page": 1, "role": "figure",
             "decision": "vision_required", "caption": None, "score": None},
        ],
        "detected_content_types": ["text", "images"],
        "graph_artifact_refs": [],
        "index_artifact_refs": [],
        "quality_signals": {
            "parse_quality_score": 0.9,
            "text_sufficiency_score": 0.85,
            "layout_complexity_score": 0.3,
            "empty_page_ratio": 0.0,
            "text_extractable_ratio": 1.0,
        },
        "final_quality_verdict": "good",
        "duration_ms": 12500,
        "warnings": [],
        "errors": [],
        "retry_history": [
            {"attempt_number": 1, "mode": "standard", "status": "succeeded",
             "parser": "raganything", "parse_method": "auto",
             "chunks_count": 2, "extracted_text_chars": 8000,
             "quality": "good", "retry_reason": None,
             "started_at": "2026-05-11T10:00:00Z",
             "completed_at": "2026-05-11T10:05:00Z",
             "warnings": []},
        ],
        "final_compile_mode": "standard",
    }


def _write_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    payload: dict | None = None,
    artifact_id: str = "a-compile-summary",
    updated_at: datetime | None = None,
) -> dict:
    if payload is None:
        payload = _make_payload()
    filename = f"compile_result_summary_{run_id}_{artifact_id}.json"
    location = f"compiled/{filename}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload), encoding="utf-8")
    ts = updated_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id, project=ctx,
        kind=ARTIFACT_KIND_COMPILE_RESULT_SUMMARY,
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
    result = service.get_run_compile_result(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["unavailableReason"] is None
    assert result["runId"] == "run-1"
    assert result["documentId"] == "doc-1"
    assert result["artifactId"] == "a-compile-summary"
    assert result["plan"] == payload
    assert result["plan"]["chunks_count"] == 2
    assert result["plan"]["detected_content_types"] == ["text", "images"]


def test_returns_unavailable_when_no_artifact(service, run_store, ctx):
    run_store.upsert(ctx, _make_run())
    result = service.get_run_compile_result(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert "compile" in result["unavailableReason"].lower()


def test_returns_unavailable_when_payload_malformed(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    filename = "compile_result_summary_run-1_a-bad.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-bad", project=ctx,
        kind=ARTIFACT_KIND_COMPILE_RESULT_SUMMARY,
        location=f"compiled/{filename}", content_hash="hash-bad",
        byte_size=full.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=now, updated_at=now,
        source_document_ids=["doc-1"], source_artifact_ids=[],
        metadata={"run_id": "run-1"},
    ))
    result = service.get_run_compile_result(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert "unexpected shape" in result["unavailableReason"].lower()


def test_404s_for_unknown_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_compile_result(ctx, "missing-run")


def test_does_not_leak_cross_project_compile_result(
    service, run_store, artifact_registry, workspace, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run())
    _write_artifact(workspace, artifact_registry, ctx)
    with pytest.raises(ReviewNotFound):
        service.get_run_compile_result(other_ctx, "run-1")


def test_picks_latest_artifact_when_multiple_exist(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Compile + replay can produce duplicate summary artifacts.
    Most-recent wins."""
    run_store.upsert(ctx, _make_run())
    old_payload = _make_payload()
    old_payload["chunks_count"] = 0
    _write_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-old", payload=old_payload,
        updated_at=datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc),
    )
    new_payload = _make_payload()
    new_payload["chunks_count"] = 7
    _write_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-new", payload=new_payload,
        updated_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
    )
    result = service.get_run_compile_result(ctx, "run-1")
    assert result["artifactId"] == "a-new"
    assert result["plan"]["chunks_count"] == 7
