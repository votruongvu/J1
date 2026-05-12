"""Tests for `ActiveScope` resolution.

Spec section 9: validation must explicitly choose between two
scopes — "validate this specific run" (RunScope) and "validate
what users can actually search" (ActiveScope). These tests pin
the resolver's behavior: ActiveScope → RunScope(active_run_id)
when the document is attached and has a promoted run; falls back
to a no-match sentinel otherwise.

The sentinel-match approach (vs. raising) means the validation
surface always gets a valid, empty result set rather than a 500 —
"no active knowledge to validate" is a meaningful answer, not
an error.
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
    active_run_id: str | None = "r-active",
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
        active_run_id=active_run_id,
    )


# ---- Pass-through scopes -----------------------------------------


def test_workspace_scope_passes_through_unchanged(ctx):
    """The resolver only does work for ActiveScope. WorkspaceScope
 and RunScope should round-trip untouched."""
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


# ---- ActiveScope happy path --------------------------------------


def test_active_scope_resolves_to_documents_active_run(ctx):
    """Headline behaviour: an attached document with a promoted
 run → RunScope(active_run_id). This is what "validate what
 users can actually search" means in practice."""
    registry = _StubRegistry([_doc(ctx=ctx, active_run_id="r-active")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == "r-active"


# ---- ActiveScope no-active-knowledge paths -----------------------


def test_active_scope_on_detached_document_returns_no_match_sentinel(ctx):
    """A detached document has no usable active knowledge — the
 operator must attach (or re-upload, for removed) first. The
 resolver returns a sentinel that matches no artifact, which
 the downstream filter renders as an empty result set."""
    registry = _StubRegistry([_doc(ctx=ctx, state="detached")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert isinstance(result, RunScope)
    # The sentinel run_id is the same regardless of why we got
    # here — the caller treats "no active knowledge" uniformly.
    assert result.run_id == "__no_active_run__"


def test_active_scope_on_removed_document_returns_sentinel(ctx):
    """Removed documents are even more excluded. Same sentinel."""
    registry = _StubRegistry([_doc(ctx=ctx, state="removed")])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == "__no_active_run__"


def test_active_scope_on_doc_without_active_run_returns_sentinel(ctx):
    """Document attached but no active_run_id (just uploaded, first
 ingestion still queued / failed pre-compile). No active
 knowledge to validate yet."""
    registry = _StubRegistry([_doc(ctx=ctx, active_run_id=None)])
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == "__no_active_run__"


def test_active_scope_on_missing_document_returns_sentinel(ctx):
    """Document not in the registry → sentinel. Lets the caller
 handle the missing-document case as 'empty active knowledge'
 rather than raising."""
    registry = _StubRegistry([])  # empty
    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-ghost"),
        registry=registry, ctx=ctx,
    )
    assert result.run_id == "__no_active_run__"


# ---- ActiveScope vs RunScope semantic distinction ----------------


def test_active_and_run_scope_can_differ_after_reindex(ctx):
    """The whole point of ActiveScope: when a reindex has produced
 a new active run, ActiveScope picks the NEW run while
 RunScope still points at the original. The validation surface
 uses ActiveScope to test current user-visible knowledge."""
    # Document's active_run_id is now r-new after a successful
    # reindex; r-old is the previous run that's still on disk.
    doc = _doc(ctx=ctx, active_run_id="r-new")
    registry = _StubRegistry([doc])

    active_resolution = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=registry, ctx=ctx,
    )
    # RunScope on the OLD run is a pass-through, unchanged.
    old_scope = resolve_to_concrete_scope(
        RunScope(run_id="r-old"), registry=registry, ctx=ctx,
    )

    # The two scopes target different runs — exactly what spec
    # section 9 requires.
    assert active_resolution.run_id == "r-new"
    assert old_scope.run_id == "r-old"
    assert active_resolution.run_id != old_scope.run_id


# ---- Resolver robustness -----------------------------------------


def test_resolver_quiet_on_registry_exceptions(ctx):
    """Registry could raise for any number of reasons (file I/O,
 lock contention). The resolver MUST NOT propagate — it
 returns the sentinel so validation gets an empty result
 set instead of 500."""

    class _BrokenRegistry:
        def get(self, *args, **kwargs):
            raise RuntimeError("disk caught fire")

    result = resolve_to_concrete_scope(
        ActiveScope(document_id="doc-1"),
        registry=_BrokenRegistry(),  # type: ignore[arg-type]
        ctx=ctx,
    )
    assert isinstance(result, RunScope)
    assert result.run_id == "__no_active_run__"
