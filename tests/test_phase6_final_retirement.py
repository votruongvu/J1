"""Phase 6 — final retirement invariants.

These tests lock the Phase 6 changes:

  * ``BM25Adapter`` is now a backward-compat alias for
    ``LexicalEvidenceAdapter``; the underlying ranker is Postgres
    FTS ``ts_rank_cd``, not true BM25.
  * ``_legacy_workspace_path_for_run`` is GONE; the bridge raises
    when no working_dir_override or snapshot_id is supplied.
  * ``supersede_previous_active_artifacts`` keys on ``snapshot_id``,
    not ``run_id``.
  * ``RAGAnythingCompileRequest`` carries ``snapshot_id`` +
    ``working_dir_override``.
"""

from __future__ import annotations

import pytest

from j1.projects.context import ProjectContext


@pytest.fixture
def ctx():
    return ProjectContext(tenant_id="t", project_id="p", profile=None)


# ---- BM25Adapter → LexicalEvidenceAdapter ----------------------


def test_bm25_adapter_is_alias_for_lexical_evidence_adapter():
    """Phase 6 renamed the class; the old name is preserved as an
    import alias so external code keeps working. Phase 7 deletes
    the alias."""
    from j1.query.retrieval_routes import BM25Adapter, LexicalEvidenceAdapter
    assert BM25Adapter is LexicalEvidenceAdapter


def test_lexical_evidence_adapter_advertises_ts_rank_cd_not_bm25():
    """The class docstring should NOT pretend ts_rank_cd is true
    BM25. Phase 6 explicitly names the underlying ranker."""
    from j1.query.retrieval_routes import LexicalEvidenceAdapter
    doc = LexicalEvidenceAdapter.__doc__ or ""
    assert "ts_rank_cd" in doc
    assert "NOT true BM25" in doc or "not true BM25" in doc.lower()


# ---- workspace_path_for_run + legacy helper are DELETED --------


def test_legacy_workspace_path_helper_is_gone():
    """Phase 6 deleted the internal underscore helper. The bridge
    has no run-keyed workspace fallback anymore."""
    from j1.providers.raganything import _bridge
    assert not hasattr(_bridge, "_legacy_workspace_path_for_run")
    assert not hasattr(_bridge, "workspace_path_for_run")


def test_snapshot_workspace_path_replaces_run_keyed_path():
    """The new helper computes a snapshot-scoped path. No run_id
    segment anywhere."""
    from j1.providers.raganything._bridge import _snapshot_workspace_path
    from pathlib import Path

    class _Settings:
        workdir = "/tmp/x"

    path = _snapshot_workspace_path(
        _Settings(),
        ProjectContext(tenant_id="t", project_id="p", profile=None),
        "doc-1",
        "snap-1",
    )
    assert path is not None
    parts = list(path.parts)
    assert "snapshots" in parts
    assert "runs" not in parts
    assert "snap-1" in parts


def test_bridge_resolver_raises_when_neither_override_nor_snapshot_given(ctx):
    """The Phase 6 contract: write paths refuse without an
    explicit working_dir_override or snapshot_id."""
    from j1.providers.raganything._bridge import _resolve_bridge_workspace

    class _Request:
        ctx = ProjectContext(tenant_id="t", project_id="p", profile=None)
        document_id = "doc-1"
        snapshot_id = None
        working_dir_override = None

        class settings:
            workdir = "/tmp/x"
            storage_dir = None

    from j1.providers.errors import WorkspaceScopeMissing
    with pytest.raises(
        WorkspaceScopeMissing, match="working_dir_override.*snapshot_id",
    ):
        _resolve_bridge_workspace(_Request(), fallback_to_global=False)


def test_bridge_resolver_uses_explicit_override_when_set(ctx, tmp_path):
    """When the snapshot-aware adapter supplies working_dir_override,
    the resolver returns it unchanged."""
    from j1.providers.raganything._bridge import _resolve_bridge_workspace

    class _Request:
        ctx = ProjectContext(tenant_id="t", project_id="p", profile=None)
        document_id = "doc-1"
        snapshot_id = None
        working_dir_override = tmp_path / "my-override"

        class settings:
            workdir = "/tmp/x"
            storage_dir = None

    out = _resolve_bridge_workspace(_Request(), fallback_to_global=False)
    assert out == tmp_path / "my-override"


def test_bridge_resolver_falls_back_to_global_when_allowed(tmp_path):
    """Read-side paths (draft collection) accept
    ``fallback_to_global=True`` and return ``settings.workdir`` when
    nothing scoped is configured. Write paths use
    ``fallback_to_global=False``."""
    from j1.providers.raganything._bridge import _resolve_bridge_workspace

    class _Request:
        ctx = ProjectContext(tenant_id="t", project_id="p", profile=None)
        document_id = "doc-1"
        snapshot_id = None
        working_dir_override = None

        class settings:
            workdir = str(tmp_path / "global")
            storage_dir = None

    out = _resolve_bridge_workspace(_Request(), fallback_to_global=True)
    assert "global" in str(out)


# ---- Supersede keys on snapshot_id -----------------------------


def test_supersede_signature_uses_snapshot_id_not_run_id():
    """Phase 6 renamed the parameters. Callers must pass
    ``new_snapshot_id`` / ``previous_snapshot_id``."""
    import inspect
    from j1.documents.artifact_state import supersede_previous_active_artifacts

    sig = inspect.signature(supersede_previous_active_artifacts)
    params = set(sig.parameters)
    assert "new_snapshot_id" in params
    assert "previous_snapshot_id" in params
    assert "new_run_id" not in params
    assert "previous_run_id" not in params


def test_supersede_marks_metadata_with_superseded_by_snapshot_id(ctx, tmp_path):
    """The metadata key advertised on superseded artifacts is now
    ``superseded_by_snapshot_id``, not ``superseded_by_run_id``."""
    from datetime import datetime, timezone
    from j1.artifacts.models import ArtifactRecord
    from j1.documents.artifact_state import supersede_previous_active_artifacts
    from j1.jobs.status import ProcessingStatus, ReviewStatus

    class _Registry:
        def __init__(self):
            self.store = {}

        def add(self, r):
            self.store[r.artifact_id] = r

        def list_artifacts(self, _ctx, **_):
            return list(self.store.values())

        def update_metadata(self, _ctx, artifact_id, metadata):
            self.store[artifact_id].metadata = dict(metadata)

        def get(self, _ctx, aid):
            return self.store[aid]

    reg = _Registry()
    reg.add(ArtifactRecord(
        artifact_id="prev", project=ctx, kind="chunk",
        location="a/b", content_hash="x", byte_size=1,
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED, version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source_document_ids=["doc-1"],
        snapshot_id="snap-old",
    ))
    stamped = supersede_previous_active_artifacts(
        ctx=ctx, artifacts=reg,
        document_id="doc-1",
        new_snapshot_id="snap-new",
        previous_snapshot_id="snap-old",
    )
    assert stamped == 1
    assert reg.store["prev"].metadata["superseded_by_snapshot_id"] == "snap-new"
    # No run-id key was introduced.
    assert "superseded_by_run_id" not in reg.store["prev"].metadata


# ---- Request dataclasses carry snapshot_id ---------------------


def test_compile_request_has_snapshot_id_field():
    import inspect
    from j1.providers.raganything.compiler import RAGAnythingCompileRequest
    fields = RAGAnythingCompileRequest.__dataclass_fields__
    assert "snapshot_id" in fields
    assert "working_dir_override" in fields


def test_graph_request_has_snapshot_id_field():
    from j1.providers.raganything.graph import RAGAnythingGraphRequest
    fields = RAGAnythingGraphRequest.__dataclass_fields__
    assert "snapshot_id" in fields
    assert "working_dir_override" in fields


def test_query_request_has_snapshot_id_field():
    from j1.providers.raganything.retrieval import RAGAnythingQueryRequest
    fields = RAGAnythingQueryRequest.__dataclass_fields__
    assert "snapshot_id" in fields
    assert "working_dir_override" in fields


# ---- LexicalEvidenceAdapter uses Postgres FTS contract ---------


def test_lexical_adapter_passes_snapshot_allowlist_to_backend(ctx):
    """The route forwards the snapshot allowlist (the canonical
    visibility key) to the evidence adapter. No run_id."""
    from j1.query.retrieval_routes import (
        LexicalEvidenceAdapter,
        RouteContext,
    )
    from j1.query.query_plan import RetrievalJob
    from j1.query.scope import WorkspaceScope
    from j1.query.retrieval_routes import RetrievalRouteKind

    calls = []

    class _Backend:
        def search(self, _ctx, *, query, allowed_snapshot_ids, max_results):
            calls.append({
                "query": query,
                "allowed_snapshot_ids": list(allowed_snapshot_ids),
                "max_results": max_results,
            })
            return []

    adapter = LexicalEvidenceAdapter(_Backend())
    adapter.execute(
        RetrievalJob(route=RetrievalRouteKind.BM25, query="x"),
        RouteContext(
            ctx=ctx, scope=WorkspaceScope(),
            eligible_snapshot_ids=frozenset({"snap-a", "snap-b"}),
        ),
    )
    assert calls
    assert sorted(calls[0]["allowed_snapshot_ids"]) == ["snap-a", "snap-b"]
