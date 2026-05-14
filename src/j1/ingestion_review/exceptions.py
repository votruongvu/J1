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


class RunStillActive(J1Error):
    """Operation can't run while the workflow is still active.

 Delete refuses to operate on a RUNNING / PAUSED / CANCELLING /
 ASSESSING run — the workflow could still be writing artifacts.
 Mapped to 409 at the REST boundary; clients are expected to
 `cancel` first."""
