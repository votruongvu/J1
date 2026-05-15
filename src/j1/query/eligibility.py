"""Active-snapshot eligibility resolver.

Single source of query visibility. Every retrieval path that wants
to surface document-backed knowledge MUST resolve the set of
eligible active ``snapshot_id`` values through this module first,
then filter evidence / vector / graph rows by ``snapshot_id IN
<eligible_set>``.

Phase 9 invariant (queries MUST NOT use ``document_id`` alone, and
``active_run_id`` no longer exists on document records):

  eligible = {
      doc.active_snapshot_id
      for doc in documents(ctx)
      if doc.knowledge_state == "attached"
         and doc.active_snapshot_id is not None
         and doc.lifecycle_status not in {"removing", "removed",
                                          "failed", "cleanup_failed"}
  }

Returning an empty set is a legitimate "nothing is queryable
right now" answer (e.g. an empty project, or every document
detached). Callers translate that into ``WHERE 1=0`` so FTS
ranking sees zero rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from j1.intake.registry import SourceRegistry
from j1.projects.context import ProjectContext
from j1.query.scope import ActiveScope, QueryScope, RunScope, WorkspaceScope

if TYPE_CHECKING:
    from j1.documents.models import DocumentRecord
    from j1.documents.snapshot_store import DocumentSnapshotStore
    from j1.runs.store import IngestionRunStore


# Knowledge states + lifecycle states that DISQUALIFY a document
# from query participation. Centralised so future state additions
# update this gate in one place.
_DISALLOWED_LIFECYCLE: frozenset[str] = frozenset(
    {"removing", "removed", "failed", "cleanup_failed"},
)


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of one eligibility resolution.

    Phase 3 introduces ``snapshot_ids`` as the **primary** visibility
    key. ``run_ids`` is preserved for the legacy code paths (SQLite
    FTS WHERE clauses, validation filters) that haven't been migrated
    yet — both fields are filled in for every result so neither
    side has to look up the other set.

    ``snapshot_pairs`` carries ``(document_id, snapshot_id)`` tuples
    for routes (RAGAnything) that need per-document workspace paths
    on top of the flat snapshot set. Order matches the corresponding
    snapshot in ``snapshot_ids`` is not promised; consumers should
    treat both as set-like.

    ``unchecked`` is true when the caller bypassed the gate via
    ``ScopeOverride`` (validation diagnostic path).
    """

    snapshot_ids: frozenset[str]
    run_ids: frozenset[str]
    document_ids: frozenset[str]
    snapshot_pairs: frozenset[tuple[str, str]] = frozenset()
    unchecked: bool = False

    @property
    def is_empty(self) -> bool:
        # The snapshot side is the source of truth. A query with
        # no eligible snapshots is unrunnable.
        return not self.snapshot_ids


def resolve_eligible_active_run_ids(
    *,
    ctx: ProjectContext,
    scope: QueryScope,
    registry: SourceRegistry,
    unchecked: bool = False,
    run_store: "IngestionRunStore | None" = None,
    snapshot_store: "DocumentSnapshotStore | None" = None,
) -> EligibilityResult:
    """Return the eligibility set for a query.

    The dispatch is explicitly scope-aware: ACTIVE scopes
    (``WorkspaceScope`` / ``ActiveScope``) gate on the active-snapshot
    eligibility predicate, while ``RunScope`` resolves directly via
    the run store and ignores active-snapshot rules. This is the
    invariant that lets Run Detail validate a CANDIDATE snapshot that
    isn't promoted yet — the run is the identity, not the active set.

    ``unchecked=True`` is a separate diagnostic escape hatch (the
    legacy ``validation_scope="run"`` path) — it short-circuits to a
    bare ``run_ids`` set without document-state checks. Distinct from
    the new ``RunScope`` gated path, which DOES verify the run
    exists + has a target snapshot, just without the active filter.

    Per-scope behaviour:

      * ``ActiveScope(document_id)`` — return
        ``snapshot_ids={doc.active_snapshot_id}`` iff the document is
        eligible (attached + has active snapshot + lifecycle ok).
      * ``WorkspaceScope`` — union of every eligible document's
        ``active_snapshot_id``.
      * ``RunScope(run_id, document_id?)`` — look up the run, return
        ``snapshot_ids={run.target_snapshot_id}`` after validating the
        run exists, has a target snapshot, and (if ``document_id`` is
        supplied) belongs to that document. ``run_store`` is REQUIRED
        for this branch; without it the result is empty (fail-closed
        for legacy test wirings).
    """
    if unchecked:
        if isinstance(scope, RunScope):
            return EligibilityResult(
                snapshot_ids=frozenset(),
                run_ids=frozenset({scope.run_id}),
                document_ids=frozenset(),
                unchecked=True,
            )
        # ActiveScope / WorkspaceScope have no meaning under
        # ``unchecked`` — callers should pass RunScope. Empty
        # result here forces the caller to either set scope
        # correctly or accept zero results.
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
            unchecked=True,
        )

    if isinstance(scope, RunScope):
        return _resolve_run_scope_via_store(
            ctx, scope,
            run_store=run_store, snapshot_store=snapshot_store,
        )
    if isinstance(scope, ActiveScope):
        return _resolve_active_scope(ctx, scope.document_id, registry)
    if isinstance(scope, WorkspaceScope):
        return _resolve_workspace_scope(ctx, registry)
    # Defensive: unknown future scope class. Return empty so the
    # gate fails closed.
    return EligibilityResult(
        snapshot_ids=frozenset(),
        run_ids=frozenset(),
        document_ids=frozenset(),
    )


def _resolve_workspace_scope(
    ctx: ProjectContext, registry: SourceRegistry,
) -> EligibilityResult:
    docs = registry.list_documents(ctx)
    eligible_snaps: set[str] = set()
    eligible_docs: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for doc in docs:
        if not _is_document_eligible(doc):
            continue
        eligible_docs.add(doc.document_id)
        eligible_snaps.add(doc.active_snapshot_id)
        pairs.add((doc.document_id, doc.active_snapshot_id))
    return EligibilityResult(
        snapshot_ids=frozenset(eligible_snaps),
        run_ids=frozenset(),
        document_ids=frozenset(eligible_docs),
        snapshot_pairs=frozenset(pairs),
    )


def _resolve_active_scope(
    ctx: ProjectContext, document_id: str, registry: SourceRegistry,
) -> EligibilityResult:
    try:
        doc = registry.get(ctx, document_id)
    except Exception:  # noqa: BLE001 — DocumentNotFoundError + transient IO
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
        )
    if not _is_document_eligible(doc):
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset({doc.document_id}),
        )
    return EligibilityResult(
        snapshot_ids=frozenset({doc.active_snapshot_id}),
        run_ids=frozenset(),
        document_ids=frozenset({doc.document_id}),
        snapshot_pairs=frozenset(
            {(doc.document_id, doc.active_snapshot_id)},
        ),
    )


def _resolve_run_scope_via_store(
    ctx: ProjectContext,
    scope: RunScope,
    *,
    run_store: "IngestionRunStore | None",
    snapshot_store: "DocumentSnapshotStore | None",
) -> EligibilityResult:
    """Resolve a ``RunScope`` against the run store.

    The semantic contract: identity flows ``run → snapshot``, NOT
    ``run → document.active_*``. We deliberately bypass the
    attached/active-snapshot predicate so historical (inactive,
    superseded, failed-promotion) runs remain queryable for
    diagnostic and Run Detail validation paths.

    Reject cases (return empty ``EligibilityResult``):
      * ``run_store`` not wired (legacy test deployments). The caller
        sees zero pairs and the adapter falls back to its
        scope-aware "no eligible snapshot" message.
      * run does not exist in the store for this ``ctx``.
      * caller passed ``scope.document_id`` and it doesn't match
        ``run.document_id`` (cross-document protection).
      * run has no ``target_snapshot_id`` or no ``document_id``
        (legacy / mid-allocation runs).
      * snapshot store is wired AND the snapshot lookup returns
        ``None`` (artifacts deleted / store sweep purged the record).
      * snapshot's ``document_id`` doesn't match the run's
        ``document_id`` (data corruption — fail closed).

    We do NOT check ``snapshot.state`` — a SUPERSEDED snapshot whose
    artifacts are still on disk is a valid query target (the whole
    Run Detail flow exists to re-inspect older candidates). Storage
    sweeps that physically delete artifacts must purge the snapshot
    record itself; the ``snapshot_store.get`` check above catches
    that case.
    """
    if run_store is None:
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
        )
    try:
        run = run_store.get(ctx, scope.run_id)
    except Exception:  # noqa: BLE001 — store IO fault → fail closed
        run = None
    if run is None:
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
        )
    expected_doc = scope.document_id
    if expected_doc is not None and run.document_id != expected_doc:
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
        )
    snapshot_id = getattr(run, "target_snapshot_id", None)
    document_id = getattr(run, "document_id", None)
    if not snapshot_id or not document_id:
        return EligibilityResult(
            snapshot_ids=frozenset(),
            run_ids=frozenset(),
            document_ids=frozenset(),
        )
    if snapshot_store is not None:
        try:
            snap = snapshot_store.get(ctx, snapshot_id)
        except Exception:  # noqa: BLE001 — store IO fault → fail closed
            snap = None
        if snap is None:
            return EligibilityResult(
                snapshot_ids=frozenset(),
                run_ids=frozenset(),
                document_ids=frozenset(),
            )
        if getattr(snap, "document_id", None) != document_id:
            return EligibilityResult(
                snapshot_ids=frozenset(),
                run_ids=frozenset(),
                document_ids=frozenset(),
            )
    return EligibilityResult(
        snapshot_ids=frozenset({snapshot_id}),
        run_ids=frozenset({scope.run_id}),
        document_ids=frozenset({document_id}),
        snapshot_pairs=frozenset({(document_id, snapshot_id)}),
    )


def resolve_eligible_active_snapshot_ids(
    *,
    ctx: ProjectContext,
    scope: QueryScope,
    registry: SourceRegistry,
    unchecked: bool = False,
    run_store: "IngestionRunStore | None" = None,
    snapshot_store: "DocumentSnapshotStore | None" = None,
) -> EligibilityResult:
    """Phase-3 convenience wrapper. Returns the same
    ``EligibilityResult`` as :func:`resolve_eligible_active_run_ids`
    — callers that read the new ``snapshot_ids`` field can switch
    to this name to advertise intent."""
    return resolve_eligible_active_run_ids(
        ctx=ctx, scope=scope, registry=registry, unchecked=unchecked,
        run_store=run_store, snapshot_store=snapshot_store,
    )


def resolve_query_snapshots(
    *,
    ctx: ProjectContext,
    scope: QueryScope,
    registry: SourceRegistry,
    run_store: "IngestionRunStore | None" = None,
    snapshot_store: "DocumentSnapshotStore | None" = None,
) -> EligibilityResult:
    """Public dispatcher. Identical to
    :func:`resolve_eligible_active_run_ids` minus the
    ``unchecked`` flag.

    This is the name to prefer in new call sites — it carries the
    "active-scope eligibility vs explicit-run eligibility" intent.
    The existing entry points stay so the audit-fix tests don't
    have to be re-aimed.
    """
    return resolve_eligible_active_run_ids(
        ctx=ctx, scope=scope, registry=registry,
        run_store=run_store, snapshot_store=snapshot_store,
    )


def _is_document_eligible(doc: "DocumentRecord") -> bool:
    """The full eligibility predicate.

    Phase 3 retry: ``active_snapshot_id`` is the ONLY visibility
    key. The previous ``active_run_id`` fallback is gone — operators
    who ran against pre-Phase-3 data must reset (``scripts/dev/
    reset_docker.sh --yes``) and re-ingest. No backfill.

    A document participates in queries when:
      * ``knowledge_state == "attached"`` (operator gate)
      * ``active_snapshot_id`` is set (a successful Phase-3
        promotion happened)
      * ``lifecycle_status`` is not in the disallowed set
        (rejects removing / removed / failed / cleanup_failed
        even when the operator gate or active markers say
        otherwise).
    """
    if getattr(doc, "knowledge_state", "attached") != "attached":
        return False
    if not getattr(doc, "active_snapshot_id", None):
        return False
    lifecycle = getattr(doc, "lifecycle_status", None)
    if lifecycle is not None and lifecycle in _DISALLOWED_LIFECYCLE:
        return False
    return True
