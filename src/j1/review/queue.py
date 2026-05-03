import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from j1._serialization import to_jsonable
from j1.errors.exceptions import J1Error
from j1.jobs.status import ReviewStatus
from j1.projects.context import ProjectContext
from j1.review.models import ReviewItem
from j1.workspace.resolver import WorkspaceResolver

REVIEW_QUEUE_FILENAME = "review_items.json"
REVIEW_QUEUE_VERSION = 1


class ReviewItemNotFoundError(J1Error):
    pass


class ReviewQueue(Protocol):
    def add(self, item: ReviewItem) -> None: ...

    def get(self, ctx: ProjectContext, review_item_id: str) -> ReviewItem: ...

    def list_pending(self, ctx: ProjectContext) -> list[ReviewItem]: ...

    def list_items(self, ctx: ProjectContext) -> list[ReviewItem]: ...

    def update_status(
        self,
        ctx: ProjectContext,
        review_item_id: str,
        review_status: ReviewStatus,
        *,
        actor: str | None = None,
        notes: str | None = None,
    ) -> None: ...


class JsonReviewQueue:
    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def add(self, item: ReviewItem) -> None:
        items = self._read(item.project)
        if any(i.review_item_id == item.review_item_id for i in items):
            raise J1Error(
                f"review_item_id {item.review_item_id} already in queue"
            )
        items.append(item)
        self._write(item.project, items)

    def get(self, ctx: ProjectContext, review_item_id: str) -> ReviewItem:
        for item in self._read(ctx):
            if item.review_item_id == review_item_id:
                return item
        raise ReviewItemNotFoundError(
            f"review item {review_item_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def list_items(self, ctx: ProjectContext) -> list[ReviewItem]:
        return self._read(ctx)

    def list_pending(self, ctx: ProjectContext) -> list[ReviewItem]:
        return [i for i in self._read(ctx) if i.review_status == ReviewStatus.PENDING]

    def update_status(
        self,
        ctx: ProjectContext,
        review_item_id: str,
        review_status: ReviewStatus,
        *,
        actor: str | None = None,
        notes: str | None = None,
    ) -> None:
        items = self._read(ctx)
        for item in items:
            if item.review_item_id == review_item_id:
                item.review_status = review_status
                if actor is not None:
                    item.actor = actor
                if notes is not None:
                    item.notes = notes
                self._write(ctx, items)
                return
        raise ReviewItemNotFoundError(
            f"review item {review_item_id} not found in {ctx.tenant_id}/{ctx.project_id}"
        )

    def _path(self, ctx: ProjectContext) -> Path:
        return self._workspace.runtime(ctx) / REVIEW_QUEUE_FILENAME

    def _read(self, ctx: ProjectContext) -> list[ReviewItem]:
        path = self._path(ctx)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [_item_from_dict(d) for d in data.get("items", [])]

    def _write(self, ctx: ProjectContext, items: list[ReviewItem]) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REVIEW_QUEUE_VERSION,
            "items": [to_jsonable(i) for i in items],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)


def _item_from_dict(data: dict) -> ReviewItem:
    project_data = data["project"]
    project = ProjectContext(
        tenant_id=project_data["tenant_id"],
        project_id=project_data["project_id"],
        profile=project_data.get("profile"),
    )
    return ReviewItem(
        review_item_id=data["review_item_id"],
        project=project,
        target_kind=data["target_kind"],
        target_id=data["target_id"],
        review_status=ReviewStatus(data["review_status"]),
        requested_at=datetime.fromisoformat(data["requested_at"]),
        actor=data.get("actor"),
        notes=data.get("notes"),
        metadata=dict(data.get("metadata", {})),
    )
