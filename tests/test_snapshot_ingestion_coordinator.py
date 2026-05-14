"""End-to-end coordinator tests — Phase 2.

Exercises the full snapshot lifecycle (create → compile → evidence
index → ready → promote) with deterministic stub adapters. Verifies
the four critical Phase-2 invariants:

  1. Successful run → snapshot becomes active.
  2. Failed compile → snapshot stays NOT-active; previous active untouched.
  3. Failed evidence index → same.
  4. Promote uses snapshot_id (NOT run_id) as the visibility key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from j1.config.settings import Settings
from j1.documents.index_refs import JsonIndexRefStore
from j1.documents.snapshot import IndexKind, IndexRef, SnapshotState
from j1.documents.snapshot_layout import SnapshotLayout
from j1.documents.snapshot_lifecycle import SnapshotIngestionCoordinator
from j1.documents.snapshot_service import DocumentSnapshotService
from j1.documents.snapshot_store import JsonlDocumentSnapshotStore
from j1.processing.compile_adapter import (
    CompileRequest,
    CompileResult,
)
from j1.projects.context import ProjectContext
from j1.search.evidence_adapter import (
    EvidenceIndexRequest,
    EvidenceIndexResult,
)
from j1.workspace.resolver import WorkspaceResolver


# ---- Stub adapters --------------------------------------------


class _FakeArtifact:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id


class _GoodCompileAdapter:
    name = "stub-compile"

    def __init__(self) -> None:
        self.last_request: CompileRequest | None = None

    def compile(self, request: CompileRequest) -> CompileResult:
        self.last_request = request
        return CompileResult(
            success=True,
            artifacts=(_FakeArtifact("art-1"), _FakeArtifact("art-2")),
            metadata={"snapshot_id": request.snapshot_id},
        )


class _FailingCompileAdapter:
    name = "stub-compile-fail"

    def compile(self, request: CompileRequest) -> CompileResult:
        return CompileResult(success=False, error="simulated compile failure")


class _GoodEvidenceAdapter:
    name = "stub-evidence"

    def __init__(self) -> None:
        self.last_request: EvidenceIndexRequest | None = None

    def index(self, request: EvidenceIndexRequest) -> EvidenceIndexResult:
        self.last_request = request
        ref = IndexRef(
            snapshot_id=request.snapshot_id,
            kind=IndexKind.EVIDENCE,
            provider="stub",
            location=f"stub://{request.snapshot_id}",
            stats={"indexed": len(request.artifact_ids)},
        )
        return EvidenceIndexResult(
            success=True,
            indexed_count=len(request.artifact_ids),
            index_ref=ref,
        )

    def delete_for_snapshot(self, ctx, snapshot_id: str) -> int:
        return 0


class _FailingEvidenceAdapter:
    name = "stub-evidence-fail"

    def index(self, request: EvidenceIndexRequest) -> EvidenceIndexResult:
        ref = IndexRef(
            snapshot_id=request.snapshot_id,
            kind=IndexKind.EVIDENCE,
            provider="stub",
            location="stub://void",
        )
        return EvidenceIndexResult(
            success=False, indexed_count=0, index_ref=ref,
            error="simulated index failure",
        )

    def delete_for_snapshot(self, ctx, snapshot_id: str) -> int:
        return 0


# ---- Fixtures -------------------------------------------------


class _FixedClock:
    def __init__(self, start: datetime) -> None:
        self._now = start
        self._step = timedelta(seconds=1)

    def now(self) -> datetime:
        out = self._now
        self._now += self._step
        return out


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


@pytest.fixture
def coordinator(tmp_path):
    ws = WorkspaceResolver(Settings(data_root=tmp_path))
    store = JsonlDocumentSnapshotStore(ws)
    layout = SnapshotLayout(data_root=tmp_path)
    refs = JsonIndexRefStore(ws)
    clock = _FixedClock(datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc))
    service = DocumentSnapshotService(store=store, clock=clock)
    return SnapshotIngestionCoordinator(
        snapshot_service=service,
        layout=layout,
        index_refs=refs,
        compile_adapter=_GoodCompileAdapter(),
        evidence_adapter=_GoodEvidenceAdapter(),
    ), tmp_path


# ---- Tests ----------------------------------------------------


def test_successful_ingestion_promotes_snapshot_to_active(coordinator, ctx, tmp_path):
    coord, _ = coordinator
    src = tmp_path / "src.pdf"
    src.write_text("source bytes")
    outcome = coord.ingest(
        ctx,
        document_id="doc-1",
        run_id="run-1",
        source_path=src,
        previous_active_snapshot_id=None,
    )
    assert outcome.promoted is True
    assert outcome.snapshot.state == SnapshotState.READY
    assert outcome.snapshot.promoted_at is not None
    assert outcome.snapshot.created_by_run_id == "run-1"
    assert outcome.compile_result.success
    assert outcome.evidence_result.success


def test_compile_failure_marks_snapshot_failed_and_does_not_promote(
    coordinator, ctx, tmp_path,
):
    coord, _ = coordinator
    coord.compile_adapter = _FailingCompileAdapter()
    src = tmp_path / "src.pdf"
    src.write_text("x")
    outcome = coord.ingest(
        ctx,
        document_id="doc-1",
        run_id="run-1",
        source_path=src,
        previous_active_snapshot_id=None,
    )
    assert outcome.promoted is False
    assert outcome.snapshot.state == SnapshotState.FAILED
    assert any("compile failed" in e for e in outcome.errors)


def test_evidence_failure_marks_snapshot_failed_and_does_not_promote(
    coordinator, ctx, tmp_path,
):
    coord, _ = coordinator
    coord.evidence_adapter = _FailingEvidenceAdapter()
    src = tmp_path / "src.pdf"
    src.write_text("x")
    outcome = coord.ingest(
        ctx,
        document_id="doc-1",
        run_id="run-1",
        source_path=src,
        previous_active_snapshot_id=None,
    )
    assert outcome.promoted is False
    assert outcome.snapshot.state == SnapshotState.FAILED


def test_compile_receives_snapshot_scoped_workspace_not_run_path(
    coordinator, ctx, tmp_path,
):
    """Locks the storage rule: compile adapter must receive a path
    that contains snapshot_id, not run_id."""
    coord, root = coordinator
    src = tmp_path / "src.pdf"
    src.write_text("x")
    outcome = coord.ingest(
        ctx,
        document_id="doc-1",
        run_id="run-zzz",
        source_path=src,
        previous_active_snapshot_id=None,
    )
    req = coord.compile_adapter.last_request
    assert req is not None
    parts = str(req.snapshot_workspace).split("/")
    assert outcome.snapshot.snapshot_id in parts
    assert "run-zzz" not in parts
    assert "runs" not in parts


def test_second_run_promotes_and_supersedes_first(coordinator, ctx, tmp_path):
    coord, _ = coordinator
    src = tmp_path / "src.pdf"
    src.write_text("x")
    first = coord.ingest(
        ctx, document_id="doc-1", run_id="run-1",
        source_path=src, previous_active_snapshot_id=None,
    )
    second = coord.ingest(
        ctx, document_id="doc-1", run_id="run-2",
        source_path=src,
        previous_active_snapshot_id=first.snapshot.snapshot_id,
    )
    assert second.promoted is True
    # The first snapshot is now SUPERSEDED in the store.
    snap = coord.snapshot_service.store.get(ctx, first.snapshot.snapshot_id)
    assert snap is not None
    assert snap.state == SnapshotState.SUPERSEDED


def test_failed_run_does_not_supersede_existing_active(coordinator, ctx, tmp_path):
    coord, _ = coordinator
    src = tmp_path / "src.pdf"
    src.write_text("x")
    good = coord.ingest(
        ctx, document_id="doc-1", run_id="run-good",
        source_path=src, previous_active_snapshot_id=None,
    )
    assert good.promoted is True

    coord.compile_adapter = _FailingCompileAdapter()
    bad = coord.ingest(
        ctx, document_id="doc-1", run_id="run-bad",
        source_path=src,
        previous_active_snapshot_id=good.snapshot.snapshot_id,
    )
    assert bad.promoted is False
    # The good snapshot is still active in the store.
    good_after = coord.snapshot_service.store.get(
        ctx, good.snapshot.snapshot_id,
    )
    assert good_after is not None
    assert good_after.state == SnapshotState.READY
    assert good_after.promoted_at is not None


def test_index_ref_is_persisted_after_successful_ingestion(
    coordinator, ctx, tmp_path,
):
    coord, _ = coordinator
    src = tmp_path / "src.pdf"
    src.write_text("x")
    outcome = coord.ingest(
        ctx, document_id="doc-1", run_id="run-1",
        source_path=src, previous_active_snapshot_id=None,
    )
    refs = coord.index_refs.list_for_snapshot(
        ctx, outcome.snapshot.snapshot_id,
    )
    assert len(refs) == 1
    assert refs[0].kind == IndexKind.EVIDENCE
    assert refs[0].snapshot_id == outcome.snapshot.snapshot_id
