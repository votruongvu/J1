from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


@dataclass
class JobRecord:
    job_id: str
    project: ProjectContext
    kind: str
    status: ProcessingStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attempt: int = 0
    error_message: str | None = None
    correlation_id: str | None = None
    input_refs: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
