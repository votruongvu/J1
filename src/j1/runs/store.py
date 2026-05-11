"""IngestionRun store: append-only JSONL, latest-snapshot reads.

Mirrors the `JsonlAuditSink` pattern — every update appends a fresh
snapshot of the run record; readers reconstruct the latest state by
scanning the file. This keeps writes lock-free (open-append-close),
keeps the storage shape consistent with the rest of the J1 workspace
(per-tenant per-project JSONL), and lets the file double as an audit
trail of run state transitions.

For deployments that outgrow JSONL, swap the `IngestionRunStore`
Protocol implementation; the rest of the framework reads through the
interface.
"""

from __future__ import annotations

import json
from typing import Iterable, Protocol

from j1._serialization import to_jsonable
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.workspace.layout import WorkspaceArea
from j1.workspace.resolver import WorkspaceResolver

INGESTION_RUNS_FILENAME = "ingestion_runs.jsonl"

__all__ = ["INGESTION_RUNS_FILENAME", "IngestionRunStore", "JsonlIngestionRunStore"]


class IngestionRunStore(Protocol):
    """Read/write surface for ingestion runs."""

    def upsert(self, ctx: ProjectContext, run: IngestionRun) -> None: ...

    def get(self, ctx: ProjectContext, run_id: str) -> IngestionRun | None: ...

    def list(
        self,
        ctx: ProjectContext,
        *,
        statuses: Iterable[RunStatus] | None = None,
        limit: int | None = None,
    ) -> list[IngestionRun]: ...

    def purge(self, ctx: ProjectContext, run_id: str) -> bool:
        """Physically remove every snapshot of `run_id` from storage.
 Used by hard-delete (purge). Returns True iff at least one
 snapshot was removed; False if `run_id` wasn't present
 (idempotent — purge is allowed to run twice).

 Distinct from `upsert(run, status=DELETED)` (soft-delete),
 which appends a tombstone snapshot the reader skips. Purge
 rewrites the JSONL minus every line for `run_id` so the
 bytes physically leave the audit area."""
        ...


class JsonlIngestionRunStore:
    """JSONL append-only store. Latest snapshot wins on read.

 Writes are O(1) (append). Reads scan the file and keep the
 last-written entry per `run_id`. For workspaces with thousands of
 runs this is still cheap (sequential read of a few MB); switch
 to a SQLite-backed implementation if write volume justifies it.

 Located under the workspace's `audit` area to share retention /
 backup semantics with the audit log — if you snapshot the audit
 directory you also snapshot the run records."""

    def __init__(self, workspace: WorkspaceResolver) -> None:
        self._workspace = workspace

    # ---- Path resolution ---------------------------------------------

    def _path(self, ctx: ProjectContext):
        # Sit alongside the audit log so a single backup covers both.
        return self._workspace.area(ctx, WorkspaceArea.AUDIT) / INGESTION_RUNS_FILENAME

    # ---- Writes ------------------------------------------------------

    def upsert(self, ctx: ProjectContext, run: IngestionRun) -> None:
        path = self._path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(to_jsonable(run), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    # ---- Reads -------------------------------------------------------

    def get(self, ctx: ProjectContext, run_id: str) -> IngestionRun | None:
        latest: IngestionRun | None = None
        for run in self._iter_all(ctx):
            if run.run_id == run_id:
                latest = run
        return latest

    def list(
        self,
        ctx: ProjectContext,
        *,
        statuses: Iterable[RunStatus] | None = None,
        limit: int | None = None,
    ) -> list[IngestionRun]:
        # Build latest-per-run-id, then filter.
        latest_by_id: dict[str, IngestionRun] = {}
        for run in self._iter_all(ctx):
            latest_by_id[run.run_id] = run
        runs = list(latest_by_id.values())
        if statuses is not None:
            allowed = {s for s in statuses}
            runs = [r for r in runs if r.status in allowed]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        if limit is not None:
            runs = runs[:limit]
        return runs

    def purge(self, ctx: ProjectContext, run_id: str) -> bool:
        """Rewrite the JSONL file minus every line for `run_id`.

 Atomic via tmp-file + rename so a crash mid-purge can't
 leave a half-written file. Skips work entirely when the
 path doesn't exist or no matching lines are found —
 callers can invoke this idempotently."""
        path = self._path(ctx)
        if not path.exists():
            return False
        kept_lines: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    # Preserve unparseable lines — rewriting them
                    # would silently lose data we don't understand.
                    kept_lines.append(stripped)
                    continue
                if str(payload.get("run_id")) == run_id:
                    removed += 1
                    continue
                kept_lines.append(stripped)
        if removed == 0:
            return False
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for line in kept_lines:
                fh.write(line)
                fh.write("\n")
        tmp.replace(path)
        return True

    # ---- Internals ---------------------------------------------------

    def _iter_all(self, ctx: ProjectContext) -> Iterable[IngestionRun]:
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
                    # Skip malformed lines rather than fail the read —
                    # the JSONL contract is "best-effort, last-write
                    # wins". A truncated tail line shouldn't make all
                    # runs unreadable.
                    continue
                yield _run_from_payload(payload)


def _run_from_payload(payload: dict) -> IngestionRun:
    """Hydrate an `IngestionRun` from a JSONL payload.

 Tolerates field additions across versions: unknown fields go
 ignored, missing fields fall back to dataclass defaults."""
    from datetime import datetime

    def _parse_dt(value: object) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return None

    raw_status = payload.get("status", "created")
    try:
        status = RunStatus(raw_status)
    except ValueError:
        status = RunStatus.CREATED
    return IngestionRun(
        run_id=str(payload["run_id"]),
        document_id=str(payload["document_id"]),
        workflow_id=str(payload.get("workflow_id", "")),
        workflow_run_id=payload.get("workflow_run_id"),
        status=status,
        started_at=_parse_dt(payload.get("started_at")) or datetime.fromtimestamp(0),
        updated_at=_parse_dt(payload.get("updated_at")) or datetime.fromtimestamp(0),
        workspace_id=payload.get("workspace_id"),
        current_stage=payload.get("current_stage"),
        current_step=payload.get("current_step"),
        progress_percent=int(payload.get("progress_percent") or 0),
        completed_at=_parse_dt(payload.get("completed_at")),
        failure_code=payload.get("failure_code"),
        failure_message=payload.get("failure_message"),
        warning_count=int(payload.get("warning_count") or 0),
        metadata=dict(payload.get("metadata") or {}),
    )
