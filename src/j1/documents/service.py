"""`DocumentLifecycleService` — the document-centric knowledge
attach / detach / remove actions.

Phase 3 of the document-centric refactor. This is the layer that
*activates* Phase 2's retrieval filter: when a user detaches a
document, this service walks every artifact tied to it and stamps
``metadata.knowledge_state = "detached"`` so the filter at
``filter_to_attached_artifacts`` immediately drops them from
retrieval, graph QA, validation, and answer generation.

Three actions, all idempotent:

* **attach**  — restore knowledge usage. Stamps every artifact back
  to ``"attached"``. Refuses on removed documents (those need
  re-upload — their knowledge was purged).

* **detach** — temporarily exclude from retrieval. Document, runs,
  artifacts stay on disk; the user can re-attach later.

* **remove** — permanent (from the user's perspective) knowledge
  purge. Clears ``active_run_id``, sets ``removed_at``, stamps
  every artifact as removed. Run history is kept as a minimal
  tombstone but is hidden from normal UI. A removed document
  cannot be re-attached without re-uploading (because the
  artifacts have been disowned by the knowledge layer).

Audit: every transition emits one `j1.document.*` event with the
before-state and after-state on its payload so operators can
reconstruct the order of operations from the audit log alone.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _replace
from datetime import datetime, timezone
from typing import Any

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.audit.recorder import AuditRecorder
from j1.documents.models import DocumentRecord, KnowledgeState
from j1.errors.exceptions import DocumentNotFoundError
from j1.intake.registry import SourceRegistry
from j1.projects.context import ProjectContext

_log = logging.getLogger("j1.documents.service")


# Audit-action names. Kept as constants so audit-log readers
# (analytics jobs, the FE timeline projector) can match against
# stable strings rather than free-text.
_ACTION_ATTACH = "j1.document.attached"
_ACTION_DETACH = "j1.document.detached"
_ACTION_REMOVE = "j1.document.removed"
_TARGET_DOCUMENT = "document"


class DocumentLifecycleError(Exception):
    """Raised by the service when a state transition is rejected.

    The REST adapter translates this to HTTP 409 (Conflict) so the
    FE can render a meaningful "you can't detach a removed
    document" message instead of a generic 500.
    """


class DocumentLifecycleService:
    """Owns the document-centric attach/detach/remove flow.

    Constructor takes the same shape as `IngestionResultReviewService`
    — explicit dependencies, no facade — so it's trivially
    constructable in tests.

    Idempotency: re-running an action against a document already in
    the target state is a no-op (returns the same record without
    re-stamping artifacts or writing a duplicate audit event). Lets
    the FE confidently retry a click on a stale UI without
    corrupting state.
    """

    def __init__(
        self,
        *,
        registry: SourceRegistry,
        artifact_registry: ArtifactRegistry,
        audit: AuditRecorder | None = None,
        clock=None,
    ) -> None:
        self._registry = registry
        self._artifacts = artifact_registry
        self._audit = audit
        # Injected clock for deterministic tests; default to real UTC.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---- Public actions ------------------------------------------

    def attach(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        actor: str,
    ) -> DocumentRecord:
        """Mark the document attached and re-enable its artifacts.

        Rejected when the document was previously removed — the
        artifacts have been disowned and the user should re-upload
        to bring the document back. (We could relax this later to
        allow "attach back from removed" if there's demand, but the
        spec says removed is a one-way state for the knowledge
        layer.)
        """
        return self._transition(
            ctx, document_id, target="attached", actor=actor,
            audit_action=_ACTION_ATTACH,
        )

    def detach(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        actor: str,
    ) -> DocumentRecord:
        """Stop using the document for retrieval; preserve everything
        else. Rejected when the document is already removed."""
        return self._transition(
            ctx, document_id, target="detached", actor=actor,
            audit_action=_ACTION_DETACH,
        )

    def remove(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        actor: str,
    ) -> DocumentRecord:
        """Disown the document's generated knowledge.

        Clears ``active_run_id`` and stamps ``removed_at``. The
        document's run history stays on disk as a minimal tombstone
        for audit; this is NOT a hard purge (Phase 8 territory).
        """
        return self._transition(
            ctx, document_id, target="removed", actor=actor,
            audit_action=_ACTION_REMOVE,
        )

    # ---- Internals -----------------------------------------------

    def _transition(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        target: KnowledgeState,
        actor: str,
        audit_action: str,
    ) -> DocumentRecord:
        doc = self._registry.get(ctx, document_id)
        previous_state = doc.knowledge_state

        # Validation rule: removed is a one-way terminal state for
        # this iteration. Attach/Detach against a removed document
        # need a re-upload first.
        if doc.knowledge_state == "removed" and target != "removed":
            raise DocumentLifecycleError(
                f"document {document_id} has been removed; re-upload to "
                f"restore knowledge — cannot {target}",
            )

        # Idempotency short-circuit. Detect by comparing the new
        # state against the current — repeated calls return the
        # existing record without re-touching artifacts or emitting
        # duplicate audit events.
        if previous_state == target:
            return doc

        now = self._clock()
        updates: dict[str, Any] = {
            "knowledge_state": target,
            "updated_at": now,
        }
        if target == "removed":
            # Removing should clear the "current usable result"
            # pointer so any FE that hasn't read this transition
            # yet won't try to render a removed run as the active
            # result. The active run still exists in the run-store
            # tombstone; it's just no longer the document's pick.
            updates["active_run_id"] = None
            updates["removed_at"] = now
        elif previous_state == "removed":
            # Defensive: should be caught by the rejection above.
            # Belt + braces.
            raise DocumentLifecycleError(
                "cannot transition out of removed without re-upload",
            )

        updated_doc = self._registry.update_document_fields(
            ctx, document_id, **updates,
        )

        # Stamp every artifact tied to this document so the
        # retrieval gate at `filter_to_attached_artifacts` sees the
        # new state on the next read. We DO NOT delete files; this
        # is a metadata-only flip, fully reversible until Phase 8's
        # hard-purge action.
        stamped = self._stamp_artifacts(ctx, document_id, state=target)

        if self._audit is not None:
            try:
                self._audit.record(
                    ctx,
                    actor=actor,
                    action=audit_action,
                    target_kind=_TARGET_DOCUMENT,
                    target_id=document_id,
                    payload={
                        "previous_state": previous_state,
                        "new_state": target,
                        "stamped_artifact_count": stamped,
                    },
                )
            except Exception:  # noqa: BLE001 — audit failures must not break the action
                _log.warning(
                    "audit failed for %s on document %s",
                    audit_action, document_id, exc_info=True,
                )

        return updated_doc

    def _stamp_artifacts(
        self,
        ctx: ProjectContext,
        document_id: str,
        *,
        state: KnowledgeState,
    ) -> int:
        """Flip ``metadata.knowledge_state`` on every artifact whose
        ``source_document_ids`` lists this document.

        Returns the number of artifacts stamped (useful for the
        audit payload + tests). Doesn't touch artifacts that
        already carry the target state — keeps the operation O(N)
        but skips redundant writes.

        Failure handling: each artifact's stamp is best-effort. A
        single artifact write failure logs at WARNING and continues;
        the action as a whole succeeds. The alternative (atomic
        all-or-nothing) would require a multi-write transaction the
        JSONL registry can't provide.
        """
        update = getattr(self._artifacts, "update_metadata", None)
        if not callable(update):
            # In-memory test fixtures sometimes don't implement
            # update_metadata. Fall back to nothing — the test can
            # build the artifacts pre-stamped if it needs to.
            return 0
        stamped = 0
        try:
            artifacts = self._artifacts.list_artifacts(ctx)
        except Exception:  # noqa: BLE001
            _log.warning(
                "failed to list artifacts for document %s",
                document_id, exc_info=True,
            )
            return 0
        for artifact in artifacts:
            if document_id not in (artifact.source_document_ids or []):
                continue
            existing = dict(getattr(artifact, "metadata", None) or {})
            if existing.get("knowledge_state") == state:
                continue
            existing["knowledge_state"] = state
            try:
                update(ctx, artifact.artifact_id, existing)
                stamped += 1
            except Exception:  # noqa: BLE001 — best effort
                _log.warning(
                    "failed to stamp knowledge_state on artifact %s",
                    artifact.artifact_id, exc_info=True,
                )
        return stamped


__all__ = [
    "DocumentLifecycleError",
    "DocumentLifecycleService",
]
