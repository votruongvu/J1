import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from j1._serialization import to_jsonable
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

FEEDBACK_FILENAME = "feedback.jsonl"

TARGET_KIND_ARTIFACT = "artifact"
TARGET_KIND_QUERY = "query"
TARGET_KIND_DOCUMENT = "document"
TARGET_KIND_REVIEW_ITEM = "review_item"


@dataclass(frozen=True)
class FeedbackRecord:
    feedback_id: str
    project: ProjectContext
    target_kind: str
    target_id: str
    submitted_at: datetime
    rating: int | None = None
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class FeedbackStore(Protocol):
    def add(self, record: FeedbackRecord) -> None: ...

    def list_for(
        self,
        ctx: ProjectContext,
        *,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> list[FeedbackRecord]: ...


class JsonlFeedbackStore:
    """Append-only JSONL feedback log at `<project>/runtime/feedback.jsonl`.

 Mirrors the audit/cost sink pattern: one event per line, idempotent
 appends, no in-place updates.
 """

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, record: FeedbackRecord) -> None:
        path = self._path(record.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(record), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def list_for(
        self,
        ctx: ProjectContext,
        *,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> list[FeedbackRecord]:
        path = self._path(ctx)
        if not path.exists():
            return []
        results: list[FeedbackRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if target_kind is not None and data.get("target_kind") != target_kind:
                continue
            if target_id is not None and data.get("target_id") != target_id:
                continue
            results.append(_record_from_dict(data))
        return results

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / FEEDBACK_FILENAME


def _record_from_dict(data: dict) -> FeedbackRecord:
    project_data = data["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    return FeedbackRecord(
        feedback_id=data["feedback_id"],
        project=project,
        target_kind=data["target_kind"],
        target_id=data["target_id"],
        submitted_at=datetime.fromisoformat(data["submitted_at"]),
        rating=data.get("rating"),
        comment=data.get("comment"),
        actor=data.get("actor"),
        correlation_id=data.get("correlation_id"),
        metadata=dict(data.get("metadata", {})),
    )
