"""Resolver for `ActiveScope` → concrete `RunScope`.

After the Phase 9 snapshot-centered cleanup, documents no longer
carry ``active_run_id``; visibility is keyed off
``active_snapshot_id``. The RunScope-based filtering path that
this resolver feeds (validation's `_filter_by_scope`) is therefore
unable to derive a concrete run_id from a document anymore — the
snapshot store is the canonical source of truth.

This resolver is preserved for caller compatibility (validation
service still calls it for ``validation_scope="active"`` requests)
but the result is always the sentinel `RunScope(_NO_ACTIVE_RUN_SENTINEL)`
when the input is an `ActiveScope`. Downstream `_filter_by_scope`
matches zero artifacts, which renders as the correct
"no active knowledge to validate via the run-id path — use the
snapshot-centered query path instead" answer.

`RunScope` / `WorkspaceScope` inputs are returned unchanged.
"""

from __future__ import annotations

from j1.intake.registry import SourceRegistry
from j1.projects.context import ProjectContext
from j1.query.scope import ActiveScope, QueryScope, RunScope


# Sentinel run_id used when ActiveScope cannot be resolved to a
# concrete run. Format is intentionally not a valid uuid hex so no
# real artifact can ever match it — the filter returns an empty
# result, which is the correct
# "no-active-knowledge-to-validate-via-run-id" answer.
_NO_ACTIVE_RUN_SENTINEL = "__no_active_run__"


def resolve_to_concrete_scope(
    scope: QueryScope,
    *,
    registry: SourceRegistry,
    ctx: ProjectContext,
) -> QueryScope:
    """Resolve `ActiveScope` against the source registry; pass
    `RunScope` / `WorkspaceScope` through unchanged.

    Phase 9: documents no longer expose ``active_run_id``, so
    ActiveScope always resolves to the sentinel. Callers that
    need active-knowledge filtering should use the
    snapshot-centered eligibility resolver in
    ``j1.query.eligibility`` directly, not this RunScope-based
    bridge.
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

    if not doc.active_snapshot_id:
        # Attached but no successful snapshot promotion yet.
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    # Phase 9: the snapshot is the truth, but RunScope downstream
    # filters by run_id. Without a snapshot→run reverse lookup
    # wired into this code path, return the sentinel and let
    # snapshot-centered query paths handle the real filtering.
    return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)


__all__ = ["resolve_to_concrete_scope"]
