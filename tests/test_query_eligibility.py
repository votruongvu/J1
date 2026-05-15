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
from j1.query.eligibility import (
    resolve_eligible_active_run_ids,
    resolve_query_snapshots,
)
from j1.query.scope import ActiveScope, RunScope, WorkspaceScope
from j1.runs.models import IngestionRun, RunStatus


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


def test_workspace_scope_returns_snapshot_pairs_for_fan_out(ctx):
    """RAGAnything needs ``(document_id, snapshot_id)`` tuples so the
    bridge can compute per-snapshot workspace paths. The eligibility
    result MUST carry pairs alongside the flat snapshot-id set."""
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="r1"),
        _doc(ctx=ctx, document_id="d2", active_snapshot_id="r2"),
        _doc(ctx=ctx, document_id="d3", state="detached",
             active_snapshot_id="r3"),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=WorkspaceScope(), registry=registry,
    )
    assert result.snapshot_pairs == frozenset({
        ("d1", "r1"), ("d2", "r2"),
    })
    # Detached document's pair is excluded — same gate as the flat
    # snapshot_ids set.
    assert ("d3", "r3") not in result.snapshot_pairs


def test_active_scope_returns_single_snapshot_pair_for_eligible_doc(ctx):
    """ActiveScope on an eligible document returns a one-element
    pair set — the RAGAnything adapter uses it for the single-doc
    detail page path."""
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="s1"),
    ])
    from j1.query.scope import ActiveScope as _ActiveScope
    result = resolve_eligible_active_run_ids(
        ctx=ctx, scope=_ActiveScope(document_id="d1"), registry=registry,
    )
    assert result.snapshot_pairs == frozenset({("d1", "s1")})


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


# ---- RunScope via run store (the Run Detail fix) -----------------
#
# The new contract: gated RunScope resolution looks up the run in the
# IngestionRunStore and returns ``(document_id, target_snapshot_id)``.
# Active-snapshot eligibility is INTENTIONALLY bypassed — historical
# / candidate / non-promoted snapshots remain queryable. This is the
# whole point of "validate the snapshot this run produced" from Run
# Detail.


class _StubRunStore:
    def __init__(self, *runs: IngestionRun):
        self._by_id = {r.run_id: r for r in runs}
        self.calls: list[str] = []
    def get(self, ctx, run_id):
        self.calls.append(run_id)
        return self._by_id.get(run_id)


def _historical_run(
    *, run_id="run-old", document_id="d1", snapshot_id="snap-old",
) -> IngestionRun:
    return IngestionRun(
        run_id=run_id,
        document_id=document_id,
        workflow_id=f"wf-{run_id}",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id=snapshot_id,
        metadata={},
    )


def test_run_scope_resolves_historical_run_via_run_store(ctx):
    """A run whose snapshot is NOT the document's active snapshot
    is still queryable through ``RunScope``. The document carries
    ``active_snapshot_id="snap-current"`` but we name the older
    ``run-old`` — its ``target_snapshot_id`` is the answer."""
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="snap-current"),
    ])
    runs = _StubRunStore(_historical_run(
        run_id="run-old", document_id="d1", snapshot_id="snap-superseded",
    ))
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-old"),
        registry=registry,
        run_store=runs,
    )
    assert result.snapshot_ids == frozenset({"snap-superseded"})
    assert result.run_ids == frozenset({"run-old"})
    assert result.document_ids == frozenset({"d1"})
    assert result.snapshot_pairs == frozenset({("d1", "snap-superseded")})


def test_run_scope_ignores_active_snapshot_eligibility(ctx):
    """Eligibility predicate (attached + active_snapshot_id +
    lifecycle ok) must NOT gate the run scope. A detached document
    with no active snapshot still surfaces its historical run's
    snapshot when the run is named explicitly."""
    registry = _StubRegistry([
        _doc(
            ctx=ctx, document_id="d1",
            state="detached",  # would fail project-active gate
            active_snapshot_id=None,  # would fail document-active gate
        ),
    ])
    runs = _StubRunStore(_historical_run(
        run_id="run-detached", document_id="d1", snapshot_id="snap-x",
    ))
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-detached"),
        registry=registry,
        run_store=runs,
    )
    assert result.snapshot_pairs == frozenset({("d1", "snap-x")})


def test_run_scope_rejects_missing_target_snapshot(ctx):
    """Legacy runs without a ``target_snapshot_id`` resolve to
    empty — there's nothing to query."""
    legacy = IngestionRun(
        run_id="run-legacy",
        document_id="d1",
        workflow_id="wf",
        workflow_run_id=None,
        status=RunStatus.SUCCEEDED,
        started_at=_NOW,
        updated_at=_NOW,
        completed_at=_NOW,
        target_snapshot_id=None,
        metadata={},
    )
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-legacy"),
        registry=_StubRegistry([]),
        run_store=_StubRunStore(legacy),
    )
    assert result.snapshot_ids == frozenset()


def test_run_scope_rejects_unknown_run(ctx):
    """An unknown run id is fail-closed. Importantly, the resolver
    does NOT fall back to the document's current active snapshot —
    that would leak the wrong answer to a tester asking about a
    specific historical attempt."""
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="ghost"),
        registry=_StubRegistry([
            _doc(ctx=ctx, document_id="d1", active_snapshot_id="snap-curr"),
        ]),
        run_store=_StubRunStore(),
    )
    assert result.snapshot_ids == frozenset()
    assert result.snapshot_pairs == frozenset()


def test_run_scope_rejects_cross_document_when_document_id_set(ctx):
    """``RunScope(run_id, document_id)`` rejects runs that don't
    belong to the named document. Prevents leaking a stranger's
    run at a document-keyed endpoint."""
    other = _historical_run(
        run_id="run-x", document_id="other-doc", snapshot_id="snap-x",
    )
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-x", document_id="d1"),
        registry=_StubRegistry([]),
        run_store=_StubRunStore(other),
    )
    assert result.snapshot_pairs == frozenset()


def test_run_scope_accepts_matching_document_id_guard(ctx):
    """When ``RunScope.document_id`` matches the run's document the
    pair resolves — this is the happy ``document_run`` path."""
    run = _historical_run(
        run_id="run-y", document_id="d1", snapshot_id="snap-y",
    )
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-y", document_id="d1"),
        registry=_StubRegistry([]),
        run_store=_StubRunStore(run),
    )
    assert result.snapshot_pairs == frozenset({("d1", "snap-y")})


def test_run_scope_rejects_when_snapshot_store_missing_record(ctx):
    """If a snapshot_store is wired and the snapshot record is gone
    (artifacts purged), the resolver fails closed. The caller sees
    an empty result and the adapter surfaces 'snapshot was deleted'
    via its scope-aware message."""
    run = _historical_run(
        run_id="run-deleted", document_id="d1", snapshot_id="snap-purged",
    )

    class _PurgedSnapStore:
        def get(self, ctx, snapshot_id):
            return None

    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=RunScope(run_id="run-deleted"),
        registry=_StubRegistry([]),
        run_store=_StubRunStore(run),
        snapshot_store=_PurgedSnapStore(),
    )
    assert result.snapshot_ids == frozenset()


def test_active_scope_still_requires_attached_and_active_snapshot(ctx):
    """Regression guard: the run-scope refactor must not loosen the
    eligibility predicate on ACTIVE scopes. Detached / no-active-
    snapshot / removed documents must still fail the active gate."""
    detached = _doc(
        ctx=ctx, document_id="d1",
        state="detached", active_snapshot_id="snap-once",
    )
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=ActiveScope(document_id="d1"),
        registry=_StubRegistry([detached]),
    )
    assert result.snapshot_ids == frozenset()


def test_project_active_still_requires_attached_documents(ctx):
    """Regression guard: project-active scope still rejects
    detached / removed documents."""
    docs = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", state="detached", active_snapshot_id="x"),
        _doc(ctx=ctx, document_id="d2", state="attached", active_snapshot_id=None),
    ])
    result = resolve_eligible_active_run_ids(
        ctx=ctx,
        scope=WorkspaceScope(),
        registry=docs,
    )
    assert result.snapshot_pairs == frozenset()


def test_resolve_query_snapshots_is_alias_for_main_resolver(ctx):
    """``resolve_query_snapshots`` is the new public name; it's a
    thin wrapper that omits the ``unchecked`` flag. Pin the
    equivalence so call sites can pick either name."""
    registry = _StubRegistry([
        _doc(ctx=ctx, document_id="d1", active_snapshot_id="snap-1"),
    ])
    a = resolve_query_snapshots(
        ctx=ctx, scope=ActiveScope(document_id="d1"), registry=registry,
    )
    b = resolve_eligible_active_run_ids(
        ctx=ctx, scope=ActiveScope(document_id="d1"), registry=registry,
    )
    assert a == b


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
