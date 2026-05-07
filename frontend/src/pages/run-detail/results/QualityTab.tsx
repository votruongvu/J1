/**
 * Results > Quality tab.
 *
 * Renders the neutral quality report from
 * `GET /ingestion-runs/{id}/quality-report`. Shows overall + per-
 * modality confidence, warnings, skipped steps, failed-optional
 * steps, and low-confidence findings. Source-traceability fields
 * (page / chunk / artifact) are surfaced inline so reviewers can
 * locate the issue without leaving the tab.
 */

import type {
  ReviewLowConfidenceFinding,
  ReviewQualityReport,
} from "@/types/review";

interface QualityTabProps {
  report: ReviewQualityReport | null;
  loading: boolean;
  error: string | null;
}

export function QualityTab({ report, loading, error }: QualityTabProps) {
  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load quality report.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading || !report) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading quality report…
      </div>
    );
  }

  const isEmpty =
    report.overallConfidence == null &&
    report.modalityConfidences.length === 0 &&
    report.warnings.length === 0 &&
    report.skippedSteps.length === 0 &&
    report.failedOptionalSteps.length === 0 &&
    report.lowConfidenceFindings.length === 0;

  if (isEmpty) {
    return (
      <div className="results__empty">
        No quality data was produced for this run.
      </div>
    );
  }

  return (
    <div className="results-quality">
      <section className="results-quality__scorecard">
        <ConfidenceCard
          label="Overall confidence"
          value={report.overallConfidence}
        />
        {report.modalityConfidences.map((m) => (
          <ConfidenceCard
            key={m.modality}
            label={m.modality}
            value={m.confidence}
            sampleCount={m.sampleCount ?? null}
          />
        ))}
      </section>

      {report.warnings.length > 0 ? (
        <section className="results-quality__section">
          <h3 className="results-quality__section-title">Warnings</h3>
          <ul className="results-quality__list">
            {report.warnings.map((w, i) => (
              <li key={`w-${i}`}>
                <span className={`results-tag results-tag--${w.severity}`}>
                  {w.severity.toUpperCase()}
                </span>
                <span className="results-quality__list-text">{w.message}</span>
                <span className="results-quality__list-meta">
                  {traceMeta({
                    page: w.page,
                    chunkId: w.chunkId,
                    artifactId: w.artifactId,
                    step: w.step,
                  })}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report.skippedSteps.length > 0 ? (
        <section className="results-quality__section">
          <h3 className="results-quality__section-title">Skipped steps</h3>
          <ul className="results-quality__list">
            {report.skippedSteps.map((s) => (
              <li key={`sk-${s.step}`}>
                <span className="results-tag results-tag--info">SKIPPED</span>
                <span className="results-quality__list-text">{s.step}</span>
                <span className="results-quality__list-meta">
                  {s.reason ?? "—"}
                  {s.policy ? ` · driven by ${s.policy}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report.failedOptionalSteps.length > 0 ? (
        <section className="results-quality__section">
          <h3 className="results-quality__section-title">
            Optional steps that failed
          </h3>
          <ul className="results-quality__list">
            {report.failedOptionalSteps.map((f) => (
              <li key={`fo-${f.step}`}>
                <span className="results-tag results-tag--warning">FAILED</span>
                <span className="results-quality__list-text">{f.step}</span>
                <span className="results-quality__list-meta">
                  {f.reason ?? "—"}
                  {f.errorType ? ` · ${f.errorType}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {report.lowConfidenceFindings.length > 0 ? (
        <section className="results-quality__section">
          <h3 className="results-quality__section-title">
            Low-confidence findings
          </h3>
          <ul className="results-quality__list">
            {report.lowConfidenceFindings.map((f, i) => (
              <li key={`lc-${i}`}>
                <span className="results-tag results-tag--warning">
                  {(f.score * 100).toFixed(0)}%
                </span>
                <span className="results-quality__list-text">
                  {f.message ?? f.category}
                </span>
                <span className="results-quality__list-meta">
                  {findingMeta(f)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function ConfidenceCard({
  label,
  value,
  sampleCount,
}: {
  label: string;
  value: number | null | undefined;
  sampleCount?: number | null;
}) {
  const pct = value != null ? `${(value * 100).toFixed(0)}%` : "—";
  const tone =
    value == null ? "" : value >= 0.8 ? "good" : value >= 0.6 ? "warn" : "bad";
  return (
    <div className={`results-conf-card results-conf-card--${tone}`}>
      <div className="results-conf-card__label">{label}</div>
      <div className="results-conf-card__value">{pct}</div>
      {sampleCount != null ? (
        <div className="results-conf-card__samples">{sampleCount} samples</div>
      ) : null}
    </div>
  );
}

function traceMeta(t: {
  step?: string | null;
  page?: number | null;
  chunkId?: string | null;
  artifactId?: string | null;
}): string {
  const bits: string[] = [];
  if (t.step) bits.push(`step ${t.step}`);
  if (t.page != null) bits.push(`page ${t.page}`);
  if (t.chunkId) bits.push(`chunk ${t.chunkId}`);
  if (t.artifactId) bits.push(`artifact ${t.artifactId}`);
  return bits.length ? `(${bits.join(" · ")})` : "";
}

function findingMeta(f: ReviewLowConfidenceFinding): string {
  return traceMeta({
    page: f.page,
    chunkId: f.chunkId,
    artifactId: f.artifactId,
  }) || `category: ${f.category}`;
}
