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
