"""BatchRun store: append-only JSONL of multi-upload batch records.

A `BatchRun` groups N child ingestion runs created from a single
multi-upload request. The FE needs the batch view (file list,
per-file status, current-running file) without joining N times.

Storage is parallel to `IngestionRunStore` — separate JSONL file
under the workspace's `audit` area; latest-snapshot wins on read.

Status of a batch is derived from its child runs at read-time, not
persisted, so we never have to write a synchronised view of N rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol

from j1._serialization import to_jsonable
from j1.projects.context import ProjectContext
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

BATCH_RUNS_FILENAME = "batch_runs.jsonl"

__all__ = [
    "BATCH_RUNS_FILENAME",
    "BatchRun",
    "BatchRunStore",
    "JsonlBatchRunStore",
]


@dataclass
class BatchRun:
    """One multi-upload batch.

 `run_ids` is the ordered list of child IngestionRun ids (one per
 file). `file_count` is `len(run_ids)` at creation. The batch's
 aggregate status is derived from the child runs at read-time —
 never persisted, never goes stale."""

    batch_run_id: str
    tenant_id: str
    project_id: str
    run_ids: list[str]
    file_count: int
    started_at: datetime
    actor: str = "system"
    metadata: dict = field(default_factory=dict)


class BatchRunStore(Protocol):
    """Read/write surface for batch records."""

    def upsert(self, ctx: ProjectContext, batch: BatchRun) -> None: ...

    def get(
        self, ctx: ProjectContext, batch_run_id: str,
    ) -> BatchRun | None: ...

    def list(
        self,
        ctx: ProjectContext,
        *,
        limit: int | None = None,
    ) -> list[BatchRun]: ...


class JsonlBatchRunStore:
    """Mirror of `JsonlIngestionRunStore` but for batch records."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    def _path(self, ctx: ProjectContext):
        return self._workspace.area(ctx, WorkspaceArea.AUDIT) / BATCH_RUNS_FILENAME

    def upsert(self, ctx: ProjectContext, batch: BatchRun) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(batch), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def get(
        self, ctx: ProjectContext, batch_run_id: str,
    ) -> BatchRun | None:
        latest: BatchRun | None = None
        for batch in self._iter_all(ctx):
            if batch.batch_run_id == batch_run_id:
                latest = batch
        return latest

    def list(
        self,
        ctx: ProjectContext,
        *,
        limit: int | None = None,
    ) -> list[BatchRun]:
        latest_by_id: dict[str, BatchRun] = {}
        for batch in self._iter_all(ctx):
            latest_by_id[batch.batch_run_id] = batch
        batches = list(latest_by_id.values())
        batches.sort(key=lambda b: b.started_at, reverse=True)
        if limit is not None:
            batches = batches[:limit]
        return batches

    def _iter_all(self, ctx: ProjectContext) -> Iterable[BatchRun]:
        path = self._path(ctx)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield _batch_from_payload(payload)


def _batch_from_payload(payload: dict) -> BatchRun:
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        started_at_dt = datetime.fromisoformat(started_at)
    elif isinstance(started_at, datetime):
        started_at_dt = started_at
    else:
        started_at_dt = datetime.fromtimestamp(0)
    return BatchRun(
        batch_run_id=str(payload["batch_run_id"]),
        tenant_id=str(payload["tenant_id"]),
        project_id=str(payload["project_id"]),
        run_ids=list(payload.get("run_ids") or []),
        file_count=int(payload.get("file_count") or 0),
        started_at=started_at_dt,
        actor=str(payload.get("actor") or "system"),
        metadata=dict(payload.get("metadata") or {}),
    )


def derive_batch_status(child_statuses: Iterable[str]) -> str:
    """Aggregate child IngestionRun statuses → batch-level status.

 Rules:
 * any child still active → `running`.
 * all `succeeded` (or `succeeded_with_warnings`) → `completed`.
 * all `succeeded` + ≥1 `succeeded_with_warnings` → `completed_with_warnings`.
 * mix of succeeded + failed → `partially_failed`.
 * all `failed` (or `cancelled`) → `failed`.
 * all `deleted` → `deleted`.
 """
    statuses = [str(s).lower() for s in child_statuses]
    if not statuses:
        return "running"
    active = {"created", "assessing", "plan_ready", "running", "paused",
              "cancelling", "waiting_for_confirmation"}
    if any(s in active for s in statuses):
        return "running"
    if all(s == "deleted" for s in statuses):
        return "deleted"
    succeeded = {"succeeded", "succeeded_with_warnings"}
    failed = {"failed", "cancelled"}
    if all(s in succeeded for s in statuses):
        if any(s == "succeeded_with_warnings" for s in statuses):
            return "completed_with_warnings"
        return "completed"
    if all(s in failed for s in statuses):
        return "failed"
    return "partially_failed"
