"""Tests for the active-run query eligibility resolver.

Pins ``j1.query.eligibility.resolve_eligible_active_run_ids``
behavior across the three scope shapes plus the ``unchecked``
diagnostic bypass. Every retrieval path post-refactor must
resolve eligibility through this gate, so the rules need
explicit coverage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from j1.documents.models import DocumentRecord
from j1.errors.exceptions import DocumentNotFoundError
from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext
from j1.query.eligibility import resolve_eligible_active_run_ids
from j1.query.scope import ActiveScope, RunScope, WorkspaceScope


_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


class _StubRegistry:
    def __init__(self, docs: list[DocumentRecord]):
        self._by_id = {d.document_id: d for d in docs}

    def get(self, ctx, document_id):
        if document_id not in self._by_id:
            raise DocumentNotFoundError(document_id)
        return self._by_id[document_id]

    def list_documents(self, ctx):
        return list(self._by_id.values())


def _doc(
    *,
    ctx: ProjectContext,
    document_id: str = "doc-1",
    state: str = "attached",
    active_snapshot_id: str | None = "r-1",
    lifecycle: str | None = None,
) -> DocumentRecord:
    doc = DocumentRecord(
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
    # ``lifecycle_status`` field is being added in a subsequent
    # step of the refactor; the eligibility module reads it via
    # ``getattr`` so we can pin the rule via attribute injection
    # ahead of the schema change.
    if lifecycle is not None:
        object.__setattr__(doc, "lifecycle_status", lifecycle)
    return doc


# ---- WorkspaceScope ----------------------------------------------


def test_workspace_scope_unions_eligible_active_snapshots(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
        _doc(ctx=ctx, document_id="d2", active_snapshot_id="r2"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=registry,
    )
    assert result.snapshot_ids == frozenset({"r1", "r2"})
    assert result.document_ids == frozenset({"d1", "d2"})


def test_workspace_scope_drops_detached_and_removed(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
        _doc(ctx=ctx, document_id="d2", state="detached", active_snapshot_id="r2"),
        _doc(
            ctx=ctx, document_id="d3",
            active_snapshot_id="r3", lifecycle="removing",
        ),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=registry,
    )
    assert result.snapshot_ids == frozenset({"r1"})


def test_workspace_scope_empty_project_returns_empty(ctx):
    registry = _StubRegistry([])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=registry,
    )
    assert result.is_empty
    assert result.snapshot_ids == frozenset()


# ---- ActiveScope -------------------------------------------------


def test_active_scope_returns_active_snapshot_for_eligible_doc(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=ActiveScope(document_id="d1"), registry=registry,
    )
    assert result.snapshot_ids == frozenset({"r1"})


def test_active_scope_returns_empty_when_detached(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", state="detached", active_snapshot_id="r1"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=ActiveScope(document_id="d1"), registry=registry,
    )
    assert result.snapshot_ids == frozenset()


def test_active_scope_returns_empty_when_no_active_snapshot(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id=None),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=ActiveScope(document_id="d1"), registry=registry,
    )
    assert result.snapshot_ids == frozenset()


def test_active_scope_unknown_document_returns_empty(ctx):
    registry = _StubRegistry([])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=ActiveScope(document_id="ghost"), registry=registry,
    )
    assert result.snapshot_ids == frozenset()


# ---- RunScope ----------------------------------------------------
# Phase 9: gated RunScope no longer maps to a document (documents
# expose only ``active_snapshot_id``). The gated path returns empty
# — operators must use ``unchecked=True`` for diagnostic run
# scoping. These tests pin that contract.


def test_gated_run_scope_returns_empty(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=RunScope(run_id="r1"), registry=registry,
    )
    assert result.snapshot_ids == frozenset()
    assert result.run_ids == frozenset()


def test_gated_run_scope_empty_when_no_doc_owns_run(ctx):
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r-promoted"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=RunScope(run_id="r-superseded"), registry=registry,
    )
    assert result.snapshot_ids == frozenset()
    assert result.run_ids == frozenset()


# ---- unchecked (diagnostic bypass) -------------------------------


def test_unchecked_returns_run_without_doc_check(ctx):
    """Validation's ``validation_scope="run"`` diagnostic path
    asserts that operators can intentionally query a run that
    fails the document gate."""
    registry = _StubRegistry([])  # no docs at all
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="r-diag"),
        registry=registry,
        unchecked=True,
    )
    assert result.run_ids == frozenset({"r-diag"})
    assert result.unchecked is True


def test_unchecked_with_active_or_workspace_returns_empty(ctx):
    """``unchecked=True`` is only meaningful for RunScope. Other
    scopes return empty so the operator either fixes the scope or
    accepts zero results."""
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=WorkspaceScope(),
        registry=registry,
        unchecked=True,
    )
    assert result.run_ids == frozenset()
    assert result.unchecked is True
