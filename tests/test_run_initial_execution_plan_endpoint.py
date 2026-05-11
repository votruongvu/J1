"""Tests for `IngestionResultReviewService.get_run_initial_execution_plan`.

Pins the contract of `/ingestion-runs/{id}/initial-execution-plan`
at the service layer:
  * artifact present → status=completed + plan payload + artifactId
  * artifact missing → status=unavailable + reason
  * unreadable / malformed payload → status=unavailable + reason
  * missing run → ReviewNotFound
  * cross-project leakage prevented

Mirrors `test_run_enrich_plan_endpoint.py` shape; the REST adapter
is a thin envelope so service-layer coverage is end-to-end.
"""

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
from j1.processing.results import ARTIFACT_KIND_INITIAL_EXECUTION_PLAN
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


def _make_payload() -> dict:
    return {
        "schema_version": "1",
        "document_id": "doc-1",
        "run_compile": True,
        "compile_engine": "raganything",
        "domain_profile_id": "civil_engineering",
        "enrichment_policy": "always",
        "candidate_enrichment_modules": [
            "requirement_extraction",
            "risk_extraction",
        ],
        "cheap_signals": {
            "extension": ".pdf",
            "page_count": 10,
        },
        "resource_hints": {"default_model_tier": "fast"},
        "reasons": ["domain pack: civil_engineering"],
        "warnings": [],
        "compile_plan": {
            "schema_version": "1",
            "document_id": "doc-1",
            "mode": "standard",
            "document_type": "specification",
            "complexity": "medium",
            "confidence": 0.8,
            "required_capabilities": ["text_extraction"],
            "optional_capabilities": [],
            "risk_flags": [],
            "fallback_policy": "degrade_with_warning",
            "reason": "",
            "recommended_path": "standard_compile",
        },
    }


def _write_plan_artifact(
    workspace,
    artifact_registry,
    ctx,
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    payload: dict | None = None,
    artifact_id: str = "a-initial-plan",
    updated_at: datetime | None = None,
) -> dict:
    """Write an `initial_execution_plan` artifact's bytes into the
    COMPILED workspace area + register the record. Returns the
    payload that was written."""
    if payload is None:
        payload = _make_payload()
    filename = f"initial_execution_plan_{run_id}_{artifact_id}.json"
    location = f"compiled/{filename}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload), encoding="utf-8")

    timestamp = updated_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=ARTIFACT_KIND_INITIAL_EXECUTION_PLAN,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=len(json.dumps(payload).encode("utf-8")),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=timestamp,
        updated_at=timestamp,
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata={"run_id": run_id},
    ))
    return payload


# ---- happy path ------------------------------------------------------


def test_returns_completed_with_payload_when_artifact_exists(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run())
    payload = _write_plan_artifact(workspace, artifact_registry, ctx)
    result = service.get_run_initial_execution_plan(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["unavailableReason"] is None
    assert result["runId"] == "run-1"
    assert result["documentId"] == "doc-1"
    assert result["artifactId"] == "a-initial-plan"
    assert result["plan"] == payload
    assert result["plan"]["domain_profile_id"] == "civil_engineering"
    assert result["plan"]["enrichment_policy"] == "always"
    assert "requirement_extraction" in result["plan"]["candidate_enrichment_modules"]


# ---- unavailable paths ------------------------------------------------


def test_returns_unavailable_when_no_initial_plan_artifact(
    service, run_store, ctx,
):
    """Run exists but never produced a pre-compile plan artifact —
    surface as unavailable + reason. Don't 404 (run is real)."""
    run_store.upsert(ctx, _make_run())
    result = service.get_run_initial_execution_plan(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert result["unavailableReason"]
    assert "pre-compile" in result["unavailableReason"].lower()


def test_returns_unavailable_when_payload_malformed(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Artifact exists but on-disk JSON isn't a dict. Surface as
    unavailable so the FE renders a placeholder rather than crashing."""
    run_store.upsert(ctx, _make_run())
    filename = "initial_execution_plan_run-1_a-bad.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id="a-bad",
        project=ctx,
        kind=ARTIFACT_KIND_INITIAL_EXECUTION_PLAN,
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
    result = service.get_run_initial_execution_plan(ctx, "run-1")
    assert result["status"] == "unavailable"
    assert result["plan"] is None
    assert "unexpected shape" in result["unavailableReason"].lower()


# ---- 404 + cross-project isolation -----------------------------------


def test_404s_for_unknown_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_initial_execution_plan(ctx, "missing-run")


def test_does_not_leak_cross_project_initial_plan(
    service, run_store, artifact_registry, workspace, ctx, other_ctx,
):
    """A run + its artifact in project alpha must not be visible from
    project beta — service uses ctx-scoped reads end-to-end."""
    run_store.upsert(ctx, _make_run())
    _write_plan_artifact(workspace, artifact_registry, ctx)
    with pytest.raises(ReviewNotFound):
        service.get_run_initial_execution_plan(other_ctx, "run-1")


# ---- replay duplication ----------------------------------------------


def test_picks_latest_artifact_when_multiple_exist(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Replays + retries can produce duplicate plan artifacts. The
    service must return the most recent (highest updated_at)."""
    run_store.upsert(ctx, _make_run())
    older_payload = _make_payload()
    older_payload["domain_profile_id"] = "general"
    _write_plan_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-old",
        payload=older_payload,
        updated_at=datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc),
    )
    newer_payload = _make_payload()
    newer_payload["domain_profile_id"] = "civil_engineering"
    _write_plan_artifact(
        workspace, artifact_registry, ctx,
        artifact_id="a-new",
        payload=newer_payload,
        updated_at=datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc),
    )
    result = service.get_run_initial_execution_plan(ctx, "run-1")
    assert result["status"] == "completed"
    assert result["artifactId"] == "a-new"
    assert result["plan"]["domain_profile_id"] == "civil_engineering"
