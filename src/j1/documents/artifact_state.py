"""System-controlled artifact ``search_state`` stampers.

The lifecycle module (`j1.documents.lifecycle`) defines the
retrieval filter that drops artifacts whose `search_state` is
anything other than ``"active"``. This module is the writer side —
the helpers that flip artifacts to ``"superseded"`` or
``"invalid"`` so the filter takes them out of retrieval results.

Two operations land here:

* ``supersede_previous_active_artifacts`` — called from the
  active-run promotion hook (`RunsActivities._maybe_promote_to_active`).
  When a new run becomes the document's active run, the previous
  active run's artifacts get marked ``"superseded"`` so they stop
  appearing in retrieval. The artifacts themselves stay on disk
  for audit; only the retrieval visibility flips.

* ``invalidate_orphan_artifacts`` — called from the reindex
  endpoint's pre-dispatch sweep. Any artifact tied to the target
  document that has ``run_id is None`` (a leftover from the
  pre-lineage-fix era) gets marked ``"invalid"`` so the new
  reindex run isn't competing for retrieval with broken
  metadata-less rows.

Both operations are best-effort: a single artifact write failure
logs at WARNING and continues. The alternative (atomic all-or-
nothing across N artifacts) would need transaction semantics the
JSONL registry can't provide; for these housekeeping operations
"mostly succeeded" is the right contract.
"""

from __future__ import annotations

import logging
from typing import Iterable

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.projects.context import ProjectContext

_log = logging.getLogger("j1.documents.artifact_state")


# Vocabulary the lifecycle filter reads. Kept as constants so
# producers + consumers can match against stable strings rather
# than reinventing the literals at each callsite.
SEARCH_STATE_ACTIVE = "active"
SEARCH_STATE_SUPERSEDED = "superseded"
SEARCH_STATE_INVALID = "invalid"


def supersede_previous_active_artifacts(
    *,
    ctx: ProjectContext,
    artifacts: ArtifactRegistry,
    document_id: str,
    new_run_id: str,
    previous_run_id: str | None,
) -> int:
    """Flip the previous active run's artifacts to ``search_state=
    superseded`` for one document.

    Triggered by the post-promotion hook in
    ``RunsActivities._maybe_promote_to_active``: when a new run
    successfully terminates and becomes the document's active run,
    we want retrieval / answer synthesis / validation to stop
    surfacing the previous run's artifacts immediately. Without
    this stamp, a follow-up retrieval call would return artifacts
    from BOTH runs (the new active and the leftover previous one),
    producing the "mixed-run results" failure mode the lineage
    test reports keep flagging.

    No-op when:
      * ``previous_run_id`` is None (first run for this document —
        nothing to supersede).
      * ``previous_run_id == new_run_id`` (same run is being
        re-promoted on a continue-as-new boundary).

    Returns the number of artifacts re-stamped (useful for the
    audit payload + tests). Artifacts already in any non-active
    state are skipped to avoid double-stamping (idempotent).
    """
    if not previous_run_id or previous_run_id == new_run_id:
        return 0

    update = getattr(artifacts, "update_metadata", None)
    if not callable(update):
        return 0

    try:
        records = artifacts.list_artifacts(ctx)
    except Exception:  # noqa: BLE001 — defensive
        _log.warning(
            "failed to list artifacts for supersede on document %s",
            document_id, exc_info=True,
        )
        return 0

    stamped = 0
    for artifact in records:
        if not _belongs_to(artifact, document_id=document_id, run_id=previous_run_id):
            continue
        meta = dict(getattr(artifact, "metadata", None) or {})
        # Skip artifacts that are already non-active — keeps the
        # operation idempotent. A previously-invalidated artifact
        # stays invalid; a previously-superseded one doesn't
        # re-stamp.
        if meta.get("search_state") and meta["search_state"] != SEARCH_STATE_ACTIVE:
            continue
        meta["search_state"] = SEARCH_STATE_SUPERSEDED
        meta["superseded_by_run_id"] = new_run_id
        try:
            update(ctx, artifact.artifact_id, meta)
            stamped += 1
        except Exception:  # noqa: BLE001 — best-effort
            _log.warning(
                "failed to mark artifact %s superseded",
                artifact.artifact_id, exc_info=True,
            )
    return stamped


def invalidate_orphan_artifacts(
    *,
    ctx: ProjectContext,
    artifacts: ArtifactRegistry,
    document_id: str,
) -> int:
    """Flip artifacts with no ``run_id`` (pre-lineage-fix orphans)
    to ``search_state=invalid`` so retrieval can't surface them.

    Called from the reindex endpoint's pre-dispatch sweep. Without
    this, a fresh reindex would compete for retrieval with old
    broken artifacts — the new run can't fix them by writing new
    ones because the broken ones still claim retrieval slots.

    Returns the number of orphans invalidated. Idempotent — already-
    invalid artifacts skip.
    """
    update = getattr(artifacts, "update_metadata", None)
    if not callable(update):
        return 0

    try:
        records = artifacts.list_artifacts(ctx)
    except Exception:  # noqa: BLE001
        _log.warning(
            "failed to list artifacts for orphan sweep on document %s",
            document_id, exc_info=True,
        )
        return 0

    stamped = 0
    for artifact in records:
        # Only orphans tied to THIS document. An artifact with no
        # source_document_ids match isn't our problem to clean up.
        if document_id not in (artifact.source_document_ids or []):
            continue
        meta = dict(getattr(artifact, "metadata", None) or {})
        if meta.get("run_id"):
            continue  # has a run_id — not an orphan
        if meta.get("search_state") == SEARCH_STATE_INVALID:
            continue  # already invalidated
        meta["search_state"] = SEARCH_STATE_INVALID
        meta["invalid_reason"] = "missing_run_id"
        try:
            update(ctx, artifact.artifact_id, meta)
            stamped += 1
            _log.info(
                "invalidated orphan artifact %s (kind=%s) "
                "for document %s — no run_id stamped",
                artifact.artifact_id, artifact.kind, document_id,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "failed to invalidate orphan artifact %s",
                artifact.artifact_id, exc_info=True,
            )
    return stamped


def _belongs_to(
    artifact: ArtifactRecord, *, document_id: str, run_id: str,
) -> bool:
    """Cheap "is this artifact for the document+run pair?" check.

    Matches on ``source_document_ids`` (the registry's structural
    link) AND ``metadata.run_id`` (the lineage stamp). Both must
    align — an artifact tagged for a different run shouldn't be
    flipped by another run's supersede pass even if document ids
    overlap.
    """
    if document_id not in (artifact.source_document_ids or []):
        return False
    meta = artifact.metadata if isinstance(artifact.metadata, dict) else {}
    return meta.get("run_id") == run_id


__all__ = [
    "SEARCH_STATE_ACTIVE",
    "SEARCH_STATE_INVALID",
    "SEARCH_STATE_SUPERSEDED",
    "invalidate_orphan_artifacts",
    "supersede_previous_active_artifacts",
]
