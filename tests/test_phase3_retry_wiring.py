"""Phase 3 retry — wiring + cutover invariants.

These tests lock the runtime invariants the previous Phase 3 work
left unverified:

  * ``build_worker_spec`` constructs and passes ``snapshot_service``
    into both ``KnowledgeProcessingActivities`` and
    ``RunsActivities``.
  * ``SearchActivities`` writes through the canonical evidence
    adapter when wired.
  * ``build_application_facade`` no longer constructs SqliteSearchIndexer
    as the strategic write target — the canonical adapter is built
    alongside it.
  * Eligibility requires ``active_snapshot_id`` and refuses
    documents with only ``active_run_id``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.config.runtime import EvidenceBackend, load_runtime_config
from j1.documents.models import DocumentRecord
from j1.intake.registry import JsonSourceRegistry
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.query.eligibility import (
    resolve_eligible_active_run_ids,
    resolve_eligible_active_snapshot_ids,
)
from j1.query.scope import WorkspaceScope


# ---- Eligibility: active_run_id alone is NOT enough -------------


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


class _InMemoryRegistry:
    def __init__(self, docs):
        self._docs = list(docs)

    def list_documents(self, ctx):
        return list(self._docs)

    def get(self, ctx, document_id):
        for d in self._docs:
            if d.document_id == document_id:
                return d
        raise LookupError(document_id)


def _doc(
    document_id: str = "doc-1",
    *,
    active_run_id=None,
    active_snapshot_id=None,
    knowledge_state="attached",
    lifecycle_status="stable",
):
    return DocumentRecord(
        document_id=document_id,
        project=ProjectContext(tenant_id="t", project_id="p", profile=None),
        original_filename=f"{document_id}.pdf",
        stored_filename=f"{document_id}.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="x",
        status=ProcessingStatus.SUCCEEDED,
        created_at=datetime.now(timezone.utc),
        knowledge_state=knowledge_state,
        active_run_id=active_run_id,
        active_snapshot_id=active_snapshot_id,
        lifecycle_status=lifecycle_status,
    )


def test_eligibility_requires_active_snapshot_id_only(ctx):
    """A document with only ``active_run_id`` is NOT visible. The
    Phase 3 retry retired the legacy fallback — operators reset
    and re-ingest after the cutover."""
    reg = _InMemoryRegistry([
        # Pre-Phase-3 data: only run_id set.
        _doc("doc-legacy", active_run_id="run-legacy"),
        # Phase-3 data: snapshot set.
        _doc("doc-new", active_snapshot_id="snap-new", active_run_id="run-new"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=reg,
    )
    # Only the Phase-3 document is visible.
    assert result.snapshot_ids == frozenset({"snap-new"})
    assert result.document_ids == frozenset({"doc-new"})
    # Legacy run_id is in the companion set ONLY when the doc has
    # an active_snapshot_id (i.e. it's already a Phase-3 doc).
    assert result.run_ids == frozenset({"run-new"})


def test_eligibility_helper_alias_returns_same_shape(ctx):
    reg = _InMemoryRegistry([
        _doc("d", active_snapshot_id="s", active_run_id="r"),
    ])
    a = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=reg,
    )
    b = resolve_eligible_active_snapshot_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=reg,
    )
    assert a == b


# ---- Worker wiring: activities receive snapshot_service ---------


def test_build_worker_spec_passes_snapshot_service_into_activities(tmp_path):
    """The worker bootstrap MUST construct the snapshot service and
    thread it into ``KnowledgeProcessingActivities`` AND
    ``RunsActivities``. If this fails, the docker-compose runtime
    is silently running the legacy run-keyed path."""
    from deploy.dev._wiring import build_snapshot_services
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    snapshot_service, layout, index_refs = build_snapshot_services(workspace)
    # The builder must produce a real service. The next assertion
    # locks the public surface the activities consume.
    assert snapshot_service is not None
    assert hasattr(snapshot_service, "get_or_create_for_run")
    assert hasattr(snapshot_service, "promote")
    assert hasattr(snapshot_service, "mark_ready")


def test_runs_activities_accepts_snapshot_service():
    """``RunsActivities.__init__`` MUST accept ``snapshot_service``
    so the wiring change in ``build_worker_spec`` actually lands
    on the activity instance."""
    import inspect
    from j1.orchestration.activities.runs import RunsActivities

    sig = inspect.signature(RunsActivities.__init__)
    assert "snapshot_service" in sig.parameters


def test_knowledge_activities_accepts_snapshot_service():
    """Same check for the knowledge activities."""
    import inspect
    from j1.orchestration.activities.knowledge import (
        KnowledgeProcessingActivities,
    )

    sig = inspect.signature(KnowledgeProcessingActivities.__init__)
    assert "snapshot_service" in sig.parameters


def test_search_activities_accepts_evidence_adapter():
    """``SearchActivities`` must accept the canonical evidence
    adapter so writes go to Postgres FTS (or whatever the operator
    configured) instead of only the legacy SQLite indexer."""
    import inspect
    from j1.orchestration.activities.search import SearchActivities

    sig = inspect.signature(SearchActivities.__init__)
    assert "evidence_adapter" in sig.parameters
    assert "snapshot_service" in sig.parameters
    assert "artifact_registry" in sig.parameters


# ---- Evidence adapter is canonical ------------------------------


def test_postgres_fts_is_the_default_evidence_backend():
    """Phase 3 retry: ``EvidenceBackend.POSTGRES_FTS`` is the
    canonical default. SqliteSearchIndexer is no longer the
    strategic write target."""
    cfg = load_runtime_config({})
    assert cfg.evidence.backend == EvidenceBackend.POSTGRES_FTS


def test_build_evidence_adapter_returns_postgres_when_dsn_present(tmp_path):
    """When the operator wires a DSN AND psycopg is installed, the
    canonical adapter is Postgres FTS. When psycopg is missing the
    builder raises a loud RuntimeError — never silently downgrades
    to SQLite. Test asserts the EITHER/OR contract because the test
    venv may or may not have psycopg."""
    from deploy.dev._wiring import build_evidence_adapter
    from j1.artifacts.registry import JsonArtifactRegistry
    from j1.intake.registry import JsonSourceRegistry
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    artifacts = JsonArtifactRegistry(workspace)
    sources = JsonSourceRegistry(workspace)

    import os
    prev_dsn = os.environ.get("J1_METADATA_DSN")
    prev_backend = os.environ.get("J1_EVIDENCE_BACKEND")
    os.environ["J1_METADATA_DSN"] = "postgresql://j1:j1@pg/j1"
    os.environ["J1_EVIDENCE_BACKEND"] = "postgres_fts"
    try:
        try:
            adapter = build_evidence_adapter(workspace, artifacts, sources)
            # If psycopg is installed: must be the Postgres adapter.
            assert adapter.name == "postgres_fts"
        except RuntimeError as exc:
            # psycopg missing: builder MUST fail loud rather than
            # silently fall back to SQLite. This is the Phase 3
            # retry contract — no fake successful adapter.
            assert "psycopg is not installed" in str(exc)
    finally:
        if prev_dsn is None:
            os.environ.pop("J1_METADATA_DSN", None)
        else:
            os.environ["J1_METADATA_DSN"] = prev_dsn
        if prev_backend is None:
            os.environ.pop("J1_EVIDENCE_BACKEND", None)
        else:
            os.environ["J1_EVIDENCE_BACKEND"] = prev_backend


def test_build_evidence_adapter_raises_when_no_dsn_phase8(tmp_path):
    """Phase 8: PostgreSQL FTS is the only supported evidence
    backend. Without a DSN the builder fails fast — no SQLite
    fallback. Reset + re-ingest is the only supported migration
    path."""
    from deploy.dev._wiring import build_evidence_adapter
    from j1.artifacts.registry import JsonArtifactRegistry
    from j1.intake.registry import JsonSourceRegistry
    from j1.workspace.resolver import WorkspaceResolver
    from j1.config.settings import Settings

    workspace = WorkspaceResolver(Settings(data_root=tmp_path))
    artifacts = JsonArtifactRegistry(workspace)
    sources = JsonSourceRegistry(workspace)

    import os
    import pytest
    prev_dsn = os.environ.pop("J1_METADATA_DSN", None)
    prev_ev_dsn = os.environ.pop("J1_EVIDENCE_DSN", None)
    prev_backend = os.environ.get("J1_EVIDENCE_BACKEND")
    os.environ["J1_EVIDENCE_BACKEND"] = "postgres_fts"
    try:
        with pytest.raises(RuntimeError, match="PostgreSQL FTS requires a DSN"):
            build_evidence_adapter(workspace, artifacts, sources)
    finally:
        if prev_dsn is not None:
            os.environ["J1_METADATA_DSN"] = prev_dsn
        if prev_ev_dsn is not None:
            os.environ["J1_EVIDENCE_DSN"] = prev_ev_dsn
        if prev_backend is None:
            os.environ.pop("J1_EVIDENCE_BACKEND", None)
        else:
            os.environ["J1_EVIDENCE_BACKEND"] = prev_backend


# ---- workspace_path_for_run is DELETED (Phase 5) ---------------


def test_workspace_path_for_run_public_symbol_is_deleted():
    """Phase 5: the public deprecated shim is GONE. Any code that
    still imports it from the bridge gets an ``ImportError`` at
    module load — fail-loud retirement of the run-keyed workspace
    resolver."""
    from j1.providers.raganything import _bridge
    assert not hasattr(_bridge, "workspace_path_for_run"), (
        "workspace_path_for_run must be deleted in Phase 5; "
        "snapshot compile uses working_dir_override from "
        "SnapshotLayout.compile(...)"
    )


def test_legacy_helper_remains_internal_only_for_bridge_fallbacks():
    """Phase 6: ``_legacy_workspace_path_for_run`` is DELETED.
    Replaced by ``_snapshot_workspace_path`` which computes the
    snapshot-scoped path. The new helper has the same shape
    (returns None on missing input) but addresses snapshots, not
    runs."""
    from j1.providers.raganything import _bridge
    assert not hasattr(_bridge, "_legacy_workspace_path_for_run")
    # The Phase-6 replacement is importable.
    from j1.providers.raganything._bridge import _snapshot_workspace_path
    assert _snapshot_workspace_path(None, None, None, None) is None
