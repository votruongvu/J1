"""Tests for `IngestionResultReviewService.get_run_enrich_plan`.

Pins the contract of the new `/ingestion-runs/{id}/enrich-plan`
endpoint at the service layer:
 * artifact present → status=completed + plan payload + artifactId
 * artifact missing → status=unavailable + reason
 * unreadable / malformed payload → status=unavailable + reason
 * missing run → ReviewNotFound

The REST adapter layer is a thin envelope, so testing the service
method covers the contract end-to-end."""

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
from j1.processing.results import ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN
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
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id="wfr-1",
        status=RunStatus.SUCCEEDED,
        started_at=now,
        updated_at=now,
        completed_at=now,
        warning_count=0,
        metadata={"document_name": "spec.pdf"},
    )


def _write_enrich_plan_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    payload: dict | None = None,
    artifact_id: str = "a-enrich-plan",
) -> dict:
    """Write a `post_compile_enrich_plan` artifact's bytes into the
 COMPILED workspace area + register the record. Returns the
 payload that was written."""
    if payload is None:
        payload = {
            "schema_version": "1",
            "overall_recommendation": "recommended",
            "reasons": ["document contains 3 image(s)"],
            "recommended_tasks": ["image_captioning", "vision_enrichment"],
            "skipped_tasks": ["table_enrichment"],
            "blocking_issues": [],
            "source_signals": {
                "compile_status": "succeeded",
                "image_count": 3,
            },
            "decision_source": "rule_based",
        }
    filename = f"post_compile_enrich_plan_{run_id}.json"
    location = f"compiled/{filename}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload), encoding="utf-8")

    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=len(json.dumps(payload).encode("utf-8")),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata={"run_id": run_id},
    ))
    return payload


def test_returns_completed_with_payload_when_artifact_exists(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    payload = _write_enrich_plan_artifact(
        workspace, artifact_registry, ctx,
    )
    result = service.get_run_enrich_plan(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["unavailableReason"] is None
    assert result["runId"] == "run-1"
    assert result["documentId"] == "doc-1"
    assert result["artifactId"] == "a-enrich-plan"
    assert result["plan"] == payload
    assert result["plan"]["overall_recommendation"] == "recommended"
    assert result["plan"]["recommended_tasks"] == [
        "image_captioning", "vision_enrichment",
    ]


def test_returns_unavailable_when_no_enrich_plan_artifact(
    service, run_store, ctx,
):
    """Run exists but never produced an enrich plan artifact —
 surface as unavailable + reason. Don't 404 (run is real)."""
    run_store.upsert(ctx, _make_run())
    result = service.get_run_enrich_plan(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert result["unavailableReason"]
    assert "post-compile" in result["unavailableReason"].lower()


def test_returns_unavailable_when_payload_malformed(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Artifact exists but on-disk JSON isn't a dict (or unreadable).
 Surface as unavailable so the FE renders a placeholder rather
 than crashing."""
    run_store.upsert(ctx, _make_run())
    # Register the artifact with a location but write a non-object
    # payload — passes JSON parsing but fails the dict check.
    filename = "post_compile_enrich_plan_run-1.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-bad",
        project=ctx,
        kind=ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
        location=f"compiled/{filename}",
        content_hash="hash-bad",
        byte_size=full.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={"run_id": "run-1"},
    ))
    result = service.get_run_enrich_plan(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert "unexpected shape" in result["unavailableReason"].lower()


def test_404s_for_unknown_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_enrich_plan(ctx, "missing-run")


def test_does_not_leak_cross_project_enrich_plan(
    service, run_store, artifact_registry, workspace, ctx, other_ctx,
):
    """A run + its artifact in project alpha must not be visible from
 project beta — service uses ctx-scoped reads end-to-end."""
    run_store.upsert(ctx, _make_run())
    _write_enrich_plan_artifact(workspace, artifact_registry, ctx)
    with pytest.raises(ReviewNotFound):
        service.get_run_enrich_plan(other_ctx, "run-1")


def test_picks_latest_artifact_when_multiple_exist(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Replays + retries can produce duplicate artifacts. The service
 must return the most recent one."""
    run_store.upsert(ctx, _make_run())
    _write_enrich_plan_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-old",
        payload={
            "schema_version": "1",
            "overall_recommendation": "optional",
            "reasons": [],
            "recommended_tasks": [],
            "skipped_tasks": [],
            "blocking_issues": [],
            "source_signals": {},
            "decision_source": "rule_based",
        },
    )
    # Second artifact with later updated_at
    new_payload = {
        "schema_version": "1",
        "overall_recommendation": "required",
        "reasons": ["forced by operator"],
        "recommended_tasks": ["table_enrichment"],
        "skipped_tasks": [],
        "blocking_issues": [],
        "source_signals": {},
        "decision_source": "rule_based",
    }
    filename = "post_compile_enrich_plan_run-1_v2.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(new_payload), encoding="utf-8")
    later = datetime(2026, 5, 10, 13, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-new",
        project=ctx,
        kind=ARTIFACT_KIND_POST_COMPILE_ENRICH_PLAN,
        location=f"compiled/{filename}",
        content_hash="hash-new",
        byte_size=full.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=later,
        updated_at=later,
        source_document_ids=["doc-1"],
        source_artifact_ids=[],
        metadata={"run_id": "run-1"},
    ))
    result = service.get_run_enrich_plan(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["artifactId"] == "a-new"
    assert result["plan"]["overall_recommendation"] == "required"
