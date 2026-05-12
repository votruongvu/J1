"""Document-lifecycle helpers used across the run-detail, retrieval,
and validation surfaces.

These are pure functions that operate on `ArtifactRecord` / list-of-
records. They're collected in one module so the "is this artifact
usable as knowledge right now?" rule lives in exactly one place —
the same architectural decision that put the `metadata.deleted_at`
soft-delete gate inside
`IngestionResultReviewService._resolve_run_artifacts`.

The filter cooperates with the document-centric refactor: when
Phase 3 introduces the detach/remove actions, those handlers stamp
`metadata.knowledge_state = "detached" | "removed"` on every
artifact tied to the affected document. Until then this module is a
no-op — artifacts without an explicit knowledge_state field are
treated as ``"attached"`` so existing data flows through unchanged.

Why metadata-stamping instead of a live registry join: the existing
soft-delete pattern already proved that denormalising the gate onto
the artifact's own metadata is cheap, race-free against retrieval,
and avoids growing every query provider's constructor with a new
dependency. The detach/remove handlers carry the cost of stamping
once per state change; retrieval stays a single-pass filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from j1.artifacts.models import ArtifactRecord


# Three legal states. Anything else (None, "", legacy values) is
# treated as ``"attached"`` because every artifact written before
# this refactor predates the field — the safe default is "visible".
_ATTACHED_STATES: frozenset[str] = frozenset({"attached"})


def is_attached(record: "ArtifactRecord") -> bool:
    """Return True iff this artifact's source document is currently
    attached to the knowledge base.

    Reads ``metadata.knowledge_state`` with a default of
    ``"attached"`` when the field is missing on disk — same
    forward-compatible rule the run-store deserializer uses.
    """
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return True
    state = metadata.get("knowledge_state")
    if not state:
        # Pre-refactor records have no field → visible.
        return True
    return state in _ATTACHED_STATES


def filter_to_attached_artifacts(
    records: list["ArtifactRecord"],
) -> list["ArtifactRecord"]:
    """Drop any artifact whose source document has been detached or
    removed from the knowledge base.

    Single choke point. Both ``_resolve_run_artifacts`` (run-detail
    surfaces) and ``_filter_by_scope`` (graph / consistency
    providers) call this so the detach action doesn't have to chase
    every read path separately.
    """
    return [r for r in records if is_attached(r)]


__all__ = ["is_attached", "filter_to_attached_artifacts"]
