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

    # ---- Snapshot-centered fields (Phase 2) -----------------------
    # ``snapshot_id`` is the strict storage / visibility key. Phase 2
    # producers stamp it; Phase 3 makes it required for the lineage-
    # required kinds (graph_json + friends). ``None`` is allowed
    # during the migration so existing artifacts deserialise cleanly
    # without a rewrite.
    #
    # ``created_by_run_id`` records the execution that produced this
    # artifact. Replaces the metadata-stamped run_id (which stays in
    # ``metadata["run_id"]`` for now so legacy readers keep working).
    snapshot_id: str | None = None
    created_by_run_id: str | None = None
