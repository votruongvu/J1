import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from j1.artifacts.models import ArtifactRecord
from j1.checksum import (
    assert_artifact_integrity,
    assert_document_integrity,
    hash_file,
    verify_artifact,
    verify_document,
)
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import (
    ChecksumMismatchError,
    WorkspaceLockedError,
)
from j1.heartbeat import heartbeat
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.locks import (
    AREA_COMPILED,
    AREA_ENRICHED,
    AREA_GRAPH,
    AREA_PROJECT,
    LockHandle,
    WorkspaceLock,
)
from j1.logging.setup import JsonFormatter, configure_logging, get_logger
from j1.projects.context import ProjectContext
from j1.testing import TestEnvironment, make_test_environment
from j1.workspace.layout import (
    DURABLE_AREAS,
    REBUILDABLE_AREAS,
    WorkspaceArea,
    is_durable,
    is_rebuildable,
)


# ============================================================
# WorkspaceLock
# ============================================================


def _now_factory(start: datetime):
    """Returns a callable that yields times advancing from `start`.

 Each call to the returned function returns the *current* simulated time;
 use the `.advance(seconds)` method to move forward.
 """

    class _Clock:
        def __init__(self, t: datetime) -> None:
            self.t = t

        def __call__(self) -> datetime:
            return self.t

        def advance(self, seconds: float) -> None:
            self.t = self.t + timedelta(seconds=seconds)

    return _Clock(start)


def test_acquire_returns_handle_with_owner_and_expiry(workspace, ctx):
    lock = WorkspaceLock(workspace, lease_seconds=60)
    handle = lock.acquire(ctx, owner="workflow:1", area=AREA_COMPILED)
    assert isinstance(handle, LockHandle)
    assert handle.owner == "workflow:1"
    assert handle.area == AREA_COMPILED
    assert handle.expires_at > handle.acquired_at


def test_concurrent_acquire_same_area_blocks_second(workspace, ctx):
    lock = WorkspaceLock(workspace, lease_seconds=60)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    with pytest.raises(WorkspaceLockedError) as exc:
        lock.acquire(ctx, owner="B", area=AREA_COMPILED)
    assert exc.value.owner == "A"
    assert exc.value.area == AREA_COMPILED


def test_concurrent_acquire_different_area_allowed(workspace, ctx):
    """Per spec: writes to different areas of the same project don't conflict."""
    lock = WorkspaceLock(workspace, lease_seconds=60)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    h2 = lock.acquire(ctx, owner="B", area=AREA_GRAPH)
    assert h2.owner == "B"


def test_concurrent_acquire_different_project_allowed(
    workspace, ctx, other_ctx
):
    """Per spec: different projects of the same tenant don't conflict."""
    lock = WorkspaceLock(workspace, lease_seconds=60)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    h2 = lock.acquire(other_ctx, owner="B", area=AREA_COMPILED)
    assert h2.owner == "B"


def test_concurrent_acquire_different_tenant_allowed(workspace, settings):
    lock = WorkspaceLock(workspace, lease_seconds=60)
    a = ProjectContext(tenant_id="tenant-a", project_id="alpha")
    b = ProjectContext(tenant_id="tenant-b", project_id="alpha")
    lock.acquire(a, owner="A", area=AREA_COMPILED)
    h2 = lock.acquire(b, owner="B", area=AREA_COMPILED)
    assert h2.owner == "B"


def test_release_allows_subsequent_acquire(workspace, ctx):
    lock = WorkspaceLock(workspace, lease_seconds=60)
    handle = lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    lock.release(ctx, handle)
    h2 = lock.acquire(ctx, owner="B", area=AREA_COMPILED)
    assert h2.owner == "B"


def test_release_does_not_steal_other_holders_lock(workspace, ctx):
    """If a stale handle is released after a new owner has taken over,
 the new owner's lock must remain."""
    clock = _now_factory(datetime(2026, 1, 1, tzinfo=timezone.utc))
    lock = WorkspaceLock(workspace, lease_seconds=10, clock=clock)
    h_a = lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    clock.advance(20)  # A's lease expired
    h_b = lock.acquire(ctx, owner="B", area=AREA_COMPILED)
    # A tries to release stale handle.
    lock.release(ctx, h_a)
    # B's lock still in place.
    assert lock.is_held(ctx, area=AREA_COMPILED)


def test_stale_lock_can_be_reclaimed(workspace, ctx):
    clock = _now_factory(datetime(2026, 1, 1, tzinfo=timezone.utc))
    lock = WorkspaceLock(workspace, lease_seconds=10, clock=clock)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    clock.advance(20)
    handle = lock.acquire(ctx, owner="B", area=AREA_COMPILED)
    assert handle.owner == "B"


def test_hold_context_manager_releases_on_success(workspace, ctx):
    lock = WorkspaceLock(workspace, lease_seconds=60)
    with lock.hold(ctx, owner="A", area=AREA_COMPILED):
        assert lock.is_held(ctx, area=AREA_COMPILED)
    assert not lock.is_held(ctx, area=AREA_COMPILED)


def test_hold_context_manager_releases_on_exception(workspace, ctx):
    """Failed activities (raised exception) must release their locks."""
    lock = WorkspaceLock(workspace, lease_seconds=60)
    with pytest.raises(RuntimeError):
        with lock.hold(ctx, owner="A", area=AREA_COMPILED):
            raise RuntimeError("activity failed")
    assert not lock.is_held(ctx, area=AREA_COMPILED)


def test_is_held_returns_false_when_no_lock(workspace, ctx):
    lock = WorkspaceLock(workspace)
    assert not lock.is_held(ctx, area=AREA_COMPILED)


def test_is_held_returns_false_for_expired_lock(workspace, ctx):
    clock = _now_factory(datetime(2026, 1, 1, tzinfo=timezone.utc))
    lock = WorkspaceLock(workspace, lease_seconds=5, clock=clock)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    assert lock.is_held(ctx, area=AREA_COMPILED)
    clock.advance(10)
    assert not lock.is_held(ctx, area=AREA_COMPILED)


def test_force_release_removes_lock(workspace, ctx):
    lock = WorkspaceLock(workspace, lease_seconds=600)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    lock.force_release(ctx, area=AREA_COMPILED)
    assert not lock.is_held(ctx, area=AREA_COMPILED)


def test_lock_file_lives_in_runtime(workspace, ctx):
    lock = WorkspaceLock(workspace)
    lock.acquire(ctx, owner="A", area=AREA_COMPILED)
    expected = workspace.runtime(ctx) / "locks" / f"{AREA_COMPILED}.lock"
    assert expected.is_file()


@pytest.mark.parametrize("area", [AREA_PROJECT, AREA_COMPILED, AREA_ENRICHED, AREA_GRAPH])
def test_separate_files_per_area(workspace, ctx, area):
    lock = WorkspaceLock(workspace)
    lock.acquire(ctx, owner="A", area=area)
    expected = workspace.runtime(ctx) / "locks" / f"{area}.lock"
    assert expected.is_file()


# ============================================================
# Checksum validation
# ============================================================


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_artifact(workspace, ctx, artifact_registry, *, content: bytes = b"hello"):
    area_dir = workspace.compiled(ctx)
    area_dir.mkdir(parents=True, exist_ok=True)
    (area_dir / "art-1.txt").write_bytes(content)
    import hashlib
    record = ArtifactRecord(
        artifact_id="art-1",
        project=ctx,
        kind="compiled.text",
        location="compiled/art-1.txt",
        content_hash=f"sha256:{hashlib.sha256(content).hexdigest()}",
        byte_size=len(content),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=_now(),
        updated_at=_now(),
    )
    artifact_registry.add(record)
    return record


def test_verify_artifact_returns_true_for_intact(workspace, ctx, artifact_registry):
    record = _make_artifact(workspace, ctx, artifact_registry)
    assert verify_artifact(workspace, ctx, record) is True


def test_verify_artifact_returns_false_for_modified(workspace, ctx, artifact_registry):
    record = _make_artifact(workspace, ctx, artifact_registry)
    (workspace.compiled(ctx) / "art-1.txt").write_bytes(b"tampered")
    assert verify_artifact(workspace, ctx, record) is False


def test_verify_artifact_returns_false_when_missing(workspace, ctx, artifact_registry):
    record = _make_artifact(workspace, ctx, artifact_registry)
    (workspace.compiled(ctx) / "art-1.txt").unlink()
    assert verify_artifact(workspace, ctx, record) is False


def test_assert_artifact_integrity_passes_when_intact(
    workspace, ctx, artifact_registry
):
    record = _make_artifact(workspace, ctx, artifact_registry)
    assert_artifact_integrity(workspace, ctx, record)  # does not raise


def test_assert_artifact_integrity_raises_on_mismatch(
    workspace, ctx, artifact_registry
):
    record = _make_artifact(workspace, ctx, artifact_registry)
    (workspace.compiled(ctx) / "art-1.txt").write_bytes(b"tampered")
    with pytest.raises(ChecksumMismatchError) as exc:
        assert_artifact_integrity(workspace, ctx, record)
    assert exc.value.expected == record.content_hash
    assert exc.value.actual is not None
    assert exc.value.actual != record.content_hash


def test_assert_artifact_integrity_raises_on_missing(
    workspace, ctx, artifact_registry
):
    record = _make_artifact(workspace, ctx, artifact_registry)
    (workspace.compiled(ctx) / "art-1.txt").unlink()
    with pytest.raises(ChecksumMismatchError) as exc:
        assert_artifact_integrity(workspace, ctx, record)
    assert exc.value.actual is None


def test_verify_document_round_trips(workspace, ctx, intake_service, tmp_path):
    src = tmp_path / "doc.txt"
    src.write_bytes(b"document content")
    record = intake_service.register_from_path(ctx, src)
    assert verify_document(workspace, ctx, record) is True


def test_verify_document_returns_false_for_modified(
    workspace, ctx, intake_service, tmp_path
):
    src = tmp_path / "doc.txt"
    src.write_bytes(b"document content")
    record = intake_service.register_from_path(ctx, src)
    (workspace.raw(ctx) / record.stored_filename).write_bytes(b"tampered")
    assert verify_document(workspace, ctx, record) is False


def test_assert_document_integrity_raises_on_mismatch(
    workspace, ctx, intake_service, tmp_path
):
    src = tmp_path / "doc.txt"
    src.write_bytes(b"document content")
    record = intake_service.register_from_path(ctx, src)
    (workspace.raw(ctx) / record.stored_filename).write_bytes(b"tampered")
    with pytest.raises(ChecksumMismatchError):
        assert_document_integrity(workspace, ctx, record)


def test_hash_file_returns_sha256_prefix(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hello")
    h = hash_file(p)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


# ============================================================
# Idempotency: registering same content does not duplicate
# ============================================================


def test_artifact_registration_dedupes_unchanged_content(
    processing_service, artifact_registry, workspace, ctx
):
    """Existing dedup behavior: re-registering same content reuses the record.
 Verified via the activity-layer register_compiled_artifacts (which exercises
 find_by_content_hash). This is the framework's reprocessing-control story."""
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )
    from j1.orchestration.activities.payloads import (
        DraftPayload,
        ProjectScope,
        RegisterArtifactsInput,
    )

    activities = KnowledgeProcessingActivities(
        workspace=workspace,
        sources=processing_service._artifacts,  # noqa
        artifacts=artifact_registry,
        audit=processing_service._audit,  # noqa
        cost=processing_service._cost,  # noqa
    )
    drafts = [
        DraftPayload(kind="compiled.text", content=b"same content", suggested_extension=".txt")
    ]
    payload = ProjectScope.from_context(ctx)
    a = activities.register_compiled_artifacts_activity(
        RegisterArtifactsInput(scope=payload, drafts=drafts)
    )
    b = activities.register_compiled_artifacts_activity(
        RegisterArtifactsInput(scope=payload, drafts=drafts)
    )
    assert a.artifact_ids
    assert b.artifact_ids == []
    assert b.reused_artifact_ids == a.artifact_ids


# ============================================================
# Heartbeat
# ============================================================


def test_heartbeat_no_op_outside_activity_context():
    """Outside a Temporal activity context, calling heartbeat is a safe no-op."""
    heartbeat()  # does not raise
    heartbeat({"progress": "halfway"})  # does not raise


# ============================================================
# Structured logs
# ============================================================


def test_json_formatter_emits_required_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="j1.test",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    out = formatter.format(record)
    parsed = json.loads(out)
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "j1.test"
    assert "ts" in parsed


def test_json_formatter_includes_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="j1.test",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.workflow_id = "wf-123"
    record.project_id = "alpha"
    parsed = json.loads(formatter.format(record))
    assert parsed["workflow_id"] == "wf-123"
    assert parsed["project_id"] == "alpha"


def test_json_formatter_coerces_unjsonable_values():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="j1.test",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    record.something = object()  # not JSON-encodable
    parsed = json.loads(formatter.format(record))
    assert "something" in parsed
    assert isinstance(parsed["something"], str)


def test_configure_logging_json_output(capsys):
    configure_logging(level="INFO", json_output=True)
    logger = get_logger("j1.test")
    logger.info("structured", extra={"workflow_id": "wf-1", "stage": "compile"})
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["msg"] == "structured"
    assert parsed["workflow_id"] == "wf-1"
    assert parsed["stage"] == "compile"


def test_configure_logging_text_output_remains_default(capsys):
    configure_logging(level="INFO")
    logger = get_logger("j1.test")
    logger.info("plain message")
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    # Plain (not JSON): should not start with `{`.
    assert not line.startswith("{")
    assert "plain message" in line


# ============================================================
# Workspace layout: durable vs rebuildable
# ============================================================


def test_durable_areas_include_authoritative_state():
    for area in (
        WorkspaceArea.RAW,
        WorkspaceArea.COMPILED,
        WorkspaceArea.ENRICHED,
        WorkspaceArea.GRAPH,
        WorkspaceArea.AUDIT,
        WorkspaceArea.RUNTIME,
    ):
        assert area in DURABLE_AREAS
        assert is_durable(area)


def test_rebuildable_areas_include_search():
    assert WorkspaceArea.SEARCH in REBUILDABLE_AREAS
    assert is_rebuildable(WorkspaceArea.SEARCH)
    assert not is_durable(WorkspaceArea.SEARCH)


def test_durable_and_rebuildable_are_disjoint():
    assert DURABLE_AREAS.isdisjoint(REBUILDABLE_AREAS)


def test_every_area_is_classified():
    classified = DURABLE_AREAS | REBUILDABLE_AREAS
    assert classified == set(WorkspaceArea)


# ============================================================
# TestEnvironment
# ============================================================


def test_make_test_environment_returns_wired_services(tmp_path):
    env = make_test_environment(tmp_path)
    assert isinstance(env, TestEnvironment)
    assert env.ctx.tenant_id == "acme"
    assert env.ctx.project_id == "alpha"
    assert env.workspace.project_root(env.ctx).is_dir()
    # All services wired and non-None.
    assert env.intake_service is not None
    assert env.processing_service is not None
    assert env.cost_aggregator is not None
    assert env.workspace_lock is not None


def test_test_environment_workspace_lock_is_isolated(tmp_path):
    env_a = make_test_environment(tmp_path / "a")
    env_b = make_test_environment(tmp_path / "b")
    env_a.workspace_lock.acquire(env_a.ctx, owner="A", area=AREA_COMPILED)
    # Different data_root → no conflict.
    h = env_b.workspace_lock.acquire(env_b.ctx, owner="B", area=AREA_COMPILED)
    assert h.owner == "B"


def test_test_environment_can_be_used_for_intake(tmp_path):
    env = make_test_environment(tmp_path)
    src = tmp_path / "doc.txt"
    src.write_bytes(b"hello")
    record = env.intake_service.register_from_path(env.ctx, src)
    assert record.document_id
    assert verify_document(env.workspace, env.ctx, record) is True
