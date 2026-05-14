"""Tests for `ActiveScope` resolution.

Phase 9: documents no longer carry ``active_run_id``; visibility
is snapshot-centered. The legacy ActiveScope → RunScope resolver
preserved here always returns the sentinel for ActiveScope inputs
and passes RunScope / WorkspaceScope through unchanged. Callers
that need active-knowledge filtering should use the
snapshot-centered eligibility resolver in ``j1.query.eligibility``
directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.query.active_scope import resolve_to_concrete_scope
from j1.query.scope import ActiveScope, RunScope, WorkspaceScope


_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
_SENTINEL = "__no_active_run__"


@pytest.fixture
def ctx() -> ProjectContext:
    return ProjectContext(tenant_id="acme", project_id="alpha")


class _StubRegistry:
    """Just enough of the SourceRegistry surface for the resolver."""

    def __init__(self, docs: list[DocumentRecord]):
        self._by_id = {d.document_id: d for d in docs}

    def get(self, ctx, document_id):
        if document_id not in self._by_id:
            raise DocumentNotFoundError(document_id)
        return self._by_id[document_id]


def _doc(
    *, ctx: ProjectContext, document_id: str = "doc-1",
    state: str = "attached",
    active_snapshot_id: str | None = "snap-active",
) -> DocumentRecord:
    return DocumentRecord(
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
        active_snapshot_id=active_snapshot_id,
    )


# ---- Pass-through scopes -----------------------------------------


def test_workspace_scope_passes_through_unchanged(ctx):
    registry = _StubRegistry([])
    result = resolve_to_concrete_scope(
        WorkspaceScope(), registry=registry, ctx=ctx,
    )
    assert isinstance(result, WorkspaceScope)


def test_run_scope_passes_through_unchanged(ctx):
    registry = _StubRegistry([])
    result = resolve_to_concrete_scope(
        RunScope(run_id="r-1"), registry=registry, ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == "r-1"


# ---- ActiveScope: always sentinel after Phase 9 ------------------


def test_active_scope_on_attached_returns_sentinel(ctx):
    """Phase 9: ActiveScope resolves to the sentinel even for an
    attached document with a promoted snapshot. The RunScope-based
    filtering path can't derive a run_id from a snapshot without
    a reverse lookup — callers should use the snapshot eligibility
    resolver instead."""
    registry = _StubRegistry([_doc(ctx=ctx, active_snapshot_id="s-a")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == _SENTINEL


def test_active_scope_on_detached_document_returns_sentinel(ctx):
    registry = _StubRegistry([_doc(ctx=ctx, state="detached")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == _SENTINEL


def test_active_scope_on_removed_document_returns_sentinel(ctx):
    registry = _StubRegistry([_doc(ctx=ctx, state="removed")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == _SENTINEL


def test_active_scope_on_doc_without_active_snapshot_returns_sentinel(ctx):
    registry = _StubRegistry([_doc(ctx=ctx, active_snapshot_id=None)])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == _SENTINEL


def test_active_scope_on_missing_document_returns_sentinel(ctx):
    registry = _StubRegistry([])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-ghost"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == _SENTINEL


# ---- Resolver robustness -----------------------------------------


def test_resolver_quiet_on_registry_exceptions(ctx):
    class _BrokenRegistry:
        def get(self, *args, **kwargs):
            raise RuntimeError("disk caught fire")

    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=_BrokenRegistry(),  # type: ignore[arg-type]
        ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == _SENTINEL
