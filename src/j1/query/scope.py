"""Query scoping primitives.

`QueryScope` is the value object the query / retrieval surface uses to
narrow a search to a subset of indexed artifacts. The scope is applied
INSIDE the index layer (in the SQL `WHERE` clause) so BM25 ranking
sees only the filtered set — post-topK pruning would distort scores.

Today only `WorkspaceScope` (the existing default — no extra filter)
and `RunScope` (everything tagged with `metadata.run_id == run_id`)
are supported. The union exists so future scopes (`DocumentScope`,
`ArtifactLineageScope`, …) slot in without touching the request DTO
shape every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class WorkspaceScope:
    """Default scope: search the whole `(tenant, project)` index.

 Equivalent to the historical "no filter" behaviour. Callers that
 don't supply a scope get this implicitly so legacy code paths
 keep working unchanged.
 """


@dataclass(frozen=True)
class RunScope:
    """Restrict retrieval to artifacts produced by a single ingestion run.

 The validation surface uses this so a tester's "did this document
 answer correctly?" question can never accidentally pull evidence
 from a sibling run in the same project.
 """

    run_id: str


@dataclass(frozen=True)
class ActiveScope:
    """Restrict retrieval to what a normal user can actually search.

 Distinct from `RunScope` (which targets one specific attempt) —
 `ActiveScope` answers "validate the knowledge state users see
 today". Resolved by `j1.query.active_scope.resolve_to_concrete_scope`
 into a concrete `RunScope(active_run_id)` BEFORE the query
 dispatch, so the engine layer never has to grow registry
 awareness.

 The resolver's rules:

   1. The document is ``knowledge_state=attached`` (operator hasn't
      detached/removed it).
   2. The document has a non-empty ``active_run_id`` (its
      promotion hook fired on a successful run).

 When either condition fails, the resolver yields a sentinel
 `RunScope` that matches no artifact — the validation surface
 then renders "no active knowledge to validate" rather than
 raising.

 Used by validation-against-active-knowledge: a tester clicking
 "validate what users see right now" gets this scope, while
 a tester clicking "validate THIS run's output" gets `RunScope`.
 The two are never mixed implicitly — the caller picks one.

 `document_id` is the only field — the resolver does the
 lookup. Frozen so the dataclass is hashable and matches the
 pattern of `RunScope`.
 """

    document_id: str


QueryScope = Union[WorkspaceScope, RunScope, ActiveScope]
"""Public alias — accepts any of the concrete scope dataclasses."""


_DEFAULT_SCOPE = WorkspaceScope()


def default_scope() -> QueryScope:
    """Return the project-wide default scope.

 Cached singleton so callers can use it as a sentinel without
 allocating a new instance on every request.
 """
    return _DEFAULT_SCOPE
