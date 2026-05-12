"""Helper to resolve `ActiveScope` against the source registry.

`ActiveScope` is a marker — it doesn't carry a run_id. The
validation service calls this resolver to turn it into a concrete
``RunScope`` by looking up the document's currently promoted run.
Keeping the resolution in a dedicated module means the query
provider layer never needs registry access; it stays a pure
filtering function over `RunScope` / `WorkspaceScope`.

Resolution rules:

* Document is **attached** AND has an ``active_run_id`` → resolve
  to ``RunScope(active_run_id)``.
* Document is **detached** or **removed** → return a sentinel
  `RunScope(_NO_ACTIVE_RUN_SENTINEL)`. Downstream `_filter_by_scope`
  will match zero artifacts against that sentinel, which is the
  right "validate the active knowledge" answer when there is no
  active knowledge to validate.
* Document **missing** from the registry → same sentinel.
* Document attached but ``active_run_id`` is None (just uploaded,
  first ingestion still queued) → same sentinel.

The sentinel approach (vs. raising) matches the rest of the
codebase's "quiet degradation" pattern — the validation surface
still gets a result it can render ("no active artifacts") instead
of a 500.
"""

from __future__ import annotations

from j1.intake.registry import SourceRegistry
from j1.projects.context import ProjectContext
from j1.query.scope import ActiveScope, QueryScope, RunScope


# Sentinel run_id used when the document isn't in a state that has
# usable active knowledge. Format is intentionally not a valid
# uuid hex so no real artifact can ever match it — the filter
# returns an empty result, which is the correct
# "no-active-knowledge-to-validate" answer.
_NO_ACTIVE_RUN_SENTINEL = "__no_active_run__"


def resolve_to_concrete_scope(
    scope: QueryScope,
    *,
    registry: SourceRegistry,
    ctx: ProjectContext,
) -> QueryScope:
    """Resolve `ActiveScope` against the source registry; pass
    `RunScope` / `WorkspaceScope` through unchanged.

    Called by the validation service before dispatching the query
    so the query provider only ever sees concrete scopes. Quiet
    on every failure path (missing document, registry hiccup) —
    returns the sentinel so the caller still gets a valid empty
    result set.
    """
    if not isinstance(scope, ActiveScope):
        return scope

    try:
        doc = registry.get(ctx, scope.document_id)
    except Exception:  # noqa: BLE001 — quiet degradation
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    # Detached / removed documents have no usable active knowledge.
    # Operator must attach (or re-upload, for removed) before
    # active-scoped validation makes sense.
    if doc.knowledge_state != "attached":
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    if not doc.active_run_id:
        # Document attached but never reached a usable terminal
        # state. No promotion has happened; there's no run to
        # validate against.
        return RunScope(run_id=_NO_ACTIVE_RUN_SENTINEL)

    return RunScope(run_id=doc.active_run_id)


__all__ = ["resolve_to_concrete_scope"]
