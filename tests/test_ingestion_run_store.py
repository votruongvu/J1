"""Tests for `IngestionRun` + `JsonlIngestionRunStore`."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.config.settings import Settings
from j1.projects.context import ProjectContext
from j1.runs import IngestionRun, JsonlIngestionRunStore, RunStatus
from j1.workspace.resolver import WorkspaceResolver


@pytest.fixture
def workspace(tmp_path) -> WorkspaceResolver:
    return WorkspaceResolver(settings=Settings(data_root=tmp_path))


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


@pytest.fixture
def store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


def _make_run(
    *, run_id: str = "run-1", status: RunStatus = RunStatus.CREATED,
) -> IngestionRun:
    now = datetime.now(timezone.utc)
    return IngestionRun(
        run_id=run_id,
        document_id="doc-1",
        workflow_id="wf-1",
        workflow_run_id="wfr-1",
        status=status,
        started_at=now,
        updated_at=now,
    )


# ---- Round-trip --------------------------------------------------


def test_upsert_then_get_round_trips_full_record(store, ctx):
    """A run written with all fields must be readable byte-for-byte
 (status enum + datetimes round-trip through JSONL)."""
    run = _make_run()
    run.workspace_id = "ws-1"
    run.current_stage = "COMPILE"
    run.current_step = "LAYOUT_PREPARATION"
    run.progress_percent = 35
    run.warning_count = 2
    run.metadata = {"engine": "MinerU"}

    store.upsert(ctx, run)
    fetched = store.get(ctx, "run-1")

    assert fetched is not None
    assert fetched.run_id == "run-1"
    assert fetched.status == RunStatus.CREATED
    assert fetched.workspace_id == "ws-1"
    assert fetched.current_stage == "COMPILE"
    assert fetched.current_step == "LAYOUT_PREPARATION"
    assert fetched.progress_percent == 35
    assert fetched.warning_count == 2
    assert fetched.metadata == {"engine": "MinerU"}


def test_upsert_appends_latest_snapshot_wins(store, ctx):
    """The store appends every update; the latest snapshot per
 `run_id` is what `get` returns. This makes the JSONL doubles
 as a state-transition audit trail."""
    run = _make_run()
    store.upsert(ctx, run)

    run.status = RunStatus.RUNNING
    run.progress_percent = 50
    store.upsert(ctx, run)

    run.status = RunStatus.SUCCEEDED
    run.progress_percent = 100
    run.completed_at = datetime.now(timezone.utc)
    store.upsert(ctx, run)

    fetched = store.get(ctx, "run-1")
    assert fetched.status == RunStatus.SUCCEEDED
    assert fetched.progress_percent == 100
    assert fetched.completed_at is not None


def test_get_returns_none_for_unknown_run(store, ctx):
    assert store.get(ctx, "missing") is None


# ---- Listing -----------------------------------------------------


def test_list_returns_latest_per_run_sorted_by_started_at_desc(store, ctx):
    older = _make_run(run_id="run-old")
    older.started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    older.updated_at = older.started_at
    newer = _make_run(run_id="run-new")
    newer.started_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    newer.updated_at = newer.started_at

    store.upsert(ctx, older)
    store.upsert(ctx, newer)

    runs = store.list(ctx)
    assert [r.run_id for r in runs] == ["run-new", "run-old"]


def test_list_filters_by_status(store, ctx):
    a = _make_run(run_id="r-a", status=RunStatus.RUNNING)
    b = _make_run(run_id="r-b", status=RunStatus.SUCCEEDED)
    c = _make_run(run_id="r-c", status=RunStatus.FAILED)
    for r in (a, b, c):
        store.upsert(ctx, r)

    succeeded = store.list(ctx, statuses=[RunStatus.SUCCEEDED])
    assert {r.run_id for r in succeeded} == {"r-b"}

    terminal = store.list(ctx, statuses=[RunStatus.SUCCEEDED, RunStatus.FAILED])
    assert {r.run_id for r in terminal} == {"r-b", "r-c"}


def test_list_respects_limit(store, ctx):
    for i in range(5):
        run = _make_run(run_id=f"r-{i}")
        run.started_at = datetime(2030, 1, i + 1, tzinfo=timezone.utc)
        store.upsert(ctx, run)

    assert len(store.list(ctx, limit=2)) == 2


# ---- Tolerance ---------------------------------------------------


def test_get_tolerates_malformed_jsonl_line(store, ctx, tmp_path):
    """A truncated tail line must NOT make the entire file
 unreadable — JSONL append-only logs occasionally end mid-line on
 crash."""
    run = _make_run()
    store.upsert(ctx, run)

    # Append a corrupt line manually.
    path = store._path(ctx)
    with path.open("a", encoding="utf-8") as f:
        f.write('{"this is not valid json\n')

    fetched = store.get(ctx, "run-1")
    assert fetched is not None  # the good line still loaded
    assert fetched.run_id == "run-1"
