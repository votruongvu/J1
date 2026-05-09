"""End-to-end tests for `PlanningActivities.build_planning_result`.

Drives the activity via its plain method (no Temporal worker
required) — same pattern other activity tests in this repo use."""

from __future__ import annotations

import json

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.orchestration.activities.planning import (
    BuildPlanningResultInput,
    PlanningActivities,
)
from j1.orchestration.activities.payloads import ProjectScope
from j1.processing.manifest import (
    ParsedContentManifest,
    ParsedContentStats,
)
from j1.processing.planning_settings import PlanningSettings
from j1.processing.results import (
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
    ARTIFACT_KIND_PLANNING_RESULT,
)
from j1.workspace.layout import WorkspaceArea


def _scope(ctx) -> ProjectScope:
    return ProjectScope.from_context(ctx)


def _write_manifest(workspace, ctx, *, document_id: str, run_id: str):
    manifest = ParsedContentManifest(
        document_id=document_id,
        document_hash="h", parser="raganything", parser_version="1",
        parse_method="auto", profile=None,
        stats=ParsedContentStats(
            text_blocks=80, tables=2, images=1, equations=0,
            total_items=83, page_count=10,
            parse_quality_score=0.9,
        ),
        items=[],
    )
    filename = f"{document_id}.parsed_content_manifest.json"
    path = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest.to_json_bytes())

    from datetime import datetime, timezone
    record = ArtifactRecord(
        artifact_id="manifest-1",
        project=ctx,
        kind=ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
        location=f"compiled/{filename}",
        content_hash="sha256:m",
        byte_size=path.stat().st_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
        source_document_ids=[document_id],
        source_artifact_ids=[],
        metadata={"run_id": run_id},
    )
    return record


def test_activity_returns_none_when_post_compile_disabled(
    workspace, artifact_registry, ctx,
):
    activities = PlanningActivities(
        workspace=workspace,
        artifacts=artifact_registry,
        llm_registry=None,
        planning_settings=PlanningSettings(post_compile_enabled=False),
    )
    out = activities.build_planning_result(BuildPlanningResultInput(
        scope=_scope(ctx),
        run_id="r1",
        document_id="doc-1",
    ))
    assert out is None


def test_activity_returns_none_when_no_manifest(
    workspace, artifact_registry, ctx,
):
    activities = PlanningActivities(
        workspace=workspace,
        artifacts=artifact_registry,
        llm_registry=None,
        planning_settings=PlanningSettings(),
    )
    out = activities.build_planning_result(BuildPlanningResultInput(
        scope=_scope(ctx),
        run_id="r1",
        document_id="doc-1",
    ))
    assert out is None


def test_activity_persists_planning_result_artifact_for_rule_based_path(
    workspace, artifact_registry, ctx,
):
    """Happy path: manifest present → activity writes the
    `planning_result` artifact + returns the high-level decisions."""
    record = _write_manifest(workspace, ctx, document_id="doc-1", run_id="r1")
    artifact_registry.add(record)

    activities = PlanningActivities(
        workspace=workspace,
        artifacts=artifact_registry,
        llm_registry=None,
        planning_settings=PlanningSettings(),
    )
    out = activities.build_planning_result(BuildPlanningResultInput(
        scope=_scope(ctx),
        run_id="r1",
        document_id="doc-1",
        document_filename="doc-1.pdf",
    ))
    assert out is not None
    assert out.source == "rule_based"
    assert out.recommended_profile in {"fast", "balanced", "premium", "diagnostic", "custom"}

    # Verify artifact registered + payload round-trips.
    artifacts = artifact_registry.list_artifacts(ctx)
    planning_records = [
        a for a in artifacts if a.kind == ARTIFACT_KIND_PLANNING_RESULT
    ]
    assert len(planning_records) == 1
    persisted_path = (
        workspace.area(ctx, WorkspaceArea.COMPILED)
        / planning_records[0].location.split("/", 1)[1]
    )
    payload = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert payload["source"] == "rule_based"
    assert payload["run_id"] == "r1"
    assert "execution_plan" in payload
    assert "document_understanding" in payload


def test_activity_uses_llm_planner_when_enabled_and_falls_back_on_failure(
    workspace, artifact_registry, ctx, monkeypatch,
):
    """LLM enabled but the registry returns None → fallback to
    rule-based and the artifact still lands."""
    record = _write_manifest(workspace, ctx, document_id="doc-1", run_id="r1")
    artifact_registry.add(record)

    class _Reg:
        def try_fast(self): return None
        def try_text(self): return None
        def try_premium_or_text(self): return None

    activities = PlanningActivities(
        workspace=workspace,
        artifacts=artifact_registry,
        llm_registry=_Reg(),
        planning_settings=PlanningSettings(
            llm_planning_enabled=True, fail_open=True,
        ),
    )
    out = activities.build_planning_result(BuildPlanningResultInput(
        scope=_scope(ctx),
        run_id="r1",
        document_id="doc-1",
    ))
    assert out is not None
    # Without a registered planner, the activity falls back silently
    # (no llm_planner callable is built) — source stays rule_based.
    assert out.source == "rule_based"
