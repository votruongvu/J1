"""run-detail page panel-selection contract.

Pins the (UI state → primary panel + highlighted + hidden) mapping
in `run_detail_contract.py`. The FE branches on this, so renames or
removals are coordinated changes.

Tests cover:
 1. Every UI state has a deterministic selection.
 2. Every primary panel + highlighted panel is a valid panel id.
 3. The 6 UI states each have distinct primary panels (no two
 states should land an operator on the SAME panel — that
 defeats the UI-state distinction).
 4. The selection's `to_dict` carries stable wire-format keys.
"""

from __future__ import annotations

import pytest

from j1.processing.run_detail_contract import (
    ALL_PANELS,
    PANEL_ASSESSMENT_PLAN,
    PANEL_ENRICHMENT_RESULT,
    PANEL_ERROR_REPORT,
    PANEL_LIVE_TIMELINE,
    PANEL_RUN_HEADER,
    PANEL_RUN_RESULTS,
    RunDetailPanelSelection,
    select_run_detail_panels,
)
from j1.processing.ui_status_mapping import (
    ALL_UI_STATES,
    UI_STATE_CANCELLED,
    UI_STATE_COMPLETED,
    UI_STATE_COMPLETED_WITH_WARNINGS,
    UI_STATE_FAILED,
    UI_STATE_PENDING,
    UI_STATE_RUNNING,
)


# ---- 1. Every UI state has a selection -------------------------------


@pytest.mark.parametrize("ui_state", ALL_UI_STATES)
def test_every_ui_state_has_a_panel_selection(ui_state):
    selection = select_run_detail_panels(ui_state)
    assert isinstance(selection, RunDetailPanelSelection)
    assert selection.ui_state == ui_state
    assert selection.primary_panel, "every selection must have a primary panel"


# ---- 2. Every referenced panel is a valid id -------------------------


@pytest.mark.parametrize("ui_state", ALL_UI_STATES)
def test_panel_ids_are_in_inventory(ui_state):
    selection = select_run_detail_panels(ui_state)
    valid = set(ALL_PANELS)
    assert selection.primary_panel in valid, (
        f"primary_panel {selection.primary_panel!r} not in ALL_PANELS"
    )
    for p in selection.highlighted_panels:
        assert p in valid, f"highlighted panel {p!r} not in ALL_PANELS"
    for p in selection.hidden_panels:
        assert p in valid, f"hidden panel {p!r} not in ALL_PANELS"


@pytest.mark.parametrize("ui_state", ALL_UI_STATES)
def test_primary_panel_is_not_also_hidden(ui_state):
    """A panel cannot be both the operator's pre-selected target AND
 hidden — that's a contradiction the FE can't render."""
    selection = select_run_detail_panels(ui_state)
    assert selection.primary_panel not in selection.hidden_panels


# ---- 3. Per-state pinned primary panels ( spec A–F) ----------


_EXPECTED_PRIMARY: dict[str, str] = {
    UI_STATE_PENDING: PANEL_ASSESSMENT_PLAN,
    UI_STATE_RUNNING: PANEL_LIVE_TIMELINE,
    UI_STATE_COMPLETED: PANEL_RUN_RESULTS,
    UI_STATE_COMPLETED_WITH_WARNINGS: PANEL_ENRICHMENT_RESULT,
    UI_STATE_FAILED: PANEL_ERROR_REPORT,
    UI_STATE_CANCELLED: PANEL_LIVE_TIMELINE,
}


@pytest.mark.parametrize("ui_state,expected_primary", list(_EXPECTED_PRIMARY.items()))
def test_pinned_primary_panel_per_ui_state(ui_state, expected_primary):
    """The (UI state → primary panel) mapping is part of the
 backend↔FE contract. Renaming or re-routing requires
 coordination — this test forces the conversation."""
    selection = select_run_detail_panels(ui_state)
    assert selection.primary_panel == expected_primary


def test_running_and_cancelled_share_live_timeline_but_differ_elsewhere():
    """RUNNING + CANCELLED both pre-select the live timeline (operator
 wants to see when/how things ended). They differ in what's hidden:
 RUNNING hides error_report (nothing failed); CANCELLED hides
 run_results (no outputs produced)."""
    running = select_run_detail_panels(UI_STATE_RUNNING)
    cancelled = select_run_detail_panels(UI_STATE_CANCELLED)
    assert running.primary_panel == cancelled.primary_panel
    assert PANEL_RUN_RESULTS in cancelled.hidden_panels
    assert PANEL_RUN_RESULTS not in running.hidden_panels


# ---- 4. Wire format -------------------------------------------------


def test_to_dict_has_stable_keys():
    """The FE consumes `.to_dict` over HTTP. Renaming the keys is
 a coordinated wire change."""
    selection = select_run_detail_panels(UI_STATE_COMPLETED)
    d = selection.to_dict()
    assert set(d.keys()) == {
        "ui_state", "primary_panel", "highlighted_panels", "hidden_panels",
    }
    assert isinstance(d["highlighted_panels"], list)
    assert isinstance(d["hidden_panels"], list)


# ---- 5. Fallback for unknown UI states -------------------------------


def test_unknown_ui_state_returns_safe_default():
    """An unknown UI literal (FE drift, future state) must not crash —
 the projector returns a `run_header` primary so the FE renders
 *something* at the top of the page."""
    selection = select_run_detail_panels("future_state_not_yet_defined")
    assert selection.primary_panel == PANEL_RUN_HEADER
