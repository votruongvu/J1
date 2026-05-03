from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


@dataclass(frozen=True)
class SourceDocument:
    uri: str
    content_type: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentRecord:
    document_id: str
    project: ProjectContext
    original_filename: str
    stored_filename: str
    mime_type: str | None
    file_size: int
    checksum: str
    status: ProcessingStatus
    created_at: datetime

    @property
    def tenant_id(self) -> str:
        return self.project.tenant_id

    @property
    def project_id(self) -> str:
        return self.project.project_id
