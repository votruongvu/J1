"""run-detail page data-model contract.

The FE run-detail page renders a fixed set of panels in a fixed
order. Which panels are *highlighted* vs *secondary* depends on the
run's UI state (see `ui_status_mapping.UIRunState`).

This module is the single source of truth for that mapping so the
backend, the FE, and the audit-log dashboard agree on which panel
gets the operator's attention for each status.

The full panel inventory the run-detail page CAN render:

 * `run_header` — RunHeader.tsx (status badge,
 run id, started/ended timestamps).
 * `primary_status` — PrimaryStatusPanel.tsx (headline +
 recommended action).
 * `assessment_plan` — AssessmentPlanPanel.tsx (the
 pre-compile cheap profile).
 * `initial_execution_plan` — InitialExecutionPlan artifact
 (domain pack + enrichment policy
 + candidate modules).
 * `compile_strategy` — CompileStrategyPanel.tsx (mode +
 retry attempts + quality).
 * `compile_result` — typed NormalizedCompileResult
 (chunks_count, detected_tables, …).
 * `enrich_plan` — EnrichPlanPanel.tsx (post-compile
 rule-based + fast-LLM recommendation).
 * `enrichment_result` — typed EnrichmentResult overlay
 (per-module outcomes + provenance).
 * `live_timeline` — LiveTimeline.tsx (SSE-driven event
 stream + macro-stage grouping).
 * `run_results` — RunResults tab cluster (Overview /
 Quality / Validation / Assets / Raw).
 * `error_report` — error_report artifact.
 * `tech_drawer` — TechDrawer.tsx (advanced
 operator-only view).

For each UI state we return:

 * `primary_panel` — the one panel the FE pre-selects /
 pre-scrolls to (operator's
 attention sink).
 * `highlighted_panels` — panels the FE marks "important"
 (badge / chevron / colored border).
 * `hidden_panels` — panels the FE collapses or hides
 because they'd carry stale info.

Pure / deterministic. No I/O. Same UI state → same panel selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from j1.processing.ui_status_mapping import (
    UI_STATE_CANCELLED,
    UI_STATE_COMPLETED,
    UI_STATE_COMPLETED_WITH_WARNINGS,
    UI_STATE_FAILED,
    UI_STATE_PENDING,
    UI_STATE_RUNNING,
)


__all__ = [
    "PANEL_ASSESSMENT_PLAN",
    "PANEL_COMPILE_RESULT",
    "PANEL_COMPILE_STRATEGY",
    "PANEL_ENRICH_PLAN",
    "PANEL_ENRICHMENT_RESULT",
    "PANEL_ERROR_REPORT",
    "PANEL_INITIAL_EXECUTION_PLAN",
    "PANEL_LIVE_TIMELINE",
    "PANEL_PRIMARY_STATUS",
    "PANEL_RUN_HEADER",
    "PANEL_RUN_RESULTS",
    "PANEL_TECH_DRAWER",
    "ALL_PANELS",
    "RunDetailPanelSelection",
    "select_run_detail_panels",
]


# ---- Panel inventory (stable IDs) ---------------------------------

PANEL_RUN_HEADER = "run_header"
PANEL_PRIMARY_STATUS = "primary_status"
PANEL_ASSESSMENT_PLAN = "assessment_plan"
PANEL_INITIAL_EXECUTION_PLAN = "initial_execution_plan"
PANEL_COMPILE_STRATEGY = "compile_strategy"
PANEL_COMPILE_RESULT = "compile_result"
PANEL_ENRICH_PLAN = "enrich_plan"
PANEL_ENRICHMENT_RESULT = "enrichment_result"
PANEL_LIVE_TIMELINE = "live_timeline"
PANEL_RUN_RESULTS = "run_results"
PANEL_ERROR_REPORT = "error_report"
PANEL_TECH_DRAWER = "tech_drawer"


ALL_PANELS: tuple[str, ...] = (
    PANEL_RUN_HEADER,
    PANEL_PRIMARY_STATUS,
    PANEL_ASSESSMENT_PLAN,
    PANEL_INITIAL_EXECUTION_PLAN,
    PANEL_COMPILE_STRATEGY,
    PANEL_COMPILE_RESULT,
    PANEL_ENRICH_PLAN,
    PANEL_ENRICHMENT_RESULT,
    PANEL_LIVE_TIMELINE,
    PANEL_RUN_RESULTS,
    PANEL_ERROR_REPORT,
    PANEL_TECH_DRAWER,
)


@dataclass(frozen=True)
class RunDetailPanelSelection:
    """Typed FE-facing panel selection for one run state.

 `primary_panel` is the panel the FE pre-selects when the page
 loads. `highlighted_panels` carry a marker the FE renders as a
 badge / coloured border. `hidden_panels` are collapsed in the
 accordion (still accessible by click — never load-bearing for
 audit). Pure data."""

    ui_state: str
    primary_panel: str
    highlighted_panels: tuple[str, ...] = ()
    hidden_panels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, str | list[str]]:
        return {
            "ui_state": self.ui_state,
            "primary_panel": self.primary_panel,
            "highlighted_panels": list(self.highlighted_panels),
            "hidden_panels": list(self.hidden_panels),
        }


def select_run_detail_panels(ui_state: str) -> RunDetailPanelSelection:
    """Project a `UI_STATE_*` literal onto the run-detail page's
 panel selection. The FE consumes this to decide which panel to
 pre-scroll to + which to dim.

 Behaviour per UI state ( spec, A–F):

 A. PENDING — primary: assessment_plan
 (operator wants to confirm the run scope while waiting).
 B. RUNNING — primary: live_timeline
 (operator wants live progress).
 C. COMPLETED — primary: run_results
 (operator wants to inspect outputs).
 D. COMPLETED_WITH_WARNINGS — primary: enrichment_result
 (operator wants to see which modules warned).
 E. FAILED — primary: error_report
 (operator wants to triage the failure).
 F. CANCELLED — primary: live_timeline
 (operator wants to see when/why the cancel landed).

 Unknown states fall back to RUN_HEADER as a non-crashing default."""

    if ui_state == UI_STATE_PENDING:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_PENDING,
            primary_panel=PANEL_ASSESSMENT_PLAN,
            highlighted_panels=(PANEL_INITIAL_EXECUTION_PLAN,),
            hidden_panels=(
                PANEL_COMPILE_RESULT, PANEL_ENRICHMENT_RESULT,
                PANEL_RUN_RESULTS, PANEL_ERROR_REPORT,
            ),
        )

    if ui_state == UI_STATE_RUNNING:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_RUNNING,
            primary_panel=PANEL_LIVE_TIMELINE,
            highlighted_panels=(PANEL_PRIMARY_STATUS,),
            hidden_panels=(PANEL_ERROR_REPORT,),
        )

    if ui_state == UI_STATE_COMPLETED:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_COMPLETED,
            primary_panel=PANEL_RUN_RESULTS,
            highlighted_panels=(
                PANEL_COMPILE_RESULT, PANEL_ENRICHMENT_RESULT,
            ),
            hidden_panels=(PANEL_ERROR_REPORT,),
        )

    if ui_state == UI_STATE_COMPLETED_WITH_WARNINGS:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_COMPLETED_WITH_WARNINGS,
            primary_panel=PANEL_ENRICHMENT_RESULT,
            highlighted_panels=(
                PANEL_ENRICH_PLAN, PANEL_COMPILE_RESULT,
                PANEL_PRIMARY_STATUS,
            ),
            hidden_panels=(),
        )

    if ui_state == UI_STATE_FAILED:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_FAILED,
            primary_panel=PANEL_ERROR_REPORT,
            highlighted_panels=(
                PANEL_PRIMARY_STATUS, PANEL_LIVE_TIMELINE,
                PANEL_COMPILE_RESULT,
            ),
            hidden_panels=(),
        )

    if ui_state == UI_STATE_CANCELLED:
        return RunDetailPanelSelection(
            ui_state=UI_STATE_CANCELLED,
            primary_panel=PANEL_LIVE_TIMELINE,
            highlighted_panels=(PANEL_PRIMARY_STATUS,),
            hidden_panels=(PANEL_ERROR_REPORT, PANEL_RUN_RESULTS),
        )

    # Unknown / future UI state — return a non-crashing default.
    return RunDetailPanelSelection(
        ui_state=ui_state,
        primary_panel=PANEL_RUN_HEADER,
    )
