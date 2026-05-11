/**
 * Canonical SSE progress-event type strings. Must match the values
 * emitted by the backend's `j1.runs.reporter.PROGRESS_EVENT_*`
 * constants — the contract is tested at the assertion call sites.
 *
 * Use the constant rather than a literal string so a backend rename
 * shows up as a TypeScript error instead of silent stream drift.
 */

export const EVENT_TYPES = {
  RUN_CREATED: "run.created",
  DOCUMENT_RECEIVED: "document.received",
  ASSESSMENT_STARTED: "assessment.started",
  ASSESSMENT_COMPLETED: "assessment.completed",
  PLAN_GENERATED: "plan.generated",
  PLAN_REVISED: "plan.revised",
  PLAN_CONFIRMED: "plan.confirmed",
  STEP_STARTED: "step.started",
  STEP_PROGRESS: "step.progress",
  STEP_SKIPPED: "step.skipped",
  STEP_WARNING: "step.warning",
  STEP_COMPLETED: "step.completed",
  STEP_FAILED: "step.failed",
  RUN_COMPLETED: "run.completed",
  RUN_FAILED: "run.failed",
  RUN_CANCELLED: "run.cancelled",
  HUMAN_REVIEW_REQUIRED: "human_review.required",
  // Canonical macro-stage events (Phase 3). Currently derived
  // client-side from existing `step.*` events via
  // `deriveMacroEventType()` — the constants exist so the FE can
  // render macro-stage section headers and so a future
  // emit-from-server change has a stable target.
  COMPILE_STARTED: "compile.started",
  COMPILE_COMPLETED: "compile.completed",
  COMPILE_FAILED: "compile.failed",
  VERIFICATION_STARTED: "verification.started",
  VERIFICATION_COMPLETED: "verification.completed",
  VERIFICATION_FAILED: "verification.failed",
} as const;

export type ProgressEventType = (typeof EVENT_TYPES)[keyof typeof EVENT_TYPES];

/**
 * Event types that close the SSE stream. Mirrors the backend's
 * `PROGRESS_TERMINAL_EVENT_TYPES` set.
 */
export const TERMINAL_EVENT_TYPES: ReadonlySet<ProgressEventType> = new Set<ProgressEventType>([
  EVENT_TYPES.RUN_COMPLETED,
  EVENT_TYPES.RUN_FAILED,
  EVENT_TYPES.RUN_CANCELLED,
  EVENT_TYPES.HUMAN_REVIEW_REQUIRED,
]);

export function isTerminalEvent(eventType: ProgressEventType | string): boolean {
  return TERMINAL_EVENT_TYPES.has(eventType as ProgressEventType);
}
