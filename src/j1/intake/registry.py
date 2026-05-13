import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from j1._serialization import to_jsonable
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError, IntakeError
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

REGISTRY_FILENAME = "documents.json"
# Bumped from 1 → 2 on the document-centric refactor. v2 records
# carry `knowledge_state`, `active_run_id`, `latest_version_id`,
# `removed_at`, `updated_at`. The deserializer below remains
# tolerant of v1 records: missing fields fall back to safe defaults
# (knowledge_state="attached") so existing project workspaces keep
# working without any explicit migration step. Bumping the version
# is a write-side signal only — readers still accept both.
REGISTRY_VERSION = 2


class SourceRegistry(Protocol):
    def add(self, record: DocumentRecord) -> None: ...

    def get(self, ctx: ProjectContext, document_id: str) -> DocumentRecord: ...

    def find_by_checksum(
        self, ctx: ProjectContext, checksum: str
    ) -> DocumentRecord | None: ...

    def list_documents(self, ctx: ProjectContext) -> list[DocumentRecord]: ...

    def update_status(
        self,
        ctx: ProjectContext,
        document_id: str,
        status: ProcessingStatus,
    ) -> None:
        """Transition a document's status.

 Called by the workflow after each document finishes (or fails)
 to flip it off `PENDING` so subsequent project-wide jobs
 don't re-pick the same documents. Raises
 `DocumentNotFoundError` if the document isn't registered."""
        ...

    def update_document_fields(
        self,
        ctx: ProjectContext,
        document_id: str,
        **updates,
    ) -> DocumentRecord:
        """Generic field update for the document-centric lifecycle.

 Used by `DocumentLifecycleService` to flip `knowledge_state` +
 the timestamp/active-run fields. Raises `DocumentNotFoundError`
 when the id isn't registered."""
        ...

    def try_acquire_operation_lock(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        operation: str,
        run_id: str | None = None,
    ) -> DocumentRecord | None:
        """Best-effort CAS lock for a document mutation.

        Atomically (under the registry's write barrier) checks that
        ``pending_operation`` is None and sets it to ``operation``
        with ``run_id`` and a fresh ``started_at``. Returns the
        updated record on success, ``None`` if the lock was already
        held by another operation."""
        ...

    def release_operation_lock(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> DocumentRecord:
        """Release the per-document operation lock.

        When ``expected_run_id`` is supplied, only releases the lock
        if the stored ``pending_operation_run_id`` matches — this
        guards against a stale handler clearing a newer
        operation's lock."""
        ...

    def delete(
        self, ctx: ProjectContext, document_id: str,
    ) -> bool:
        """Physically remove the ``DocumentRecord`` entry.

        Used by ``DocumentCleanupService.cleanup_document`` at the
        tail of a successful Remove so the user can re-upload the
        same file as a fresh document (new id). Returns True iff a
        record was removed; False (idempotent) when the id wasn't
        present."""
        ...

    def try_promote_active_run_id(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        new_run_id: str,
        expected_active_run_id: str | None,
        completed_at,
    ) -> DocumentRecord | None:
        """Atomic CAS: set ``active_run_id = new_run_id`` only if the
        current value equals ``expected_active_run_id`` AND the
        document isn't being removed.

        Returns the updated record on success, ``None`` if the CAS
        precondition didn't hold (concurrent promotion / document
        removed / mid-removal). Callers MUST treat ``None`` as "the
        candidate is now orphaned; trigger cleanup"."""
        ...


class JsonSourceRegistry:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, record: DocumentRecord) -> None:
        records = self._read(record.project)
        if any(r.document_id == record.document_id for r in records):
            raise IntakeError(
                f"document_id {record.document_id} already present in registry"
            )
        records.append(record)
        self._write(record.project, records)

    def get(self, ctx: ProjectContext, document_id: str) -> DocumentRecord:
        for record in self._read(ctx):
            if record.document_id == document_id:
                return record
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def find_by_checksum(
        self, ctx: ProjectContext, checksum: str
    ) -> DocumentRecord | None:
        for record in self._read(ctx):
            if record.checksum == checksum:
                return record
        return None

    def list_documents(self, ctx: ProjectContext) -> list[DocumentRecord]:
        return self._read(ctx)

    def update_status(
        self,
        ctx: ProjectContext,
        document_id: str,
        status: ProcessingStatus,
    ) -> None:
        records = self._read(ctx)
        for record in records:
            if record.document_id == document_id:
                record.status = status
                self._write(ctx, records)
                return
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def update_document_fields(
        self,
        ctx: ProjectContext,
        document_id: str,
        **updates,
    ) -> DocumentRecord:
        """Replace one or more fields on a stored `DocumentRecord`.

        Public surface for the document-centric lifecycle actions
        (attach / detach / remove). Distinct from `update_status`
        because `status` carries the *ingestion* outcome of the
        upload itself; the new fields (`knowledge_state`,
        `active_run_id`, `removed_at`, `updated_at`,
        `latest_version_id`) describe knowledge-layer state which is
        conceptually separate.

        Raises `DocumentNotFoundError` if the id isn't present.
        Returns the updated record so callers don't have to re-read.
        """
        from dataclasses import replace as _replace
        records = self._read(ctx)
        for i, record in enumerate(records):
            if record.document_id == document_id:
                merged = _replace(record, **updates)
                records[i] = merged
                self._write(ctx, records)
                return merged
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def try_acquire_operation_lock(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        operation: str,
        run_id: str | None = None,
    ) -> DocumentRecord | None:
        """Atomic compare-and-set for the per-document mutation lock.

        The atomicity is "atomic enough" for dev mode: read + check
        + write happens inside one call, and ``_write`` uses
        ``tmp.replace(path)`` so concurrent writers can't observe a
        torn file. A higher-bar implementation (e.g. file lock)
        belongs in the eventual production registry."""
        from dataclasses import replace as _replace
        from datetime import timezone

        records = self._read(ctx)
        for i, record in enumerate(records):
            if record.document_id != document_id:
                continue
            # Lock already held → CAS fails. Same-operation retries
            # are NOT auto-allowed here: the caller must release
            # then re-acquire so we don't accidentally extend a
            # crashed handler's lock.
            if record.pending_operation is not None:
                return None
            merged = _replace(
                record,
                pending_operation=operation,  # type: ignore[arg-type]
                pending_operation_run_id=run_id,
                pending_operation_started_at=datetime.now(timezone.utc),
            )
            records[i] = merged
            self._write(ctx, records)
            return merged
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def delete(
        self, ctx: ProjectContext, document_id: str,
    ) -> bool:
        records = self._read(ctx)
        before = len(records)
        kept = [r for r in records if r.document_id != document_id]
        if len(kept) == before:
            return False
        self._write(ctx, kept)
        return True

    def try_promote_active_run_id(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        new_run_id: str,
        expected_active_run_id: str | None,
        completed_at,
    ) -> DocumentRecord | None:
        """Compare-and-set promotion.

        Promotes ``new_run_id`` to ``active_run_id`` IFF:

          * the document's current ``active_run_id`` equals
            ``expected_active_run_id`` (no concurrent promotion
            stole the slot mid-run), AND
          * the document is NOT in a removed knowledge state, AND
          * the document is NOT mid-removal (lifecycle_status in
            {``removing``, ``removed``, ``failed``,
            ``cleanup_failed``}).

        Returns the updated record on success, ``None`` when the
        CAS precondition didn't hold. ``None`` is the signal the
        caller must use to trigger candidate-cleanup — the run
        succeeded but its result is now orphan."""
        from dataclasses import replace as _replace

        records = self._read(ctx)
        for i, record in enumerate(records):
            if record.document_id != document_id:
                continue
            if record.knowledge_state == "removed":
                return None
            if record.lifecycle_status in (
                "removing", "removed", "failed", "cleanup_failed",
            ):
                return None
            if record.active_run_id != expected_active_run_id:
                return None
            merged = _replace(
                record,
                active_run_id=new_run_id,
                updated_at=completed_at,
            )
            records[i] = merged
            self._write(ctx, records)
            return merged
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def release_operation_lock(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> DocumentRecord:
        """Idempotent release.

        Always-safe: if the lock is already clear, returns the
        record unchanged. If ``expected_run_id`` is supplied and
        doesn't match the currently-held lock, the release is
        skipped — a stale handler can't clear someone else's lock."""
        from dataclasses import replace as _replace

        records = self._read(ctx)
        for i, record in enumerate(records):
            if record.document_id != document_id:
                continue
            if record.pending_operation is None:
                return record
            if (
                expected_run_id is not None
                and record.pending_operation_run_id != expected_run_id
            ):
                return record
            merged = _replace(
                record,
                pending_operation=None,
                pending_operation_run_id=None,
                pending_operation_started_at=None,
            )
            records[i] = merged
            self._write(ctx, records)
            return merged
        raise DocumentNotFoundError(
            f"document {document_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / REGISTRY_FILENAME

    def _read(self, ctx: ProjectContext) -> list[DocumentRecord]:
        path = self._path(ctx)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [_record_from_dict(d) for d in data.get("documents", [])]

    def _write(
        self, ctx: ProjectContext, records: list[DocumentRecord]
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "documents": [to_jsonable(r) for r in records],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _record_from_dict(d: dict) -> DocumentRecord:
    project_data = d["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    # New v2 fields default safely when absent — legacy v1 documents
    # on disk parse without modification. `knowledge_state` defaults
    # to "attached" so retrieval treats pre-refactor documents
    # exactly as before.
    raw_state = d.get("knowledge_state") or "attached"
    if raw_state not in ("attached", "detached", "removed"):
        raw_state = "attached"
    raw_lifecycle = d.get("lifecycle_status") or "stable"
    if raw_lifecycle not in (
        "stable", "removing", "removed", "cleanup_failed", "failed",
    ):
        raw_lifecycle = "stable"
    raw_pending_op = d.get("pending_operation")
    if raw_pending_op not in (
        "reindex", "refresh_enrich", "detach", "attach", "remove", None,
    ):
        raw_pending_op = None
    removed_at = (
        datetime.fromisoformat(d["removed_at"])
        if d.get("removed_at") else None
    )
    updated_at = (
        datetime.fromisoformat(d["updated_at"])
        if d.get("updated_at") else None
    )
    pending_started = (
        datetime.fromisoformat(d["pending_operation_started_at"])
        if d.get("pending_operation_started_at") else None
    )
    return DocumentRecord(
        document_id=d["document_id"],
        project=project,
        original_filename=d["original_filename"],
        stored_filename=d["stored_filename"],
        mime_type=d.get("mime_type"),
        file_size=d["file_size"],
        checksum=d["checksum"],
        status=ProcessingStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        knowledge_state=raw_state,  # type: ignore[arg-type]
        active_run_id=d.get("active_run_id"),
        latest_version_id=d.get("latest_version_id"),
        removed_at=removed_at,
        updated_at=updated_at,
        lifecycle_status=raw_lifecycle,  # type: ignore[arg-type]
        pending_operation=raw_pending_op,  # type: ignore[arg-type]
        pending_operation_run_id=d.get("pending_operation_run_id"),
        pending_operation_started_at=pending_started,
    )
