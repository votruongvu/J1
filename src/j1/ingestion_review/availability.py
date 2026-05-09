"""Per-view availability resolver for the Results tabs.

The single source of truth for *whether* each Results tab should be
enabled and *why not* when it isn't. Keeping the reason strings here
(rather than hardcoded in the FE) means the Overview tab and disabled
tab tooltips always agree.
"""

from __future__ import annotations

from typing import Iterable

from j1.artifacts.models import ArtifactRecord
from j1.connectors.graph.config import ARTIFACT_KIND_GRAPH_JSON
from j1.ingestion_review.dtos import AvailabilityDTO, AvailableViewsDTO
from j1.processing.results import (
    ARTIFACT_KIND_CHUNK,
    ARTIFACT_KIND_PARSED_CONTENT_MANIFEST,
)
from j1.runs.models import IngestionRun


# Artifact-kind taxonomy used for availability checks. Kept narrow on
# purpose — adding a new asset kind here is the one-line opt-in for
# the Assets tab. New `kind=` strings produced by future enrichers
# should be added explicitly so we don't silently surface garbage.
_ASSET_KINDS = frozenset({
    "enriched.tables",
    "enriched.visuals",
    "enriched.formulas",
})

_QUALITY_KINDS = frozenset({
    "enriched.confidence_assessment",
    "enriched.consistency_findings",
})

_GRAPH_KIND = ARTIFACT_KIND_GRAPH_JSON
_CHUNK_KIND = ARTIFACT_KIND_CHUNK
_PARSED_CONTENT_KIND = ARTIFACT_KIND_PARSED_CONTENT_MANIFEST


def resolve_available_views(
    run: IngestionRun,
    artifacts: Iterable[ArtifactRecord],
) -> AvailableViewsDTO:
    """Compute per-tab availability for the given run.

    Inputs are intentionally minimal — the resolver is pure and
    cheap; callers (the service) gather everything once and pass it
    in. No I/O happens here."""
    artifact_kinds = {a.kind for a in artifacts}

    chunks_present = _CHUNK_KIND in artifact_kinds
    graph_present = _GRAPH_KIND in artifact_kinds
    assets_present = bool(artifact_kinds & _ASSET_KINDS)
    quality_present = bool(artifact_kinds & _QUALITY_KINDS)
    raw_present = bool(artifact_kinds)
    parsed_content_present = _PARSED_CONTENT_KIND in artifact_kinds

    # Quality is also available when the run carries warnings, even if
    # no quality artifact was emitted — the Quality tab can still
    # surface those.
    if not quality_present and (run.warning_count or 0) > 0:
        quality_present = True

    # Validation tab gates: terminal-success run with at least one
    # chunk artifact. Without chunks there's nothing to query — the
    # Phase 1 manual test query would return zero retrieval and fail
    # every check. Failed/cancelled runs disable the tab entirely
    # rather than expose a confusing "test a broken run" surface.
    validation_available = (
        _is_terminal_success(run) and chunks_present
    )

    return AvailableViewsDTO(
        chunks=AvailabilityDTO(
            available=chunks_present,
            reason=None if chunks_present else _chunks_reason(run),
        ),
        assets=AvailabilityDTO(
            available=assets_present,
            reason=None if assets_present else _assets_reason(run),
        ),
        graph=AvailabilityDTO(
            available=graph_present,
            reason=None if graph_present else _graph_reason(run),
        ),
        quality=AvailabilityDTO(
            available=quality_present,
            reason=None if quality_present else _quality_reason(run),
        ),
        raw_artifacts=AvailabilityDTO(
            available=raw_present,
            reason=None if raw_present else "No artifacts produced.",
        ),
        validation=AvailabilityDTO(
            available=validation_available,
            reason=(
                None if validation_available
                else _validation_reason(run, chunks_present)
            ),
        ),
        parsed_content=AvailabilityDTO(
            available=parsed_content_present,
            reason=(
                None if parsed_content_present
                else _parsed_content_reason(run)
            ),
        ),
    )


# ---- Reason strings ------------------------------------------------
#
# Reasons are intentionally short, neutral, and operator-readable. No
# vendor names. Re-used both in the Overview banner and the disabled-
# tab tooltip on the FE.

_FAILED_OR_CANCELLED_STATUSES = frozenset({"failed", "cancelled"})


def _chunks_reason(run: IngestionRun) -> str:
    if str(run.status) in _FAILED_OR_CANCELLED_STATUSES:
        return "Compile stage did not produce chunks before the run ended."
    return "No chunks were produced."


def _assets_reason(run: IngestionRun) -> str:  # noqa: ARG001 — reserved for future use
    return "No assets were produced for this run."


def _graph_reason(run: IngestionRun) -> str:
    return graph_unavailable_reason(run)


def graph_unavailable_reason(run: IngestionRun) -> str:
    """Public version of `_graph_reason`. Single source of truth for
    the "why no graph?" string — used by both the availability
    resolver (`/summary`) and the graph snapshot projector
    (`/graph`'s `unavailable.reason` field) so the FE shows the
    same copy across surfaces.

    Three documented reasons in priority order:
      1. Skipped by policy / planner.
      2. Attempted but failed.
      3. Generic fallback when neither signal is present.

    Step-result-derived reasons require Phase 4's
    `metadata["step_results"]` persistence to be effective; without
    it the function falls back to the generic copy."""
    step_results = run.metadata.get("step_results")
    if isinstance(step_results, list):
        for entry in step_results:
            if not isinstance(entry, dict):
                continue
            # Accept either lowercase ("graph", what the workflow
            # writes) or uppercase ("GRAPH", what some early test
            # fixtures used). Tolerance keeps the resolver stable
            # across the Phase 1 → Phase 4 transition.
            if str(entry.get("step") or "").lower() != "graph":
                continue
            status = str(entry.get("status") or "").lower()
            if status == "skipped":
                source = str(entry.get("source") or "").lower()
                if source == "policy":
                    return "Graph generation was skipped by policy."
                return "Graph stage was not selected by the planner."
            if status == "failed":
                return "Graph generation failed."
    return "No graph snapshot was produced for this run."


def _quality_reason(run: IngestionRun) -> str:  # noqa: ARG001 — reserved
    return "No quality data was produced for this run."


_TERMINAL_SUCCESS_STATUSES = frozenset({
    "succeeded",
    "succeeded_with_warnings",
})


def _is_terminal_success(run: IngestionRun) -> bool:
    """Validation-tab gate: only enable for runs that finished cleanly
    enough to have a queryable index. Failed/cancelled runs never
    qualify — even if some chunks slipped through, exposing a 'test
    a broken run' surface confuses operators more than it helps."""
    return str(run.status) in _TERMINAL_SUCCESS_STATUSES


def _parsed_content_reason(run: IngestionRun) -> str:
    """Operator-readable reason for an unavailable Content Inventory.

    Three precedence rules:
      1. Run failed/cancelled before compile produced a manifest →
         dedicated copy so reviewers don't go looking for parser output.
      2. Run is still in compile / hasn't reached the manifest-emit
         step yet → "compile in progress" so the FE can decide
         whether to show a spinner vs an empty state.
      3. Compile completed but no manifest artifact exists — typically
         a legacy run from before this feature shipped.
    """
    if str(run.status) in _FAILED_OR_CANCELLED_STATUSES:
        return (
            "Compile stage did not produce a parsed-content manifest "
            "before the run ended."
        )
    if str(run.status) in {"created", "assessing", "running",
                            "plan_ready", "waiting_for_confirmation"}:
        return (
            "Compile stage has not yet produced a parsed-content "
            "manifest."
        )
    # Terminal run, no manifest artifact — almost certainly a run
    # that pre-dates the feature.
    return (
        "This run was created before Content Inventory tracking "
        "was added."
    )


def _validation_reason(run: IngestionRun, chunks_present: bool) -> str:
    """Two operator-facing reasons in priority order: (1) the run
    didn't complete cleanly, (2) the run completed but produced no
    chunks. The FE renders this as a tooltip on the disabled tab."""
    if not _is_terminal_success(run):
        return "Run did not complete successfully."
    if not chunks_present:
        return "Run produced no chunks to query."
    # Defensive fallback — shouldn't be reached given the caller's
    # gate logic, but a non-empty reason is required when
    # `available=False` so the FE never renders an empty tooltip.
    return "Validation is not available for this run."
