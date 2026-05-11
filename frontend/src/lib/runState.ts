/**
 * frontend port of the backend status projection.
 *
 * Mirrors:
 * - `src/j1/processing/final_status.py::project_final_status`
 * - `src/j1/processing/ui_status_mapping.py::project_ui_state`
 *
 * Lets the run-detail page compute the FE state machine (PENDING /
 * RUNNING / COMPLETED / COMPLETED_WITH_WARNINGS / FAILED / CANCELLED)
 * and the underlying `INGESTION_STATUS_*` literal from the data the
 * FE already has — `run.status`, `run.final`, and the per-stage
 * artifact responses. No new backend endpoint is required.
 *
 * Stays in lock-step with the Python source. Tests in
 * `__tests__/runState.test.ts` pin the (input → output) mapping
 * against the same cases the backend test pins.
 */

import {
  COMPLETED_STATUSES,
  REVIEW_STATUSES,
  RUN_STATUS,
  RUNNING_STATUSES,
  WARNING_STATUSES,
  type RunStatus,
} from "@/lib/constants/runStatus";
import type { IngestionRun, RunFinal } from "@/types/ingestion";
import type { FinalIngestionReportPayload } from "@/types/review";


// ---- final-status vocabulary (mirrors final_status.py) -----

export const INGESTION_STATUS = {
  COMPLETED_WITHOUT_ENRICHMENT: "completed_without_enrichment",
  COMPLETED_WITH_ENRICHMENT: "completed_with_enrichment",
  COMPLETED_WITH_ENRICHMENT_WARNINGS: "completed_with_enrichment_warnings",
  FAILED_COMPILE: "failed_compile",
  FAILED_ENRICHMENT_REQUIRED: "failed_enrichment_required",
  FAILED_FINALIZATION: "failed_finalization",
  FAILED_UNKNOWN: "failed",
  CANCELLED: "cancelled",
} as const;

export type IngestionFinalStatus =
  (typeof INGESTION_STATUS)[keyof typeof INGESTION_STATUS];


export const ALL_INGESTION_FINAL_STATUSES: readonly IngestionFinalStatus[] = [
  INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT,
  INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT,
  INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS,
  INGESTION_STATUS.FAILED_COMPILE,
  INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED,
  INGESTION_STATUS.FAILED_FINALIZATION,
  INGESTION_STATUS.FAILED_UNKNOWN,
  INGESTION_STATUS.CANCELLED,
];


// ---- UI state surface (mirrors ui_status_mapping.py) --------------

export const UI_STATE = {
  PENDING: "pending",
  RUNNING: "running",
  COMPLETED: "completed",
  COMPLETED_WITH_WARNINGS: "completed_with_warnings",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const;

export type UiState = (typeof UI_STATE)[keyof typeof UI_STATE];


export const ALL_UI_STATES: readonly UiState[] = [
  UI_STATE.PENDING,
  UI_STATE.RUNNING,
  UI_STATE.COMPLETED,
  UI_STATE.COMPLETED_WITH_WARNINGS,
  UI_STATE.FAILED,
  UI_STATE.CANCELLED,
];


export type UiSeverity = "info" | "success" | "warning" | "error" | "neutral";

export type PrimaryArtifact =
  | "final_summary"
  | "compile_result_summary"
  | "enrichment_result"
  | "error_report"
  | null;

export type RecommendedAction =
  | "none"
  | "review_warnings"
  | "inspect_error_report"
  | "inspect_compile_output"
  | "retry";


/**
 * The typed FE-facing projection mirroring `UIRunState` in
 * `ui_status_mapping.py`. Pure data — components branch on this
 * one struct instead of re-running predicates on raw status.
 */
export interface UiRunState {
  uiState: UiState;
  severity: UiSeverity;
  headline: string;
  primaryArtifact: PrimaryArtifact;
  recommendedAction: RecommendedAction;
  /** The `INGESTION_STATUS_*` literal when terminal; null while in-flight. */
  underlyingFinalStatus: IngestionFinalStatus | null;
}


// ---- Inputs we project from -------------------------------------

/**
 * Snapshot of the post-compile enrichment-result artifact's status
 * + the resolved policy flags. Only consumed when terminal and
 * compile succeeded. All fields optional so partial data still
 * projects cleanly.
 */
export interface EnrichmentSignals {
  /** `succeeded` / `succeeded_with_warnings` / `failed` / `skipped`. */
  status?: "succeeded" | "succeeded_with_warnings" | "failed" | "skipped";
  /** Operator-readable reason for a skip (FE renders alongside the badge). */
  skippedReason?: string;
  /** Whether the active policy requires enrichment success. */
  requireEnrichmentSuccess?: boolean;
}


// ---- Final-status projection (port of `project_final_status`) ----

export function projectFinalStatus(
  run: Pick<IngestionRun, "status" | "final">,
  enrichment: EnrichmentSignals = {},
): IngestionFinalStatus {
  const status = run.status;
  const final: RunFinal | undefined | null = run.final;
  const failureCode = (final?.failure_code || "").toUpperCase();

  // Cancelled wins on the cancellation path.
  if (status === RUN_STATUS.CANCELLED) {
    return INGESTION_STATUS.CANCELLED;
  }

  // Specific failure codes win when present.
  if (failureCode === "ENRICHMENT_REQUIRED") {
    return INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED;
  }
  if (
    failureCode === "COMPILE_FAILED"
    || failureCode === "CHUNK_FAILED"
    || failureCode === "INDEX_FAILED"
    || failureCode === "VERIFICATION_FAILED"
    || failureCode === "EMPTY_DOCUMENT"
  ) {
    return INGESTION_STATUS.FAILED_COMPILE;
  }
  if (failureCode === "FINALIZATION_FAILED") {
    return INGESTION_STATUS.FAILED_FINALIZATION;
  }

  // Other terminal failures fall through to UNKNOWN.
  if (status === RUN_STATUS.FAILED) {
    return INGESTION_STATUS.FAILED_UNKNOWN;
  }

  // Success-ish paths — refine via enrichment signals.
  if (enrichment.status === "skipped") {
    return INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT;
  }
  if (enrichment.status === "failed" && !enrichment.requireEnrichmentSuccess) {
    return INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS;
  }
  if (
    enrichment.status === "succeeded_with_warnings"
    || WARNING_STATUSES.has(status)
  ) {
    return INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS;
  }
  if (enrichment.status === "succeeded") {
    return INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT;
  }
  if (COMPLETED_STATUSES.has(status)) {
    // Completed with no enrichment signals — legacy or
    // enrichment-skipped-without-artifact run.
    return INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT;
  }

  // In-flight (no terminal status yet). Caller decides what to do
  // with this — `projectUiState` treats absent final-status as
  // "use ingest stage instead".
  return INGESTION_STATUS.FAILED_UNKNOWN;
}


// ---- UI state projection (port of `project_ui_state`) -----------

/**
 * Project a run into the 6-state UI surface. Caller supplies
 * `enrichment` when the enrichment-result artifact is available
 * (post-compile only). For in-flight runs, leave it empty — the
 * projector reads the run's status enum to pick the running /
 * pending bucket.
 */
export function projectUiState(
  run: Pick<IngestionRun, "status" | "final">,
  enrichment: EnrichmentSignals = {},
): UiRunState {
  const status = run.status;
  const isTerminal = isTerminalStatus(status);

  if (status === RUN_STATUS.CANCELLED || status === RUN_STATUS.CANCELLING) {
    return {
      uiState: UI_STATE.CANCELLED,
      severity: "neutral",
      headline: "run cancelled by operator",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: INGESTION_STATUS.CANCELLED,
    };
  }

  if (REVIEW_STATUSES.has(status)) {
    return {
      uiState: UI_STATE.PENDING,
      severity: "warning",
      headline: "awaiting human review",
      primaryArtifact: null,
      recommendedAction: "review_warnings",
      underlyingFinalStatus: null,
    };
  }

  if (isTerminal) {
    const finalStatus = projectFinalStatus(run, enrichment);
    return projectTerminal(finalStatus, run);
  }

  // ---- In-flight branches (ingest_stage analogues) ----
  if (
    status === RUN_STATUS.CREATED
    || status === RUN_STATUS.RECEIVED
    || status === RUN_STATUS.ASSESSING
    || status === RUN_STATUS.ASSESSMENT_READY
    || status === RUN_STATUS.PLAN_READY
    || status === RUN_STATUS.WAITING_FOR_CONFIRMATION
    || status === RUN_STATUS.COMPILE_PENDING
  ) {
    return {
      uiState: UI_STATE.PENDING,
      severity: "neutral",
      headline: "run accepted; waiting to start",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: null,
    };
  }

  if (status === RUN_STATUS.COMPILING) {
    return {
      uiState: UI_STATE.RUNNING,
      severity: "info",
      headline: "compiling document",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: null,
    };
  }

  if (status === RUN_STATUS.VERIFYING) {
    return {
      uiState: UI_STATE.RUNNING,
      severity: "info",
      headline: "verifying compile output",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: null,
    };
  }

  if (RUNNING_STATUSES.has(status)) {
    return {
      uiState: UI_STATE.RUNNING,
      severity: "info",
      headline: "run in progress",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: null,
    };
  }

  if (status === RUN_STATUS.PAUSED) {
    return {
      uiState: UI_STATE.PENDING,
      severity: "neutral",
      headline: "run paused",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: null,
    };
  }

  // Unknown / future status — never crash; render as PENDING.
  return {
    uiState: UI_STATE.PENDING,
    severity: "neutral",
    headline: "run starting",
    primaryArtifact: null,
    recommendedAction: "none",
    underlyingFinalStatus: null,
  };
}


function projectTerminal(
  finalStatus: IngestionFinalStatus,
  run: Pick<IngestionRun, "final">,
): UiRunState {
  const final = run.final;
  if (finalStatus === INGESTION_STATUS.CANCELLED) {
    return {
      uiState: UI_STATE.CANCELLED,
      severity: "neutral",
      headline: "run cancelled by operator",
      primaryArtifact: null,
      recommendedAction: "none",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT) {
    return {
      uiState: UI_STATE.COMPLETED,
      severity: "success",
      headline: "completed with enrichment",
      primaryArtifact: "final_summary",
      recommendedAction: "none",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT) {
    return {
      uiState: UI_STATE.COMPLETED_WITH_WARNINGS,
      severity: "warning",
      headline: "completed without enrichment",
      primaryArtifact: "final_summary",
      recommendedAction: "review_warnings",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS) {
    return {
      uiState: UI_STATE.COMPLETED_WITH_WARNINGS,
      severity: "warning",
      headline: "completed with warnings",
      primaryArtifact: "enrichment_result",
      recommendedAction: "review_warnings",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.FAILED_COMPILE) {
    return {
      uiState: UI_STATE.FAILED,
      severity: "error",
      headline: final?.failure_message
        ? `compile failed: ${final.failure_message}`
        : "compile failed",
      primaryArtifact: "error_report",
      recommendedAction: "inspect_compile_output",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED) {
    return {
      uiState: UI_STATE.FAILED,
      severity: "error",
      headline: "required enrichment did not complete",
      primaryArtifact: "compile_result_summary",
      recommendedAction: "retry",
      underlyingFinalStatus: finalStatus,
    };
  }
  if (finalStatus === INGESTION_STATUS.FAILED_FINALIZATION) {
    return {
      uiState: UI_STATE.FAILED,
      severity: "error",
      headline: "finalize failed after a successful pipeline",
      primaryArtifact: "error_report",
      recommendedAction: "inspect_error_report",
      underlyingFinalStatus: finalStatus,
    };
  }
  // FAILED_UNKNOWN fallthrough.
  return {
    uiState: UI_STATE.FAILED,
    severity: "error",
    headline: final?.failure_message || "run failed",
    primaryArtifact: "error_report",
    recommendedAction: "inspect_error_report",
    underlyingFinalStatus: finalStatus,
  };
}


// ---- Predicate ---------------------------------------------------

export function isTerminalStatus(status: RunStatus): boolean {
  if (status === RUN_STATUS.FAILED) return true;
  if (status === RUN_STATUS.CANCELLED) return true;
  if (COMPLETED_STATUSES.has(status)) return true;
  return false;
}


// ---- report-preferred projection ----------------------

/**
 * project a `UiRunState` directly from the typed
 * `FinalIngestionReport`. The report carries the backend's
 * authoritative `final_status` literal so the FE doesn't have to
 * re-derive it from `run.status + run.final + enrichment_result`.
 *
 * Falls back to `projectUiState(run, enrichmentSignals)` when the
 * report is null (pre- runs, in-flight runs).
 */
export function projectUiStateFromReport(
  run: Pick<IngestionRun, "status" | "final"> | null,
  report: FinalIngestionReportPayload | null,
  fallbackEnrichment: EnrichmentSignals = {},
): UiRunState | null {
  if (!run) return null;
  if (report?.final_status) {
    const finalStatus = report.final_status as IngestionFinalStatus;
    return projectTerminalFromReport(finalStatus, report, run);
  }
  return projectUiState(run, fallbackEnrichment);
}


function projectTerminalFromReport(
  finalStatus: IngestionFinalStatus,
  report: FinalIngestionReportPayload,
  run: Pick<IngestionRun, "final">,
): UiRunState {
  // Use the report's `final_status_reason` as the headline override
  // when present — the backend already produced operator-readable
  // copy. Otherwise fall through to the per-status defaults.
  const ui = projectTerminal(finalStatus, run);
  if (report.final_status_reason) {
    return { ...ui, headline: report.final_status_reason };
  }
  return ui;
}
