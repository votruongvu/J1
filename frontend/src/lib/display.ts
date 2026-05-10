/**
 * Centralised display mappings for status / decision / severity / stage / event-type.
 *
 * EVERY label or styling string the UI reads from a backend enum
 * lives here. When backend strings change, edit this file — never
 * the components.
 */

import type {
  Decision,
  ProgressEventType,
  RunStatus,
  Severity,
  Stage,
} from "@/types/ingestion";
import type { ValidationStatus, ValidationResultStatus } from "@/types/review";

// ---- Status display -------------------------------------------------

/** Tone keys used by `<StatusBadge />` to pick a colour family. */
export type StatusTone = "neutral" | "info" | "accent" | "success" | "warning" | "error";

export interface StatusMeta {
  label: string;
  tone: StatusTone;
  /** When true, the badge dot pulses to indicate live activity. */
  pulse: boolean;
}

export const StatusDisplay: Readonly<Record<RunStatus, StatusMeta>> = {
  CREATED: { label: "Created", tone: "neutral", pulse: false },
  ASSESSING: { label: "Assessing", tone: "info", pulse: true },
  PLAN_READY: { label: "Plan ready", tone: "accent", pulse: false },
  WAITING_FOR_CONFIRMATION: { label: "Awaiting confirmation", tone: "accent", pulse: true },
  RUNNING: { label: "Running", tone: "info", pulse: true },
  PAUSED: { label: "Paused", tone: "warning", pulse: false },
  CANCELLING: { label: "Cancelling", tone: "neutral", pulse: true },
  COMPLETED: { label: "Completed", tone: "success", pulse: false },
  COMPLETED_WITH_WARNINGS: { label: "Completed · warnings", tone: "warning", pulse: false },
  SUCCEEDED: { label: "Succeeded", tone: "success", pulse: false },
  SUCCEEDED_WITH_WARNINGS: { label: "Succeeded · warnings", tone: "warning", pulse: false },
  FAILED: { label: "Failed", tone: "error", pulse: false },
  AWAITING_HUMAN_REVIEW: { label: "Human review", tone: "warning", pulse: true },
  REQUIRES_HUMAN_REVIEW: { label: "Human review", tone: "warning", pulse: true },
  CANCELLED: { label: "Cancelled", tone: "neutral", pulse: false },
  // Soft-deleted runs are hidden from most listings; this entry
  // exists so a direct deep-link to a deleted run still renders a
  // sensible badge instead of an "Unknown" fallback.
  deleted: { label: "Deleted", tone: "neutral", pulse: false },
};

/** Safe lookup that falls back to a neutral tone for unknown strings. */
export function statusMeta(status: string | undefined | null): StatusMeta {
  if (status && status in StatusDisplay) {
    return StatusDisplay[status as RunStatus];
  }
  return { label: status ?? "Unknown", tone: "neutral", pulse: false };
}

// ---- Decision display -----------------------------------------------

export interface DecisionMeta {
  label: string;
  /** CSS class name applied to the decision badge. */
  className: string;
}

export const DecisionDisplay: Readonly<Record<Decision, DecisionMeta>> = {
  RUN: { label: "Run", className: "decision--run" },
  SKIP: { label: "Skip", className: "decision--skip" },
  CONDITIONAL: { label: "Conditional", className: "decision--conditional" },
};

export function decisionMeta(decision: string | undefined): DecisionMeta {
  if (decision && decision in DecisionDisplay) {
    return DecisionDisplay[decision as Decision];
  }
  return { label: decision ?? "—", className: "decision--skip" };
}

// ---- Severity display -----------------------------------------------

export const SeverityDisplay: Readonly<Record<Severity, string>> = {
  INFO: "info",
  WARNING: "warning",
  ERROR: "error",
};

// ---- Stage display --------------------------------------------------

export const StageDisplay: Readonly<Record<Stage, string>> = {
  COMPILE: "Compile",
  ENRICH: "Enrich",
  GRAPH: "Graph",
  INDEX: "Index",
};

// ---- Event-type display ---------------------------------------------

export const EventTypeDisplay: Readonly<Record<ProgressEventType, string>> = {
  "run.created": "Run created",
  "document.received": "Document received",
  "assessment.started": "Assessment started",
  "assessment.completed": "Assessment completed",
  "plan.generated": "Plan generated",
  "plan.revised": "Plan revised",
  "plan.confirmed": "Plan confirmed",
  "step.started": "Step started",
  "step.progress": "Progress",
  "step.skipped": "Step skipped",
  "step.warning": "Step warning",
  "step.completed": "Step completed",
  "step.failed": "Step failed",
  "run.completed": "Run completed",
  "run.failed": "Run failed",
  "run.cancelled": "Run cancelled",
  "human_review.required": "Human review required",
};

export function eventTypeLabel(type: string): string {
  if (type in EventTypeDisplay) {
    return EventTypeDisplay[type as ProgressEventType];
  }
  return type;
}

// ---- Validation status display --------------------------------------
//
// `ValidationStatus` is the run-level outcome (passed / failed / etc).
// `ValidationResultStatus` is the per-test-case outcome. The two
// vocabularies overlap but are distinct types — keep both maps so a
// rename only touches this file.

export interface ValidationStatusMeta {
  label: string;
  className: string;
}

const _NOT_RUN_LABEL = "Not run";

/**
 * Run-level validation outcome. The "not_run" key isn't part of the
 * `ValidationStatus` union — it's the synthetic value for a run that
 * has no validation attached yet. Look it up via
 * `validationStatusMeta(undefined | "not_run")`.
 */
export const ValidationStatusDisplay: Readonly<
  Record<ValidationStatus, ValidationStatusMeta>
> = {
  passed: { label: "Passed", className: "validation-status--ok" },
  passed_with_warnings: {
    label: "Passed with warnings",
    className: "validation-status--warn",
  },
  failed: { label: "Failed", className: "validation-status--fail" },
  inconclusive: { label: "Inconclusive", className: "validation-status--unknown" },
};

export function validationStatusMeta(
  status: ValidationStatus | "not_run" | null | undefined,
): ValidationStatusMeta {
  if (!status || status === "not_run") {
    return { label: _NOT_RUN_LABEL, className: "validation-status--unknown" };
  }
  if (status in ValidationStatusDisplay) {
    return ValidationStatusDisplay[status as ValidationStatus];
  }
  return { label: status, className: "validation-status--unknown" };
}

/** Per-test-case outcome — the table-row status. */
export const ValidationResultDisplay: Readonly<
  Record<ValidationResultStatus, ValidationStatusMeta>
> = {
  passed: { label: "Passed", className: "validation-status--ok" },
  warning: { label: "Warning", className: "validation-status--warn" },
  failed: { label: "Failed", className: "validation-status--fail" },
  skipped: { label: "Skipped", className: "validation-status--unknown" },
};

export function validationResultMeta(
  status: ValidationResultStatus | null | undefined,
): ValidationStatusMeta {
  if (!status) {
    return { label: "—", className: "validation-status--unknown" };
  }
  if (status in ValidationResultDisplay) {
    return ValidationResultDisplay[status as ValidationResultStatus];
  }
  return { label: status, className: "validation-status--unknown" };
}
