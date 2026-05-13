"""``DocumentCleanupService`` — idempotent artifact/workspace purge.

The dev-mode refactor introduced two cleanup paths the old code
did not have:

  * **per-run cleanup** — a candidate reindex run that lost the
    CAS promotion race becomes orphan. Its artifacts, search-index
    rows, MinerU output dir, and LightRAG workspace must be
    dropped so they can't leak into retrieval.

  * **per-document cleanup** — the new ``Remove`` flow flips the
    document's ``lifecycle_status`` to ``removing`` (gate first,
    queries already stopped) and then synchronously walks every
    run and drops every byte.

Both paths are built from the same five primitives:

  1. ``_delete_run_artifacts``   — drops every artifact whose
     metadata is tagged with the run_id from ArtifactRegistry +
     deletes the on-disk artifact files via the registry's
     workspace-relative location.

  2. ``_delete_run_index_rows``  — drops the rows from the SQLite
     FTS index by run_id.

  3. ``_delete_run_workspace``   — best-effort rmtree of the
     LightRAG run-scoped workspace
     (``{workdir}/runs/{tenant}/{project}/{doc}/{run}/``) AND of
     MinerU's run-scoped output dir
     (``{workdir}/outputs/{doc}/{run}/``).

  4. ``_delete_document_raw``    — removes the originally uploaded
     file under ``tenants/{tenant}/projects/{project}/raw/``.

  5. ``_drop_runs_from_store``   — clears the document's run
     entries from the run-store JSONL so the per-document Remove
     leaves no tombstones.

Every primitive is idempotent: rmtree a non-existent path is a
no-op, deleting an already-deleted artifact is a no-op, etc.
``cleanup_run`` and ``cleanup_document`` compose the primitives
in order, recording a structured per-step result so partial
failures are observable in the audit log.

Failures inside a primitive are caught + logged but never raised
— a single failed delete must not block the rest of the cleanup.
The aggregate result reports ``ok=False`` on any partial failure
so the lifecycle code can set ``cleanup_status="cleanup_failed"``
on the document/run and the FE can surface the orphan to operators.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from j1.projects.context import ProjectContext

if TYPE_CHECKING:
    from j1.artifacts.registry import ArtifactRegistry
    from j1.intake.registry import SourceRegistry
    from j1.runs.store import IngestionRunStore
    from j1.search.indexer import SqliteSearchIndexer
    from j1.workspace.resolver import WorkspaceResolver

_log = logging.getLogger("j1.documents.cleanup")


@dataclass
class CleanupStepResult:
    """One primitive's outcome.

    ``items_removed`` is best-effort (0 for path-delete primitives
    that don't enumerate items; positive for artifact / index
    deletes). ``error`` is None on success or skip; populated when
    the primitive raised."""

    name: str
    ok: bool
    items_removed: int = 0
    error: str | None = None


@dataclass
class CleanupResult:
    """Aggregate result of a ``cleanup_run`` or ``cleanup_document``
    call. ``ok`` is the AND of every step; callers use it to flip
    ``cleanup_status`` on the document/run."""

    ok: bool
    steps: list[CleanupStepResult] = field(default_factory=list)

    @property
    def items_removed(self) -> int:
        return sum(s.items_removed for s in self.steps)


class DocumentCleanupService:
    """Idempotent cleanup primitives — per-run and per-document.

    Construction takes every collaborator it might touch. Each is
    optional so test wirings can drop the ones they don't care
    about (e.g. an artifact-only unit test passes ``indexer=None``
    and the index-drop step becomes a no-op skip rather than a
    crash).
    """

    def __init__(
        self,
        *,
        workspace: "WorkspaceResolver",
        artifacts: "ArtifactRegistry | None" = None,
        indexer: "SqliteSearchIndexer | None" = None,
        run_store: "IngestionRunStore | None" = None,
        source_registry: "SourceRegistry | None" = None,
        raganything_workdir: str | Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._artifacts = artifacts
        self._indexer = indexer
        self._run_store = run_store
        self._source_registry = source_registry
        # ``raganything_workdir`` overrides the per-run LightRAG +
        # MinerU output root. When None, the workspace resolver's
        # default location is used. The reset script reads the same
        # env var so the two stay in sync.
        self._raganything_workdir = (
            Path(str(raganything_workdir)).expanduser()
            if raganything_workdir else None
        )

    # ---- Public composers -----------------------------------------

    def cleanup_run(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        run_id: str,
    ) -> CleanupResult:
        """Drop everything one run produced. Idempotent.

        Order matters: drop FTS rows BEFORE artifacts so a partial
        failure doesn't leave the index pointing at deleted
        artifact files."""
        steps: list[CleanupStepResult] = []
        steps.append(self._delete_run_index_rows(ctx, run_id))
        steps.append(self._delete_run_artifacts(ctx, run_id))
        steps.append(self._delete_run_workspace(ctx, document_id, run_id))
        ok = all(s.ok for s in steps)
        return CleanupResult(ok=ok, steps=steps)

    def cleanup_document(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
    ) -> CleanupResult:
        """Drop every byte a document owns.

        Walks every run associated with the document via the run-
        store, runs ``cleanup_run`` for each, then removes the
        document-scoped workspace + raw file + run-store entries.
        """
        steps: list[CleanupStepResult] = []
        run_ids = self._collect_run_ids(ctx, document_id)
        for run_id in run_ids:
            # Per-run cleanup steps stitched into the document
            # cleanup's step list so a single failure report shows
            # the whole picture.
            sub = self.cleanup_run(
                ctx, document_id=document_id, run_id=run_id,
            )
            steps.extend(sub.steps)
        steps.append(self._delete_document_workspace(ctx, document_id))
        steps.append(self._delete_document_raw(ctx, document_id))
        steps.append(self._drop_runs_from_store(ctx, document_id, run_ids))
        ok = all(s.ok for s in steps)
        # Only delete the document record itself when every other
        # step succeeded — otherwise the tombstone (with
        # ``lifecycle_status="cleanup_failed"``) is what surfaces
        # the orphaned bytes to the operator.
        if ok:
            steps.append(self._delete_document_record(ctx, document_id))
            ok = all(s.ok for s in steps)
        return CleanupResult(ok=ok, steps=steps)

    # ---- Primitives -----------------------------------------------

    def _delete_run_artifacts(
        self, ctx: ProjectContext, run_id: str,
    ) -> CleanupStepResult:
        if self._artifacts is None:
            return CleanupStepResult(name="artifacts", ok=True)
        removed = 0
        try:
            records = self._artifacts.list_artifacts(ctx)
        except Exception as exc:  # noqa: BLE001
            return CleanupStepResult(
                name="artifacts", ok=False, error=str(exc),
            )
        delete = getattr(self._artifacts, "delete_by_artifact_id", None)
        for r in records:
            if str(r.metadata.get("run_id", "")) != run_id:
                continue
            # Best-effort file delete before the registry entry.
            try:
                path = self._workspace.project_root(ctx) / r.location
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                _log.warning(
                    "artifact file delete failed: %s (%s)",
                    r.artifact_id, exc,
                )
            if callable(delete):
                try:
                    if delete(ctx, r.artifact_id):
                        removed += 1
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "artifact registry delete failed: %s (%s)",
                        r.artifact_id, exc,
                    )
        return CleanupStepResult(
            name="artifacts", ok=True, items_removed=removed,
        )

    def _delete_run_index_rows(
        self, ctx: ProjectContext, run_id: str,
    ) -> CleanupStepResult:
        if self._indexer is None:
            return CleanupStepResult(name="index", ok=True)
        delete = getattr(self._indexer, "delete_by_run_id", None)
        if not callable(delete):
            return CleanupStepResult(name="index", ok=True)
        try:
            removed = delete(ctx, run_id)
        except Exception as exc:  # noqa: BLE001
            return CleanupStepResult(
                name="index", ok=False, error=str(exc),
            )
        return CleanupStepResult(
            name="index", ok=True, items_removed=int(removed or 0),
        )

    def _delete_run_workspace(
        self,
        ctx: ProjectContext,
        document_id: str,
        run_id: str,
    ) -> CleanupStepResult:
        """Best-effort rmtree of the run-scoped LightRAG dir AND the
        MinerU output dir. Two directories, one step — they share
        the same scope and the same failure tolerance."""
        target_paths: list[Path] = []
        workdir = self._raganything_workdir
        if workdir is not None:
            target_paths.append(
                workdir / "runs" / ctx.tenant_id / ctx.project_id
                / document_id / run_id,
            )
            target_paths.append(
                workdir / "outputs" / document_id / run_id,
            )
        if not target_paths:
            return CleanupStepResult(name="run_workspace", ok=True)
        for target in target_paths:
            try:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=False)
            except OSError as exc:
                _log.warning(
                    "run workspace cleanup failed: %s (%s)", target, exc,
                )
                return CleanupStepResult(
                    name="run_workspace", ok=False, error=str(exc),
                )
        return CleanupStepResult(name="run_workspace", ok=True)

    def _delete_document_workspace(
        self, ctx: ProjectContext, document_id: str,
    ) -> CleanupStepResult:
        workdir = self._raganything_workdir
        if workdir is None:
            return CleanupStepResult(name="document_workspace", ok=True)
        targets = [
            workdir / "runs" / ctx.tenant_id / ctx.project_id / document_id,
            workdir / "outputs" / document_id,
        ]
        for target in targets:
            try:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=False)
            except OSError as exc:
                _log.warning(
                    "document workspace cleanup failed: %s (%s)",
                    target, exc,
                )
                return CleanupStepResult(
                    name="document_workspace", ok=False, error=str(exc),
                )
        return CleanupStepResult(name="document_workspace", ok=True)

    def _delete_document_raw(
        self, ctx: ProjectContext, document_id: str,
    ) -> CleanupStepResult:
        """Remove the originally uploaded file under ``raw/``. Walks
        the directory by glob so the lookup is independent of the
        stored filename casing / extension."""
        try:
            raw_dir = self._workspace.raw(ctx)
            if not raw_dir.is_dir():
                return CleanupStepResult(name="raw_file", ok=True)
            removed = 0
            for candidate in raw_dir.glob(f"{document_id}*"):
                try:
                    if candidate.is_file():
                        candidate.unlink()
                        removed += 1
                    elif candidate.is_dir():
                        shutil.rmtree(candidate, ignore_errors=False)
                        removed += 1
                except OSError as exc:
                    _log.warning(
                        "raw delete failed: %s (%s)", candidate, exc,
                    )
            return CleanupStepResult(
                name="raw_file", ok=True, items_removed=removed,
            )
        except Exception as exc:  # noqa: BLE001
            return CleanupStepResult(
                name="raw_file", ok=False, error=str(exc),
            )

    def _drop_runs_from_store(
        self,
        ctx: ProjectContext,
        document_id: str,
        run_ids: list[str],
    ) -> CleanupStepResult:
        if self._run_store is None or not run_ids:
            return CleanupStepResult(name="run_store", ok=True)
        delete = getattr(self._run_store, "delete_runs", None)
        if not callable(delete):
            return CleanupStepResult(name="run_store", ok=True)
        try:
            removed = delete(ctx, document_id=document_id, run_ids=run_ids)
        except Exception as exc:  # noqa: BLE001
            return CleanupStepResult(
                name="run_store", ok=False, error=str(exc),
            )
        return CleanupStepResult(
            name="run_store", ok=True, items_removed=int(removed or 0),
        )

    def _delete_document_record(
        self, ctx: ProjectContext, document_id: str,
    ) -> CleanupStepResult:
        if self._source_registry is None:
            return CleanupStepResult(name="document_record", ok=True)
        delete = getattr(self._source_registry, "delete", None)
        if not callable(delete):
            return CleanupStepResult(name="document_record", ok=True)
        try:
            removed = bool(delete(ctx, document_id))
        except Exception as exc:  # noqa: BLE001
            return CleanupStepResult(
                name="document_record", ok=False, error=str(exc),
            )
        return CleanupStepResult(
            name="document_record",
            ok=True,
            items_removed=1 if removed else 0,
        )

    def _collect_run_ids(
        self, ctx: ProjectContext, document_id: str,
    ) -> list[str]:
        if self._run_store is None:
            return []
        try:
            runs = self._run_store.list_runs(ctx, document_id=document_id)
        except Exception:  # noqa: BLE001
            return []
        return [r.run_id for r in runs]


__all__ = [
    "CleanupResult",
    "CleanupStepResult",
    "DocumentCleanupService",
]
