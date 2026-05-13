import hashlib
import mimetypes
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from j1.audit.events import AuditEvent
from j1.audit.sink import AuditSink
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import (
    DuplicateDocumentError,
    IntakeError,
    UnsupportedFileTypeError,
    UploadTooLargeError,
)
from j1.intake.registry import SourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

ACTION_REGISTERED = "document.registered"
ACTION_DUPLICATE = "document.duplicate_detected"
TARGET_KIND = "document"
CHECKSUM_PREFIX = "sha256:"
_CHUNK_SIZE = 64 * 1024

# Default upload size cap. Stops a single multipart request from
# filling the workspace volume. Override with `max_upload_bytes=` on
# `DocumentIntakeService`. Operators wiring an `J1_MAX_UPLOAD_BYTES`
# env var should plumb it at construction time. 200 MiB matches the
# UI's stated cap so the user-visible limit and the boundary check
# don't disagree.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Magic-byte signatures for the binary formats in the default
# allow-list. Plain-text extensions map to None — text has no stable
# magic bytes, so the extension check is the only signal. The sniff
# runs AFTER the streaming copy lands in the temp file (cheap re-read
# of the first 16 bytes) and BEFORE the rename to final. Mismatch
# raises the same `UnsupportedFileTypeError` the extension allow-list
# uses, surfacing as 415 at the REST boundary.
#
# The signatures are defense-in-depth — they protect against renamed-
# extension uploads (a `.exe` posted as `.pdf`) that would otherwise
# slip past the extension filter and crash MinerU mid-parse with a
# vendor exception. Format choices below cover the common confused-
# deputy cases; format-specific magic libraries (python-magic) would
# be more thorough but add a system-level dependency.
_BINARY_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    # ZIP-based formats (Office Open XML + ODF).
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".ods": (b"PK\x03\x04",),
    # Legacy OLE2 compound documents.
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

# Default allow-list for upload filename extensions. Tracks the
# Execution Console's `accept=` attribute plus the planner's plain-
# text extensions, so the boundary accepts every shape the bundled
# pipeline knows how to compile. Operators with broader / narrower
# needs override via the `allowed_extensions=` constructor arg
# (typically wired from `J1_ALLOWED_UPLOAD_EXTENSIONS`). Pass an
# empty tuple to disable the boundary check entirely; pass the
# default to keep it.
DEFAULT_ALLOWED_UPLOAD_EXTENSIONS: tuple[str, ...] = (
    # Documents
    ".pdf",
    ".docx",
    ".doc",
    # Spreadsheets / tables (compile path supports these)
    ".xlsx",
    ".xls",
    ".csv",
    ".ods",
    # Web
    ".html",
    ".htm",
    # Plain text (planner's `_PLAIN_TEXT_EXTENSIONS`)
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_id() -> str:
    return uuid.uuid4().hex


class DocumentIntakeService:
    def __init__(
        self,
        workspace: WorkspaceResolver,
        registry: SourceRegistry,
        audit_sink: AuditSink,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        allowed_extensions: tuple[str, ...] = DEFAULT_ALLOWED_UPLOAD_EXTENSIONS,
    ) -> None:
        self._workspace = workspace
        self._registry = registry
        self._audit = audit_sink
        self._clock = clock or _default_clock
        self._id_factory = id_factory or _default_id
        self._max_upload_bytes = max_upload_bytes
        # Empty tuple = boundary disabled. Otherwise normalise to
        # lowercase + leading dot so the check is case-insensitive
        # (`.PDF` and `.pdf` map to the same allow entry).
        self._allowed_extensions: frozenset[str] = frozenset(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in allowed_extensions
        )

    def register_from_path(
        self,
        ctx: ProjectContext,
        source_path: Path,
        *,
        original_filename: str | None = None,
        mime_type: str | None = None,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> DocumentRecord:
        if not source_path.is_file():
            raise IntakeError(f"source path is not a file: {source_path}")
        name = original_filename or source_path.name
        with source_path.open("rb") as stream:
            return self._register(
                ctx,
                stream,
                original_filename=name,
                mime_type=mime_type,
                actor=actor,
                correlation_id=correlation_id,
            )

    def register_from_stream(
        self,
        ctx: ProjectContext,
        stream: BinaryIO,
        *,
        original_filename: str,
        mime_type: str | None = None,
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> DocumentRecord:
        if not original_filename:
            raise IntakeError("original_filename is required for stream uploads")
        return self._register(
            ctx,
            stream,
            original_filename=original_filename,
            mime_type=mime_type,
            actor=actor,
            correlation_id=correlation_id,
        )

    def _register(
        self,
        ctx: ProjectContext,
        stream: BinaryIO,
        *,
        original_filename: str,
        mime_type: str | None,
        actor: str,
        correlation_id: str | None,
    ) -> DocumentRecord:
        self._enforce_extension(original_filename)
        raw_dir = self._workspace.raw(ctx)
        raw_dir.mkdir(parents=True, exist_ok=True)

        # Stage in raw_dir so the final rename is on the same filesystem (atomic).
        tmp = tempfile.NamedTemporaryFile(
            dir=raw_dir,
            prefix=".intake_",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        try:
            try:
                checksum, file_size = _copy_and_hash(
                    stream, tmp, max_bytes=self._max_upload_bytes,
                )
            finally:
                tmp.close()

            # Defense-in-depth: the extension allow-list passed before
            # the copy started. Now that we have bytes on disk, sniff
            # the first 16 to confirm a binary format actually carries
            # its expected magic. Catches the renamed-extension case
            # (`malware.exe` posted as `report.pdf`) before the parser
            # gets a chance to crash on garbage input. Plain-text
            # extensions are exempted — text has no stable magic.
            self._enforce_magic_bytes(original_filename, tmp_path)

            existing = self._registry.find_by_checksum(ctx, checksum)
            if existing is not None:
                # Re-upload guard: a document being Removed (or in
                # ``cleanup_failed`` state) is not stable enough to
                # accept a fresh upload. Reject so the operator
                # waits for cleanup to complete (or unsticks the
                # orphan) before re-uploading the same file. On a
                # successful Remove the registry record was
                # deleted, so ``find_by_checksum`` returns None and
                # the upload proceeds as a fresh document — exactly
                # the user's mental model.
                existing_lifecycle = getattr(
                    existing, "lifecycle_status", "stable",
                )
                if existing_lifecycle in ("removing", "cleanup_failed"):
                    tmp_path.unlink(missing_ok=True)
                    raise DuplicateDocumentError(
                        f"document {existing.document_id} is currently "
                        f"{existing_lifecycle}; wait for cleanup to "
                        f"complete before re-uploading",
                        existing_document_id=existing.document_id,
                        checksum=checksum,
                    )
                tmp_path.unlink(missing_ok=True)
                self._emit_duplicate(
                    ctx=ctx,
                    existing_document_id=existing.document_id,
                    checksum=checksum,
                    original_filename=original_filename,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                raise DuplicateDocumentError(
                    f"checksum {checksum} already registered as {existing.document_id}",
                    existing_document_id=existing.document_id,
                    checksum=checksum,
                )

            document_id = self._id_factory()
            stored_filename = f"{document_id}{Path(original_filename).suffix}"
            final_path = raw_dir / stored_filename
            tmp_path.rename(final_path)

            resolved_mime = mime_type or mimetypes.guess_type(original_filename)[0]
            record = DocumentRecord(
                document_id=document_id,
                project=ctx,
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=resolved_mime,
                file_size=file_size,
                checksum=checksum,
                status=ProcessingStatus.PENDING,
                created_at=self._clock(),
            )
            try:
                self._registry.add(record)
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            self._emit_registered(
                ctx=ctx,
                record=record,
                actor=actor,
                correlation_id=correlation_id,
            )
            return record
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def _enforce_magic_bytes(
        self, original_filename: str, tmp_path: Path,
    ) -> None:
        """Reject files whose magic bytes don't match their extension.

 Looks up the suffix in `_BINARY_MAGIC_SIGNATURES`; absent →
 no-op (text formats and rare binaries fall through). Reads
 the first 16 bytes and matches against any of the expected
 prefixes — Office Open XML is a ZIP, OLE2 has its own header,
 PDF starts with `%PDF-`. On mismatch raises the same typed
 error the extension allow-list uses, so the REST adapter
 surfaces it as 415 with a consistent shape.
 """
        suffix = Path(original_filename).suffix.lower()
        signatures = _BINARY_MAGIC_SIGNATURES.get(suffix)
        if signatures is None:
            return
        try:
            head = tmp_path.read_bytes()[:16]
        except OSError:
            # Best-effort; an unreadable temp file would be caught
            # downstream when the staging rename fails. Don't double-
            # surface here.
            return
        if any(head.startswith(sig) for sig in signatures):
            return
        raise UnsupportedFileTypeError(
            f"file content does not match extension {suffix!r} "
            f"(expected magic bytes for that format were not present)",
            extension=suffix,
            allowed_extensions=tuple(sorted(self._allowed_extensions)),
        )

    def _enforce_extension(self, original_filename: str) -> None:
        """Reject filenames whose extension isn't in the allow-list.

 Empty allow-list disables the boundary entirely (operator
 opt-out). Otherwise compares case-insensitively. Raised
 before the streaming copy starts so an oversize-of-the-wrong-
 type upload doesn't waste bytes — the typed error surfaces as
 a 415 at the REST adapter.
 """
        if not self._allowed_extensions:
            return
        suffix = Path(original_filename).suffix.lower()
        if suffix in self._allowed_extensions:
            return
        # Sort the allowed set for a deterministic message (tests +
        # operator-readable response details).
        allowed_sorted = tuple(sorted(self._allowed_extensions))
        raise UnsupportedFileTypeError(
            f"file extension {suffix!r} is not in the upload allow-list",
            extension=suffix,
            allowed_extensions=allowed_sorted,
        )

    def _emit_registered(
        self,
        *,
        ctx: ProjectContext,
        record: DocumentRecord,
        actor: str,
        correlation_id: str | None,
    ) -> None:
        self._audit.write(
            AuditEvent(
                event_id=self._id_factory(),
                occurred_at=self._clock(),
                project=ctx,
                actor=actor,
                action=ACTION_REGISTERED,
                target_kind=TARGET_KIND,
                target_id=record.document_id,
                correlation_id=correlation_id,
                payload={
                    "checksum": record.checksum,
                    "file_size": record.file_size,
                    "mime_type": record.mime_type,
                    "original_filename": record.original_filename,
                    "stored_filename": record.stored_filename,
                },
            )
        )

    def _emit_duplicate(
        self,
        *,
        ctx: ProjectContext,
        existing_document_id: str,
        checksum: str,
        original_filename: str,
        actor: str,
        correlation_id: str | None,
    ) -> None:
        self._audit.write(
            AuditEvent(
                event_id=self._id_factory(),
                occurred_at=self._clock(),
                project=ctx,
                actor=actor,
                action=ACTION_DUPLICATE,
                target_kind=TARGET_KIND,
                target_id=existing_document_id,
                correlation_id=correlation_id,
                payload={
                    "checksum": checksum,
                    "original_filename": original_filename,
                },
            )
        )


def _copy_and_hash(
    src: BinaryIO, dest: BinaryIO, *, max_bytes: int,
) -> tuple[str, int]:
    """Stream-copy `src` into `dest` while computing the SHA-256.

 Raises `UploadTooLargeError` as soon as the cumulative byte count
 exceeds `max_bytes`. The boundary check happens during the copy
 rather than after, so an oversize stream stops writing immediately
 instead of filling the disk first. The temp file is left for the
 caller's outer try/except to unlink.
 """
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = src.read(_CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise UploadTooLargeError(
                f"upload exceeds {max_bytes}-byte cap (read {size} so far)",
                size_bytes=size,
                max_bytes=max_bytes,
            )
        dest.write(chunk)
        hasher.update(chunk)
    return f"{CHECKSUM_PREFIX}{hasher.hexdigest()}", size
