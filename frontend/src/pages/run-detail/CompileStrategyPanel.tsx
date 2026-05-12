/**
 * Compile Strategy panel — Assessment Plan + mapped CompileConfig +
 * per-attempt timeline + warning banners + final quality summary.
 *
 * Source of truth: the `compile_strategy_report` artifact the
 * workflow persists after compile. Fetched lazily via
 * `client.listRunArtifacts({kind: 'compile_strategy_report'})` +
 * `client.getRunArtifactContent` once the artifact exists.
 *
 * Backward-compat: when the artifact isn't present (legacy runs,
 * runs that finished before this code shipped, runs where compile
 * failed before persisting), the panel renders a "No assessment
 * data available" placeholder instead of fake values.
 */

import { useCallback, useEffect, useState } from "react";
import { Typewriter } from "@/components/Typewriter";
import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import {
  appliedCapabilities,
  bannersForReport,
  COMPILE_STRATEGY_REPORT_KIND,
  isFallbackOnly,
  type CompileAttemptRecord,
  type CompileStrategyReport,
} from "./compile-strategy-helpers";

interface CompileStrategyPanelProps {
  runId: string;
  /** SSE event from the parent. See `AssessmentPlanPanel` for the
 * full rationale; same pattern. */
  latestEvent?: ProgressEvent | null;
}

export function CompileStrategyPanel({
  runId, latestEvent,
}: CompileStrategyPanelProps) {
  const client = useClient();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "missing" }
    | { kind: "ready"; report: CompileStrategyReport }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  const loadReport = useCallback(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await client.listRunArtifacts(runId, {
          kind: COMPILE_STRATEGY_REPORT_KIND,
        });
        const artifact = page.items[0];
        if (!artifact) {
          if (!cancelled) {
            setState((prev) => prev.kind === "ready" ? prev : { kind: "missing" });
          }
          return;
        }
        const content = await client.getRunArtifactContent(
          runId,
          artifact.artifactId,
        );
        const text = await content.blob.text();
        const report = JSON.parse(text) as CompileStrategyReport;
        if (!cancelled) setState({ kind: "ready", report });
      } catch (e) {
        if (!cancelled) {
          setState((prev) =>
            prev.kind === "ready"
              ? prev
              : {
                  kind: "error",
                  message: e instanceof Error ? e.message : "load failed",
                },
          );
        }
      }
    })();
    return () => { cancelled = true; };
  }, [client, runId]);

  useEffect(() => {
    setState({ kind: "loading" });
    return loadReport();
  }, [loadReport]);

  // Refetch on compile-completion-adjacent SSE events so the panel
  // picks up the report when the user loaded mid-flight. STEP_STARTED
  // is included because compile_strategy_report is persisted by a
  // separate silent activity (no progress signal) between compile's
  // step.completed and the next stage's step.started — without
  // STEP_STARTED here the panel only re-loads on later step.completed
  // events, by which point several silent persists have piled up.
  useEffect(() => {
    if (!latestEvent) return;
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_STARTED,
      EVENT_TYPES.STEP_COMPLETED,
      EVENT_TYPES.STEP_FAILED,
      EVENT_TYPES.STEP_SKIPPED,
    ]);
    if (refreshOn.has(latestEvent.event) || isTerminalEvent(latestEvent.event)) {
      loadReport();
    }
  }, [latestEvent, loadReport]);

  if (state.kind === "loading") {
    return (
      <div className="card" data-testid="compile-strategy-loading">
        <div className="card__body">Loading compile strategy…</div>
      </div>
    );
  }
  if (state.kind === "missing") {
    return (
      <div className="card" data-testid="compile-strategy-missing">
        <div className="card__body">
          <h3>Compile Strategy</h3>
          <p style={{ color: "var(--text-muted)" }}>
            No assessment data available for this run. The compile-strategy
            report artifact hasn't been written yet.
          </p>
        </div>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="compile-strategy-error">
        <div className="card__body">
          <h3>Compile Strategy</h3>
          <p style={{ color: "var(--error-fg)" }}>
            Couldn't load compile strategy: {state.message}
          </p>
        </div>
      </div>
    );
  }
  return <CompileStrategyContent report={state.report} />;
}

function CompileStrategyContent({ report }: { report: CompileStrategyReport }) {
  // Note: AssessmentPlan rendering is owned by `AssessmentPlanPanel`
  // (mounted FIRST on the run-detail page). This panel focuses on
  // the compile-side: mapped config, per-attempt timeline, and
  // final-quality summary.
  return (
    <div
      className="compile-strategy-panel"
      data-testid="compile-strategy-panel"
    >
      <Banners report={report} />
      <CompileStrategyCard report={report} />
      <CompileAttemptsTimeline attempts={report.attempts} />
      <FinalQualitySummary report={report} />
    </div>
  );
}

function CapList({ caps }: { caps: string[] }) {
  return (
    <div className="cap-pills">
      {caps.map((c) => (
        <span key={c} className="badge cap-pill">{c}</span>
      ))}
    </div>
  );
}

// ---- Compile Strategy card -----------------------------------------

function CompileStrategyCard({ report }: { report: CompileStrategyReport }) {
  const required = report.assessment_plan.required_capabilities ?? [];
  const unhandled = report.unhandled_capabilities ?? [];
  const applied = appliedCapabilities(report);
  const lastAttempt = report.attempts[report.attempts.length - 1];
  const mappedConfig = lastAttempt?.mapped_compile_config ?? {};
  const planMissing = isFallbackOnly(report);
  return (
    <div className="card" data-testid="compile-strategy-card">
      <div className="card__body">
        <h3>Compile Strategy</h3>
        <dl className="kv">
          <dt>Compiler adapter</dt>
          <dd>{lastAttempt?.parser ?? "—"}</dd>
          <dt>Parser</dt>
          <dd>{lastAttempt?.parser ?? "—"}</dd>
          <dt>parse_method</dt>
          <dd className="mono">{lastAttempt?.parse_method ?? "—"}</dd>
          <dt>Config source</dt>
          <dd>
            {planMissing
              ? "fallback (no AssessmentPlan)"
              : "assessment_plan"}
          </dd>
          <dt>Requested capabilities</dt>
          <dd>{required.length === 0 ? "—" : <CapList caps={required} />}</dd>
          <dt>Applied capabilities</dt>
          <dd>{applied.length === 0 ? "—" : <CapList caps={applied} />}</dd>
          <dt>Unhandled capabilities</dt>
          <dd>
            {unhandled.length === 0
              ? <span style={{ color: "var(--success-fg)" }}>none</span>
              : <CapList caps={unhandled} />}
          </dd>
          <dt>Mapped config</dt>
          <dd>
            <pre className="mono small-pre">
              {JSON.stringify(mappedConfig, null, 2)}
            </pre>
          </dd>
        </dl>
      </div>
    </div>
  );
}

// ---- 3) Compile Attempts timeline ---------------------------------

function CompileAttemptsTimeline({
  attempts,
}: { attempts: CompileAttemptRecord[] }) {
  if (!attempts.length) {
    return (
      <div className="card" data-testid="compile-attempts-empty">
        <div className="card__body">
          <h3>Compile Attempts</h3>
          <p style={{ color: "var(--text-muted)" }}>No attempts recorded.</p>
        </div>
      </div>
    );
  }
  return (
    <div className="card" data-testid="compile-attempts-timeline">
      <div className="card__body">
        <h3>Compile Attempts</h3>
        <ol className="attempt-list">
          {attempts.map((a) => (
            <li
              key={a.attempt_number}
              data-testid={`compile-attempt-${a.attempt_number}`}
              className={`attempt attempt--${a.quality} attempt--status-${a.status}`}
            >
              <div className="attempt__head">
                <span className="attempt__num">#{a.attempt_number}</span>
                <span className={`badge mode-badge mode-badge--${a.mode ?? "unknown"}`}>
                  {a.mode ?? "—"}
                </span>
                <span className={`badge quality-badge quality-badge--${a.quality}`}>
                  {a.quality}
                </span>
                <span className="attempt__status mono">{a.status}</span>
              </div>
              <dl className="kv">
                <dt>Parser / parse_method</dt>
                <dd className="mono">
                  {a.parser} / {a.parse_method ?? "—"}
                </dd>
                <dt>chunks</dt>
                <dd>{a.chunks_count}</dd>
                <dt>extracted text chars</dt>
                <dd>{a.extracted_text_chars ?? "unknown"}</dd>
                {a.retry_reason && (
                  <>
                    <dt>Retry reason</dt>
                    <dd className="mono">{a.retry_reason}</dd>
                  </>
                )}
                {a.warnings.length > 0 && (
                  <>
                    <dt>Warnings</dt>
                    <dd>
                      <ul className="bullet-list">
                        {a.warnings.map((w, i) => <li key={i}>{w}</li>)}
                      </ul>
                    </dd>
                  </>
                )}
              </dl>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

// ---- 4) Banners ---------------------------------------------------

function Banners({ report }: { report: CompileStrategyReport }) {
  const banners = bannersForReport(report);
  if (banners.length === 0) return null;
  return (
    <div data-testid="strategy-banners">
      {banners.map((b, i) => (
        <div
          key={i}
          className={`banner banner--${b.kind}`}
          data-testid={b.testid}
        >
          {i === 0 ? (
            <Typewriter text={b.message} speed={140} cursor />
          ) : b.message}
        </div>
      ))}
    </div>
  );
}

// ---- 5) Final Compile Quality summary -----------------------------

function FinalQualitySummary({ report }: { report: CompileStrategyReport }) {
  const last = report.attempts[report.attempts.length - 1];
  return (
    <div className="card" data-testid="final-quality-summary">
      <div className="card__body">
        <h3>Final Compile Quality</h3>
        <dl className="kv">
          <dt>Quality</dt>
          <dd>
            <span className={`badge quality-badge quality-badge--${report.final_compile_quality}`}>
              {report.final_compile_quality}
            </span>
          </dd>
          <dt>Initial mode</dt>
          <dd>{report.initial_mode ?? "—"}</dd>
          <dt>Final mode</dt>
          <dd>{report.final_mode ?? "—"}</dd>
          <dt>Retry used</dt>
          <dd>{report.retry_used ? "yes" : "no"}</dd>
          <dt>Attempts</dt>
          <dd>{report.attempts_count}</dd>
          <dt>Final chunks</dt>
          <dd>{last?.chunks_count ?? 0}</dd>
          <dt>Final extracted chars</dt>
          <dd>{last?.extracted_text_chars ?? "unknown"}</dd>
          {report.final_warnings.length > 0 && (
            <>
              <dt>Final warnings</dt>
              <dd>
                <ul className="bullet-list">
                  {report.final_warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}
