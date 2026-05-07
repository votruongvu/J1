/**
 * Terminal-status helpers for the Results section.
 *
 * Lives in its own module (not next to the component) so the
 * `react-refresh` plugin doesn't flag the index file for mixing
 * components + non-component exports.
 */

const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "COMPLETED",
  "COMPLETED_WITH_WARNINGS",
  "SUCCEEDED",
  "SUCCEEDED_WITH_WARNINGS",
  "FAILED",
  "CANCELLED",
  "AWAITING_HUMAN_REVIEW",
  "REQUIRES_HUMAN_REVIEW",
]);

/** True when the run has reached a terminal state — Results is
 * visible only when this returns true. Single source of truth so
 * components don't drift from one another on the predicate. */
export function isTerminalRunStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return TERMINAL_STATUSES.has(String(status).toUpperCase());
}
