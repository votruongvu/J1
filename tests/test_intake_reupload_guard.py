"""Tests for the re-upload checksum guard.

Once Remove is gate-first + sync cleanup, the user's mental
model is "after Remove, the document is gone — I can re-upload".
Three cases the guard pins:

  * ``lifecycle_status="stable"`` + same checksum
        → existing duplicate path (409 with existing_document_id).
  * ``lifecycle_status="removing"`` + same checksum
        → reject 409 with "wait for cleanup" message — re-upload
          must not race the in-flight cleanup.
  * ``lifecycle_status="cleanup_failed"`` + same checksum
        → reject 409 — operator must unstick the orphan.
  * After successful Remove, the document record is gone, so
    ``find_by_checksum`` returns None and the upload proceeds as
    a fresh document (exercised end-to-end via the cleanup +
    intake collaboration).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DuplicateDocumentError
from j1.intake.service import DocumentIntakeService
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


def _make_intake(workspace, registry, audit_sink):
    return DocumentIntakeService(
        workspace=workspace,
        registry=registry,
        audit_sink=audit_sink,
        clock=lambda: _NOW,
        id_factory=lambda: "fresh-doc",
    )


def _seed_existing(
    registry, ctx, *,
    document_id="doc-existing",
    checksum="sha256:abc",
    lifecycle_status="stable",
):
    record = DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename="x.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=checksum,
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state="attached",
        lifecycle_status=lifecycle_status,  # type: ignore[arg-type]
    )
    registry.add(record)
    return record


def _upload_stream(content: bytes = b"%PDF-1.4 content"):
    """Stream the bytes through the intake service. ``register_stream``
    is the surface the FastAPI upload handler calls."""
    return io.BytesIO(content)


def _compute_checksum(content: bytes) -> str:
    import hashlib
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_reupload_blocked_while_removing(
    ctx, workspace, registry, audit_sink,
):
    content = b"%PDF-1.4 body"
    checksum = _compute_checksum(content)
    _seed_existing(
        registry, ctx,
        checksum=checksum, lifecycle_status="removing",
    )
    intake = _make_intake(workspace, registry, audit_sink)

    with pytest.raises(DuplicateDocumentError) as exc:
        intake.register_from_stream(
            ctx, _upload_stream(content),
            original_filename="x.pdf", actor="op",
        )


    assert "removing" in str(exc.value)


def test_reupload_blocked_when_cleanup_failed(
    ctx, workspace, registry, audit_sink,
):
    content = b"%PDF-1.4 body"
    checksum = _compute_checksum(content)
    _seed_existing(
        registry, ctx,
        checksum=checksum, lifecycle_status="cleanup_failed",
    )
    intake = _make_intake(workspace, registry, audit_sink)

    with pytest.raises(DuplicateDocumentError) as exc:
        intake.register_from_stream(
            ctx, _upload_stream(content),
            original_filename="x.pdf", actor="op",
        )


    assert "cleanup_failed" in str(exc.value)


def test_reupload_after_successful_remove_creates_fresh_doc(
    ctx, workspace, registry, audit_sink,
):
    """After a successful Remove the registry has no record —
    the new upload becomes a fresh document with a new id."""
    intake = _make_intake(workspace, registry, audit_sink)
    content = b"%PDF-1.4 body"

    # No existing record — upload succeeds.
    record = intake.register_from_stream(
        ctx, _upload_stream(content),
        original_filename="x.pdf", actor="op",
    )
    assert record.document_id == "fresh-doc"


def test_duplicate_with_stable_lifecycle_uses_legacy_duplicate_path(
    ctx, workspace, registry, audit_sink,
):
    """A stable existing doc + same checksum still raises the
    classic DuplicateDocumentError — message format unchanged."""
    content = b"%PDF-1.4 body"
    checksum = _compute_checksum(content)
    _seed_existing(registry, ctx, checksum=checksum)
    intake = _make_intake(workspace, registry, audit_sink)

    with pytest.raises(DuplicateDocumentError) as exc:
        intake.register_from_stream(
            ctx, _upload_stream(content),
            original_filename="x.pdf", actor="op",
        )


    assert "already registered" in str(exc.value)
