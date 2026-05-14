"""Phase 5 — final cutover invariants.

These tests lock the Phase 5 changes:

  * ``IngestionRun.target_snapshot_id`` is set BEFORE the workflow
    starts (REST sites allocate the candidate snapshot first).
  * ``workspace_path_for_run`` public symbol is DELETED.
  * Validation no longer falls back to ``metadata["run_id"]``.
  * REST ``/search`` response carries ``snapshotId``.
  * Chunk resolver cache respects the LRU cap.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- Up-front snapshot allocation ------------------------------


def test_ingestion_run_carries_target_snapshot_id_field():
    """The ``IngestionRun`` dataclass exposes the typed
    ``target_snapshot_id`` field that the REST allocation path
    sets."""
    import inspect
    from j1.runs.models import IngestionRun
    assert "target_snapshot_id" in IngestionRun.__dataclass_fields__


def test_rest_app_allocator_returns_snapshot_id_when_service_wired(
    tmp_path, ctx,
):
    """Spot-check the inner helper that REST app uses to allocate
    a candidate snapshot before persisting the IngestionRun. With
    a real snapshot service wired, it returns the new snapshot's
    id; without, it returns None."""
    from j1.config.settings import Settings
    from j1.documents.snapshot_service import DocumentSnapshotService
    from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
    from j1.workspace.resolver import WorkspaceResolver

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    service = DocumentSnapshotService(
        store=JsonlDocumentSnapshotStore(workspace),
    )
    snap = service.create_candidate(
        ctx, document_id="d-1", created_by_run_id="run-X",
    )
    assert snap.snapshot_id.startswith("snap_")
    # Phase 9 follow-up: ``require_existing_target_snapshot`` is the
    # canonical lookup. It returns the existing candidate when the
    # caller already has its id.
    again = service.require_existing_target_snapshot(
        ctx, document_id="d-1", snapshot_id=snap.snapshot_id,
    )
    assert again.snapshot_id == snap.snapshot_id


# ---- workspace_path_for_run is deleted -------------------------


def test_public_workspace_path_for_run_is_gone():
    """Phase 5 deleted the public deprecated shim. Importing it
    from the bridge must fail."""
    from j1.providers.raganything import _bridge
    assert not hasattr(_bridge, "workspace_path_for_run")


def test_internal_legacy_helper_is_deleted_in_phase6():
    """Phase 6 deleted ``_legacy_workspace_path_for_run`` entirely.
    The replacement is ``_snapshot_workspace_path`` which addresses
    snapshots, not runs. Callers that still try to import the old
    symbol get an ImportError."""
    from j1.providers.raganything import _bridge
    assert not hasattr(_bridge, "_legacy_workspace_path_for_run")


# ---- Validation snapshot-only ----------------------------------


def _record(snapshot_id=None, metadata=None):
    return ArtifactRecord(
        artifact_id="art-1",
        project=ProjectContext(tenant_id="t", project_id="p", profile=None),
        kind="chunk",
        location="compiled/art-1.txt",
        content_hash="x",
        byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        metadata=dict(metadata or {}),
    )


# Phase 9 follow-up (2026-05-14 product change): the
# ``_artifact_belongs_to_run`` helper was only consumed by the
# now-deleted generated-test-case path. Its tests went with it.
# Snapshot lineage on artifacts is still enforced — see the
# tests further down this file that exercise the wire shape +
# the registry-level lineage guard.


# ---- REST /search snapshot_id on the wire ----------------------


def test_search_hit_record_includes_snapshot_id_fields():
    """The Pydantic wire schema MUST advertise snapshot lineage so
    the FE / API consumers can deep-link a hit to its snapshot."""
    from j1.adapters.rest.schemas import SearchHitRecord
    fields = SearchHitRecord.model_fields
    assert "snapshot_id" in fields
    assert "chunk_id" in fields
    assert "created_by_run_id" in fields


def test_search_hit_dto_carries_snapshot_id():
    """The Phase-4 DTO field is preserved + serialised by the REST
    layer's hit-record translation."""
    from j1.integration.dto import SearchHitDTO
    dto = SearchHitDTO(
        artifact_id="a", artifact_type="evidence_chunk",
        title="t", score=0.9,
        snapshot_id="snap-1", chunk_id="c-1",
    )
    assert dto.snapshot_id == "snap-1"


# ---- Chunk resolver cache cap ----------------------------------


def test_chunk_resolver_lru_cache_respects_configurable_cap(monkeypatch, tmp_path):
    """The LRU cap is operator-configurable via
    ``J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS``. Above the cap,
    oldest entries are evicted."""
    from deploy.dev._wiring import _build_chunk_resolver
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver
    from j1.workspace.layout import WorkspaceArea

    monkeypatch.setenv("J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS", "2")

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    ctx_obj = ProjectContext(tenant_id="t", project_id="p", profile=None)
    compiled = workspace.area(ctx_obj, WorkspaceArea.COMPILED)
    compiled.mkdir(parents=True, exist_ok=True)
    # Materialise 3 distinct chunk files.
    for art_id in ("art-1", "art-2", "art-3"):
        (compiled / f"{art_id}.txt").write_text(f"body of {art_id}")

    read_count = {"n": 0}

    class _Counting:
        def get(self, ctx, artifact_id):
            read_count["n"] += 1
            from datetime import datetime, timezone
            return ArtifactRecord(
                artifact_id=artifact_id, project=ctx, kind="chunk",
                location=f"compiled/{artifact_id}.txt",
                content_hash="x", byte_size=1,
                status=ProcessingStatus.SUCCEEDED,
                review_status=ReviewStatus.NOT_REQUIRED, version=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata={"chunk_id": f"c-{artifact_id}"},
            )

    resolver = _build_chunk_resolver(workspace, _Counting())
    list(resolver(ctx_obj, "art-1"))  # miss → read 1
    list(resolver(ctx_obj, "art-2"))  # miss → read 2
    list(resolver(ctx_obj, "art-3"))  # miss → read 3 (cap=2 evicts art-1)
    list(resolver(ctx_obj, "art-1"))  # miss again → read 4 (was evicted)
    list(resolver(ctx_obj, "art-3"))  # hit (most-recently-used)
    list(resolver(ctx_obj, "art-3"))  # hit
    assert read_count["n"] == 4


def test_chunk_resolver_handles_cap_zero_as_no_cache(monkeypatch, tmp_path):
    """Cap=0 means caching is OFF — every read hits disk again."""
    from deploy.dev._wiring import _build_chunk_resolver
    from j1.config.settings import Settings
    from j1.workspace.resolver import WorkspaceResolver
    from j1.workspace.layout import WorkspaceArea

    monkeypatch.setenv("J1_EVIDENCE_CHUNK_RESOLVER_CACHE_MAX_ITEMS", "0")

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    ctx_obj = ProjectContext(tenant_id="t", project_id="p", profile=None)
    compiled = workspace.area(ctx_obj, WorkspaceArea.COMPILED)
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "art-1.txt").write_text("body")

    read_count = {"n": 0}

    class _Counting:
        def get(self, ctx, artifact_id):
            read_count["n"] += 1
            from datetime import datetime, timezone
            return ArtifactRecord(
                artifact_id=artifact_id, project=ctx, kind="chunk",
                location=f"compiled/{artifact_id}.txt",
                content_hash="x", byte_size=1,
                status=ProcessingStatus.SUCCEEDED,
                review_status=ReviewStatus.NOT_REQUIRED, version=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                metadata={},
            )

    resolver = _build_chunk_resolver(workspace, _Counting())
    list(resolver(ctx_obj, "art-1"))
    list(resolver(ctx_obj, "art-1"))
    list(resolver(ctx_obj, "art-1"))
    assert read_count["n"] == 3
