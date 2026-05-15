/**
 * RecentRunsPanel — flat table of the most recent ingestion
 * runs across every document. Each row is a click-through to
 * Run Detail. Pure presentation; rows are pre-computed by
 * `collectRecentRuns` in the helpers.
 *
 * Columns: Document · Type · Status · Duration · Started ·
 *          [View Run]
 *
 * Action ownership: this panel does NOT render run actions
 * (cancel / re-index / etc.) — those stay on Run Detail per
 * the refactor brief. The "View Run" link is the only path.
 */

import { StatusBadge } from "@/components/badges";
import { relativeTime } from "@/lib/format";

import {
  formatDuration,
  runTypeLabel,
  type RecentRunRow,
} from "./home-dashboard-helpers";


interface RecentRunsPanelProps {
  rows: readonly RecentRunRow[];
  loading?: boolean;
  /** Click handler — opens the Run Detail page. The owner
   * supplies this so navigation stays in App.tsx. */
  onOpenRun: (runId: string) => void;
}


export function RecentRunsPanel({
  rows,
  loading = false,
  onOpenRun,
}: RecentRunsPanelProps) {
  return (
    <section
      className="card recent-runs-panel"
      data-testid="home-recent-runs"
      aria-busy={loading}
    >
      <header className="card__header">
        <h3 className="card__title">Recent runs</h3>
      </header>
      {rows.length === 0 && !loading && (
        <p
          className="recent-runs-panel__empty"
          data-testid="home-recent-runs-empty"
        >
          No runs yet. Upload a document to start.
        </p>
      )}
      {rows.length === 0 && loading && (
        <p className="recent-runs-panel__empty">Loading…</p>
      )}
      {rows.length > 0 && (
        <table className="recent-runs-panel__table">
          <thead>
            <tr>
              <th scope="col">Document</th>
              <th scope="col">Type</th>
              <th scope="col">Status</th>
              <th scope="col">Duration</th>
              <th scope="col">Started</th>
              <th scope="col" className="visually-hidden">Action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.runId} data-testid={`recent-run-${row.runId}`}>
                <td className="recent-runs-panel__doc-cell">
                  <span title={row.documentName}>{row.documentName}</span>
                </td>
                <td>{runTypeLabel(row.runType)}</td>
                <td><StatusBadge status={row.status} /></td>
                <td>{formatDuration(row.durationMs)}</td>
                <td title={row.startedAt ?? undefined}>
                  {relativeTime(row.startedAt)}
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => onOpenRun(row.runId)}
                    data-testid={`recent-run-open-${row.runId}`}
                  >
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
