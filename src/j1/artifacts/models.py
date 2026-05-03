from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from j1.jobs.status import ProcessingStatus, ReviewStatus
from j1.projects.context import ProjectContext


@dataclass
class ArtifactRecord:
    artifact_id: str
    project: ProjectContext
    kind: str
    location: str
    content_hash: str
    byte_size: int
    status: ProcessingStatus
    review_status: ReviewStatus
    version: int
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
