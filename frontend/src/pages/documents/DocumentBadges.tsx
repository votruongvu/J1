/**
 * Visual badges used across the document-centric surface.
 *
 * Two components:
 *
 *  * `KnowledgeStateBadge` — colored pill for attached/detached/
 *    removed. Same look as `StatusBadge` so the document list reads
 *    consistently with the run list during the migration.
 *
 *  * `ResultStatusBadge` — renders the current-result summary the
 *    backend projects from the active run. Falls back to an
 *    operator-friendly "Not yet processed" pill when there's no
 *    active run.
 */

import type {
  DocumentResultSummary,
  KnowledgeState,
} from "@/types/documents";


const STATE_META: Record<KnowledgeState, { label: string; className: string }> = {
  attached: { label: "Attached", className: "knowledge-state knowledge-state--attached" },
  detached: { label: "Detached", className: "knowledge-state knowledge-state--detached" },
  removed:  { label: "Removed",  className: "knowledge-state knowledge-state--removed" },
};


export function KnowledgeStateBadge({ state }: { state: KnowledgeState }) {
  const meta = STATE_META[state] ?? STATE_META.attached;
  return (
    <span
      className={meta.className}
      data-testid={`knowledge-state-${state}`}
      aria-label={`Knowledge state: ${meta.label}`}
    >
      {meta.label}
    </span>
  );
}


// Maps the active-run terminal status to operator-readable copy.
// `none` covers the just-uploaded-not-yet-processed case so the
// FE never renders an awkward empty status pill.
const RESULT_LABEL: Record<string, { label: string; className: string }> = {
  none:                       { label: "Not yet processed",        className: "result-pill result-pill--neutral" },
  succeeded:                  { label: "Completed",                className: "result-pill result-pill--ok" },
  succeeded_with_warnings:    { label: "Completed with warnings",  className: "result-pill result-pill--warn" },
  failed:                     { label: "Failed",                   className: "result-pill result-pill--err" },
  cancelled:                  { label: "Cancelled",                className: "result-pill result-pill--neutral" },
  requires_human_review:      { label: "Awaiting review",          className: "result-pill result-pill--warn" },
  running:                    { label: "Running",                  className: "result-pill result-pill--info" },
  assessing:                  { label: "Assessing",                className: "result-pill result-pill--info" },
  paused:                     { label: "Paused",                   className: "result-pill result-pill--neutral" },
};


export function ResultStatusBadge(
  { summary }: { summary: DocumentResultSummary },
) {
  const meta = RESULT_LABEL[summary.status] ?? {
    label: summary.status || "Unknown",
    className: "result-pill result-pill--neutral",
  };
  return (
    <span
      className={meta.className}
      data-testid={`result-status-${summary.status}`}
    >
      {meta.label}
    </span>
  );
}
