from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError, IntakeError
from j1.intake.registry import (
    REGISTRY_FILENAME,
    JsonSourceRegistry,
)
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


def _make_record(
    *,
    document_id: str = "doc-1",
    checksum: str = "sha256:aaa",
    project: ProjectContext | None = None,
    original: str = "a.pdf",
    stored: str | None = None,
) -> DocumentRecord:
    project = project or ProjectContext(tenant_id="acme", project_id="alpha")
    return DocumentRecord(
        document_id=document_id,
        project=project,
        original_filename=original,
        stored_filename=stored or f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=42,
        checksum=checksum,
        status=ProcessingStatus.PENDING,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_empty_registry_returns_empty_list(registry, ctx):
    assert registry.list_documents(ctx) == []


def test_add_then_list(registry, ctx):
    record = _make_record(project=ctx)
    registry.add(record)
    assert registry.list_documents(ctx) == [record]


def test_get_returns_record(registry, ctx):
    record = _make_record(project=ctx)
    registry.add(record)
    assert registry.get(ctx, record.document_id) == record


def test_get_missing_raises(registry, ctx):
    with pytest.raises(DocumentNotFoundError):
        registry.get(ctx, "nonexistent")


def test_find_by_checksum_hit(registry, ctx):
    record = _make_record(project=ctx, checksum="sha256:zzz")
    registry.add(record)
    assert registry.find_by_checksum(ctx, "sha256:zzz") == record


def test_find_by_checksum_miss(registry, ctx):
    assert registry.find_by_checksum(ctx, "sha256:nope") is None


def test_duplicate_document_id_rejected(registry, ctx):
    record = _make_record(project=ctx)
    registry.add(record)
    duplicate = _make_record(project=ctx, checksum="sha256:bbb")
    with pytest.raises(IntakeError):
        registry.add(duplicate)


def test_persistence_roundtrip(workspace, ctx):
    first = JsonSourceRegistry(workspace)
    record = _make_record(project=ctx)
    first.add(record)

    second = JsonSourceRegistry(workspace)
    listed = second.list_documents(ctx)
    assert listed == [record]
    assert second.get(ctx, record.document_id) == record


def test_registry_isolates_projects(registry, ctx, other_ctx):
    a = _make_record(document_id="doc-a", project=ctx, checksum="sha256:a")
    b = _make_record(document_id="doc-b", project=other_ctx, checksum="sha256:b")
    registry.add(a)
    registry.add(b)
    assert registry.list_documents(ctx) == [a]
    assert registry.list_documents(other_ctx) == [b]
    assert registry.find_by_checksum(ctx, "sha256:b") is None


def test_registry_file_lives_in_runtime_area(registry, workspace, ctx):
    registry.add(_make_record(project=ctx))
    expected = workspace.runtime(ctx) / REGISTRY_FILENAME
    assert expected.is_file()


def test_write_is_atomic(registry, workspace, ctx):
    registry.add(_make_record(project=ctx))
    runtime = workspace.runtime(ctx)
    leftover = [p.name for p in runtime.iterdir() if p.suffix == ".tmp"]
    assert leftover == []


def test_update_status_flips_pending_to_succeeded(registry, ctx):
    """Workflows call this after each processed doc so subsequent
 bulk jobs don't re-pick it. Without the transition, a freshly
 uploaded document stays PENDING forever and every project-wide
 job loops it again."""
    registry.add(_make_record(project=ctx, document_id="doc-1"))
    registry.update_status(ctx, "doc-1", ProcessingStatus.SUCCEEDED)
    after = registry.get(ctx, "doc-1")
    assert after.status == ProcessingStatus.SUCCEEDED


def test_update_status_persists_across_reads(workspace, ctx):
    """Status update must survive a re-instantiation of the
 registry (file-backed write, not in-memory only)."""
    first = JsonSourceRegistry(workspace)
    first.add(_make_record(project=ctx, document_id="doc-1"))
    first.update_status(ctx, "doc-1", ProcessingStatus.FAILED)
    second = JsonSourceRegistry(workspace)
    assert second.get(ctx, "doc-1").status == ProcessingStatus.FAILED


def test_update_status_unknown_document_raises(registry, ctx):
    with pytest.raises(DocumentNotFoundError):
        registry.update_status(ctx, "missing", ProcessingStatus.SUCCEEDED)


def test_update_status_does_not_touch_other_documents(registry, ctx):
    registry.add(_make_record(project=ctx, document_id="doc-1"))
    registry.add(_make_record(
        project=ctx, document_id="doc-2", checksum="sha256:bbb",
    ))
    registry.update_status(ctx, "doc-1", ProcessingStatus.SUCCEEDED)
    assert registry.get(ctx, "doc-2").status == ProcessingStatus.PENDING
