import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from j1.errors.exceptions import WorkspaceLockedError
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

DEFAULT_LEASE_SECONDS = 600  # 10 minutes
LOCKS_SUBDIR = "locks"

# Standard lock area names. Callers may pass arbitrary strings — these are
# the conventional names used by the framework.
AREA_PROJECT = "project"
AREA_COMPILED = "compiled"
AREA_ENRICHED = "enriched"
AREA_GRAPH = "graph"
AREA_SEARCH = "search"


@dataclass(frozen=True)
class LockHandle:
    lock_id: str
    owner: str
    area: str
    acquired_at: datetime
    expires_at: datetime


class WorkspaceLock:
    """File-based per-project, per-area lock with lease.

 Atomic acquire via `O_EXCL`. If a stale lock (past expiry) is encountered,
 the new caller takes it over. The release verifies `lock_id` so a stale
 holder can't accidentally delete a fresh holder's lock.

 Locks are scoped to `(tenant_id, project_id, area)`. Different tenants,
 different projects, and different areas of the same project never collide
 — exactly matching the locking rules in the spec.
 """

    def __init__(
        self,
        workspace: WorkspaceResolver,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def acquire(
        self,
        ctx: ProjectContext,
        owner: str,
        *,
        area: str = AREA_PROJECT,
    ) -> LockHandle:
        path = self._lock_path(ctx, area)
        path.parent.mkdir(parents=True, exist_ok=True)

        now = self._clock()
        expires_at = now + timedelta(seconds=self._lease_seconds)
        lock_id = self._id_factory()
        payload = {
            "lock_id": lock_id,
            "owner": owner,
            "area": area,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "acquired_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "pid": os.getpid(),
        }

        try:
            fd = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            return _handle_from(payload)
        except FileExistsError:
            pass  # check expiry below

        existing = self._read_lock(path)
        if existing is None:
            # Unreadable; treat as held by an unknown owner to be safe.
            raise WorkspaceLockedError(
                f"workspace area {area!r} lock file unreadable",
                area=area,
            )
        existing_expires = _parse_dt(existing.get("expires_at"))
        if existing_expires is None or existing_expires > now:
            raise WorkspaceLockedError(
                f"workspace area {area!r} held by {existing.get('owner')!r} "
                f"until {existing_expires}",
                owner=existing.get("owner"),
                area=area,
            )

        # Stale lock — claim it via atomic replace.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)
        return _handle_from(payload)

    def release(self, ctx: ProjectContext, handle: LockHandle) -> None:
        path = self._lock_path(ctx, handle.area)
        if not path.exists():
            return
        existing = self._read_lock(path)
        if existing is None or existing.get("lock_id") != handle.lock_id:
            # Not our lock; do nothing rather than steal it.
            return
        path.unlink(missing_ok=True)

    def is_held(
        self, ctx: ProjectContext, *, area: str = AREA_PROJECT
    ) -> bool:
        path = self._lock_path(ctx, area)
        if not path.exists():
            return False
        existing = self._read_lock(path)
        if existing is None:
            return False
        expires = _parse_dt(existing.get("expires_at"))
        return expires is not None and expires > self._clock()

    def force_release(
        self, ctx: ProjectContext, *, area: str = AREA_PROJECT
    ) -> None:
        """Unconditionally remove the lock for an area.

 Use only for operator recovery — normal release goes through `release`.
 """
        path = self._lock_path(ctx, area)
        path.unlink(missing_ok=True)

    @contextmanager
    def hold(
        self,
        ctx: ProjectContext,
        owner: str,
        *,
        area: str = AREA_PROJECT,
    ) -> Iterator[LockHandle]:
        handle = self.acquire(ctx, owner, area=area)
        try:
            yield handle
        finally:
            self.release(ctx, handle)

    def _lock_path(self, ctx: ProjectContext, area: str) -> Path:
        return self._workspace.runtime(ctx) / LOCKS_SUBDIR / f"{area}.lock"

    @staticmethod
    def _read_lock(path: Path) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None


def _handle_from(payload: dict) -> LockHandle:
    return LockHandle(
        lock_id=payload["lock_id"],
        owner=payload["owner"],
        area=payload["area"],
        acquired_at=datetime.fromisoformat(payload["acquired_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
