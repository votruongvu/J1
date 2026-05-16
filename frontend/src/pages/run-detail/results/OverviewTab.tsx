/**
 * Results > Overview tab.
 *
 * Renders the at-a-glance summary returned by
 * `GET /ingestion-runs/{id}/summary`: status row, key counters
 * (artifacts produced, total bytes, warnings, duration), the
 * per-stage step table, and the warnings list. Drives availability
 * for sibling tabs via `summary.availableViews`.
 *
 * Run Detail (simplified): the Pipeline Steps table filters to
 * the steps that are part of the run-execution model — Assessment
 * Plan + Compile. Legacy rows like `enrich SKIPPED disabled by
 * selected execution profile 'standard'` are stripped because
 * those concerns moved to Document Detail's
 * `ActiveKnowledgeResultPanel`; surfacing them here as "skipped"
 * was confusing operators into thinking the run was incomplete.
 */

import type { ReviewRunSummary, ReviewStepResult } from "@/types/review";


// Run-step ids that are part of the current run-execution model
// and should always appear in the Pipeline Steps table when the
// run summary lists them. Anything outside this set (enrich,
// graph, index, quality, validation) is a snapshot-level concern
// shown elsewhere; we additionally guard against showing it here
// when it's flagged as `skipped` with an execution-profile reason
// (the legacy noise the operator complained about).
const RUN_PRIMARY_STEPS: ReadonlySet<string> = new Set([
  "assess_compile_strategy",
  "assessment_plan",
  "build_initial_execution_plan",
  "compile",
]);

// Reasons that mean "this step exists in the summary only because
// the planner records every pipeline slot — there's nothing for
// the operator to act on". The row gets filtered out so the
// Pipeline Steps table reads as the run's actual execution model.
const LEGACY_SKIP_REASON_FRAGMENTS: ReadonlyArray<string> = [
  "disabled by selected execution profile",
  "indexer_kind not provided",
];


function _isLegacySkipped(step: ReviewStepResult): boolean {
  if (step.status !== "skipped") return false;
  const reason = (step.reason ?? "").toLowerCase();
  return LEGACY_SKIP_REASON_FRAGMENTS.some(
    (frag) => reason.includes(frag),
  );
}


/**
 * Filter the backend's step list to the rows that belong on Run
 * Detail's Pipeline Steps table.
 *
 * Kept exported so the contract test can drive it directly with
 * synthetic step rows.
 */
export function filterPrimaryRunSteps(
  steps: ReviewStepResult[],
): ReviewStepResult[] {
  return steps.filter((step) => {
    if (_isLegacySkipped(step)) return false;
    if (RUN_PRIMARY_STEPS.has(step.step)) return true;
    // Keep rows whose status is meaningfully non-trivial (failed,
    // running, completed with data) even when the step id isn't
    // in the canonical primary set — this preserves visibility for
    // future step ids we haven't added to the allowlist yet.
    return step.status !== "skipped";
  });
}

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
  // RAGAnything/LightRAG builds the base graph/index during
  // compile, but stores it inside the snapshot-scoped LightRAG
  // workspace — not as a registered J1 artifact. The legacy
  // `build_graph` activity (which would copy those workspace
  // files into a `graph_json` artifact) is off by default on the
  // standard profile. Surface this once in the Overview rather
  // than pretending the run produced no graph at all.
  const graphArtifactCount = summary.artifactCounts["graph_json"] ?? 0;
  const showGraphNote = graphArtifactCount === 0;

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

      {showGraphNote && (
        <section
          className="results-overview__section results-overview__graph-note"
          data-testid="results-overview-graph-note"
        >
          <strong>Base graph / index</strong>
          <p className="muted">
            Managed by the RAGAnything/LightRAG workspace for this
            snapshot. J1 does not currently persist a separate
            graph artifact for this run — query reads the workspace
            directly via{" "}
            <code>RAGAnything.aquery(mode=&quot;hybrid&quot;)</code>.
            Domain enrichment and Knowledge Memory are shown on
            Document Detail.
          </p>
        </section>
      )}

      <section className="results-overview__section">
        <h3 className="results-overview__section-title">Pipeline steps</h3>
        {(() => {
          const visibleSteps = filterPrimaryRunSteps(summary.steps);
          if (visibleSteps.length === 0) {
            return (
              <div className="results__empty results__empty--inline">
                No step results were recorded for this run.
              </div>
            );
          }
          return (
            <table
              className="results-step-table"
              role="table"
              data-testid="results-pipeline-steps"
            >
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
                {visibleSteps.map((step) => (
                  <StepRow key={`${step.step}-${step.source}`} step={step} />
                ))}
              </tbody>
            </table>
          );
        })()}
        <p
          className="muted results-overview__step-note"
          data-testid="results-pipeline-steps-note"
        >
          Post-compile domain enrichment and Knowledge Memory are
          managed on the document&apos;s active snapshot — see
          Document Detail.
        </p>
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
