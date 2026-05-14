"""High-level snapshot lifecycle coordinator — Phase 2.

This is the entrypoint the ingestion code should use when it wants
to "ingest one document into a new snapshot". The coordinator owns
the sequencing rules:

    1. Create a candidate snapshot (state=BUILDING).
    2. Allocate the on-disk snapshot workspace.
    3. Hand the compile adapter the snapshot workspace.
    4. Hand the evidence adapter the artifact IDs.
    5. Mark the snapshot READY when every stage succeeded.
    6. Promote on-success (CAS against the document's current
       active_snapshot_id).
    7. On any failure: mark FAILED, leave previous active untouched.

The coordinator does NOT touch the Temporal workflow today — Phase
2 ships the coordinator alongside the existing
``DocumentProcessingWorkflow`` so tests can exercise the snapshot
path without rewiring the workflow. Phase 3 will migrate the
workflow to call this coordinator directly.

A run is execution metadata: ``created_by_run_id`` is the only
mention of run_id at this layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from j1.documents.index_refs import IndexRefStore
from j1.documents.snapshot import DocumentSnapshot, IndexRef, SnapshotState
from j1.documents.snapshot_layout import SnapshotLayout
from j1.documents.snapshot_service import (
    DocumentSnapshotService,
    SnapshotConflictError,
)
from j1.processing.compile_adapter import (
    CompileEngineAdapter,
    CompileRequest,
    CompileResult,
)
from j1.projects.context import ProjectContext
from j1.search.evidence_adapter import (
    EvidenceIndexAdapter,
    EvidenceIndexRequest,
    EvidenceIndexResult,
)

_log = logging.getLogger("j1.documents.snapshot_lifecycle")


@dataclass
class IngestionOutcome:
    snapshot: DocumentSnapshot
    promoted: bool
    previous_active_snapshot_id: str | None
    compile_result: CompileResult | None
    evidence_result: EvidenceIndexResult | None
    errors: list[str]


@dataclass
class SnapshotIngestionCoordinator:
    """Glue between the snapshot service + workspace + adapters."""

    snapshot_service: DocumentSnapshotService
    layout: SnapshotLayout
    index_refs: IndexRefStore
    compile_adapter: CompileEngineAdapter
    evidence_adapter: EvidenceIndexAdapter

    def ingest(
        self,
        ctx: ProjectContext,
        *,
        document_id: str,
        run_id: str,
        source_path: Path,
        previous_active_snapshot_id: str | None,
        profile_id: str | None = None,
        compile_config: dict[str, Any] | None = None,
    ) -> IngestionOutcome:
        errors: list[str] = []

        # 1. Create candidate snapshot.
        snap = self.snapshot_service.create_candidate(
            ctx,
            document_id=document_id,
            created_by_run_id=run_id,
        )

        # 2. Allocate workspace.
        snap_root = self.layout.ensure(ctx, document_id, snap.snapshot_id)

        # 3. Compile.
        compile_req = CompileRequest(
            ctx=ctx,
            document_id=document_id,
            snapshot_id=snap.snapshot_id,
            created_by_run_id=run_id,
            profile_id=profile_id,
            source_path=source_path,
            snapshot_workspace=snap_root,
            compile_config=dict(compile_config or {}),
        )
        compile_result = self.compile_adapter.compile(compile_req)
        if not compile_result.success:
            errors.append(
                f"compile failed: {compile_result.error or 'unknown error'}"
            )
            self.snapshot_service.mark_failed(
                ctx,
                snapshot_id=snap.snapshot_id,
                reason=compile_result.error or "compile failed",
            )
            return IngestionOutcome(
                snapshot=self.snapshot_service.store.get(
                    ctx, snap.snapshot_id,
                ) or snap,
                promoted=False,
                previous_active_snapshot_id=previous_active_snapshot_id,
                compile_result=compile_result,
                evidence_result=None,
                errors=errors,
            )

        # 4. Evidence index. The compile adapter returns adapter-
        # native drafts; the lifecycle only needs the IDs.
        artifact_ids = tuple(
            getattr(a, "artifact_id", None) or getattr(a, "id", "")
            for a in compile_result.artifacts
        )
        artifact_ids = tuple(a for a in artifact_ids if a)
        evidence_req = EvidenceIndexRequest(
            ctx=ctx,
            document_id=document_id,
            snapshot_id=snap.snapshot_id,
            created_by_run_id=run_id,
            artifact_ids=artifact_ids,
        )
        evidence_result = self.evidence_adapter.index(evidence_req)
        if not evidence_result.success:
            errors.append(
                f"evidence index failed: "
                f"{evidence_result.error or 'unknown error'}"
            )
            self.snapshot_service.mark_failed(
                ctx,
                snapshot_id=snap.snapshot_id,
                reason=evidence_result.error or "evidence index failed",
            )
            return IngestionOutcome(
                snapshot=self.snapshot_service.store.get(
                    ctx, snap.snapshot_id,
                ) or snap,
                promoted=False,
                previous_active_snapshot_id=previous_active_snapshot_id,
                compile_result=compile_result,
                evidence_result=evidence_result,
                errors=errors,
            )

        # Persist the index ref.
        self.index_refs.register(ctx, evidence_result.index_ref)
        self.snapshot_service.attach_index_ref(
            ctx,
            snapshot_id=snap.snapshot_id,
            ref=evidence_result.index_ref,
        )

        # 5. Mark READY.
        self.snapshot_service.mark_ready(
            ctx,
            snapshot_id=snap.snapshot_id,
            summary={
                "indexed_artifacts": evidence_result.indexed_count,
                "compile_metadata": dict(compile_result.metadata or {}),
            },
        )

        # 6. Promote-on-success.
        promoted = False
        try:
            new_active, _ = self.snapshot_service.promote(
                ctx,
                document_id=document_id,
                snapshot_id=snap.snapshot_id,
                previous_active_snapshot_id=previous_active_snapshot_id,
            )
            promoted = True
            snap = new_active
        except SnapshotConflictError as exc:
            errors.append(
                f"promote conflict: {exc}; snapshot is READY but did "
                "not become active. Operator can promote manually."
            )
        return IngestionOutcome(
            snapshot=snap,
            promoted=promoted,
            previous_active_snapshot_id=previous_active_snapshot_id,
            compile_result=compile_result,
            evidence_result=evidence_result,
            errors=errors,
        )


__all__ = [
    "IngestionOutcome",
    "SnapshotIngestionCoordinator",
]
