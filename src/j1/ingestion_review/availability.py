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
from j1.processing.results import ARTIFACT_KIND_CHUNK
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


def resolve_available_views(
    run: IngestionRun,
    artifacts: Iterable[ArtifactRecord],
    *,
    planning_present: bool = False,  # kept for caller-API compatibility
) -> AvailableViewsDTO:
    """Compute per-tab availability for the given run.

    Inputs are intentionally minimal — the resolver is pure and
    cheap; callers (the service) gather everything once and pass it
    in. No I/O happens here.

    `planning_present` is accepted but no longer consulted — the
    Planning Report tab is now always available (its content endpoint
    handles the empty state). Callers can drop the kwarg at their
    convenience; we keep the parameter for API stability across the
    in-flight rework."""
    artifact_kinds = {a.kind for a in artifacts}

    chunks_present = _CHUNK_KIND in artifact_kinds
    graph_present = _GRAPH_KIND in artifact_kinds
    assets_present = bool(artifact_kinds & _ASSET_KINDS)
    quality_present = bool(artifact_kinds & _QUALITY_KINDS)
    raw_present = bool(artifact_kinds)

    # Quality also surfaces when the run carries:
    #   * warnings — the Quality tab renders them as a list, OR
    #   * skipped / failed-optional step_results — the projector
    #     emits `skippedSteps[]` / `failedOptionalSteps[]` from
    #     `run.metadata.step_results` regardless of whether any
    #     quality artifact landed. A run that skipped optional
    #     stages cleanly (no warnings, no enrichment artifacts)
    #     still has actionable rows for reviewers.
    if not quality_present and (run.warning_count or 0) > 0:
        quality_present = True
    if not quality_present and _has_actionable_step_results(run):
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
        # Content Inventory + Execution Plan tabs are ALWAYS clickable.
        # The tab content endpoints (`get_run_content_inventory` and
        # `get_run_planning`) already return a `status="unavailable"`
        # payload with an operator-readable reason when no manifest /
        # planning artifact exists for the run, and the FE renders
        # that reason as an empty-state inside the tab. Gating the tab
        # button on top of that produced two whole-class bugs:
        #   * The audit-log signal (`plan.revised` event) and the
        #     artifact signal (`planning_result` kind) were written
        #     by independent code paths; a deployment where one path
        #     ran but not the other left the tab disabled even when
        #     the data existed.
        #   * The artifact-tag run_id and the URL run_id had to match
        #     exactly; any post-compile re-tagging or lineage-fallback
        #     scenario silently disabled the tab.
        # Always-available means: one less place to lie. The tab
        # content takes responsibility for the empty state.
        parsed_content=AvailabilityDTO(available=True, reason=None),
        planning=AvailabilityDTO(available=True, reason=None),
    )


# ---- Reason strings ------------------------------------------------
#
# Reasons are intentionally short, neutral, and operator-readable. No
# vendor names. Re-used both in the Overview banner and the disabled-
# tab tooltip on the FE.

_FAILED_OR_CANCELLED_STATUSES = frozenset({"failed", "cancelled"})


def _has_actionable_step_results(run: IngestionRun) -> bool:
    """True when the run's `metadata.step_results` carries entries
    the Quality projector would surface (skipped steps or failed
    optional steps). Lets the Quality tab unlock on those signals
    even when no warnings or enrichment artifacts exist."""
    raw = run.metadata.get("step_results")
    if not isinstance(raw, list):
        return False
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status == "skipped":
            return True
        if status == "failed" and not entry.get("required", True):
            return True
    return False


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

    The availability resolver now always reports the tab as available
    — the tab content endpoint owns the empty-state messaging. This
    helper is kept because the content endpoint still imports it for
    its own `unavailable_reason` field when no manifest exists.

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


def _planning_reason(run: IngestionRun) -> str:
    """Operator-readable reason when the Planning Report tab has
    no data to show.

    The resolver no longer gates the tab on this — kept here because
    `get_run_planning` still imports it for its own
    `unavailable_reason` field when neither the artifact nor the
    audit-log event yields a plan.

    Three precedence rules mirror `_parsed_content_reason`:
      1. Run failed/cancelled before the planner emitted anything.
      2. Run is still pre-plan (created / assessing / waiting for
         confirmation) — the planner hasn't run yet.
      3. Terminal run with no plan event — typically a legacy run
         from before adaptive planning shipped, or a deployment that
         disabled planning entirely.
    """
    if str(run.status) in _FAILED_OR_CANCELLED_STATUSES:
        return (
            "Planner did not produce a plan before the run ended."
        )
    if str(run.status) in {"created", "assessing"}:
        return "Planner has not produced a plan yet."
    return (
        "Planning report is not available for this run "
        "(planner may be disabled or the run pre-dates the feature)."
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
