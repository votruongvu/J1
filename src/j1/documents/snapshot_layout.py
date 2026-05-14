"""Snapshot-scoped workspace layout — Phase 2.

The Phase 1 ``WorkspaceArea`` enum carves the project workspace by
*stage* (raw / compiled / enriched / graph / search / audit /
runtime / validation). It does NOT carry a snapshot dimension, so
two runs of the same document end up writing into the same stage
directories — the bug Phase 2 is fixing.

``SnapshotLayout`` adds the snapshot dimension below the project
root:

    {data_root}/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}/
      ├── source/
      ├── parsed/
      ├── compile/
      ├── indexes/
      ├── enrichment/
      ├── reports/
      └── debug/

run_id does NOT appear in the path. The run that created the
snapshot is recorded as metadata on the snapshot record
(``created_by_run_id``), never as a directory.

This module is additive — the existing ``WorkspaceResolver`` keeps
working unchanged. Phase 2 storage writes go through
``SnapshotLayout``; Phase 3 will migrate the legacy run-scoped
writes that still happen via ``WorkspaceResolver`` + the
RAGAnything bridge's legacy per-run workspace resolver (deleted in
Phase 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from j1.projects.context import ProjectContext


class SnapshotArea(StrEnum):
    """Stages within a single snapshot. Stable string values so the
    layout can be reasoned about from logs / tests / operator
    tooling without importing the enum."""

    SOURCE = "source"           # uploaded file copy (immutable)
    PARSED = "parsed"           # MinerU / LibreOffice intermediate parse
    COMPILE = "compile"         # RAGAnything compile output
    INDEXES = "indexes"         # vector / graph / evidence index handles
    ENRICHMENT = "enrichment"   # LLM enrichment artifacts
    REPORTS = "reports"         # human-readable summaries + verdicts
    DEBUG = "debug"             # diagnostic dumps, kept per cleanup policy


@dataclass(frozen=True)
class SnapshotLayout:
    """Resolves the on-disk paths for a single ``(document, snapshot)``
    pair.

    Construct with ``data_root`` once per worker, then call the
    per-stage helpers. The class is pure path math — it does not
    create directories until ``ensure(...)`` is called.
    """

    data_root: Path

    # ---- Path helpers --------------------------------------------

    def snapshot_root(
        self,
        ctx: ProjectContext,
        document_id: str,
        snapshot_id: str,
    ) -> Path:
        """The single root every stage hangs off."""
        return (
            self.data_root
            / "tenants" / ctx.tenant_id
            / "projects" / ctx.project_id
            / "documents" / document_id
            / "snapshots" / snapshot_id
        )

    def area(
        self,
        ctx: ProjectContext,
        document_id: str,
        snapshot_id: str,
        area: SnapshotArea,
    ) -> Path:
        return self.snapshot_root(ctx, document_id, snapshot_id) / area.value

    def source(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.SOURCE)

    def parsed(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.PARSED)

    def compile(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.COMPILE)

    def indexes(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.INDEXES)

    def enrichment(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(
            ctx, document_id, snapshot_id, SnapshotArea.ENRICHMENT,
        )

    def reports(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.REPORTS)

    def debug(self, ctx, document_id, snapshot_id) -> Path:
        return self.area(ctx, document_id, snapshot_id, SnapshotArea.DEBUG)

    # ---- Mutating helpers ----------------------------------------

    def ensure(
        self,
        ctx: ProjectContext,
        document_id: str,
        snapshot_id: str,
    ) -> Path:
        """Create every stage directory under the snapshot root and
        return the root itself. Idempotent."""
        root = self.snapshot_root(ctx, document_id, snapshot_id)
        for area in SnapshotArea:
            (root / area.value).mkdir(parents=True, exist_ok=True)
        return root


__all__ = ["SnapshotArea", "SnapshotLayout"]
