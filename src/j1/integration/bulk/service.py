"""Bulk export and import services.

These are transport-neutral: the export methods yield byte lines (one
NDJSON record per line) and the import methods accept an iterable of
raw lines. The REST adapter wraps the byte iterator in a
`StreamingResponse` and pipes the request body in line-by-line. A
future CLI or Temporal-backed worker can reuse the same services
without touching this module.

Concurrency / sizing note: today's `JsonSourceRegistry` /
`JsonArtifactRegistry` are flat per-project JSON files (single-writer,
no locking). Bulk export is a deterministic projection — cheap. Bulk
import is bounded by the registry's `add` cost; for very large
imports run the call from a worker process. The framework's existing
single-writer assumption is documented in CLAUDE.md.
"""

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

from pydantic import ValidationError

from j1.artifacts.registry import ArtifactRegistry
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DuplicateDocumentError
from j1.integration.bulk.result import (
    BulkImportFailureRecord,
    BulkImportResult,
    ERROR_CODE_DOCUMENT_NOT_FOUND,
    ERROR_CODE_INTEGRITY_MISMATCH,
    ERROR_CODE_INVALID_JSON,
    ERROR_CODE_PROJECT_MISMATCH,
    ERROR_CODE_SCHEMA,
)
from j1.integration.bulk.schemas import (
    ArtifactExportRecord,
    CitationExportRecord,
    DocumentExportRecord,
    FeedbackExportRecord,
    MetadataExportRecord,
    SourceExportRecord,
)
from j1.integration.feedback import FeedbackStore
from j1.intake.registry import SourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


# ---- Helpers ---------------------------------------------------------


def _to_line(payload: dict) -> bytes:
    """Pack `payload` as one NDJSON line (no embedded newlines, then `\\n`)."""
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode("utf-8")


# ---- Export ----------------------------------------------------------


class BulkExportService:
    """Yields NDJSON byte lines for each supported file type.

 Each method takes a `ProjectContext` so output is tenant/project
 scoped — the same access discipline as every other read endpoint.
 """

    def __init__(
        self,
        sources: SourceRegistry,
        artifacts: ArtifactRegistry,
        feedback: FeedbackStore,
    ) -> None:
        self._sources = sources
        self._artifacts = artifacts
        self._feedback = feedback

    # documents.ndjson + sources.ndjson share the same row shape;
    # the REST adapter exposes both endpoints for clarity.
    def export_documents(self, ctx: ProjectContext) -> Iterator[bytes]:
        for record in self._sources.list_documents(ctx):
            yield _to_line(_document_to_export(record).model_dump(by_alias=True, mode="json"))

    def export_sources(self, ctx: ProjectContext) -> Iterator[bytes]:
        for record in self._sources.list_documents(ctx):
            payload = _document_to_export(record).model_dump(by_alias=True, mode="json")
            yield _to_line(SourceExportRecord(**payload).model_dump(by_alias=True, mode="json"))

    def export_artifacts(self, ctx: ProjectContext) -> Iterator[bytes]:
        for record in self._artifacts.list_artifacts(ctx):
            yield _to_line(_artifact_to_export(record).model_dump(by_alias=True, mode="json"))

    def export_citations(self, ctx: ProjectContext) -> Iterator[bytes]:
        # Citations are derived from artifact lineage — one row per
        # (artifact, source_document_id) pair. Keeps the export shape
        # flat for analytics tools.
        for artifact in self._artifacts.list_artifacts(ctx):
            for doc_id in artifact.source_document_ids:
                citation = CitationExportRecord(
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.kind,
                    source_document_id=doc_id,
                    source_location=str(artifact.metadata.get("source_location") or "")
                                    or None,
                )
                yield _to_line(citation.model_dump(by_alias=True, mode="json"))

    def export_metadata(self, ctx: ProjectContext) -> Iterator[bytes]:
        for record in self._sources.list_documents(ctx):
            metadata = MetadataExportRecord(
                document_id=record.document_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                original_filename=record.original_filename,
                mime_type=record.mime_type,
                file_size=record.file_size,
                checksum=record.checksum,
                status=record.status.value,
                created_at=record.created_at,
            )
            yield _to_line(metadata.model_dump(by_alias=True, mode="json"))

    def export_feedback(self, ctx: ProjectContext) -> Iterator[bytes]:
        for record in self._feedback.list_for(ctx):
            payload = FeedbackExportRecord(
                feedback_id=record.feedback_id,
                tenant_id=record.project.tenant_id,
                project_id=record.project.project_id,
                target_kind=record.target_kind,
                target_id=record.target_id,
                submitted_at=record.submitted_at,
                rating=record.rating,
                comment=record.comment,
                actor=record.actor,
                correlation_id=record.correlation_id,
                metadata=dict(record.metadata),
            )
            yield _to_line(payload.model_dump(by_alias=True, mode="json"))


def _document_to_export(record: DocumentRecord) -> DocumentExportRecord:
    return DocumentExportRecord(
        document_id=record.document_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        original_filename=record.original_filename,
        stored_filename=record.stored_filename,
        mime_type=record.mime_type,
        file_size=record.file_size,
        checksum=record.checksum,
        status=record.status.value,
        created_at=record.created_at,
    )


def _artifact_to_export(record) -> ArtifactExportRecord:
    return ArtifactExportRecord(
        artifact_id=record.artifact_id,
        tenant_id=record.project.tenant_id,
        project_id=record.project.project_id,
        kind=record.kind,
        location=record.location,
        content_hash=record.content_hash,
        byte_size=record.byte_size,
        status=record.status.value,
        review_status=record.review_status.value,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        source_document_ids=list(record.source_document_ids),
        source_artifact_ids=list(record.source_artifact_ids),
        metadata=dict(record.metadata),
    )


# ---- Import ----------------------------------------------------------


class BulkImportService:
    """Validates NDJSON lines and writes accepted records to the registries.

 All methods are idempotent by default: rows whose identity already
 exists (document checksum, etc.) are counted as
 `skipped_idempotent` rather than overwriting. There is no overwrite
 flag in this revision — explicit replacement should go through the
 single-record endpoints once they support it.
 """

    def __init__(self, sources: SourceRegistry) -> None:
        self._sources = sources

    def import_documents(
        self,
        ctx: ProjectContext,
        lines: Iterable[bytes | str],
    ) -> BulkImportResult:
        return self._import_documents_inner(ctx, lines, schema=DocumentExportRecord)

    def import_sources(
        self,
        ctx: ProjectContext,
        lines: Iterable[bytes | str],
    ) -> BulkImportResult:
        # Same shape as documents — sources == documents in this codebase.
        return self._import_documents_inner(ctx, lines, schema=SourceExportRecord)

    def verify_metadata(
        self,
        ctx: ProjectContext,
        lines: Iterable[bytes | str],
    ) -> BulkImportResult:
        """Round-trip integrity verifier.

 Each `metadata.ndjson` row must reference a document that exists
 in the registry, and the provided fields must match the stored
 values. No state is mutated. Used to validate a backup/restore
 before promoting the new instance.
 """
        succeeded = 0
        failures: list[BulkImportFailureRecord] = []
        for line_no, raw, parsed_or_err in _iter_records(lines, MetadataExportRecord):
            if isinstance(parsed_or_err, BulkImportFailureRecord):
                failures.append(parsed_or_err)
                continue
            record: MetadataExportRecord = parsed_or_err
            if record.tenant_id != ctx.tenant_id or record.project_id != ctx.project_id:
                failures.append(BulkImportFailureRecord(
                    line_number=line_no, record_id=record.document_id,
                    code=ERROR_CODE_PROJECT_MISMATCH,
                    message="record tenant/project does not match the request scope",
                ))
                continue
            try:
                stored = self._sources.get(ctx, record.document_id)
            except Exception:
                failures.append(BulkImportFailureRecord(
                    line_number=line_no, record_id=record.document_id,
                    code=ERROR_CODE_DOCUMENT_NOT_FOUND,
                    message=f"document {record.document_id!r} is not in the registry",
                ))
                continue
            mismatches = _metadata_mismatches(stored, record)
            if mismatches:
                failures.append(BulkImportFailureRecord(
                    line_number=line_no, record_id=record.document_id,
                    code=ERROR_CODE_INTEGRITY_MISMATCH,
                    message=f"metadata mismatch: {', '.join(mismatches)}",
                ))
                continue
            succeeded += 1
        return BulkImportResult(succeeded=succeeded, failures=failures)

    # --- internals ---

    def _import_documents_inner(
        self,
        ctx: ProjectContext,
        lines: Iterable[bytes | str],
        *,
        schema: type[DocumentExportRecord],
    ) -> BulkImportResult:
        succeeded = 0
        skipped = 0
        failures: list[BulkImportFailureRecord] = []
        for line_no, raw, parsed_or_err in _iter_records(lines, schema):
            if isinstance(parsed_or_err, BulkImportFailureRecord):
                failures.append(parsed_or_err)
                continue
            record: DocumentExportRecord = parsed_or_err
            if record.tenant_id != ctx.tenant_id or record.project_id != ctx.project_id:
                failures.append(BulkImportFailureRecord(
                    line_number=line_no, record_id=record.document_id,
                    code=ERROR_CODE_PROJECT_MISMATCH,
                    message="record tenant/project does not match the request scope",
                ))
                continue
            # Idempotency: same checksum already registered? Skip silently.
            existing = self._sources.find_by_checksum(ctx, record.checksum)
            if existing is not None:
                skipped += 1
                continue
            try:
                domain = _export_to_document(ctx, record)
            except ValueError as exc:
                failures.append(BulkImportFailureRecord(
                    line_number=line_no, record_id=record.document_id,
                    code=ERROR_CODE_SCHEMA, message=str(exc),
                ))
                continue
            try:
                self._sources.add(domain)
            except DuplicateDocumentError:
                # Race / second importer — treat as idempotent skip.
                skipped += 1
                continue
            succeeded += 1
        return BulkImportResult(
            succeeded=succeeded, skipped_idempotent=skipped, failures=failures,
        )


def _iter_records(
    lines: Iterable[bytes | str], schema,
):
    """Decode + validate each line. Yields (line_number, raw, parsed_or_failure)."""
    for line_no, raw in enumerate(lines, start=1):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        text = text.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            yield line_no, text, BulkImportFailureRecord(
                line_number=line_no, record_id=None,
                code=ERROR_CODE_INVALID_JSON, message=str(exc),
            )
            continue
        try:
            record = schema.model_validate(payload)
        except ValidationError as exc:
            record_id = None
            if isinstance(payload, dict):
                record_id = (
                    payload.get("documentId")
                    or payload.get("document_id")
                    or payload.get("id")
                )
            yield line_no, text, BulkImportFailureRecord(
                line_number=line_no, record_id=record_id,
                code=ERROR_CODE_SCHEMA,
                message=_truncate(str(exc), 500),
            )
            continue
        yield line_no, text, record


def _export_to_document(ctx: ProjectContext, record: DocumentExportRecord) -> DocumentRecord:
    try:
        status = ProcessingStatus(record.status)
    except ValueError as exc:
        raise ValueError(f"unknown status {record.status!r}") from exc
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return DocumentRecord(
        document_id=record.document_id,
        project=ctx,
        original_filename=record.original_filename,
        stored_filename=record.stored_filename,
        mime_type=record.mime_type,
        file_size=record.file_size,
        checksum=record.checksum,
        status=status,
        created_at=created_at,
    )


def _metadata_mismatches(
    stored: DocumentRecord, supplied: MetadataExportRecord,
) -> list[str]:
    diffs: list[str] = []
    if stored.original_filename != supplied.original_filename:
        diffs.append("originalFilename")
    if stored.mime_type != supplied.mime_type:
        diffs.append("mimeType")
    if stored.file_size != supplied.file_size:
        diffs.append("fileSize")
    if stored.checksum != supplied.checksum:
        diffs.append("checksum")
    if stored.status.value != supplied.status:
        diffs.append("status")
    return diffs


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
