"""shared UI status mapping.

The backend writes two stable vocabularies the FE depends on:

 * `J1IngestStage` — macro-stage during run (see
 `project_processing.INGEST_STAGE_*`: `received`, `compiling`,
 `verifying`, `completed`, `failed`, `cancelled`, …).
 * `INGESTION_STATUS_*` — the final-status projection
 (`completed_with_enrichment`, `failed_enrichment_required`, …)
 persisted on the final-summary artifact.

The FE branches on a SINGLE UI-state enum it can render — `running`,
`completed`, `completed_with_warnings`, `failed`, `cancelled`,
`pending`. This module is the single source of truth for that
projection so the FE state machine + backend audit log stay in
lockstep.

Inputs the projector consumes:

 * `ingest_stage` — the latest `J1IngestStage` value.
 * `final_status` — the `INGESTION_STATUS_*` string (only set
 when the run reached a terminal state).
 * `is_terminal` — True once the workflow has run to a
 conclusion (completed / failed / cancelled).

Output is a typed `UIRunState` carrying:

 * `ui_state` — one of the `UI_STATE_*` literals.
 * `severity` — `info` / `success` / `warning` / `error` /
 `neutral` — FE renders the badge colour.
 * `headline` — short operator-readable line for the badge.
 * `primary_artifact` — which artifact tab the FE pre-selects on
 the run-detail page (`final_summary`,
 `compile_result_summary`,
 `enrichment_result`, `error_report`, or
 None for in-flight runs).
 * `recommended_action` — explicit verb the FE can attach to a CTA
 (`none`, `review_warnings`,
 `inspect_error_report`,
 `inspect_compile_output`, `retry`).
 * `underlying_final_status` — the projected `INGESTION_STATUS_*`
 literal when terminal, else None.

Pure / deterministic. Same inputs → same projection. The mapping is
testable + AST-checkable so renames are caught at CI time.
"""

from __future__ import annotations

from dataclasses import dataclass

from j1.processing.final_status import (
    INGESTION_STATUS_CANCELLED,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT,
    INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS,
    INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT,
    INGESTION_STATUS_FAILED_COMPILE,
    INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED,
    INGESTION_STATUS_FAILED_FINALIZATION,
    INGESTION_STATUS_FAILED_UNKNOWN,
)


__all__ = [
    "UI_STATE_PENDING",
    "UI_STATE_RUNNING",
    "UI_STATE_COMPLETED",
    "UI_STATE_COMPLETED_WITH_WARNINGS",
    "UI_STATE_FAILED",
    "UI_STATE_CANCELLED",
    "ALL_UI_STATES",
    "SEVERITY_INFO",
    "SEVERITY_SUCCESS",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "SEVERITY_NEUTRAL",
    "PRIMARY_ARTIFACT_FINAL_SUMMARY",
    "PRIMARY_ARTIFACT_COMPILE_RESULT_SUMMARY",
    "PRIMARY_ARTIFACT_ENRICHMENT_RESULT",
    "PRIMARY_ARTIFACT_ERROR_REPORT",
    "ACTION_NONE",
    "ACTION_REVIEW_WARNINGS",
    "ACTION_INSPECT_ERROR_REPORT",
    "ACTION_INSPECT_COMPILE_OUTPUT",
    "ACTION_RETRY",
    "UIRunState",
    "project_ui_state",
]


# ---- UI states (6 — matches the FE state machine) ----------------

# Workflow accepted, no compile attempt yet (queued / awaiting
# trigger in two-phase mode).
UI_STATE_PENDING = "pending"
# Compile / verification / enrichment is actively in flight.
UI_STATE_RUNNING = "running"
# Reached a terminal state with no warnings + no errors.
UI_STATE_COMPLETED = "completed"
# Reached a terminal state but enrichment produced warnings OR was
# skipped entirely (the FE treats both as "look at the result").
UI_STATE_COMPLETED_WITH_WARNINGS = "completed_with_warnings"
# Reached a terminal state via any failure path (compile, finalize,
# required enrichment, unknown).
UI_STATE_FAILED = "failed"
# Operator cancelled the run.
UI_STATE_CANCELLED = "cancelled"


ALL_UI_STATES: tuple[str, ...] = (
    UI_STATE_PENDING,
    UI_STATE_RUNNING,
    UI_STATE_COMPLETED,
    UI_STATE_COMPLETED_WITH_WARNINGS,
    UI_STATE_FAILED,
    UI_STATE_CANCELLED,
)


# ---- Severity (FE renders the badge colour from this) ------------

SEVERITY_INFO = "info"
SEVERITY_SUCCESS = "success"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_NEUTRAL = "neutral"


# ---- Primary artifact kinds (FE pre-selects this tab) ------------

PRIMARY_ARTIFACT_FINAL_SUMMARY = "final_summary"
PRIMARY_ARTIFACT_COMPILE_RESULT_SUMMARY = "compile_result_summary"
PRIMARY_ARTIFACT_ENRICHMENT_RESULT = "enrichment_result"
PRIMARY_ARTIFACT_ERROR_REPORT = "error_report"


# ---- Recommended actions -----------------------------------------

ACTION_NONE = "none"
ACTION_REVIEW_WARNINGS = "review_warnings"
ACTION_INSPECT_ERROR_REPORT = "inspect_error_report"
ACTION_INSPECT_COMPILE_OUTPUT = "inspect_compile_output"
ACTION_RETRY = "retry"


# ---- Macro-stage values the workflow writes (mirrored from
# project_processing.py — kept as plain strings here so this module
# stays import-cycle-free with the workflow).
_INGEST_STAGE_RECEIVED = "received"
_INGEST_STAGE_ASSESSING = "assessing"
_INGEST_STAGE_ASSESSMENT_READY = "assessment_ready"
_INGEST_STAGE_COMPILE_PENDING = "compile_pending"
_INGEST_STAGE_COMPILING = "compiling"
_INGEST_STAGE_VERIFYING = "verifying"
_INGEST_STAGE_RUNNING = "running"
_INGEST_STAGE_STARTING = "starting"
_INGEST_STAGE_CANCELLED = "cancelled"
_INGEST_STAGE_COMPLETED = "completed"
_INGEST_STAGE_FAILED = "failed"

# Stages the projector treats as "still in flight pre-compile" — the
# FE renders these as PENDING (the run exists but compile hasn't
# started).
_PENDING_STAGES: frozenset[str] = frozenset({
    _INGEST_STAGE_RECEIVED,
    _INGEST_STAGE_STARTING,
    _INGEST_STAGE_ASSESSING,
    _INGEST_STAGE_ASSESSMENT_READY,
    _INGEST_STAGE_COMPILE_PENDING,
})


@dataclass(frozen=True)
class UIRunState:
    """Typed projection the FE consumes via the run-detail endpoint.

 Pure data — same inputs to `project_ui_state` produce the same
 `UIRunState`."""

    ui_state: str
    severity: str
    headline: str
    primary_artifact: str | None
    recommended_action: str
    underlying_final_status: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "ui_state": self.ui_state,
            "severity": self.severity,
            "headline": self.headline,
            "primary_artifact": self.primary_artifact,
            "recommended_action": self.recommended_action,
            "underlying_final_status": self.underlying_final_status,
        }


def project_ui_state(
    *,
    ingest_stage: str | None = None,
    final_status: str | None = None,
    is_terminal: bool = False,
) -> UIRunState:
    """Project the macro stage + final-status string onto a UI state.

 Precedence:
 1. `is_terminal=True` (or a terminal final_status) wins —
 project off the final-status vocabulary.
 2. Otherwise, project off the macro stage.

 Unknown / missing inputs fall back to PENDING with a neutral
 badge — the FE renders "starting up" rather than crashing on a
 missing field."""

    # ---- Terminal projection (final_status wins when known) -----
    if is_terminal or final_status:
        return _project_terminal(final_status or "")

    stage = (ingest_stage or "").strip().lower()

    if stage in _PENDING_STAGES:
        return UIRunState(
            ui_state=UI_STATE_PENDING,
            severity=SEVERITY_NEUTRAL,
            headline="run accepted; waiting to start",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
        )

    if stage == _INGEST_STAGE_COMPILING:
        return UIRunState(
            ui_state=UI_STATE_RUNNING,
            severity=SEVERITY_INFO,
            headline="compiling document",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
        )

    if stage == _INGEST_STAGE_VERIFYING:
        return UIRunState(
            ui_state=UI_STATE_RUNNING,
            severity=SEVERITY_INFO,
            headline="verifying compile output",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
        )

    if stage == _INGEST_STAGE_RUNNING:
        return UIRunState(
            ui_state=UI_STATE_RUNNING,
            severity=SEVERITY_INFO,
            headline="run in progress",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
        )

    if stage == _INGEST_STAGE_CANCELLED:
        return UIRunState(
            ui_state=UI_STATE_CANCELLED,
            severity=SEVERITY_NEUTRAL,
            headline="run cancelled by operator",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
            underlying_final_status=INGESTION_STATUS_CANCELLED,
        )

    if stage == _INGEST_STAGE_COMPLETED:
        # Terminal completed stage but no final-status string yet —
        # the projector landed mid-write. Render as RUNNING so the
        # FE doesn't flash a misleading "clean" badge.
        return UIRunState(
            ui_state=UI_STATE_RUNNING,
            severity=SEVERITY_INFO,
            headline="finalising run",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
        )

    if stage == _INGEST_STAGE_FAILED:
        return UIRunState(
            ui_state=UI_STATE_FAILED,
            severity=SEVERITY_ERROR,
            headline="run failed",
            primary_artifact=PRIMARY_ARTIFACT_ERROR_REPORT,
            recommended_action=ACTION_INSPECT_ERROR_REPORT,
            underlying_final_status=INGESTION_STATUS_FAILED_UNKNOWN,
        )

    # Unknown / missing → pending, no badge promise.
    return UIRunState(
        ui_state=UI_STATE_PENDING,
        severity=SEVERITY_NEUTRAL,
        headline="run starting",
        primary_artifact=None,
        recommended_action=ACTION_NONE,
    )


def _project_terminal(final_status: str) -> UIRunState:
    """Terminal-state branch — `final_status` is the
 `INGESTION_STATUS_*` literal from the projection."""

    if final_status == INGESTION_STATUS_CANCELLED:
        return UIRunState(
            ui_state=UI_STATE_CANCELLED,
            severity=SEVERITY_NEUTRAL,
            headline="run cancelled by operator",
            primary_artifact=None,
            recommended_action=ACTION_NONE,
            underlying_final_status=INGESTION_STATUS_CANCELLED,
        )

    if final_status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT:
        return UIRunState(
            ui_state=UI_STATE_COMPLETED,
            severity=SEVERITY_SUCCESS,
            headline="completed with enrichment",
            primary_artifact=PRIMARY_ARTIFACT_FINAL_SUMMARY,
            recommended_action=ACTION_NONE,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_COMPLETED_WITHOUT_ENRICHMENT:
        return UIRunState(
            ui_state=UI_STATE_COMPLETED_WITH_WARNINGS,
            severity=SEVERITY_WARNING,
            headline="completed without enrichment",
            primary_artifact=PRIMARY_ARTIFACT_FINAL_SUMMARY,
            recommended_action=ACTION_REVIEW_WARNINGS,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_COMPLETED_WITH_ENRICHMENT_WARNINGS:
        return UIRunState(
            ui_state=UI_STATE_COMPLETED_WITH_WARNINGS,
            severity=SEVERITY_WARNING,
            headline="completed with warnings",
            primary_artifact=PRIMARY_ARTIFACT_ENRICHMENT_RESULT,
            recommended_action=ACTION_REVIEW_WARNINGS,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_FAILED_COMPILE:
        return UIRunState(
            ui_state=UI_STATE_FAILED,
            severity=SEVERITY_ERROR,
            headline="compile failed",
            primary_artifact=PRIMARY_ARTIFACT_ERROR_REPORT,
            recommended_action=ACTION_INSPECT_COMPILE_OUTPUT,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_FAILED_ENRICHMENT_REQUIRED:
        return UIRunState(
            ui_state=UI_STATE_FAILED,
            severity=SEVERITY_ERROR,
            headline="required enrichment did not complete",
            primary_artifact=PRIMARY_ARTIFACT_COMPILE_RESULT_SUMMARY,
            recommended_action=ACTION_RETRY,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_FAILED_FINALIZATION:
        return UIRunState(
            ui_state=UI_STATE_FAILED,
            severity=SEVERITY_ERROR,
            headline="finalize failed after a successful pipeline",
            primary_artifact=PRIMARY_ARTIFACT_ERROR_REPORT,
            recommended_action=ACTION_INSPECT_ERROR_REPORT,
            underlying_final_status=final_status,
        )

    if final_status == INGESTION_STATUS_FAILED_UNKNOWN:
        return UIRunState(
            ui_state=UI_STATE_FAILED,
            severity=SEVERITY_ERROR,
            headline="run failed",
            primary_artifact=PRIMARY_ARTIFACT_ERROR_REPORT,
            recommended_action=ACTION_INSPECT_ERROR_REPORT,
            underlying_final_status=final_status,
        )

    # Unknown terminal final-status string — surface as FAILED with
    # neutral copy so an FE bug or schema drift doesn't render a
    # green badge. The FE consumer logs the unknown literal so we
    # catch the drift in monitoring.
    return UIRunState(
        ui_state=UI_STATE_FAILED,
        severity=SEVERITY_ERROR,
        headline=f"run ended in unrecognised state: {final_status!r}",
        primary_artifact=PRIMARY_ARTIFACT_ERROR_REPORT,
        recommended_action=ACTION_INSPECT_ERROR_REPORT,
        underlying_final_status=final_status or None,
    )
