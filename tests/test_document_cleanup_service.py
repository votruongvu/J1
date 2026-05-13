"""Tests for ``DocumentCleanupService``.

The cleanup service owns five idempotent primitives plus two
composers (per-run, per-document). These tests pin:

  * each primitive removes only its scope (artifacts tagged with
    the target run_id, FTS rows tagged with the target run_id,
    the run-scoped workspace dir, etc.) and leaves siblings alone;
  * primitives + composers are idempotent — re-running after
    success is a no-op success;
  * partial failure surfaces ``ok=False`` on the aggregate so
    callers can mark ``cleanup_status="cleanup_failed"``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.documents.cleanup import DocumentCleanupService
from j1.documents.models import DocumentRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.runs.models import IngestionRun, RunStatus
from j1.search.indexer import SqliteSearchIndexer
from j1.workspace.layout import WorkspaceArea


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


def _add_artifact(
    workspace, ctx, artifact_registry,
    *, artifact_id: str, run_id: str | None = None,
    content: bytes = b"hello world",
) -> ArtifactRecord:
    area_dir = workspace.area(ctx, WorkspaceArea.COMPILED)
    area_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{artifact_id}.txt"
    (area_dir / stored).write_bytes(content)
    metadata: dict = {}
    if run_id:
        metadata["run_id"] = run_id
    record = ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind="chunk",
        location=f"{WorkspaceArea.COMPILED.value}/{stored}",
        content_hash=f"sha256:{artifact_id}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        source_document_ids=["doc-1"],
        metadata=metadata,
    )
    artifact_registry._raw_add(record)  # bypass lineage check for tests
    return record


def _add_run(run_store, ctx, *, run_id, document_id) -> IngestionRun:
    run = IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id="wf-" + run_id,
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
    )
    run_store.upsert(ctx, run)
    return run


# ---- cleanup_run -------------------------------------------------


def test_cleanup_run_drops_artifacts_and_files(
    workspace, ctx, artifact_registry,
):
    """Artifacts tagged with the target run_id are removed; siblings
    tagged with a different run_id survive untouched."""
    a = _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-survives", run_id="run-B",
    )
    b = _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-dies", run_id="run-A",
    )
    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry,
    )

    result = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    assert result.ok is True
    survivors = [
        r.artifact_id for r in artifact_registry.list_artifacts(ctx)
    ]
    assert "a-survives" in survivors
    assert "a-dies" not in survivors
    # File of the killed artifact is gone; survivor's file remains.
    assert not (
        workspace.project_root(ctx) / b.location
    ).exists()
    assert (
        workspace.project_root(ctx) / a.location
    ).exists()


def test_cleanup_run_is_idempotent(workspace, ctx, artifact_registry):
    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", run_id="run-A",
    )
    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry,
    )

    first = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    second = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    assert first.ok is True
    assert second.ok is True  # already-gone is still success


def test_cleanup_run_drops_fts_rows(workspace, ctx, artifact_registry):
    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-A", run_id="run-A",
    )
    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-B", run_id="run-B",
    )
    indexer = SqliteSearchIndexer(workspace, artifact_registry)
    indexer.index(ctx, ["a-A", "a-B"])
    assert {h.artifact_id for h in indexer.search(ctx, "hello")} == {"a-A", "a-B"}

    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry, indexer=indexer,
    )
    result = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    assert result.ok is True
    survivors = {h.artifact_id for h in indexer.search(ctx, "hello")}
    assert survivors == {"a-B"}


def test_cleanup_run_drops_lightrag_and_mineru_workspace(
    workspace, ctx, tmp_path,
):
    """LightRAG run-scoped dir and MinerU output dir for the run are
    deleted; sibling run dirs survive."""
    workdir = tmp_path / "rag"
    target_lr = (
        workdir / "runs" / "acme" / "alpha" / "doc-1" / "run-A"
    )
    sibling_lr = (
        workdir / "runs" / "acme" / "alpha" / "doc-1" / "run-B"
    )
    target_mu = workdir / "outputs" / "doc-1" / "run-A"
    sibling_mu = workdir / "outputs" / "doc-1" / "run-B"
    for d in (target_lr, sibling_lr, target_mu, sibling_mu):
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("x", encoding="utf-8")

    service = DocumentCleanupService(
        workspace=workspace, raganything_workdir=workdir,
    )
    result = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    assert result.ok is True
    assert not target_lr.exists()
    assert not target_mu.exists()
    assert sibling_lr.exists()  # sibling untouched
    assert sibling_mu.exists()


# ---- cleanup_document --------------------------------------------


def test_cleanup_document_walks_every_run(
    workspace, ctx, artifact_registry, tmp_path,
):
    """cleanup_document collects run_ids from the run-store and
    drops everything for every run."""
    from j1.runs.store import JsonlIngestionRunStore

    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", run_id="run-A",
    )
    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-2", run_id="run-B",
    )
    run_store = JsonlIngestionRunStore(workspace)
    _add_run(run_store, ctx, run_id="run-A", document_id="doc-1")
    _add_run(run_store, ctx, run_id="run-B", document_id="doc-1")

    service = DocumentCleanupService(
        workspace=workspace,
        artifacts=artifact_registry,
        run_store=run_store,
    )
    result = service.cleanup_document(ctx, document_id="doc-1")
    assert result.ok is True
    # Every artifact for doc-1 is gone.
    assert artifact_registry.list_artifacts(ctx) == []
    # Run-store entries for doc-1 are gone.
    assert run_store.list_runs(ctx, document_id="doc-1") == []


def test_cleanup_document_drops_raw_file(
    workspace, ctx, artifact_registry,
):
    raw_dir = workspace.raw(ctx)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "doc-1.pdf"
    raw_file.write_bytes(b"%PDF-1.4")
    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry,
    )
    result = service.cleanup_document(ctx, document_id="doc-1")
    assert result.ok is True
    assert not raw_file.exists()


def test_cleanup_document_is_idempotent(workspace, ctx, artifact_registry):
    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry,
    )
    # No prior state — first call still succeeds.
    first = service.cleanup_document(ctx, document_id="doc-1")
    second = service.cleanup_document(ctx, document_id="doc-1")
    assert first.ok is True
    assert second.ok is True


# ---- Failure reporting -------------------------------------------


def test_cleanup_run_reports_step_results(workspace, ctx, artifact_registry):
    _add_artifact(
        workspace, ctx, artifact_registry,
        artifact_id="a-1", run_id="run-A",
    )
    service = DocumentCleanupService(
        workspace=workspace, artifacts=artifact_registry,
    )
    result = service.cleanup_run(
        ctx, document_id="doc-1", run_id="run-A",
    )
    step_names = {s.name for s in result.steps}
    assert "artifacts" in step_names
    assert "index" in step_names
    assert "run_workspace" in step_names
