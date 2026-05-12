/**
 * Canonical lifecycle status strings + reusable predicate sets.
 *
 * The backend emits two parallel vocabularies for terminal success
 * — `SUCCEEDED` (preferred) and `COMPLETED` (legacy). Both are
 * carried in the type union and the success-status set so consumers
 * don't need to normalise.
 */

export const RUN_STATUS = {
  // Legacy CREATED and canonical RECEIVED are equivalent — a run
  // written under either value renders the same badge. The backend's
  // `canonical_status` helper folds CREATED onto RECEIVED for
  // status-filter expansion; treat them as interchangeable here.
  CREATED: "CREATED",
  RECEIVED: "RECEIVED",
  ASSESSING: "ASSESSING",
  // Legacy PLAN_READY and canonical ASSESSMENT_READY are equivalent.
  // Existing JSONL runs use PLAN_READY; new runs SHOULD use
  // ASSESSMENT_READY. Both pass into `/confirm` + `/compile`.
  PLAN_READY: "PLAN_READY",
  ASSESSMENT_READY: "ASSESSMENT_READY",
  WAITING_FOR_CONFIRMATION: "WAITING_FOR_CONFIRMATION",
  // Two-phase compile gate. Run is parked after assessment finished
  // and is waiting for `POST /ingestion-runs/{id}/compile` to release
  // the workflow into the compile activity.
  COMPILE_PENDING: "COMPILE_PENDING",
  RUNNING: "RUNNING",
  // Canonical name for the compile macro-stage active state. Used
  // alongside RUNNING — the workflow may surface either while inside
  // the compile retry loop depending on whether the worker writes
  // canonical or legacy values.
  COMPILING: "COMPILING",
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
  // Soft-delete tombstone (lowercase to match backend). Hidden from
  // most listing surfaces; visible only to admin / "tombstone
  // explorer" views (not yet built).
  DELETED: "deleted",
} as const;

export type RunStatus = (typeof RUN_STATUS)[keyof typeof RUN_STATUS];

// ---- Predicate sets --------------------------------------------------
//
// Pre-computed sets so callers can do `RUNNING_STATUSES.has(s)`
// instead of `["RUNNING","ASSESSING"].includes(s)`. Same predicate
// reused in 5+ files today — a single set keeps them paired.

export const RUNNING_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.COMPILING,
  RUN_STATUS.ASSESSING,
]);

export const AWAITING_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.ASSESSMENT_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
  RUN_STATUS.COMPILE_PENDING,
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
  RUN_STATUS.COMPILING,
  RUN_STATUS.ASSESSING,
]);

export const RESUMABLE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.PAUSED,
]);

export const CANCELLABLE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.COMPILING,
  RUN_STATUS.ASSESSING,
  RUN_STATUS.PAUSED,
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.ASSESSMENT_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
  RUN_STATUS.COMPILE_PENDING,
]);

// Active = workflow is still doing work or could resume. The backend
// refuses Delete + Full-reindex with HTTP 409 for these. Mirrors
// `active_states` in `IngestionResultReviewService.delete_run`.
export const ACTIVE_STATUSES: ReadonlySet<RunStatus> = new Set([
  RUN_STATUS.RUNNING,
  RUN_STATUS.COMPILING,
  RUN_STATUS.ASSESSING,
  RUN_STATUS.PAUSED,
  RUN_STATUS.CANCELLING,
  RUN_STATUS.PLAN_READY,
  RUN_STATUS.ASSESSMENT_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
  RUN_STATUS.COMPILE_PENDING,
  RUN_STATUS.CREATED,
  RUN_STATUS.RECEIVED,
]);

// Ordered list for filter dropdowns. Stable order — render this
// rather than rebuilding the array each time.
// Uses the canonical names (RECEIVED / ASSESSMENT_READY); the
// backend's status-filter expansion folds legacy values onto these.
export const LIST_STATUSES: readonly RunStatus[] = [
  RUN_STATUS.RECEIVED,
  RUN_STATUS.ASSESSING,
  RUN_STATUS.ASSESSMENT_READY,
  RUN_STATUS.WAITING_FOR_CONFIRMATION,
  RUN_STATUS.COMPILE_PENDING,
  RUN_STATUS.RUNNING,
  RUN_STATUS.SUCCEEDED,
  RUN_STATUS.SUCCEEDED_WITH_WARNINGS,
  RUN_STATUS.FAILED,
  RUN_STATUS.CANCELLED,
  RUN_STATUS.REQUIRES_HUMAN_REVIEW,
];
