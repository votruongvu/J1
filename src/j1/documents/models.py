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
    uri: str
    content_hash: str
    byte_size: int
    mime_type: str | None
    status: ProcessingStatus
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
