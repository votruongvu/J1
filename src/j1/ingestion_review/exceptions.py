"""Exceptions raised by `IngestionResultReviewService`.

All map to safe 404s at the REST boundary — a wrong-tenant /
wrong-project request must look identical to a missing run, so
existence isn't leakable across tenants.
"""

from __future__ import annotations

from j1.errors.exceptions import J1Error


class ReviewNotFound(J1Error):
    """Run / artifact / chunk not found in the requesting context.

    Raised by every public service method when the referenced entity
    either doesn't exist, or exists in a different tenant/project than
    the caller's `ProjectContext`. The REST layer maps this to 404 —
    never 403 — so cross-tenant probing can't distinguish "missing"
    from "you can't see it."
    """


class RunNotTerminal(J1Error):
    """Operation requires a terminal run; the run is still in progress.

    Some review surfaces (e.g. summary, quality report) only make sense
    once the workflow is finished. The REST layer maps this to 409
    (Conflict) so clients can distinguish "not ready yet" from
    "doesn't exist."
    """


class RunStillActive(J1Error):
    """Operation can't run while the workflow is still active.

    Soft-delete and full-reindex both refuse to operate on a RUNNING /
    PAUSED / CANCELLING / ASSESSING run — the workflow could still be
    writing artifacts. Mapped to 409 at the REST boundary; clients
    are expected to `cancel` first."""


class ResumeNotPossible(J1Error):
    """Resume-from-checkpoint can't proceed against this run.

    Raised by `resume_from_checkpoint` when the prior run lacks a
    `resume_snapshot` (terminated before the snapshot machinery
    landed, or finished via the cancelled / unknown-terminal paths
    that don't snapshot). Mapped to 412 (Precondition Failed) at
    the REST boundary — the operator should full-reindex instead."""


class ResumeIncompatible(J1Error):
    """Resume-from-checkpoint refused — settings drifted.

    Raised when the prior run's `settings_hash` doesn't match the
    candidate request's hash. Carries the structured diff
    (`{field: {"before": x, "after": y}}`) on the `.diff` attribute
    so the REST layer can surface it in the 412 response body."""

    def __init__(self, message: str, diff: dict[str, dict] | None = None) -> None:
        super().__init__(message)
        self.diff = diff or {}
