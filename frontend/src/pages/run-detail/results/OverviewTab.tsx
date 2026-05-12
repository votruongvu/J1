/**
 * Results > Overview tab.
 *
 * Renders the at-a-glance summary returned by
 * `GET /ingestion-runs/{id}/summary`: status row, key counters
 * (artifacts produced, total bytes, warnings, duration), the
 * per-stage step table, and the warnings list. Drives availability
 * for sibling tabs via `summary.availableViews`.
 */

import type { ReviewRunSummary, ReviewStepResult } from "@/types/review";

interface OverviewTabProps {
  summary: ReviewRunSummary | null;
  loading: boolean;
  error: string | null;
}

export function OverviewTab({ summary, loading, error }: OverviewTabProps) {
  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load run summary.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading || !summary) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading run summary…
      </div>
    );
  }

  const totalArtifacts = Object.values(summary.artifactCounts).reduce(
    (sum, n) => sum + (n || 0),
    0,
  );

  return (
    <div className="results-overview">
      <div className="results-overview__kpis">
        <Kpi label="Status" value={summary.status} />
        <Kpi label="Duration" value={formatDuration(summary.durationMs)} />
        <Kpi label="Artifacts produced" value={String(totalArtifacts)} />
        <Kpi label="Total bytes" value={formatBytes(summary.totalBytes)} />
        <Kpi
          label="Warnings"
          value={String(summary.warnings.length)}
          tone={summary.warnings.length > 0 ? "warn" : undefined}
        />
        {summary.qualitySummary?.overallConfidence != null ? (
          <Kpi
            label="Overall confidence"
            value={`${(summary.qualitySummary.overallConfidence * 100).toFixed(0)}%`}
          />
        ) : null}
      </div>

      <section className="results-overview__section">
        <h3 className="results-overview__section-title">Pipeline steps</h3>
        {summary.steps.length === 0 ? (
          <div className="results__empty results__empty--inline">
            No step results were recorded for this run.
          </div>
        ) : (
          <table className="results-step-table" role="table">
            <thead>
              <tr>
                <th>Step</th>
                <th>Status</th>
                <th>Required</th>
                <th>Source</th>
                <th>Duration</th>
                <th>Artifacts</th>
                <th>Reason / error</th>
              </tr>
            </thead>
            <tbody>
              {summary.steps.map((step) => (
                <StepRow key={`${step.step}-${step.source}`} step={step} />
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="results-overview__section">
        <h3 className="results-overview__section-title">
          Artifacts by kind
        </h3>
        {totalArtifacts === 0 ? (
          <div className="results__empty results__empty--inline">
            No artifacts were produced.
          </div>
        ) : (
          <ul className="results-overview__counts">
            {Object.entries(summary.artifactCounts).map(([kind, count]) => (
              <li key={kind}>
                <span
                  className="results-overview__count-kind"
                  title={kind}
                >
                  {kind}
                </span>
                <span className="results-overview__count-num">{count}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {summary.warnings.length > 0 ? (
        <section className="results-overview__section">
          <h3 className="results-overview__section-title">Warnings</h3>
          <ul className="results-overview__warnings">
            {summary.warnings.map((w, i) => (
              <li key={`${w.code}-${i}`}>
                <span className={`results-tag results-tag--${w.severity}`}>
                  {w.severity.toUpperCase()}
                </span>
                <span className="results-overview__warning-text">{w.message}</span>
                <span className="results-overview__warning-meta">
                  {warningTrace(w)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function StepRow({ step }: { step: ReviewStepResult }) {
  const failureNote = step.error
    ? `${step.error.type}: ${step.error.message}`
    : step.reason ?? "—";
  return (
    <tr>
      <td>{step.step}</td>
      <td>
        <span className={`results-status results-status--${step.status}`}>
          {step.status}
        </span>
      </td>
      <td>{step.required ? "yes" : "no"}</td>
      <td>{step.source}</td>
      <td>{formatDuration(step.durationMs ?? null)}</td>
      <td>{step.artifactCount}</td>
      <td className="results-step-table__reason">{failureNote}</td>
    </tr>
  );
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "warn";
}) {
  return (
    <div className={`results-kpi ${tone ? `results-kpi--${tone}` : ""}`}>
      <div className="results-kpi__label">{label}</div>
      <div className="results-kpi__value">{value}</div>
    </div>
  );
}

// ---- Formatting helpers (kept local — small, single-purpose) -------

function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const min = Math.floor(s / 60);
  const rest = Math.round(s - min * 60);
  return `${min}m ${rest}s`;
}

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function warningTrace(w: {
  step?: string | null;
  page?: number | null;
  chunkId?: string | null;
  artifactId?: string | null;
}): string {
  const bits: string[] = [];
  if (w.step) bits.push(`step ${w.step}`);
  if (w.page != null) bits.push(`page ${w.page}`);
  if (w.chunkId) bits.push(`chunk ${w.chunkId}`);
  if (w.artifactId) bits.push(`artifact ${w.artifactId}`);
  return bits.length ? `(${bits.join(" · ")})` : "";
}
