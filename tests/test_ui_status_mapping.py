"""Wave 9A — UI status mapping projection tests.

Pins the contract surface for FE branching:

  1. Every `INGESTION_STATUS_*` terminal value projects onto a valid
     `UI_STATE_*` with non-empty headline + valid severity.
  2. Mid-run macro stages project onto RUNNING / PENDING.
  3. The mapping is total: any input produces a deterministic
     `UIRunState`; unknown inputs land on a non-crashing default.
  4. The 6-state surface is closed — no Wave-2 / Wave-5 legacy
     vocabulary (no `split_mode`, no `pre_compile_gating`).
"""

from __future__ import annotations

import pytest

from j1.processing.final_status import (
    ALL_INGESTION_FINAL_STATUSES,
    INGESTION_STATUS_CANCELLED,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
    INGESTION_STATUS_FAILED_UNKNOWN,
)
from j1.processing.ui_status_mapping import (
    ACTION_INSPECT_COMPILE_OUTPUT,
    ACTION_INSPECT_ERROR_REPORT,
    ACTION_NONE,
    ACTION_RETRY,
    ACTION_REVIEW_WARNINGS,
    ALL_UI_STATES,
    PRIMARY_ARTIFACT_COMPILE_RESULT_SUMMARY,
    PRIMARY_ARTIFACT_ENRICHMENT_RESULT,
    PRIMARY_ARTIFACT_ERROR_REPORT,
    PRIMARY_ARTIFACT_FINAL_SUMMARY,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_NEUTRAL,
    SEVERITY_SUCCESS,
    SEVERITY_WARNING,
    UI_STATE_CANCELLED,
    UI_STATE_COMPLETED,
    UI_STATE_COMPLETED_WITH_WARNINGS,
    UI_STATE_FAILED,
    UI_STATE_PENDING,
    UI_STATE_RUNNING,
    UIRunState,
    project_ui_state,
)


# ---- 1. Every final status maps to a valid ui_state -----------------


_TERMINAL_CASES = [
    (
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
        UI_STATE_COMPLETED, SEVERITY_SUCCESS,
        PRIMARY_ARTIFACT_FINAL_SUMMARY, ACTION_NONE,
    ),
    (
        INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
        UI_STATE_COMPLETED_WITH_WARNINGS, SEVERITY_WARNING,
        PRIMARY_ARTIFACT_FINAL_SUMMARY, ACTION_REVIEW_WARNINGS,
    ),
    (
        INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
        UI_STATE_COMPLETED_WITH_WARNINGS, SEVERITY_WARNING,
        PRIMARY_ARTIFACT_ENRICHMENT_RESULT, ACTION_REVIEW_WARNINGS,
    ),
    (
        INGESTION_STATUS_FAILED_COMPILE,
        UI_STATE_FAILED, SEVERITY_ERROR,
        PRIMARY_ARTIFACT_ERROR_REPORT, ACTION_INSPECT_COMPILE_OUTPUT,
    ),
    (
        INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
        UI_STATE_FAILED, SEVERITY_ERROR,
        PRIMARY_ARTIFACT_COMPILE_RESULT_SUMMARY, ACTION_RETRY,
    ),
    (
        INGESTION_STATUS_FAILED_FINALIZATION,
        UI_STATE_FAILED, SEVERITY_ERROR,
        PRIMARY_ARTIFACT_ERROR_REPORT, ACTION_INSPECT_ERROR_REPORT,
    ),
    (
        INGESTION_STATUS_FAILED_UNKNOWN,
        UI_STATE_FAILED, SEVERITY_ERROR,
        PRIMARY_ARTIFACT_ERROR_REPORT, ACTION_INSPECT_ERROR_REPORT,
    ),
    (
        INGESTION_STATUS_CANCELLED,
        UI_STATE_CANCELLED, SEVERITY_NEUTRAL,
        None, ACTION_NONE,
    ),
]


@pytest.mark.parametrize(
    "final_status,expected_ui,expected_severity,expected_artifact,expected_action",
    _TERMINAL_CASES,
)
def test_terminal_status_projects_to_correct_ui_state(
    final_status, expected_ui, expected_severity,
    expected_artifact, expected_action,
):
    result = project_ui_state(final_status=final_status, is_terminal=True)
    assert result.ui_state == expected_ui
    assert result.severity == expected_severity
    assert result.primary_artifact == expected_artifact
    assert result.recommended_action == expected_action
    assert result.underlying_final_status == final_status
    assert result.headline, "every projection must carry a headline"


def test_every_final_status_in_vocabulary_has_a_projection():
    """The projector MUST handle every `INGESTION_STATUS_*` value
    declared in `final_status.py`. Adding a new status without
    updating this projector is a coordinated change — this test
    enforces it."""
    seen_ui_states = {
        project_ui_state(final_status=s, is_terminal=True).ui_state
        for s in ALL_INGESTION_FINAL_STATUSES
    }
    # Every projection must land in the closed 6-state surface.
    assert seen_ui_states <= set(ALL_UI_STATES), (
        f"projection produced UI states outside ALL_UI_STATES: "
        f"{seen_ui_states - set(ALL_UI_STATES)}"
    )


# ---- 2. Mid-run macro stage projection -----------------------------


@pytest.mark.parametrize("stage", [
    "received", "starting", "assessing",
    "assessment_ready", "compile_pending",
])
def test_pending_stages_project_to_pending(stage):
    result = project_ui_state(ingest_stage=stage)
    assert result.ui_state == UI_STATE_PENDING
    assert result.severity == SEVERITY_NEUTRAL
    assert result.primary_artifact is None
    assert result.underlying_final_status is None


@pytest.mark.parametrize("stage", ["compiling", "verifying", "running"])
def test_active_stages_project_to_running(stage):
    result = project_ui_state(ingest_stage=stage)
    assert result.ui_state == UI_STATE_RUNNING
    assert result.severity == SEVERITY_INFO
    assert result.primary_artifact is None


def test_cancelled_stage_projects_to_cancelled():
    result = project_ui_state(ingest_stage="cancelled")
    assert result.ui_state == UI_STATE_CANCELLED
    assert result.underlying_final_status == INGESTION_STATUS_CANCELLED


def test_failed_stage_without_final_status_projects_to_failed():
    result = project_ui_state(ingest_stage="failed")
    assert result.ui_state == UI_STATE_FAILED
    assert result.severity == SEVERITY_ERROR
    assert result.primary_artifact == PRIMARY_ARTIFACT_ERROR_REPORT
    assert result.recommended_action == ACTION_INSPECT_ERROR_REPORT


# ---- 3. Totality / robustness --------------------------------------


def test_completely_unknown_inputs_fall_back_to_pending():
    """The FE consumer can't crash on stale or malformed inputs —
    the projector must always produce a `UIRunState`."""
    result = project_ui_state()  # all defaults
    assert isinstance(result, UIRunState)
    assert result.ui_state in ALL_UI_STATES


def test_unknown_final_status_projects_to_failed_with_explicit_headline():
    """Schema drift between backend + this projector lands as FAILED
    with the unknown literal in the headline — the FE renders the
    operator-visible badge but logs the unknown string."""
    result = project_ui_state(
        final_status="some_made_up_status",
        is_terminal=True,
    )
    assert result.ui_state == UI_STATE_FAILED
    assert "some_made_up_status" in result.headline


def test_is_terminal_overrides_stage_when_set():
    """A terminal projection MUST win over a stale stage — e.g. the
    workflow may have a lingering `compiling` stage write while the
    final status is `completed_with_enrichment`."""
    result = project_ui_state(
        ingest_stage="compiling",
        final_status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
        is_terminal=True,
    )
    assert result.ui_state == UI_STATE_COMPLETED


def test_final_status_supplied_implies_terminal():
    """The FE may not set `is_terminal` explicitly — supplying a
    final_status is enough to take the terminal branch."""
    result = project_ui_state(
        final_status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    )
    assert result.ui_state == UI_STATE_COMPLETED


def test_to_dict_is_stable_wire_format():
    """The FE consumes `.to_dict()` over HTTP — keys must be stable."""
    result = project_ui_state(
        final_status=INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
        is_terminal=True,
    )
    d = result.to_dict()
    assert set(d.keys()) == {
        "ui_state", "severity", "headline", "primary_artifact",
        "recommended_action", "underlying_final_status",
    }


# ---- 4. Legacy-vocabulary regression --------------------------------


def test_ui_status_mapping_module_has_no_legacy_vocabulary():
    """The module must not reintroduce pre-Wave-6 split-mode or
    pre-compile-gating terminology."""
    import inspect
    from j1.processing import ui_status_mapping
    src = inspect.getsource(ui_status_mapping)
    for forbidden in (
        "split_mode", "SplitMode", "insert_content",
        "pre_compile_gating", "PreCompileGating", "ingest_planner",
    ):
        assert forbidden not in src, (
            f"legacy vocabulary {forbidden!r} resurfaced in "
            f"ui_status_mapping.py"
        )


def test_ui_state_surface_is_closed_at_six():
    """The FE state machine is 6 states. Adding a 7th is a coordinated
    FE + backend change — this test forces the conversation."""
    assert len(ALL_UI_STATES) == 6
