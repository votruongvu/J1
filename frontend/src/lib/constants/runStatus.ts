/**
 * Canonical lifecycle status strings + reusable predicate sets.
 *
 * The backend emits two parallel vocabularies for terminal success
 * — `SUCCEEDED` (preferred) and `COMPLETED` (legacy). Both are
 * carried in the type union and the success-status set so consumers
 * don't need to normalise.
 */

export const RUN_STATUS = {
  CREATED: "CREATED",
  ASSESSING: "ASSESSING",
  PLAN_READY: "PLAN_READY",
  WAITING_FOR_CONFIRMATION: "WAITING_FOR_CONFIRMATION",
  RUNNING: "RUNNING",
  PAUSED: "PAUSED",
  CANCELLING: "CANCELLING",
  COMPLETED: "COMPLETED",
  COMPLETED_WITH_WARNINGS: "COMPLETED_WITH_WARNINGS",
  SUCCEEDED: "SUCCEEDED",
  SUCCEEDED_WITH_WARNINGS: "SUCCEEDED_WITH_WARNINGS",
  FAILED: "FAILED",
  AWAITING_HUMAN_REVIEW: "AWAITING_HUMAN_REVIEW",
  REQUIRES_HUMAN_REVIEW: "REQUIRES_HUMAN_REVIEW",
  CANCELLED: "CANCELLED",
} as const;

export type RunStatus = (typeof RUN_STATUS)[keyof typeof RUN_STATUS];

// ---- Predicate sets --------------------------------------------------
//
// Pre-computed sets so callers can do `RUNNING_STATUSES.has(s)`
// instead of `["RUNNING","ASSESSING"].includes(s)`. Same predicate
// reused in 5+ files today — a single set keeps them paired.

export const RUNNING_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.ASSESSING,
]);

export const AWAITING_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
]);

export const COMPLETED_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.COMPLETED,
  RUN_STATUS.COMPLETED_WITH_WARNINGS,
  RUN_STATUS.SUCCEEDED,
  RUN_STATUS.SUCCEEDED_WITH_WARNINGS,
]);

export const WARNING_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.COMPLETED_WITH_WARNINGS,
  RUN_STATUS.SUCCEEDED_WITH_WARNINGS,
]);

export const REVIEW_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.AWAITING_HUMAN_REVIEW,
  RUN_STATUS.REQUIRES_HUMAN_REVIEW,
]);

// Status → action legality. Mirror the backend's 409 enforcement so
// the FE doesn't show buttons that will get rejected.

export const PAUSABLE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.ASSESSING,
]);

export const RESUMABLE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.PAUSED,
]);

export const CANCELLABLE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.ASSESSING,
  RUN_STATUS.PAUSED,
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
]);

// Ordered list for filter dropdowns. Stable order — render this
// rather than rebuilding the array each time.
export const LIST_STATUSES: readonly RunStatus[] = [
  RUN_STATUS.CREATED,
  RUN_STATUS.ASSESSING,
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
  RUN_STATUS.RUNNING,
  RUN_STATUS.SUCCEEDED,
  RUN_STATUS.SUCCEEDED_WITH_WARNINGS,
  RUN_STATUS.FAILED,
  RUN_STATUS.CANCELLED,
  RUN_STATUS.REQUIRES_HUMAN_REVIEW,
];
