"""Phase 5 — resume-from-checkpoint hardening at the service layer.

The REST adapter already gates resume on the document's
knowledge_state (Phase 3); these tests lock the same rule in at
the service layer so future async/CLI/scripted callers can't
bypass it. The compile-checkpoint gate is the spec's central rule:
"Resume should only exist after a successful compile checkpoint."

Test matrix:
 * Resume is rejected with a clear error when compile didn't reach
   the checkpoint successfully (load-bearing — the previous code
   silently re-ran compile, which violated the spec).
 * Resume is rejected when the document has been detached.
 * Resume is rejected when the document has been removed.
 * Resume still works when document is attached + compile completed
   (regression guard — the new checks shouldn't break the happy
   path).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.ingestion_review import IngestionResultReviewService
from j1.ingestion_review.exceptions import ResumeNotPossible
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.runs.models import IngestionRun, RunStatus
from j1.runs.resume import compute_settings_hash
from j1.runs.store import JsonlIngestionRunStore


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
_PRIOR_SETTINGS: dict = {
    "compiler_kind": "mock",
    "enricher_kind": None,
    "graph_builder_kind": None,
    "indexer_kind": None,
    "planner_enabled": False,
    "policy": "auto",
    "domain_override": None,
    "workspace_default_domain": None,
    "failure_policy": "fail_fast",
}


@pytest.fixture
def run_store(workspace) -> JsonlIngestionRunStore:
    return JsonlIngestionRunStore(workspace)


@pytest.fixture
def service(run_store, artifact_registry, workspace, registry) -> IngestionResultReviewService:
    """Service wired with `source_registry` — that's the Phase 5
 dependency that makes the knowledge-state gate enforceable at
 the service layer (instead of only at the REST adapter)."""
    return IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
        source_registry=registry,
    )


def _seed_doc(
    registry: JsonSourceRegistry, ctx: ProjectContext,
    *, document_id: str = "doc-1",
    state: str = "attached",
) -> None:
    registry.add(DocumentRecord(
        document_id=document_id,
        project=ctx,
        original_filename="x.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum=f"sha256:{document_id}",
        status=ProcessingStatus.SUCCEEDED,
        created_at=_NOW,
        knowledge_state=state,  # type: ignore[arg-type]
        active_run_id="r-1",
    ))


def _seed_run(
    run_store: JsonlIngestionRunStore, ctx: ProjectContext,
    *, run_id: str = "r-1", document_id: str = "doc-1",
    completed_steps: list[str] | None = None,
) -> None:
    """Seed a FAILED run with a resume snapshot. `completed_steps`
 defaults to ['compile'] which is what the happy-path resume
 needs to see."""
    if completed_steps is None:
        completed_steps = ["compile"]
    metadata: dict = {
        "resume_snapshot": {
            "settings_snapshot": _PRIOR_SETTINGS,
            "settings_hash": compute_settings_hash(_PRIOR_SETTINGS),
            "completed_steps": completed_steps,
            "produced_artifact_ids": [],
            "produced_artifact_kinds": [],
        },
    }
    run_store.upsert(ctx, IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=RunStatus.FAILED,
        started_at=_NOW,
        updated_at=_NOW,
        metadata=metadata,
    ))


# ---- Compile-checkpoint gate ------------------------------------


def test_resume_rejected_when_compile_did_not_complete(
    service, run_store, registry, ctx,
):
    """Spec rule: resume only after a successful compile checkpoint.
 A run that failed BEFORE compile finished has nothing usable to
 resume from — the service must surface this with a clear error
 message so the FE can render an actionable "use full-reindex"
 affordance."""
    _seed_doc(registry, ctx)
    _seed_run(
        run_store, ctx,
        completed_steps=["assessment"],  # NOT 'compile'
    )
    with pytest.raises(ResumeNotPossible, match="compile checkpoint"):
        service.resume_from_checkpoint(
            ctx, "r-1", candidate_settings=_PRIOR_SETTINGS,
        )


def test_resume_allowed_when_compile_completed(
    service, run_store, registry, ctx,
):
    """Regression guard: the new compile-checkpoint check must NOT
 break the happy path. A FAILED run whose snapshot lists 'compile'
 as completed is resumable."""
    _seed_doc(registry, ctx)
    _seed_run(run_store, ctx, completed_steps=["compile", "enrich"])
    plan = service.resume_from_checkpoint(
        ctx, "r-1", candidate_settings=_PRIOR_SETTINGS,
    )
    # Compile is always re-run (not in RESUMABLE_STAGES); enrich
    # comes through as policy-resumable.
    assert "enrich" in plan["resumable_steps"]
    assert "compile" not in plan["resumable_steps"]


# ---- Document knowledge-state gate ------------------------------


def test_resume_rejected_on_detached_document(
    service, run_store, registry, ctx,
):
    """Detached documents must reject resume at the service layer.
 Spec: 'Hide or disable Resume. User should attach the document
 first.' This locks the rule in for non-REST callers too."""
    _seed_doc(registry, ctx, state="detached")
    _seed_run(run_store, ctx, completed_steps=["compile"])
    with pytest.raises(ResumeNotPossible, match="detached"):
        service.resume_from_checkpoint(
            ctx, "r-1", candidate_settings=_PRIOR_SETTINGS,
        )


def test_resume_rejected_on_removed_document(
    service, run_store, registry, ctx,
):
    """Removed documents must reject resume permanently. Spec:
 'Resume must be disabled permanently.' The error message
 points the operator to re-upload (the only way back from
 removed)."""
    _seed_doc(registry, ctx, state="removed")
    _seed_run(run_store, ctx, completed_steps=["compile"])
    with pytest.raises(ResumeNotPossible, match="removed|re-upload"):
        service.resume_from_checkpoint(
            ctx, "r-1", candidate_settings=_PRIOR_SETTINGS,
        )


# ---- Service-without-source-registry compat ----------------------


def test_resume_works_when_no_source_registry_wired(
    run_store, artifact_registry, workspace, ctx,
):
    """Backward-compat: deployments that haven't passed a
 source_registry to the service must still get clean resume
 behaviour (knowledge-state gate becomes a no-op). The REST
 layer's Phase 3 guard still applies, so the safety net is
 preserved at a higher layer."""
    # Service WITHOUT source_registry → falls back to legacy path.
    legacy_service = IngestionResultReviewService(
        run_store=run_store,
        artifact_registry=artifact_registry,
        workspace=workspace,
    )
    _seed_run(run_store, ctx, completed_steps=["compile"])
    # Should not raise — no source_registry means no knowledge-state
    # gate at the service layer.
    plan = legacy_service.resume_from_checkpoint(
        ctx, "r-1", candidate_settings=_PRIOR_SETTINGS,
    )
    assert "run_id" in plan
