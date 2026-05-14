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
) -> EligibilityResult:
    """Return the eligibility set for a query.

    ``unchecked=True`` is the explicit escape hatch the validation
    surface uses when ``validation_scope="run"`` — operators
    intentionally querying a specific run (including failed /
    superseded / detached / removed) for diagnostic reasons. In
    that case the supplied scope's ``run_id`` is returned in
    ``run_ids`` without the document-state check.

    For the regular (gated) path the result is snapshot-centered:

      * ``ActiveScope(document_id)`` — return
        ``snapshot_ids={doc.active_snapshot_id}`` if eligible.
      * ``WorkspaceScope`` — union of every eligible document's
        ``active_snapshot_id``.
      * ``RunScope(run_id)`` — empty in the gated path (Phase 9
        removed the run_id-based reverse lookup; use
        ``unchecked=True`` for diagnostic run scoping).
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
        return _resolve_run_scope(ctx, scope.run_id, registry)
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


def _resolve_run_scope(
    ctx: ProjectContext, run_id: str, registry: SourceRegistry,
) -> EligibilityResult:
    """RunScope in the gated path is unreachable after Phase 9.

    Documents no longer carry ``active_run_id``; visibility is
    snapshot-centered. Callers that want a specific run_id must
    use ``unchecked=True`` (the validation-diagnostic path).
    Returning empty here is fail-closed: the gated path can't
    derive owning-document eligibility from a bare run_id without
    walking the snapshot store, and we deliberately don't do that
    (snapshot scope is the supported route).
    """
    return EligibilityResult(
        snapshot_ids=frozenset(),
        run_ids=frozenset(),
        document_ids=frozenset(),
    )


def resolve_eligible_active_snapshot_ids(
    *,
    ctx: ProjectContext,
    scope: QueryScope,
    registry: SourceRegistry,
    unchecked: bool = False,
) -> EligibilityResult:
    """Phase-3 convenience wrapper. Returns the same
    ``EligibilityResult`` as :func:`resolve_eligible_active_run_ids`
    — callers that read the new ``snapshot_ids`` field can switch
    to this name to advertise intent."""
    return resolve_eligible_active_run_ids(
        ctx=ctx, scope=scope, registry=registry, unchecked=unchecked,
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
