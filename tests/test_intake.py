import io
import json
from pathlib import Path

import pytest

from j1.audit.sink import AUDIT_LOG_FILENAME
from j1.errors.exceptions import DuplicateDocumentError, IntakeError
from j1.intake.service import (
    ACTION_DUPLICATE,
    ACTION_REGISTERED,
    CHECKSUM_PREFIX,
)
from j1.jobs.status import ProcessingStatus

SAMPLE = b"hello j1 framework\n"
OTHER = b"another payload\n"


def _write_sample(tmp_path: Path, name: str = "doc.txt", payload: bytes = SAMPLE) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def _read_audit(workspace, ctx) -> list[dict]:
    path = workspace.audit(ctx) / AUDIT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_register_from_path_returns_record(intake_service, ctx, tmp_path):
    src = _write_sample(tmp_path)
    record = intake_service.register_from_path(ctx, src)
    assert record.document_id
    assert record.original_filename == "doc.txt"
    assert record.stored_filename.startswith(record.document_id)
    assert record.file_size == len(SAMPLE)
    assert record.checksum.startswith(CHECKSUM_PREFIX)
    assert record.status is ProcessingStatus.PENDING
    assert record.mime_type == "text/plain"
    assert record.tenant_id == "acme"
    assert record.project_id == "alpha"


def test_register_from_path_writes_file_into_raw(intake_service, workspace, ctx, tmp_path):
    src = _write_sample(tmp_path)
    record = intake_service.register_from_path(ctx, src)
    stored = workspace.raw(ctx) / record.stored_filename
    assert stored.is_file()
    assert stored.read_bytes() == SAMPLE


def test_register_from_path_does_not_expose_paths(intake_service, ctx, tmp_path):
    src = _write_sample(tmp_path)
    record = intake_service.register_from_path(ctx, src)
    # No field on the record should leak an absolute filesystem path.
    for value in (
        record.document_id,
        record.original_filename,
        record.stored_filename,
        record.checksum,
    ):
        assert "/" not in value
        assert "\\" not in value


def test_register_from_stream(intake_service, workspace, ctx):
    stream = io.BytesIO(SAMPLE)
    record = intake_service.register_from_stream(
        ctx, stream, original_filename="upload.txt"
    )
    assert record.original_filename == "upload.txt"
    assert (workspace.raw(ctx) / record.stored_filename).read_bytes() == SAMPLE


def test_register_from_stream_requires_filename(intake_service, ctx):
    with pytest.raises(IntakeError):
        intake_service.register_from_stream(
            ctx, io.BytesIO(SAMPLE), original_filename=""
        )


def test_register_from_path_rejects_missing_file(intake_service, ctx, tmp_path):
    with pytest.raises(IntakeError):
        intake_service.register_from_path(ctx, tmp_path / "nope.txt")


def test_duplicate_from_path_raises(intake_service, ctx, tmp_path):
    src = _write_sample(tmp_path)
    first = intake_service.register_from_path(ctx, src)
    with pytest.raises(DuplicateDocumentError) as excinfo:
        intake_service.register_from_path(ctx, src)
    assert excinfo.value.existing_document_id == first.document_id
    assert excinfo.value.checksum == first.checksum


def test_duplicate_from_stream_raises(intake_service, ctx):
    first = intake_service.register_from_stream(
        ctx, io.BytesIO(SAMPLE), original_filename="a.txt"
    )
    with pytest.raises(DuplicateDocumentError) as excinfo:
        intake_service.register_from_stream(
            ctx, io.BytesIO(SAMPLE), original_filename="b.txt"
        )
    assert excinfo.value.existing_document_id == first.document_id


def test_duplicate_does_not_leave_files(intake_service, workspace, ctx, tmp_path):
    src = _write_sample(tmp_path)
    intake_service.register_from_path(ctx, src)
    with pytest.raises(DuplicateDocumentError):
        intake_service.register_from_path(ctx, src)
    raw_files = sorted(p.name for p in workspace.raw(ctx).iterdir())
    # Exactly one stored file (no orphaned tmps from the duplicate attempt).
    assert len(raw_files) == 1
    assert not any(name.startswith(".intake_") for name in raw_files)


def test_distinct_payloads_are_not_duplicates(intake_service, ctx, tmp_path):
    a = _write_sample(tmp_path, "a.txt", SAMPLE)
    b = _write_sample(tmp_path, "b.txt", OTHER)
    ra = intake_service.register_from_path(ctx, a)
    rb = intake_service.register_from_path(ctx, b)
    assert ra.checksum != rb.checksum
    assert ra.document_id != rb.document_id


def test_same_checksum_in_different_projects_is_not_a_duplicate(
    intake_service, ctx, other_ctx, tmp_path
):
    src = _write_sample(tmp_path)
    a = intake_service.register_from_path(ctx, src)
    b = intake_service.register_from_path(other_ctx, src)
    assert a.checksum == b.checksum
    assert a.document_id != b.document_id


def test_audit_event_written_on_registration(intake_service, workspace, ctx, tmp_path):
    src = _write_sample(tmp_path)
    record = intake_service.register_from_path(ctx, src)
    events = _read_audit(workspace, ctx)
    assert len(events) == 1
    e = events[0]
    assert e["action"] == ACTION_REGISTERED
    assert e["target_kind"] == "document"
    assert e["target_id"] == record.document_id
    assert e["project"]["tenant_id"] == "acme"
    assert e["project"]["project_id"] == "alpha"
    assert e["payload"]["checksum"] == record.checksum
    assert e["payload"]["file_size"] == record.file_size


def test_audit_event_written_on_duplicate(intake_service, workspace, ctx, tmp_path):
    src = _write_sample(tmp_path)
    first = intake_service.register_from_path(ctx, src)
    with pytest.raises(DuplicateDocumentError):
        intake_service.register_from_path(ctx, src)
    events = _read_audit(workspace, ctx)
    actions = [e["action"] for e in events]
    assert actions == [ACTION_REGISTERED, ACTION_DUPLICATE]
    assert events[-1]["target_id"] == first.document_id
    assert events[-1]["payload"]["checksum"] == first.checksum


def test_explicit_mime_type_wins(intake_service, ctx, tmp_path):
    src = _write_sample(tmp_path, "doc.bin")
    record = intake_service.register_from_path(
        ctx, src, mime_type="application/x-custom"
    )
    assert record.mime_type == "application/x-custom"


def test_correlation_id_propagates_to_audit(intake_service, workspace, ctx, tmp_path):
    src = _write_sample(tmp_path)
    intake_service.register_from_path(ctx, src, correlation_id="run-42")
    events = _read_audit(workspace, ctx)
    assert events[0]["correlation_id"] == "run-42"
