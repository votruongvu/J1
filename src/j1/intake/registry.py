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
REGISTRY_VERSION = 1


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
    )
