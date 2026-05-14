"""CompileEngineAdapter / RAGAnythingCompileAdapter tests — Phase 2.

Verifies the adapter routes RAGAnything into a snapshot-scoped
workspace and translates the bridge's native result shape into the
adapter-neutral ``CompileResult``."""

from __future__ import annotations

from pathlib import Path

import pytest

from j1.documents.snapshot_layout import SnapshotArea, SnapshotLayout
from j1.processing.compile_adapter import (
    CompileRequest,
    CompileResult,
    RAGAnythingCompileAdapter,
)
from j1.projects.context import ProjectContext


class _FakeBridgeResult:
    """Mimics ``ArtifactProcessingResult`` without importing it."""

    def __init__(
        self,
        *,
        status: str,
        drafts: tuple = (),
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.status = _Status(status)
        self.drafts = drafts
        self.error = error
        self.metadata = metadata or {}


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


def _request(tmp_path, ctx):
    layout = SnapshotLayout(data_root=tmp_path)
    snap_root = layout.ensure(ctx, "doc-1", "snap-1")
    return CompileRequest(
        ctx=ctx,
        document_id="doc-1",
        snapshot_id="snap-1",
        created_by_run_id="run-1",
        profile_id="generic",
        source_path=tmp_path / "src.pdf",
        snapshot_workspace=snap_root,
        compile_config={"parse_method": "auto"},
    ), layout


def test_adapter_returns_not_configured_when_bridge_missing(tmp_path, ctx):
    req, layout = _request(tmp_path, ctx)
    adapter = RAGAnythingCompileAdapter(layout=layout, bridge_compile=None)
    result = adapter.compile(req)
    assert result.success is False
    assert "not configured" in (result.error or "")
    assert result.metadata["snapshot_id"] == "snap-1"


def test_adapter_pins_working_dir_to_snapshot_compile_area(tmp_path, ctx):
    """The Phase-2 invariant: compile output must live under the
    snapshot's ``compile`` area, not under a run-named directory."""
    req, layout = _request(tmp_path, ctx)
    captured: dict = {}

    def _fake_bridge(*, ctx, document_id, source_path,
                     working_dir_override, compile_config, run_id):
        captured.update(
            working_dir_override=working_dir_override,
            run_id=run_id,
            snapshot_in_path=str(working_dir_override).split("/"),
        )
        return _FakeBridgeResult(
            status="succeeded",
            drafts=("draft-1",),
        )

    adapter = RAGAnythingCompileAdapter(
        layout=layout, bridge_compile=_fake_bridge,
    )
    result = adapter.compile(req)
    assert result.success is True
    assert "snap-1" in captured["snapshot_in_path"]
    assert SnapshotArea.COMPILE.value in captured["snapshot_in_path"]
    assert "runs" not in captured["snapshot_in_path"]
    assert captured["run_id"] == "run-1"  # passed through for legacy lineage


def test_adapter_translates_succeeded_with_warnings_to_success(tmp_path, ctx):
    req, layout = _request(tmp_path, ctx)

    def _fake_bridge(**kwargs):
        return _FakeBridgeResult(status="succeeded_with_warnings")

    adapter = RAGAnythingCompileAdapter(
        layout=layout, bridge_compile=_fake_bridge,
    )
    assert adapter.compile(req).success is True


def test_adapter_treats_failed_status_as_failure(tmp_path, ctx):
    req, layout = _request(tmp_path, ctx)

    def _fake_bridge(**kwargs):
        return _FakeBridgeResult(status="failed", error="boom")

    adapter = RAGAnythingCompileAdapter(
        layout=layout, bridge_compile=_fake_bridge,
    )
    result = adapter.compile(req)
    assert result.success is False
    assert result.error == "boom"


def test_adapter_catches_bridge_exception_and_returns_failure(tmp_path, ctx):
    req, layout = _request(tmp_path, ctx)

    def _fake_bridge(**kwargs):
        raise RuntimeError("LightRAG died")

    adapter = RAGAnythingCompileAdapter(
        layout=layout, bridge_compile=_fake_bridge,
    )
    result = adapter.compile(req)
    assert result.success is False
    assert "LightRAG died" in (result.error or "")
