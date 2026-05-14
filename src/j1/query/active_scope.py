"""Resolver for `ActiveScope` → concrete `RunScope`.

After the snapshot-centered cleanup, documents no longer carry
``active_run_id``; visibility is keyed off ``active_snapshot_id``.
The RunScope-based filtering path that this resolver feeds
(validation's ``_filter_by_scope``) still needs a concrete run_id to
narrow artifacts, so this resolver bridges the gap: it walks
``Document.active_snapshot_id`` → ``DocumentSnapshot.created_by_run_id``
and returns ``RunScope(created_by_run_id)``.

When the document is detached, has no active snapshot, or the snapshot
record is unreadable, the resolver returns ``RunScope`` keyed on the
sentinel ``_NO_ACTIVE_RUN_SENTINEL`` so downstream filtering yields an
empty set (the correct "no active knowledge" answer).

``RunScope`` / ``WorkspaceScope`` inputs are passed through unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from j1.intake.registry import SourceRegistry
from j1.projects.context import ProjectContext
from j1.query.scope import ActiveScope, QueryScope, RunScope

if TYPE_CHECKING:
    from j1.documents.snapshot_store import DocumentSnapshotStore


# Sentinel run_id used when ActiveScope cannot be resolved to a
# concrete run. Format is intentionally not a valid uuid hex so no
# real artifact can ever match it — the filter returns an empty
# result, which is the correct "no-active-knowledge-to-validate"
# answer.
_NO_ACTIVE_RUN_SENTINEL = "__no_active_run__"


def resolve_to_concrete_scope(
    scope: QueryScope,
    *,
    registry: SourceRegistry,
    ctx: ProjectContext,
    snapshot_store: "DocumentSnapshotStore | None" = None,
) -> QueryScope:
    """Resolve `ActiveScope` against the source registry + snapshot
    store; pass `RunScope` / `WorkspaceScope` through unchanged.

    Resolution walks ``Document.active_snapshot_id`` →
    ``DocumentSnapshot.created_by_run_id``. When ``snapshot_store`` is
    not supplied (legacy callers), or any lookup step yields nothing,
    the sentinel is returned and downstream filtering matches zero
    artifacts.
    """
    if not isinstance(scope, ActiveScope):
        return scope

    try:
        doc = registry.get(ctx, scope.document_id)
    except Exception:  # noqa: BLE001 — quiet degradation
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    # Detached / removed documents have no usable active knowledge.
    if doc.knowledge_state != "attached":
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    snapshot_id = getattr(doc, "active_snapshot_id", None)
    if not snapshot_id:
        # Attached but no successful snapshot promotion yet.
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    if snapshot_store is None:
        # No snapshot store wired — caller is using the legacy bridge.
        # Returning the sentinel here is fail-closed; the gated path
        # exists for callers that supply the store.
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    try:
        snapshot = snapshot_store.get(ctx, snapshot_id)
    except Exception:  # noqa: BLE001 — quiet degradation
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    if snapshot is None or not snapshot.created_by_run_id:
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    return RunScope(run_id=snapshot.created_by_run_id)


__all__ = ["resolve_to_concrete_scope"]
