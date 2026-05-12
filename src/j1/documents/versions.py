"""Persistence for `DocumentVersion` — one row per stored file
content under a document.

Sits alongside `JsonSourceRegistry` (which owns `DocumentRecord`)
so that the document-centric refactor can grow version history
without touching the existing documents.json format. Each project
gets its own `document_versions.json` next to `documents.json`.

Identity rule: lookup-by-hash on
``(document_id, file_hash)`` returns an existing row when present,
matching the idempotency contract the re-index flow needs ("user
uploaded the same bytes again → same version_id").

Storage shape:

    {
      "version": 1,
      "versions": [
        {
          "document_version_id": "dv-…",
          "document_id": "doc-…",
          "project": {"tenant_id": "...", "project_id": "..."},
          "file_hash": "sha256:…",
          "original_filename": "Bridge Report.pdf",
          "storage_uri": "intake/abc123.pdf",
          "mime_type": "application/pdf",
          "size_bytes": 12345,
          "created_at": "2026-05-12T…",
          "created_by_run_id": "run-…" | null
        }
      ]
    }

The store uses the same atomic-rename write pattern as
`JsonSourceRegistry` so we never end up with a partially-written
file on power loss / OS kill.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from j1._serialization import to_jsonable
from j1.documents.models import DocumentVersion
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

VERSIONS_FILENAME = "document_versions.json"
VERSIONS_STORE_VERSION = 1


class DocumentVersionStore(Protocol):
    """Read/write surface for `DocumentVersion` records.

    Methods on this protocol intentionally match `SourceRegistry`'s
    shape so callers can pass either store around without learning
    a second pattern.
    """

    def add(self, version: DocumentVersion) -> DocumentVersion: ...

    def get(
        self, ctx: ProjectContext, document_version_id: str,
    ) -> DocumentVersion: ...

    def find_by_hash(
        self,
        ctx: ProjectContext,
        document_id: str,
        file_hash: str,
    ) -> DocumentVersion | None: ...

    def list_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> list[DocumentVersion]: ...


class DocumentVersionNotFoundError(Exception):
    """Raised when `get()` can't find a requested version_id.

    A separate exception class (not the existing
    `DocumentNotFoundError`) so REST handlers can render distinct
    404 messages — the FE shouldn't conflate "the document
    doesn't exist" with "this specific version of the document
    doesn't exist".
    """


class JsonDocumentVersionStore:
    """File-backed implementation. One JSON document per project.

    Same atomic-rename pattern `JsonSourceRegistry` uses. Read paths
    are O(n) over the project's version list — fine for the
    workloads we're targeting (single-digit-to-thousands of
    versions per project).
    """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, version: DocumentVersion) -> DocumentVersion:
        records = self._read(version.project)
        # Idempotent on `(document_id, file_hash)` — re-uploading
        # the same bytes for the same document returns the existing
        # row. Lets the re-index flow keep calling `add()` without
        # needing a separate "find or create" helper.
        existing = next(
            (
                r for r in records
                if r.document_id == version.document_id
                and r.file_hash == version.file_hash
            ),
            None,
        )
        if existing is not None:
            return existing
        records.append(version)
        self._write(version.project, records)
        return version

    def get(
        self, ctx: ProjectContext, document_version_id: str,
    ) -> DocumentVersion:
        for record in self._read(ctx):
            if record.document_version_id == document_version_id:
                return record
        raise DocumentVersionNotFoundError(
            f"document_version {document_version_id!r} not found in "
            f"{ctx.tenant_id}/{ctx.project_id}"
        )

    def find_by_hash(
        self,
        ctx: ProjectContext,
        document_id: str,
        file_hash: str,
    ) -> DocumentVersion | None:
        for record in self._read(ctx):
            if (
                record.document_id == document_id
                and record.file_hash == file_hash
            ):
                return record
        return None

    def list_for_document(
        self, ctx: ProjectContext, document_id: str,
    ) -> list[DocumentVersion]:
        return [
            r for r in self._read(ctx) if r.document_id == document_id
        ]

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / VERSIONS_FILENAME

    def _read(self, ctx: ProjectContext) -> list[DocumentVersion]:
        path = self._path(ctx)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [
            _version_from_dict(entry) for entry in data.get("versions", [])
        ]

    def _write(
        self, ctx: ProjectContext, records: list[DocumentVersion],
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSIONS_STORE_VERSION,
            "versions": [to_jsonable(r) for r in records],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _version_from_dict(d: dict) -> DocumentVersion:
    """Tolerant deserialiser. Missing optional fields fall back to
    safe defaults so future schema bumps don't require migrations.
    """
    project_data = d["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    return DocumentVersion(
        document_version_id=d["document_version_id"],
        document_id=d["document_id"],
        project=project,
        file_hash=d["file_hash"],
        original_filename=d["original_filename"],
        storage_uri=d.get("storage_uri") or "",
        mime_type=d.get("mime_type"),
        size_bytes=int(d.get("size_bytes") or 0),
        created_at=datetime.fromisoformat(d["created_at"]),
        created_by_run_id=d.get("created_by_run_id"),
    )


__all__ = [
    "DocumentVersionNotFoundError",
    "DocumentVersionStore",
    "JsonDocumentVersionStore",
    "VERSIONS_FILENAME",
]
