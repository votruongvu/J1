"""Unit tests for IngestionResultReviewService.

Covers Phase 1 (summary) and Phase 2 (run-scoped artifact list +
content + path-traversal guard)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.errors.exceptions import PathTraversalError
from j1.ingestion_review import (
    IngestionResultReviewService,
    ReviewNotFound,
)
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.runs import AuditProgressReporter, JsonlIngestionRunStore
from j1.runs.models import IngestionRun, RunStatus
from j1.workspace.layout import WorkspaceArea


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def reporter(workspace) -> AuditProgressReporter:
    return AuditProgressReporter(DefaultAuditRecorder(JsonlAuditSink(workspace)))


@pytest.fixture
def service(run_store, artifact_registry, workspace) -> IngestionResultReviewService:
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )


def _make_run(
    *,
    run_id: str = "run-1",
    document_id: str = "doc-1",
    status: RunStatus = RunStatus.SUCCEEDED,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    metadata: dict | None = None,
    warning_count: int = 0,
) -> IngestionRun:
    started = started_at or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    completed = completed_at or (started + timedelta(seconds=12))
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf-1",
        workflow_run_id="wfr-1",
        status=status,
        started_at=started,
        updated_at=completed,
        completed_at=completed,
        warning_count=warning_count,
        metadata=metadata or {},
    )


def _make_artifact(
    ctx: ProjectContext,
    *,
    artifact_id: str,
    kind: str,
    byte_size: int = 100,
    source_document_ids: list[str] | None = None,
    source_artifact_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> ArtifactRecord:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=f"compiled/{artifact_id}.json",
        content_hash=f"hash-{artifact_id}",
        byte_size=byte_size,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=source_document_ids or [],
        source_artifact_ids=source_artifact_ids or [],
        metadata=metadata or {},
    )


# ---- Not-found semantics --------------------------------------------


def test_summarize_run_raises_review_not_found_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.summarize_run(ctx, "missing-run")


def test_summarize_run_does_not_leak_cross_tenant_runs(
    service, run_store, ctx, other_ctx,
):
    """A run that exists in one project must not be visible from
    another. The service uses the same `ctx`-scoped store reads as
    every other surface; cross-project access should look identical
    to 'missing'."""
    run_store.upsert(ctx, _make_run(run_id="leak-test"))
    with pytest.raises(ReviewNotFound):
        service.summarize_run(other_ctx, "leak-test")


# ---- Summary shape --------------------------------------------------


def test_summarize_run_returns_basic_fields(service, run_store, ctx):
    run = _make_run(
        run_id="run-basic",
        document_id="doc-1",
        status=RunStatus.SUCCEEDED,
    )
    run_store.upsert(ctx, run)

    summary = service.summarize_run(ctx, "run-basic")

    assert summary.run_id == "run-basic"
    assert summary.status == "succeeded"
    assert summary.document_ids == ["doc-1"]
    assert summary.duration_ms == 12_000
    assert summary.total_bytes == 0
    assert summary.artifact_counts == {}


def test_summarize_run_counts_artifacts_by_kind_via_lineage(
    service, run_store, artifact_registry, ctx,
):
    """No `metadata.run_id` tag → fall back to lineage join on
    `source_document_ids`. This is the path legacy artifacts (from
    before Phase 4) take, and it must keep working."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))

    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="a1", kind="chunk", byte_size=200,
            source_document_ids=["doc-A"],
        )
    )
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="a2", kind="chunk", byte_size=300,
            source_document_ids=["doc-A"],
        )
    )
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="a3", kind="enriched.tables", byte_size=1000,
            source_document_ids=["doc-A"],
        )
    )
    # Unrelated artifact for a different document — must NOT be counted.
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="other", kind="chunk", byte_size=50_000,
            source_document_ids=["doc-other"],
        )
    )

    summary = service.summarize_run(ctx, "run-1")

    assert summary.artifact_counts == {"chunk": 2, "enriched.tables": 1}
    assert summary.total_bytes == 1500


def test_summarize_run_lineage_walks_source_artifact_ids(
    service, run_store, artifact_registry, ctx,
):
    """Regression: graph_json + enrichment artifacts carry only
    `source_artifact_ids` (pointing at compile artifacts), NOT
    `source_document_ids`. The lineage fallback MUST walk the chain
    transitively or the Graph / Assets tabs silently disable for
    legacy untagged runs even though the artifacts exist on disk."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))

    # Compile artifact — has source_document_ids only.
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="compile-1", kind="compile",
        source_document_ids=["doc-A"],
    ))
    # Graph artifact — has ONLY source_artifact_ids pointing at the
    # compile artifact above. This is exactly what the RAGAnything
    # bridge writes via `_graph_drafts_from_storage`.
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="graph-1", kind="graph_json",
        source_artifact_ids=["compile-1"],
    ))
    # Enrichment artifact — same shape (source_artifact_ids only).
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="enrich-1", kind="enriched.tables",
        source_artifact_ids=["compile-1"],
    ))
    # Cross-run artifact — points at a DIFFERENT document's compile
    # output. Must NOT be pulled in.
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="other-compile", kind="compile",
        source_document_ids=["doc-other"],
    ))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="other-graph", kind="graph_json",
        source_artifact_ids=["other-compile"],
    ))

    summary = service.summarize_run(ctx, "run-1")

    # Compile + graph_json + enriched.tables all surface — the chain
    # walk hops through compile-1's id.
    assert summary.artifact_counts == {
        "compile": 1, "graph_json": 1, "enriched.tables": 1,
    }
    # Tabs flip available now that the artifacts are visible.
    views = summary.available_views
    assert views.graph.available is True
    assert views.assets.available is True


def test_summarize_run_lineage_walk_handles_two_hop_chains(
    service, run_store, artifact_registry, ctx,
):
    """Two-hop chain: compile → graph_json → graph-derived summary.
    Iterative fixed-point pulls in every step."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))

    artifact_registry.add(_make_artifact(
        ctx, artifact_id="compile-1", kind="compile",
        source_document_ids=["doc-A"],
    ))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="graph-1", kind="graph_json",
        source_artifact_ids=["compile-1"],
    ))
    # Hypothetical downstream artifact that depends on the graph
    # (not produced today, but the resolver should support arbitrary
    # depth).
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="graph-summary-1", kind="enriched.consistency_findings",
        source_artifact_ids=["graph-1"],
    ))

    summary = service.summarize_run(ctx, "run-1")

    assert summary.artifact_counts == {
        "compile": 1,
        "graph_json": 1,
        "enriched.consistency_findings": 1,
    }


def test_summarize_run_prefers_metadata_run_id_tag_over_lineage(
    service, run_store, artifact_registry, ctx,
):
    """When artifacts carry `metadata.run_id`, they are authoritative —
    the lineage fallback is bypassed and only tagged artifacts count."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))

    # Tagged for THIS run.
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="tagged", kind="chunk", byte_size=200,
            source_document_ids=["doc-A"],
            metadata={"run_id": "run-1"},
        )
    )
    # Tagged for a DIFFERENT run, even though lineage would match.
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="other-run", kind="chunk", byte_size=999,
            source_document_ids=["doc-A"],
            metadata={"run_id": "run-different"},
        )
    )
    # Untagged artifact, lineage matches — but tagged matches exist,
    # so this MUST be excluded (tagged wins).
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="legacy", kind="chunk", byte_size=400,
            source_document_ids=["doc-A"],
        )
    )

    summary = service.summarize_run(ctx, "run-1")

    assert summary.artifact_counts == {"chunk": 1}
    assert summary.total_bytes == 200


# ---- availableViews semantics ---------------------------------------


def test_summarize_run_marks_all_views_unavailable_when_no_artifacts(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run())

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.chunks.available is False
    assert views.chunks.reason  # reason populated
    assert views.assets.available is False
    assert views.graph.available is False
    assert views.quality.available is False
    assert views.raw_artifacts.available is False


def test_summarize_run_chunks_available_when_chunk_artifact_present(
    service, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="c1", kind="chunk",
            source_document_ids=["doc-A"],
        )
    )

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.chunks.available is True
    assert views.chunks.reason is None
    assert views.raw_artifacts.available is True


def test_summarize_run_graph_unavailable_reports_skipped_by_policy(
    service, run_store, ctx,
):
    """When step_results record GRAPH as skipped by policy, the
    availability reason must reflect that — not the generic fallback."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "GRAPH", "status": "skipped", "source": "policy",
             "required": False},
        ],
    }))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.graph.available is False
    assert "policy" in views.graph.reason.lower()


def test_summarize_run_graph_unavailable_reports_failure(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "GRAPH", "status": "failed", "source": "planner",
             "required": False},
        ],
    }))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.graph.available is False
    assert "fail" in views.graph.reason.lower()


def test_summarize_run_assets_available_for_enriched_kinds(
    service, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="t1", kind="enriched.tables",
            source_document_ids=["doc-A"],
        )
    )

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.assets.available is True


def test_summarize_run_planning_available_when_artifact_exists_without_audit_event(
    service, run_store, artifact_registry, ctx,
):
    """The planning_result artifact and the plan.revised audit event
    are written by INDEPENDENT code paths in the post-compile
    planning activity. If the audit-event reporter is None (or fails
    silently), the artifact still lands. The Planning Report tab
    must unlock on the artifact alone — gating it on the audit event
    only would leave the data invisible."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(
        _make_artifact(
            ctx, artifact_id="planning-1", kind="planning_result",
            source_document_ids=["doc-A"],
        )
    )

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.planning.available is True
    assert views.planning.reason is None


def test_summarize_run_quality_available_for_skipped_step_results(
    service, run_store, ctx,
):
    """Quality projector emits `skippedSteps[]` /
    `failedOptionalSteps[]` from step_results regardless of whether
    enrichment artifacts or warnings landed. The gate must unlock
    accordingly so reviewers can see those rows; otherwise a clean
    optional-skip leaves the data invisible."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "ENRICH", "status": "skipped", "source": "planner",
             "required": False, "reason": "text-only profile"},
        ],
    }))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.quality.available is True


def test_summarize_run_quality_available_for_failed_optional_step(
    service, run_store, ctx,
):
    """Failed-but-optional step result alone unlocks the Quality tab
    so reviewers see it under `failedOptionalSteps[]` even when no
    artifact or warning was emitted."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "GRAPH", "status": "failed", "source": "planner",
             "required": False, "error": {"type": "ActivityFailure",
                                            "message": "x"}},
        ],
    }))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.quality.available is True


def test_summarize_run_quality_stays_unavailable_when_only_required_failure(
    service, run_store, ctx,
):
    """A required-step failure is not actionable in the Quality tab
    (the run has FAILED status; the failure surfaces on Overview).
    Don't unlock Quality on it alone — without artifacts/warnings/
    skipped/optional-failed, there's nothing to render."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "COMPILE", "status": "failed", "source": "default",
             "required": True, "error": {"type": "ActivityFailure",
                                          "message": "x"}},
        ],
    }))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.quality.available is False


def test_summarize_run_unlocks_content_inventory_and_planning_for_full_split_run(
    service, run_store, artifact_registry, ctx,
):
    """End-to-end gate test mirroring the actual user scenario:
    a successful split-mode run produces three artifacts (parsed_source,
    parsed_content_manifest, planning_result) all tagged with the
    run's `run_id`. Both Content Inventory + Execution Plan tabs
    must unlock. The Knowledge Chunks tab stays gated on chunk
    artifacts (separate path).

    Pins the gating contract so a future regression that breaks
    either tab gets caught at PR time, not in production."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))

    for art_id, kind in [
        ("ps-1", "parsed_source"),
        ("manifest-1", "parsed_content_manifest"),
        ("planning-1", "planning_result"),
    ]:
        artifact_registry.add(_make_artifact(
            ctx, artifact_id=art_id, kind=kind,
            source_document_ids=["doc-A"],
            metadata={"run_id": "run-1"},
        ))

    views = service.summarize_run(ctx, "run-1").available_views

    assert views.parsed_content.available is True, (
        f"Content Inventory should unlock when parsed_content_manifest "
        f"is tagged with the run's run_id. Reason: {views.parsed_content.reason}"
    )
    assert views.parsed_content.reason is None
    assert views.planning.available is True, (
        f"Execution Plan should unlock when planning_result artifact "
        f"is tagged with the run's run_id, even without an audit "
        f"event. Reason: {views.planning.reason}"
    )
    assert views.planning.reason is None
    assert views.raw_artifacts.available is True


# ---- Step results ---------------------------------------------------


def test_summarize_run_hydrates_persisted_step_results(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {
                "step": "COMPILE", "status": "completed",
                "required": True, "source": "default",
                "duration_ms": 1234, "artifact_count": 5,
                "metadata": {"engine": "vlm"},
            },
            {
                "step": "GRAPH", "status": "failed",
                "required": False, "source": "planner",
                "error": {
                    "type": "TimeoutError",
                    "message": "graph build timed out",
                    "retryable": True,
                },
            },
        ],
    }))

    summary = service.summarize_run(ctx, "run-1")

    assert len(summary.steps) == 2
    compile_step = summary.steps[0]
    assert compile_step.step == "COMPILE"
    assert compile_step.status == "completed"
    assert compile_step.required is True
    assert compile_step.duration_ms == 1234
    assert compile_step.artifact_count == 5
    assert compile_step.metadata == {"engine": "vlm"}

    graph_step = summary.steps[1]
    assert graph_step.error is not None
    assert graph_step.error.type == "TimeoutError"
    assert graph_step.error.retryable is True


def test_summarize_run_drops_malformed_step_entries(
    service, run_store, ctx,
):
    """Defensive: a partial write or schema drift in one entry must
    not blow up the whole summary."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "COMPILE", "status": "completed", "required": True,
             "source": "default"},
            "not-a-dict",
            {"step": "ENRICH"},  # missing fields → coerced w/ defaults
        ],
    }))

    summary = service.summarize_run(ctx, "run-1")

    # The string entry is dropped; the under-specified dict survives
    # with default values.
    assert [s.step for s in summary.steps] == ["COMPILE", "ENRICH"]


# ---- Warnings -------------------------------------------------------


def test_summarize_run_collects_warning_progress_events(
    service, run_store, reporter, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="warns"))
    reporter.report_step_warning(
        ctx, run_id="warns", stage="ENRICH", step="EXTRACT_TABLES",
        message="page 7 had degraded confidence",
    )

    summary = service.summarize_run(ctx, "warns")

    assert len(summary.warnings) == 1
    warning = summary.warnings[0]
    assert warning.severity == "warning"
    assert warning.step == "EXTRACT_TABLES"
    assert "page 7" in warning.message


def test_summarize_run_excludes_info_severity_progress_events(
    service, run_store, reporter, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="info-only"))
    reporter.report_step_started(
        ctx, run_id="info-only", stage="COMPILE", step="parse",
    )

    summary = service.summarize_run(ctx, "info-only")

    assert summary.warnings == []


def test_summarize_run_warnings_isolate_by_run_id(
    service, run_store, reporter, ctx,
):
    """Audit log is shared across runs in one project — the warning
    filter must reject events for other run_ids."""
    run_store.upsert(ctx, _make_run(run_id="target"))
    reporter.report_step_warning(
        ctx, run_id="other-run", stage="ENRICH", step="x",
        message="not for us",
    )
    reporter.report_step_warning(
        ctx, run_id="target", stage="ENRICH", step="y",
        message="for us",
    )

    summary = service.summarize_run(ctx, "target")

    assert [w.message for w in summary.warnings] == ["for us"]


# ---- Quality summary projection -------------------------------------


def test_summarize_run_omits_quality_summary_when_no_data(
    service, run_store, ctx,
):
    run_store.upsert(ctx, _make_run())

    summary = service.summarize_run(ctx, "run-1")

    assert summary.quality_summary is None


def test_summarize_run_includes_quality_summary_when_warnings_present(
    service, run_store, reporter, ctx,
):
    run_store.upsert(ctx, _make_run(run_id="qs"))
    reporter.report_step_warning(
        ctx, run_id="qs", stage="ENRICH", step="x", message="sus",
    )

    summary = service.summarize_run(ctx, "qs")

    assert summary.quality_summary is not None
    assert summary.quality_summary.warning_count == 1


# =====================================================================
# Phase 2 — list_run_artifacts + read_run_artifact_content
# =====================================================================


def _write_artifact_file(
    workspace, ctx: ProjectContext,
    *, area: WorkspaceArea, location: str, body: bytes,
) -> None:
    """Mimic ProcessingService._register_draft's on-disk write —
    place the actual bytes at `<area>/<filename>` so the content
    endpoint has something to read."""
    full_path = workspace.area(ctx, area) / location.split("/", 1)[1]
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(body)


# ---- list_run_artifacts: pagination ---------------------------------


def test_list_run_artifacts_returns_paginated_results(
    service, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    for i in range(7):
        artifact_registry.add(_make_artifact(
            ctx, artifact_id=f"a{i:02d}", kind="chunk",
            source_document_ids=["doc-A"],
        ))

    page1 = service.list_run_artifacts(ctx, "run-1", page=1, page_size=3)
    page2 = service.list_run_artifacts(ctx, "run-1", page=2, page_size=3)
    page3 = service.list_run_artifacts(ctx, "run-1", page=3, page_size=3)

    assert page1.total == 7
    assert page2.total == 7
    assert page3.total == 7
    assert len(page1.items) == 3
    assert len(page2.items) == 3
    assert len(page3.items) == 1
    # No overlap.
    ids = [a.artifact_id for a in page1.items + page2.items + page3.items]
    assert len(ids) == len(set(ids))


def test_list_run_artifacts_filters_by_kind_after_run_scoping(
    service, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="c1", kind="chunk", source_document_ids=["doc-A"],
    ))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="t1", kind="enriched.tables",
        source_document_ids=["doc-A"],
    ))
    # Different document — must not show up under the run scope.
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="other", kind="chunk",
        source_document_ids=["doc-other"],
    ))

    page = service.list_run_artifacts(ctx, "run-1", kind="chunk")

    assert page.total == 1
    assert page.items[0].artifact_id == "c1"


def test_list_run_artifacts_returns_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.list_run_artifacts(ctx, "nope")


def test_list_run_artifacts_does_not_leak_cross_project_runs(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.list_run_artifacts(other_ctx, "leak")


def test_list_run_artifacts_clamps_page_size_to_max(
    service, run_store, artifact_registry, ctx,
):
    """The service trusts what it receives but caps page_size at
    MAX_PAGE_SIZE so a buggy caller can't blow up memory."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="a1", kind="chunk", source_document_ids=["doc-A"],
    ))

    page = service.list_run_artifacts(ctx, "run-1", page_size=10_000)

    assert page.page_size == 200  # MAX_PAGE_SIZE


def test_list_run_artifacts_returns_dto_fields(
    service, run_store, artifact_registry, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="a1", kind="chunk", byte_size=42,
        source_document_ids=["doc-A"],
        metadata={"engine": "vlm"},
    ))

    page = service.list_run_artifacts(ctx, "run-1")

    item = page.items[0]
    assert item.artifact_id == "a1"
    assert item.kind == "chunk"
    assert item.byte_size == 42
    assert item.source_document_ids == ["doc-A"]
    assert item.metadata == {"engine": "vlm"}


# ---- read_run_artifact_content: happy path --------------------------


def test_read_run_artifact_content_returns_bytes_and_media_type(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="a1", kind="chunk",
        source_document_ids=["doc-A"],
    ))
    _write_artifact_file(
        workspace, ctx, area=WorkspaceArea.COMPILED,
        location="compiled/a1.json",
        body=b'{"hello": "world"}',
    )

    content = service.read_run_artifact_content(ctx, "run-1", "a1")

    assert content.bytes == b'{"hello": "world"}'
    assert content.media_type == "application/json"
    assert content.is_inline is True
    assert content.filename == "a1.json"
    assert content.byte_size == 18


def test_read_run_artifact_content_unknown_extension_is_octet_stream(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    weird_record = ArtifactRecord(
        artifact_id="bin",
        project=ctx,
        kind="custom",
        location="compiled/bin.weirdext",
        content_hash="hash-bin",
        byte_size=4,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-A"],
    )
    artifact_registry.add(weird_record)
    _write_artifact_file(
        workspace, ctx, area=WorkspaceArea.COMPILED,
        location="compiled/bin.weirdext",
        body=b"\x01\x02\x03\x04",
    )

    content = service.read_run_artifact_content(ctx, "run-1", "bin")

    assert content.media_type == "application/octet-stream"
    assert content.is_inline is False  # FE downloads


# ---- read_run_artifact_content: not-found semantics -----------------


def test_read_run_artifact_content_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.read_run_artifact_content(ctx, "nope", "a1")


def test_read_run_artifact_content_404_when_artifact_belongs_to_another_run(
    service, run_store, artifact_registry, workspace, ctx,
):
    """An artifact tagged for a DIFFERENT run must not be readable
    via this run's content endpoint — even if the caller knows the
    artifact_id."""
    run_store.upsert(ctx, _make_run(run_id="run-mine", document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="other", kind="chunk",
        source_document_ids=["doc-other"],
        metadata={"run_id": "run-them"},
    ))

    with pytest.raises(ReviewNotFound):
        service.read_run_artifact_content(ctx, "run-mine", "other")


def test_read_run_artifact_content_404_when_bytes_missing_on_disk(
    service, run_store, artifact_registry, ctx,
):
    """Registry has the record but the file is gone — surface as
    not-found rather than 500. Same shape as a missing artifact, no
    filesystem state leak."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    artifact_registry.add(_make_artifact(
        ctx, artifact_id="ghost", kind="chunk",
        source_document_ids=["doc-A"],
    ))
    # No _write_artifact_file — file deliberately absent.

    with pytest.raises(ReviewNotFound):
        service.read_run_artifact_content(ctx, "run-1", "ghost")


# ---- Path-traversal guard ------------------------------------------


def test_read_run_artifact_content_rejects_path_traversal_in_location(
    service, run_store, artifact_registry, ctx,
):
    """A tampered registry — `location` contains `..` — must be
    rejected before any read happens. PathTraversalError is the
    typed signal; the REST layer will map it to 404 so callers
    can't probe for traversal-rejected paths."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    bad_record = ArtifactRecord(
        artifact_id="evil",
        project=ctx,
        kind="chunk",
        location="compiled/../../../etc/passwd",
        content_hash="hash-evil",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-A"],
    )
    artifact_registry.add(bad_record)

    with pytest.raises(PathTraversalError):
        service.read_run_artifact_content(ctx, "run-1", "evil")


def test_read_run_artifact_content_rejects_unknown_workspace_area(
    service, run_store, artifact_registry, ctx,
):
    """`location` first segment must name a known WorkspaceArea —
    `etc/passwd` is not one."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    bad_record = ArtifactRecord(
        artifact_id="badarea",
        project=ctx,
        kind="chunk",
        location="etc/passwd",
        content_hash="hash-bad",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_document_ids=["doc-A"],
    )
    artifact_registry.add(bad_record)

    with pytest.raises(ReviewNotFound):
        service.read_run_artifact_content(ctx, "run-1", "badarea")


# =====================================================================
# Phase 3 — list_run_chunks + get_run_chunk + iter_run_chunks_ndjson
# =====================================================================

import json as _json


def _write_chunks_artifact(
    workspace, ctx,
    *,
    artifact_id: str,
    payload,
    extension: str = ".json",
):
    """Write a chunk artifact's bytes under COMPILED.

    Returns the location string so the caller can register the
    matching `ArtifactRecord`."""
    location = f"compiled/{artifact_id}{extension}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / f"{artifact_id}{extension}"
    full.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        full.write_bytes(bytes(payload))
    elif isinstance(payload, str):
        full.write_text(payload, encoding="utf-8")
    else:
        full.write_text(_json.dumps(payload), encoding="utf-8")
    return location


def _register_chunk_artifact(
    artifact_registry, ctx,
    *,
    artifact_id: str,
    location: str,
    source_document_ids: list[str],
    metadata: dict | None = None,
):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=source_document_ids,
        metadata=metadata or {},
    ))


def _make_chunks_run(
    run_store, artifact_registry, workspace, ctx,
    *,
    chunks: list[dict],
    artifact_id: str = "ca1",
    run_id: str = "run-1",
):
    """Convenience: stand up a run + a single chunk artifact bundling
    the given list of chunk dicts."""
    run_store.upsert(ctx, _make_run(run_id=run_id, document_id="doc-A"))
    location = _write_chunks_artifact(
        workspace, ctx,
        artifact_id=artifact_id,
        payload={"chunks": chunks},
    )
    _register_chunk_artifact(
        artifact_registry, ctx,
        artifact_id=artifact_id, location=location,
        source_document_ids=["doc-A"],
    )


# ---- list_run_chunks: pagination ------------------------------------


def test_list_run_chunks_paginates(
    service, run_store, artifact_registry, workspace, ctx,
):
    chunks = [
        {"chunk_id": f"ch-{i}", "body": f"body {i}"}
        for i in range(7)
    ]
    _make_chunks_run(run_store, artifact_registry, workspace, ctx, chunks=chunks)

    page1 = service.list_run_chunks(ctx, "run-1", page=1, page_size=3)
    page2 = service.list_run_chunks(ctx, "run-1", page=2, page_size=3)
    page3 = service.list_run_chunks(ctx, "run-1", page=3, page_size=3)

    assert page1.total == 7
    assert page2.total == 7
    assert page3.total == 7
    assert len(page1.items) == 3
    assert len(page2.items) == 3
    assert len(page3.items) == 1
    ids = [c.chunk_id for c in page1.items + page2.items + page3.items]
    assert ids == [f"ch-{i}" for i in range(7)]


def test_list_run_chunks_filters_by_min_confidence(
    service, run_store, artifact_registry, workspace, ctx,
):
    chunks = [
        {"chunk_id": "high", "body": "x", "confidence": 0.95},
        {"chunk_id": "med", "body": "x", "confidence": 0.5},
        {"chunk_id": "low", "body": "x", "confidence": 0.1},
        {"chunk_id": "none", "body": "x"},  # no confidence — excluded
    ]
    _make_chunks_run(run_store, artifact_registry, workspace, ctx, chunks=chunks)

    page = service.list_run_chunks(ctx, "run-1", min_confidence=0.6)

    assert page.total == 1
    assert page.items[0].chunk_id == "high"


def test_list_run_chunks_filters_by_status(
    service, run_store, artifact_registry, workspace, ctx,
):
    chunks = [
        {"chunk_id": "ok", "body": "x", "metadata": {"status": "approved"}},
        {"chunk_id": "ko", "body": "x", "metadata": {"status": "rejected"}},
        {"chunk_id": "no-meta", "body": "x"},
    ]
    _make_chunks_run(run_store, artifact_registry, workspace, ctx, chunks=chunks)

    page = service.list_run_chunks(ctx, "run-1", status="APPROVED")

    assert page.total == 1
    assert page.items[0].chunk_id == "ok"


def test_list_run_chunks_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.list_run_chunks(ctx, "nope")


def test_list_run_chunks_does_not_leak_cross_project(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.list_run_chunks(other_ctx, "leak")


def test_list_run_chunks_clamps_page_size(
    service, run_store, artifact_registry, workspace, ctx,
):
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "c", "body": "x"}],
    )

    page = service.list_run_chunks(ctx, "run-1", page_size=10_000)

    assert page.page_size == 200


# ---- get_run_chunk -------------------------------------------------


def test_get_run_chunk_returns_full_body_and_lineage(
    service, run_store, artifact_registry, workspace, ctx,
):
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[
            {"chunk_id": "ch-target", "body": "the full body", "tokenCount": 3},
            {"chunk_id": "ch-other", "body": "different"},
        ],
    )

    detail = service.get_run_chunk(ctx, "run-1", "ch-target")

    assert detail.chunk_id == "ch-target"
    assert detail.body == "the full body"
    assert detail.token_count == 3
    assert detail.lineage["documentIds"] == ["doc-A"]
    assert detail.lineage["sourceArtifactId"] == "ca1"
    assert detail.lineage["stage"] == "compile"


def test_get_run_chunk_404_for_unknown_chunk(
    service, run_store, artifact_registry, workspace, ctx,
):
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "exists", "body": "x"}],
    )

    with pytest.raises(ReviewNotFound):
        service.get_run_chunk(ctx, "run-1", "missing")


def test_get_run_chunk_does_not_leak_chunks_from_other_runs(
    service, run_store, artifact_registry, workspace, ctx,
):
    """A chunk that exists in a different run must not be readable
    via this run's detail endpoint, even if the caller knows the id."""
    # Run A — chunks tagged for it.
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "secret", "body": "hidden"}],
        artifact_id="ca-a", run_id="run-a",
    )
    # Tag the artifact for run-a so lineage doesn't accidentally
    # match run-b.
    artifact = artifact_registry.get(ctx, "ca-a")
    artifact.metadata["run_id"] = "run-a"

    # Run B — separate document, no overlap.
    run_store.upsert(ctx, _make_run(run_id="run-b", document_id="doc-B"))

    with pytest.raises(ReviewNotFound):
        service.get_run_chunk(ctx, "run-b", "secret")


# ---- iter_run_chunks_ndjson ----------------------------------------


def test_iter_run_chunks_ndjson_yields_one_line_per_chunk(
    service, run_store, artifact_registry, workspace, ctx,
):
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[
            {"chunk_id": "ch-1", "body": "first"},
            {"chunk_id": "ch-2", "body": "second"},
        ],
    )

    blob = b"".join(service.iter_run_chunks_ndjson(ctx, "run-1"))
    lines = [ln for ln in blob.split(b"\n") if ln]

    assert len(lines) == 2
    parsed = [_json.loads(ln) for ln in lines]
    assert [p["chunkId"] for p in parsed] == ["ch-1", "ch-2"]


def test_iter_run_chunks_ndjson_validates_eagerly(service, ctx):
    """The 404 must propagate at call time — not when the consumer
    starts iterating. Required so REST returns a clean 404 before
    StreamingResponse commits a 200."""
    with pytest.raises(ReviewNotFound):
        service.iter_run_chunks_ndjson(ctx, "missing")


def test_iter_run_chunks_ndjson_isolates_runs(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Stream must only contain THIS run's chunks."""
    # Run A
    _make_chunks_run(
        run_store, artifact_registry, workspace, ctx,
        chunks=[{"chunk_id": "from-a", "body": "x"}],
        artifact_id="ca-a", run_id="run-a",
    )
    artifact_registry.get(ctx, "ca-a").metadata["run_id"] = "run-a"
    # Run B
    run_store.upsert(ctx, _make_run(run_id="run-b", document_id="doc-B"))

    blob = b"".join(service.iter_run_chunks_ndjson(ctx, "run-b"))

    assert blob == b""


# =====================================================================
# Phase 5 — get_run_quality_report
# =====================================================================


def _write_quality_artifact(
    workspace, ctx,
    *,
    artifact_id: str,
    kind: str,
    payload: dict,
    extension: str = ".json",
) -> str:
    """Write a quality-report artifact under ENRICHED. Returns the
    location string for registration."""
    from j1.workspace.layout import WorkspaceArea  # local re-use

    location = f"enriched/{artifact_id}{extension}"
    full = workspace.area(ctx, WorkspaceArea.ENRICHED) / f"{artifact_id}{extension}"
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_json.dumps(payload), encoding="utf-8")
    return location


def _register_quality_artifact(
    artifact_registry, ctx,
    *,
    artifact_id: str,
    kind: str,
    location: str,
    source_document_ids: list[str],
    metadata: dict | None = None,
):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=kind,
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now, updated_at=now,
        source_document_ids=source_document_ids,
        metadata=metadata or {},
    ))


def test_get_quality_report_returns_empty_when_run_has_no_quality_data(
    service, run_store, ctx,
):
    """Run exists but no enrichment artifacts, no warnings, no
    step_results — report fields are all empty / None, but the
    endpoint must still succeed."""
    run_store.upsert(ctx, _make_run())

    report = service.get_run_quality_report(ctx, "run-1")

    assert report.overall_confidence is None
    assert report.modality_confidences == []
    assert report.warnings == []
    assert report.skipped_steps == []
    assert report.failed_optional_steps == []
    assert report.low_confidence_findings == []
    assert report.raw_debug is None


def test_get_quality_report_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_quality_report(ctx, "nope")


def test_get_quality_report_does_not_leak_cross_project_runs(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.get_run_quality_report(other_ctx, "leak")


def test_get_quality_report_composes_all_sources(
    service, run_store, artifact_registry, workspace, reporter, ctx,
):
    """End-to-end inside the service: confidence + consistency
    artifacts + audit warnings + persisted step_results all flow
    into one DTO."""
    run_store.upsert(ctx, _make_run(
        document_id="doc-A",
        metadata={
            "step_results": [
                {"step": "compile", "status": "completed",
                 "required": True, "source": "caller"},
                {"step": "graph", "status": "skipped",
                 "required": False, "source": "policy",
                 "reason": "text-only mode"},
                {"step": "enrich", "status": "failed",
                 "required": False, "source": "planner",
                 "reason": "vision LLM down",
                 "error": {"type": "VisionUnavailableError"}},
            ],
        },
    ))

    # Confidence assessment artifact.
    location_ca = _write_quality_artifact(
        workspace, ctx,
        artifact_id="ca1",
        kind="enriched.confidence_assessment",
        payload={
            "assessments": [
                {"modality": "tables", "confidence": 0.9, "sample_count": 4},
                {"modality": "ocr", "confidence": 0.4,
                 "page": 7, "category": "low_confidence",
                 "message": "OCR uncertain on page 7"},
            ],
        },
    )
    _register_quality_artifact(
        artifact_registry, ctx,
        artifact_id="ca1",
        kind="enriched.confidence_assessment",
        location=location_ca,
        source_document_ids=["doc-A"],
    )

    # Consistency findings artifact.
    location_cf = _write_quality_artifact(
        workspace, ctx,
        artifact_id="cf1",
        kind="enriched.consistency_findings",
        payload={
            "findings": [
                {"page": 3, "category": "duplicate",
                 "message": "duplicate definition", "score": 0.2},
            ],
        },
    )
    _register_quality_artifact(
        artifact_registry, ctx,
        artifact_id="cf1",
        kind="enriched.consistency_findings",
        location=location_cf,
        source_document_ids=["doc-A"],
    )

    # Audit warning.
    reporter.report_step_warning(
        ctx, run_id="run-1", stage="ENRICH", step="EXTRACT_TABLES",
        message="page 9 degraded",
    )

    report = service.get_run_quality_report(ctx, "run-1")

    # Modality breakdown shows both modalities.
    by_modality = {m.modality: m for m in report.modality_confidences}
    assert by_modality["tables"].confidence == 0.9
    assert by_modality["ocr"].confidence == 0.4
    # Overall = mean of the two = 0.65
    assert report.overall_confidence == 0.65
    # Low-confidence findings: 1 from confidence (ocr score < 0.7) + 1
    # from consistency.
    assert len(report.low_confidence_findings) == 2
    pages = {f.page for f in report.low_confidence_findings}
    assert pages == {7, 3}
    # Step splits.
    assert [s.step for s in report.skipped_steps] == ["graph"]
    assert [f.step for f in report.failed_optional_steps] == ["enrich"]
    # Warnings pass through with traceability.
    assert len(report.warnings) == 1
    assert report.warnings[0].step == "EXTRACT_TABLES"
    # Raw debug not populated by default.
    assert report.raw_debug is None


def test_get_quality_report_include_raw_exposes_payload(
    service, run_store, artifact_registry, workspace, ctx,
):
    """`include_raw=True` must surface the unprojected source JSON
    under `rawDebug` — for debugging only, never the default."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    payload = {"default_confidence": 0.7, "assessments": []}
    location = _write_quality_artifact(
        workspace, ctx, artifact_id="ca1",
        kind="enriched.confidence_assessment",
        payload=payload,
    )
    _register_quality_artifact(
        artifact_registry, ctx,
        artifact_id="ca1",
        kind="enriched.confidence_assessment",
        location=location,
        source_document_ids=["doc-A"],
    )

    report = service.get_run_quality_report(ctx, "run-1", include_raw=True)

    assert report.raw_debug is not None
    assert report.raw_debug["confidence_assessment"][0] == payload
    assert report.raw_debug["consistency_findings"] == []


def test_get_quality_report_isolates_runs_via_artifact_resolution(
    service, run_store, artifact_registry, workspace, ctx,
):
    """A confidence artifact tagged for run-A must NOT appear in
    run-B's quality report (Phase 4 tag wins over lineage)."""
    # Run A — has the confidence artifact tagged.
    run_store.upsert(ctx, _make_run(run_id="run-a", document_id="doc-A"))
    location_a = _write_quality_artifact(
        workspace, ctx, artifact_id="ca-a",
        kind="enriched.confidence_assessment",
        payload={"default_confidence": 0.91},
    )
    _register_quality_artifact(
        artifact_registry, ctx,
        artifact_id="ca-a",
        kind="enriched.confidence_assessment",
        location=location_a,
        source_document_ids=["doc-A"],
        metadata={"run_id": "run-a"},
    )

    # Run B — separate document, no artifacts.
    run_store.upsert(ctx, _make_run(run_id="run-b", document_id="doc-B"))

    report_b = service.get_run_quality_report(ctx, "run-b")

    assert report_b.overall_confidence is None


# =====================================================================
# Phase 6 — get_run_graph
# =====================================================================


def _write_graph_artifact(
    workspace, ctx,
    *,
    artifact_id: str,
    filename: str,
    payload: dict | list,
) -> str:
    """Write a graph_json artifact under GRAPH. Returns location."""
    location = f"graph/{filename}"
    full = workspace.area(ctx, WorkspaceArea.GRAPH) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_json.dumps(payload), encoding="utf-8")
    return location


def _register_graph_artifact(
    artifact_registry, ctx,
    *,
    artifact_id: str,
    location: str,
    source_document_ids: list[str],
    metadata: dict | None = None,
):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="graph_json",
        location=location,
        content_hash=f"hash-{artifact_id}",
        byte_size=0,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now, updated_at=now,
        source_document_ids=source_document_ids,
        metadata=metadata or {},
    ))


def test_get_run_graph_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_graph(ctx, "nope")


def test_get_run_graph_does_not_leak_cross_project(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.get_run_graph(other_ctx, "leak")


def test_get_run_graph_returns_unavailable_with_default_reason(
    service, run_store, ctx,
):
    """No graph artifacts AND no step_results → generic fallback
    reason. Same copy as the run summary's
    `availableViews.graph.reason` field."""
    run_store.upsert(ctx, _make_run())
    snapshot = service.get_run_graph(ctx, "run-1")
    assert snapshot.unavailable is not None
    assert "graph" in snapshot.unavailable.reason.lower()
    assert snapshot.entities == []
    assert snapshot.relations == []


def test_get_run_graph_unavailable_reports_skipped_by_policy(
    service, run_store, ctx,
):
    """When step_results record GRAPH skipped by policy, the graph
    snapshot's unavailable.reason matches the availability resolver's
    copy. Single source of truth proven end-to-end."""
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "graph", "status": "skipped", "source": "policy",
             "required": False},
        ],
    }))
    snapshot = service.get_run_graph(ctx, "run-1")
    assert "policy" in snapshot.unavailable.reason.lower()


def test_get_run_graph_unavailable_reports_failure(service, run_store, ctx):
    run_store.upsert(ctx, _make_run(metadata={
        "step_results": [
            {"step": "graph", "status": "failed", "source": "planner",
             "required": False},
        ],
    }))
    snapshot = service.get_run_graph(ctx, "run-1")
    assert "fail" in snapshot.unavailable.reason.lower()


def test_get_run_graph_projects_lightrag_entities(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    location = _write_graph_artifact(
        workspace, ctx, artifact_id="ge1",
        filename="vdb_entities.json",
        payload={
            "alice": {
                "__id__": "alice",
                "__name__": "Alice",
                "__entity_type__": "PERSON",
                "__source_id__": "chunk-1;chunk-2",
                "__vector__": [0.1, 0.2],
            },
        },
    )
    _register_graph_artifact(
        artifact_registry, ctx,
        artifact_id="ge1", location=location,
        source_document_ids=["doc-A"],
    )

    snapshot = service.get_run_graph(ctx, "run-1")

    assert snapshot.unavailable is None
    assert snapshot.stats.entity_count == 1
    assert snapshot.entities[0].id == "alice"
    assert snapshot.entities[0].source_chunk_ids == ["chunk-1", "chunk-2"]
    # Vendor-internal vector dropped from neutral metadata.
    assert "__vector__" not in snapshot.entities[0].metadata


def test_get_run_graph_truncates_when_over_max_nodes(
    service, run_store, artifact_registry, workspace, ctx,
):
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    location = _write_graph_artifact(
        workspace, ctx, artifact_id="ge1",
        filename="vdb_entities.json",
        payload=[{"id": f"e{i}", "name": f"Entity {i}"} for i in range(20)],
    )
    _register_graph_artifact(
        artifact_registry, ctx,
        artifact_id="ge1", location=location,
        source_document_ids=["doc-A"],
    )

    snapshot = service.get_run_graph(ctx, "run-1", max_nodes=5)

    assert snapshot.stats.entity_count == 20
    assert len(snapshot.entities) == 5
    assert snapshot.truncated.entities is True
    assert snapshot.truncated.relations is False
    assert snapshot.truncated.limits.max_nodes == 5


def test_get_run_graph_isolates_runs_via_artifact_tagging(
    service, run_store, artifact_registry, workspace, ctx,
):
    """A graph artifact tagged for run-A must NOT appear in run-B's
    graph snapshot — Phase 4 tag wins over lineage."""
    run_store.upsert(ctx, _make_run(run_id="run-a", document_id="doc-A"))
    location_a = _write_graph_artifact(
        workspace, ctx, artifact_id="ge-a",
        filename="vdb_entities.json",
        payload=[{"id": "secret"}],
    )
    _register_graph_artifact(
        artifact_registry, ctx,
        artifact_id="ge-a", location=location_a,
        source_document_ids=["doc-A"],
        metadata={"run_id": "run-a"},
    )

    run_store.upsert(ctx, _make_run(run_id="run-b", document_id="doc-B"))

    snapshot_b = service.get_run_graph(ctx, "run-b")

    # Run B has no graph artifacts → unavailable.
    assert snapshot_b.unavailable is not None
    assert snapshot_b.entities == []


# ---- Content Inventory (parsed-content manifest) -----------------


def _write_parsed_content_manifest(
    workspace, ctx,
    *,
    artifact_id: str,
    payload: dict,
) -> str:
    """Write a parsed_content_manifest artifact under COMPILED.
    Returns the location string for registry registration."""
    filename = f"{artifact_id}.parsed_content_manifest.json"
    location = f"compiled/{filename}"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_json.dumps(payload), encoding="utf-8")
    return location


def _register_parsed_content_manifest(
    artifact_registry, ctx,
    *,
    artifact_id: str,
    location: str,
    document_id: str,
) -> None:
    from j1.processing.results import ARTIFACT_KIND_PARSED_CONTENT_MANIFEST

    record = _make_artifact(
        ctx,
        artifact_id=artifact_id,
        kind=ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
        source_document_ids=[document_id],
    )
    record = ArtifactRecord(
        artifact_id=record.artifact_id,
        project=record.project,
        kind=record.kind,
        location=location,
        content_hash=record.content_hash,
        byte_size=record.byte_size,
        status=record.status,
        review_status=record.review_status,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        source_document_ids=record.source_document_ids,
        source_artifact_ids=record.source_artifact_ids,
        metadata=record.metadata,
    )
    artifact_registry.add(record)


def _make_manifest_payload(
    *,
    document_id: str = "doc-1",
    parser: str = "raganything",
    parse_method: str = "auto",
    text_blocks: int = 5,
    images: int = 2,
    tables: int = 1,
    equations: int = 0,
    page_count: int | None = 4,
    items: list[dict] | None = None,
) -> dict:
    return {
        "document_id": document_id,
        "document_hash": "sha256:abc",
        "parser": parser,
        "parser_version": "0.1",
        "parse_method": parse_method,
        "profile": None,
        "stats": {
            "text_blocks": text_blocks,
            "images": images,
            "tables": tables,
            "equations": equations,
            "scanned_pages": None,
            "decorative_images": None,
            "diagrams": None,
            "total_items": text_blocks + images + tables + equations,
            "page_count": page_count,
            "text_chars": 0,
            "text_extractable_ratio": None,
            "parse_quality_score": None,
            "text_sufficiency_score": None,
            "layout_complexity_score": None,
        },
        "items": items or [],
        "warnings": [],
        "manifest_schema_version": "1",
    }


def test_get_run_content_inventory_unavailable_when_no_manifest(
    service, run_store, ctx,
):
    """Legacy run / mid-compile run / failed run → unavailable status
    with the operator-readable reason from the availability resolver."""
    run_store.upsert(ctx, _make_run())
    inventory = service.get_run_content_inventory(ctx, "run-1")
    assert inventory.status == "unavailable"
    assert inventory.unavailable_reason
    assert inventory.summary.total_items == 0
    assert inventory.items == []


def test_get_run_content_inventory_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_content_inventory(ctx, "nope")


def test_get_run_content_inventory_does_not_leak_cross_project(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.get_run_content_inventory(other_ctx, "leak")


def test_get_run_content_inventory_returns_completed_with_summary(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Manifest exists with non-zero items → status=completed,
    summary populated, source identifies the parser."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    location = _write_parsed_content_manifest(
        workspace, ctx,
        artifact_id="m1",
        payload=_make_manifest_payload(
            document_id="doc-A",
            text_blocks=10,
            images=3,
            tables=2,
        ),
    )
    _register_parsed_content_manifest(
        artifact_registry, ctx,
        artifact_id="m1", location=location, document_id="doc-A",
    )

    inventory = service.get_run_content_inventory(ctx, "run-1")
    assert inventory.status == "completed"
    assert inventory.summary.text_block_count == 10
    assert inventory.summary.image_count == 3
    assert inventory.summary.table_count == 2
    assert inventory.summary.page_count == 4
    assert inventory.source.parser == "raganything"
    assert inventory.source.compiler == "raganything"
    assert inventory.source.parse_method == "auto"
    assert inventory.raw_artifact_id == "m1"


def test_get_run_content_inventory_marks_empty_when_zero_items(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Manifest exists but parser produced nothing → status=empty
    (distinct from unavailable). FE can render a different empty
    state for this case."""
    run_store.upsert(ctx, _make_run(document_id="doc-empty"))
    location = _write_parsed_content_manifest(
        workspace, ctx,
        artifact_id="m1",
        payload=_make_manifest_payload(
            document_id="doc-empty",
            text_blocks=0, images=0, tables=0, equations=0,
            page_count=0,
        ),
    )
    _register_parsed_content_manifest(
        artifact_registry, ctx,
        artifact_id="m1", location=location, document_id="doc-empty",
    )

    inventory = service.get_run_content_inventory(ctx, "run-1")
    assert inventory.status == "empty"
    assert inventory.summary.total_items == 0


def test_get_run_content_inventory_aggregates_across_documents(
    service, run_store, artifact_registry, workspace, ctx,
):
    """Multi-document run → counts are summed across manifests.
    The run's `target_document_ids` metadata declares which docs the
    run covered; the resolver scopes artifact lookup to that set."""
    run_store.upsert(ctx, _make_run(
        document_id="doc-A",
        metadata={"target_document_ids": ["doc-A", "doc-B"]},
    ))
    for i, doc_id in enumerate(("doc-A", "doc-B"), start=1):
        location = _write_parsed_content_manifest(
            workspace, ctx,
            artifact_id=f"m{i}",
            payload=_make_manifest_payload(
                document_id=doc_id,
                text_blocks=4, images=1, tables=0,
                page_count=3,
            ),
        )
        _register_parsed_content_manifest(
            artifact_registry, ctx,
            artifact_id=f"m{i}", location=location, document_id=doc_id,
        )

    inventory = service.get_run_content_inventory(ctx, "run-1")
    assert inventory.status == "completed"
    assert inventory.summary.text_block_count == 8
    assert inventory.summary.image_count == 2
    assert inventory.summary.page_count == 6  # 3 + 3


def test_available_views_includes_parsed_content_when_manifest_present(
    service, run_store, artifact_registry, workspace, ctx,
):
    """The summary endpoint's `availableViews.parsedContent.available`
    flips to True the moment the compile bridge emits a manifest
    artifact. This is the contract the Content Inventory tab
    depends on for progressive visibility — tab unlocks while
    enrich/graph/index are still running."""
    run_store.upsert(ctx, _make_run(
        document_id="doc-A",
        status=RunStatus.RUNNING,  # mid-flight, NOT terminal
    ))
    location = _write_parsed_content_manifest(
        workspace, ctx,
        artifact_id="m1",
        payload=_make_manifest_payload(),
    )
    _register_parsed_content_manifest(
        artifact_registry, ctx,
        artifact_id="m1", location=location, document_id="doc-A",
    )

    summary = service.summarize_run(ctx, "run-1")
    assert summary.available_views.parsed_content.available is True
    assert summary.available_views.parsed_content.reason is None


def test_available_views_parsed_content_reason_when_compile_in_progress(
    service, run_store, ctx,
):
    """Mid-compile run with no manifest yet → reason explains the
    in-progress state so the FE shows a "waiting for parser"
    message rather than the generic empty state."""
    run_store.upsert(ctx, _make_run(status=RunStatus.RUNNING))
    summary = service.summarize_run(ctx, "run-1")
    assert summary.available_views.parsed_content.available is False
    assert "manifest" in summary.available_views.parsed_content.reason.lower()


# ---- Planning Report ----------------------------------------------


def _emit_plan_generated(reporter, ctx, *, run_id: str, plan_payload: dict):
    """Test-only helper: write a `plan.generated` audit entry the
    Planning Report projector picks up."""
    reporter.report_plan_generated(ctx, run_id=run_id, plan_payload=plan_payload)


def _basic_plan_payload(*, fast_llm_used: bool = False) -> dict:
    """Minimum-shape plan payload mirroring what
    `_emit_plan_generated` writes in the workflow."""
    return {
        "document_id": "doc-1",
        "mode": "text_only",
        "policy": "auto",
        "confidence": 0.85,
        "estimated_cost_level": "low",
        "fast_llm_used": fast_llm_used,
        "requires_vision": False,
        "requires_premium_llm": False,
        "warnings": [],
        "steps": [
            {
                "name": "compile",
                "step_id": "compile",
                "stage": "COMPILE",
                "decision": "RUN",
                "enabled": True,
                "required": True,
                "source": "planner",
                "estimated_cost_tier": "MEDIUM",
                "risk_level": "low",
                "llm_class": "none",
                "dependency_step_ids": [],
            },
            {
                "name": "enrich",
                "step_id": "enrich",
                "stage": "ENRICH",
                "decision": "SKIP",
                "enabled": False,
                "required": False,
                "source": "planner",
                "reason": "mode text_only does not include enrichment",
                "estimated_cost_tier": "MEDIUM",
                "risk_level": "low",
                "llm_class": "none",
                "dependency_step_ids": ["compile"],
            },
        ],
        "profile": {"extension": ".txt"},
        "vision_decisions": [],
    }


def test_get_run_planning_unavailable_for_run_without_plan_event(
    service, run_store, ctx,
):
    """Run exists but planner never emitted → status=unavailable
    with the operator-readable reason from the availability resolver."""
    run_store.upsert(ctx, _make_run())
    report = service.get_run_planning(ctx, "run-1")
    assert report.status == "unavailable"
    assert report.unavailable_reason
    assert report.decisions == []
    assert report.assessment is None


def test_get_run_planning_404_for_missing_run(service, ctx):
    with pytest.raises(ReviewNotFound):
        service.get_run_planning(ctx, "nope")


def test_get_run_planning_does_not_leak_cross_project(
    service, run_store, ctx, other_ctx,
):
    run_store.upsert(ctx, _make_run(run_id="leak"))
    with pytest.raises(ReviewNotFound):
        service.get_run_planning(other_ctx, "leak")


def test_get_run_planning_projects_assessment_and_decisions(
    service, run_store, reporter, ctx,
):
    """When a `plan.generated` event exists, the projector returns
    `status=completed` and projects every PlannedStep into a
    PlanningStepDecisionDTO."""
    run_store.upsert(ctx, _make_run())
    _emit_plan_generated(
        reporter, ctx,
        run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )
    report = service.get_run_planning(ctx, "run-1")
    assert report.status == "completed"
    assert report.assessment is not None
    assert report.assessment.mode == "text_only"
    assert report.assessment.policy == "auto"
    assert report.assessment.confidence == pytest.approx(0.85)
    assert report.revised is False

    by_step = {d.step_id: d for d in report.decisions}
    assert by_step["compile"].decision == "RUN"
    assert by_step["compile"].required is True
    assert by_step["enrich"].decision == "SKIP"
    assert by_step["enrich"].reason
    # Decision reasons surface in the assessment block too — useful
    # for the FE's "why this plan" panel.
    assert any("does not include enrichment" in r for r in report.assessment.reasons)


def test_get_run_planning_marks_revised_when_replan_event_seen(
    service, run_store, reporter, ctx,
):
    """A subsequent `plan.revised` overrides `plan.generated` and
    flips the `revised` flag so the FE can badge the report."""
    run_store.upsert(ctx, _make_run())
    payload = _basic_plan_payload()
    _emit_plan_generated(reporter, ctx, run_id="run-1", plan_payload=payload)
    revised = {**payload, "confidence": 0.91, "mode": "table_aware"}
    reporter.report_plan_revised(
        ctx, run_id="run-1",
        plan_payload=revised,
        reason="post-compile signals updated mode",
    )

    report = service.get_run_planning(ctx, "run-1")
    assert report.status == "completed"
    assert report.revised is True
    assert report.assessment.mode == "table_aware"
    assert report.assessment.confidence == pytest.approx(0.91)


def test_get_run_planning_includes_content_digest_when_manifest_exists(
    service, run_store, reporter, artifact_registry, workspace, ctx,
):
    """The digest panel pulls counts from the parsed-content manifest
    (when present) and records the deployment's privacy caps so
    reviewers can audit what an LLM planner would see."""
    run_store.upsert(ctx, _make_run(document_id="doc-A"))
    location = _write_parsed_content_manifest(
        workspace, ctx,
        artifact_id="m1",
        payload=_make_manifest_payload(
            document_id="doc-A", text_blocks=50, images=1, tables=1,
        ),
    )
    _register_parsed_content_manifest(
        artifact_registry, ctx,
        artifact_id="m1", location=location, document_id="doc-A",
    )
    _emit_plan_generated(
        reporter, ctx,
        run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )

    report = service.get_run_planning(ctx, "run-1")
    assert report.digest is not None
    assert report.digest.text_block_count == 50
    # Default cap from PlanningSettings — sample never exceeds the cap.
    assert report.digest.sampled_block_count == 20
    assert report.digest.max_preview_chars == 300


def test_get_run_planning_llm_recommendation_disabled_by_default(
    service, run_store, reporter, ctx,
):
    """Default settings → llm_planning_enabled=False → status=disabled
    so the FE renders the rule-based-only copy."""
    run_store.upsert(ctx, _make_run())
    _emit_plan_generated(
        reporter, ctx,
        run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )
    report = service.get_run_planning(ctx, "run-1")
    assert report.llm_recommendation.status == "disabled"


def test_get_run_planning_llm_recommendation_advisory_when_enabled(
    run_store, artifact_registry, workspace, reporter, ctx,
):
    """Feature-flagged: when J1_LLM_PLANNING_ENABLED=true, the
    projector surfaces an advisory recommendation block. Phase 2
    will replace the placeholder copy with a real LLM call."""
    from j1.ingestion_review import IngestionResultReviewService
    from j1.processing.planning_settings import PlanningSettings

    enabled = IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
        planning_settings=PlanningSettings(llm_planning_enabled=True),
    )
    run_store.upsert(ctx, _make_run())
    _emit_plan_generated(
        reporter, ctx,
        run_id="run-1",
        plan_payload=_basic_plan_payload(fast_llm_used=True),
    )
    report = enabled.get_run_planning(ctx, "run-1")
    assert report.llm_recommendation.status == "advisory"
    assert report.llm_recommendation.model_profile == "fast_planner"


def test_summary_available_views_planning_flips_when_event_seen(
    service, run_store, reporter, ctx,
):
    """`/summary` picks up the planning event for tab gating without
    a separate fetch — same progressive-availability contract the
    Content Inventory tab uses."""
    run_store.upsert(ctx, _make_run(status=RunStatus.RUNNING))
    summary_before = service.summarize_run(ctx, "run-1")
    assert summary_before.available_views.planning.available is False
    assert summary_before.available_views.planning.reason

    _emit_plan_generated(
        reporter, ctx,
        run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )
    summary_after = service.summarize_run(ctx, "run-1")
    assert summary_after.available_views.planning.available is True
    assert summary_after.available_views.planning.reason is None


# ---- Post-compile planning_result artifact projection -----------


def _write_planning_result_artifact(
    workspace, artifact_registry, ctx, *,
    run_id: str, document_id: str,
    source: str = "rule_based",
    document_type: str = "system_requirement_specification",
):
    """Persist a minimal `planning_result.json` and register it."""
    payload = {
        "run_id": run_id,
        "document_id": document_id,
        "planning_version": "1.0",
        "planning_phase": "post_compile",
        "source": source,
        "created_at": "2026-05-09T00:00:00Z",
        "recommended_profile": "premium",
        "confidence": 0.84,
        "document_understanding": {
            "title_source": "title_block",
            "detected_title": "System Requirement Specification for J1",
            "title_quality": "clear",
            "document_type": document_type,
            "document_type_confidence": 0.85,
            "intended_audience": "technical_team",
            "document_importance": "high",
            "expected_information_types": ["requirements", "risks"],
            "recommended_analysis_bias": {
                "prefer_requirement_extraction": True,
                "prefer_risk_extraction": True,
                "reason": "SRS — requirement + risk extraction.",
            },
        },
        "decision_summary": {
            "overall_assessment": "Premium ingestion for SRS.",
            "main_reasoning": ["High-value requirements document."],
        },
        "content_report": {
            "page_count": 12, "structure_quality": "good",
            "has_tables": True, "has_images": False,
        },
        "quality_report": {
            "parse_confidence": "high", "risk_level": "low",
            "manual_review_required": False,
            "detected_issues": [], "manual_review_candidates": [],
        },
        "execution_plan": {
            "estimated_time": "medium",
            "estimated_cost": "medium",
            "steps": {
                "chunking": {
                    "enabled": True, "strategy": "section_aware",
                    "reason": "Clear headings.",
                },
                "table_enrichment": {
                    "enabled": True, "scope": "selected_pages",
                    "pages": [4, 5], "reason": "Requirements tables.",
                },
                "vision_enrichment": {
                    "enabled": False, "scope": "none",
                    "pages": [], "reason": "No images.",
                },
                "graph_extraction": {
                    "enabled": True, "scope": "document",
                    "reason": "SRS — relationships.",
                    "candidate_entity_types": ["requirement", "actor"],
                },
                "embedding": {
                    "enabled": True, "scope": "document",
                    "reason": "Required for retrieval.",
                },
                "indexing": {
                    "enabled": True, "scope": "document",
                    "reason": "Required for retrieval.",
                },
            },
        },
        "rule_based_assessment": {
            "recommended_profile": "premium",
            "signals": {"has_meaningful_tables": True},
        },
        "rule_based_comparison": {},
        "warnings": [],
        "next_actions": [],
    }
    filename = f"planning_{run_id}_{document_id}.json"
    full = workspace.area(ctx, WorkspaceArea.COMPILED) / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    full.write_text(_json.dumps(payload), encoding="utf-8")

    from j1.processing.results import ARTIFACT_KIND_PLANNING_RESULT
    record = _make_artifact(
        ctx,
        artifact_id=f"planning_{run_id}_{document_id}",
        kind=ARTIFACT_KIND_PLANNING_RESULT,
        source_document_ids=[document_id],
        metadata={"run_id": run_id},
    )
    record = ArtifactRecord(
        artifact_id=record.artifact_id,
        project=record.project,
        kind=record.kind,
        location=f"compiled/{filename}",
        content_hash=record.content_hash,
        byte_size=record.byte_size,
        status=record.status,
        review_status=record.review_status,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        source_document_ids=record.source_document_ids,
        source_artifact_ids=record.source_artifact_ids,
        metadata=record.metadata,
    )
    artifact_registry.add(record)
    return record.artifact_id


def test_get_run_planning_prefers_artifact_over_audit_log(
    service, run_store, reporter, artifact_registry, workspace, ctx,
):
    """Both an artifact AND a `plan.generated` event exist — the
    artifact wins and the FE sees the post-compile fields."""
    run_store.upsert(ctx, _make_run(document_id="doc-1"))
    _emit_plan_generated(
        reporter, ctx, run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )
    _write_planning_result_artifact(
        workspace, artifact_registry, ctx,
        run_id="run-1", document_id="doc-1",
    )
    report = service.get_run_planning(ctx, "run-1")
    assert report.status == "completed"
    assert report.source == "rule_based"
    assert report.planning_phase == "post_compile"
    # Document understanding surfaces.
    assert report.document_understanding["document_type"] == \
        "system_requirement_specification"
    # Execution plan has selective-page recommendations.
    table_step = (report.execution_plan or {}).get("steps", {}).get(
        "table_enrichment"
    )
    assert table_step["pages"] == [4, 5]
    assert report.raw_artifact_id


def test_get_run_planning_audit_log_fallback_marks_source(
    service, run_store, reporter, ctx,
):
    """Without an artifact, the audit-log path produces a DTO with
    `source="audit_log"` so the FE can label its provenance."""
    run_store.upsert(ctx, _make_run())
    _emit_plan_generated(
        reporter, ctx, run_id="run-1",
        plan_payload=_basic_plan_payload(),
    )
    report = service.get_run_planning(ctx, "run-1")
    assert report.status == "completed"
    assert report.source == "audit_log"
    assert report.planning_phase == "initial"
    # Post-compile-only fields stay None on the audit-log path.
    assert report.document_understanding is None
    assert report.execution_plan is None
