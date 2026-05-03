import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from j1._serialization import to_jsonable
from j1.artifacts.models import ArtifactRecord
from j1.errors.exceptions import J1Error
from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

ARTIFACT_REGISTRY_FILENAME = "artifacts.json"
ARTIFACT_REGISTRY_VERSION = 1


class ArtifactNotFoundError(J1Error):
    pass


class ArtifactRegistry(Protocol):
    def add(self, record: ArtifactRecord) -> None: ...

    def get(self, ctx: ProjectContext, artifact_id: str) -> ArtifactRecord: ...

    def find_by_content_hash(
        self, ctx: ProjectContext, content_hash: str
    ) -> ArtifactRecord | None: ...

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None
    ) -> list[ArtifactRecord]: ...


class JsonArtifactRegistry:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, record: ArtifactRecord) -> None:
        records = self._read(record.project)
        if any(r.artifact_id == record.artifact_id for r in records):
            raise J1Error(
                f"artifact_id {record.artifact_id} already present in registry"
            )
        records.append(record)
        self._write(record.project, records)

    def get(self, ctx: ProjectContext, artifact_id: str) -> ArtifactRecord:
        for record in self._read(ctx):
            if record.artifact_id == artifact_id:
                return record
        raise ArtifactNotFoundError(
            f"artifact {artifact_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def find_by_content_hash(
        self, ctx: ProjectContext, content_hash: str
    ) -> ArtifactRecord | None:
        for record in self._read(ctx):
            if record.content_hash == content_hash:
                return record
        return None

    def list_artifacts(
        self, ctx: ProjectContext, *, kind: str | None = None
    ) -> list[ArtifactRecord]:
        records = self._read(ctx)
        if kind is None:
            return records
        return [r for r in records if r.kind == kind]

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / ARTIFACT_REGISTRY_FILENAME

    def _read(self, ctx: ProjectContext) -> list[ArtifactRecord]:
        path = self._path(ctx)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [_record_from_dict(d) for d in data.get("artifacts", [])]

    def _write(
        self, ctx: ProjectContext, records: list[ArtifactRecord]
    ) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": ARTIFACT_REGISTRY_VERSION,
            "artifacts": [to_jsonable(r) for r in records],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _record_from_dict(d: dict) -> ArtifactRecord:
    project_data = d["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    return ArtifactRecord(
        artifact_id=d["artifact_id"],
        project=project,
        kind=d["kind"],
        location=d["location"],
        content_hash=d["content_hash"],
        byte_size=d["byte_size"],
        status=ProcessingStatus(d["status"]),
        review_status=ReviewStatus(d["review_status"]),
        version=d["version"],
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        source_document_ids=list(d.get("source_document_ids", [])),
        source_artifact_ids=list(d.get("source_artifact_ids", [])),
        metadata=dict(d.get("metadata", {})),
    )
