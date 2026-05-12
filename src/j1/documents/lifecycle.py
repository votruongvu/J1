"""Document-lifecycle helpers used across the run-detail, retrieval,
and validation surfaces.

Two metadata flags are consulted at retrieval time:

  * ``metadata.knowledge_state`` — **operator-controlled**.
    ``"attached"`` (default) / ``"detached"`` / ``"removed"`` —
    stamped by the Phase 3 lifecycle actions when a user
    detaches/removes the source document.

  * ``metadata.search_state``    — **system-controlled**.
    ``"active"`` (default) / ``"superseded"`` / ``"invalid"`` —
    stamped automatically when:

       - a reindex succeeds and the *previous* active run's
         artifacts get demoted to ``"superseded"`` (so the FE never
         sees mixed-run retrieval results).
       - the pre-reindex repair sweep detects an artifact with
         no ``run_id`` and marks it ``"invalid"`` so retrieval
         drops it but audit can still find it.

Single choke point: every retrieval / validation / answer-synthesis
call routes through ``filter_to_attached_artifacts`` so the
operator flag and the system flag are enforced together. Anything
not in the "still usable" intersection drops out before it can
reach the user.

Why metadata-stamping instead of a live registry join: the existing
soft-delete pattern already proved that denormalising the gate onto
the artifact's own metadata is cheap, race-free against retrieval,
and avoids growing every query provider's constructor with a new
dependency. The detach/remove handlers + the post-promotion
supersede hook carry the cost of stamping once per state change;
retrieval stays a single-pass filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from j1.artifacts.models import ArtifactRecord


# Knowledge-state values that mean "the operator still wants this
# document used as knowledge." Anything outside this set means the
# operator detached/removed the parent document.
_ATTACHED_STATES: frozenset[str] = frozenset({"attached"})

# Search-state values that mean "the system still considers this
# artifact part of the active result set." Anything outside this
# set is bookkeeping debris — superseded by a newer reindex, or
# flagged invalid by the repair sweep — and must not reach
# retrieval.
_ACTIVE_SEARCH_STATES: frozenset[str] = frozenset({"active"})


def is_attached(record: "ArtifactRecord") -> bool:
    """Return True iff this artifact passes BOTH the operator-
    controlled and system-controlled visibility flags.

    The name stays ``is_attached`` for backward compat — every
    existing caller of this helper wants the same "is this still
    usable as knowledge?" semantics; we just enforce both flags
    inside now.

    Reads ``metadata.knowledge_state`` and ``metadata.search_state``
    with safe defaults (``"attached"`` / ``"active"``) when the
    fields are missing on disk — keeps pre-refactor records
    visible without an explicit migration step.
    """
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return True

    # Operator flag.
    knowledge_state = metadata.get("knowledge_state") or "attached"
    if knowledge_state not in _ATTACHED_STATES:
        return False

    # System flag.
    search_state = metadata.get("search_state") or "active"
    if search_state not in _ACTIVE_SEARCH_STATES:
        return False

    return True


def filter_to_attached_artifacts(
    records: list["ArtifactRecord"],
) -> list["ArtifactRecord"]:
    """Drop any artifact that fails the visibility gate.

    Single choke point — both ``_resolve_run_artifacts``
    (run-detail surfaces) and ``_filter_by_scope`` (graph /
    consistency providers) call this so the detach action,
    supersede hook, and repair sweep don't have to chase every
    read path separately.
    """
    return [r for r in records if is_attached(r)]


__all__ = ["is_attached", "filter_to_attached_artifacts"]
