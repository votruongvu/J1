/**
 * SystemStatusSummary — the "is the knowledge base ready?"
 * compact counter card. Renders five numbers:
 *
 *   Total documents · Indexed · Running · Failed · Last successful
 *
 * Pure presentation — owner (HomeDashboard) loads documents and
 * passes the pre-aggregated summary in. Loading + error states
 * are rendered by the owner so this component stays cheap to
 * re-render.
 */

import { relativeTime } from "@/lib/format";

import type { DocumentStatusSummary } from "./home-dashboard-helpers";


interface SystemStatusSummaryProps {
  summary: DocumentStatusSummary;
  /** When true, the data is mid-fetch — the counts may be from
   * the previous render. We dim the card to make the staleness
   * visible without removing the user's last-known signal. */
  loading?: boolean;
}


export function SystemStatusSummary({
  summary,
  loading = false,
}: SystemStatusSummaryProps) {
  return (
    <section
      className={
        "card system-status-summary"
        + (loading ? " system-status-summary--loading" : "")
      }
      data-testid="home-status-summary"
      aria-busy={loading}
    >
      <h3 className="card__title">System status</h3>
      <dl className="system-status-summary__grid">
        <StatusStat
          label="Total documents"
          value={summary.total}
          testid="status-total"
        />
        <StatusStat
          label="Indexed"
          value={summary.indexed}
          tone={summary.indexed > 0 ? "ok" : "muted"}
          testid="status-indexed"
        />
        <StatusStat
          label="Running"
          value={summary.running}
          tone={summary.running > 0 ? "running" : "muted"}
          testid="status-running"
        />
        <StatusStat
          label="Failed"
          value={summary.failed}
          tone={summary.failed > 0 ? "err" : "muted"}
          testid="status-failed"
        />
        <StatusStat
          label="Last successful ingest"
          // The "value" slot here holds a relative-time string
          // rather than a count. The grid renders it the same
          // way; long values wrap.
          value={
            summary.lastSuccessfulAt
              ? relativeTime(summary.lastSuccessfulAt)
              : "—"
          }
          isString
          testid="status-last-success"
        />
      </dl>
    </section>
  );
}


function StatusStat({
  label,
  value,
  tone = "neutral",
  isString = false,
  testid,
}: {
  label: string;
  value: number | string;
  tone?: "ok" | "muted" | "err" | "running" | "neutral";
  /** Render `value` as a string (no comma-grouping) — used for
   * timestamps + placeholders. */
  isString?: boolean;
  testid: string;
}) {
  const display = isString
    ? value
    : typeof value === "number"
      ? value.toLocaleString()
      : value;
  return (
    <div
      className={`system-status-summary__stat system-status-summary__stat--${tone}`}
      data-testid={testid}
    >
      <dt>{label}</dt>
      <dd>{display}</dd>
    </div>
  );
}
