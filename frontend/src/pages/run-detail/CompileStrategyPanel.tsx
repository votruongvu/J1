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

import { useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import {
  appliedCapabilities,
  bannersForReport,
  COMPILE_STRATEGY_REPORT_KIND,
  isFallbackOnly,
  type AssessmentPlanPayload,
  type CompileAttemptRecord,
  type CompileStrategyReport,
} from "./compile-strategy-helpers";

interface CompileStrategyPanelProps {
  runId: string;
}

export function CompileStrategyPanel({ runId }: CompileStrategyPanelProps) {
  const client = useClient();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "missing" }
    | { kind: "ready"; report: CompileStrategyReport }
    | { kind: "error"; message: string }
  >({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await client.listRunArtifacts(runId, {
          kind: COMPILE_STRATEGY_REPORT_KIND,
        });
        const artifact = page.items[0];
        if (!artifact) {
          if (!cancelled) setState({ kind: "missing" });
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
          setState({
            kind: "error",
            message: e instanceof Error ? e.message : "load failed",
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, client]);

  if (state.kind === "loading") {
    return (
      <div className="card" data-testid="compile-strategy-loading">
        Loading compile strategy…
      </div>
    );
  }
  if (state.kind === "missing") {
    return (
      <div className="card" data-testid="compile-strategy-missing">
        <h3>Compile Strategy</h3>
        <p style={{ color: "var(--text-muted)" }}>
          No assessment data available for this run. Compile may have
          run via the legacy single-shot path, or the report artifact
          hasn't been written yet.
        </p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="compile-strategy-error">
        <h3>Compile Strategy</h3>
        <p style={{ color: "var(--error-fg)" }}>
          Couldn't load compile strategy: {state.message}
        </p>
      </div>
    );
  }
  return <CompileStrategyContent report={state.report} />;
}

function CompileStrategyContent({ report }: { report: CompileStrategyReport }) {
  return (
    <div data-testid="compile-strategy-panel">
      <Banners report={report} />
      <AssessmentPlanCard plan={report.assessment_plan} />
      <CompileStrategyCard report={report} />
      <CompileAttemptsTimeline attempts={report.attempts} />
      <FinalQualitySummary report={report} />
    </div>
  );
}

// ---- 1) Assessment Plan card ---------------------------------------

function AssessmentPlanCard({ plan }: { plan: AssessmentPlanPayload }) {
  const required = plan.required_capabilities ?? [];
  const optional = plan.optional_capabilities ?? [];
  const risk = plan.risk_flags ?? [];
  return (
    <div className="card" data-testid="assessment-plan-card">
      <h3>Assessment Plan</h3>
      <dl className="kv">
        <dt>Selected mode</dt>
        <dd>
          <span className={`badge mode-badge mode-badge--${plan.mode ?? "unknown"}`}>
            {plan.mode ?? "—"}
          </span>
        </dd>
        <dt>Confidence</dt>
        <dd>{plan.confidence != null ? `${(plan.confidence * 100).toFixed(0)}%` : "—"}</dd>
        <dt>Document type</dt>
        <dd>{plan.document_type ?? "—"}</dd>
        <dt>Complexity</dt>
        <dd>{plan.complexity ?? "—"}</dd>
        <dt>Required capabilities</dt>
        <dd>{required.length === 0 ? "—" : <CapList caps={required} />}</dd>
        <dt>Optional capabilities</dt>
        <dd>{optional.length === 0 ? "—" : <CapList caps={optional} />}</dd>
        <dt>Risk flags</dt>
        <dd>
          {risk.length === 0 ? "—" : (
            <ul className="bullet-list">
              {risk.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
        </dd>
        <dt>Reason</dt>
        <dd>{plan.reason || "—"}</dd>
      </dl>
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

// ---- 2) Compile Strategy card --------------------------------------

function CompileStrategyCard({ report }: { report: CompileStrategyReport }) {
  const required = report.assessment_plan.required_capabilities ?? [];
  const unhandled = report.unhandled_capabilities ?? [];
  const applied = appliedCapabilities(report);
  const lastAttempt = report.attempts[report.attempts.length - 1];
  const mappedConfig = lastAttempt?.mapped_compile_config ?? {};
  const planMissing = isFallbackOnly(report);
  return (
    <div className="card" data-testid="compile-strategy-card">
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
  );
}

// ---- 3) Compile Attempts timeline ---------------------------------

function CompileAttemptsTimeline({
  attempts,
}: { attempts: CompileAttemptRecord[] }) {
  if (!attempts.length) {
    return (
      <div className="card" data-testid="compile-attempts-empty">
        <h3>Compile Attempts</h3>
        <p style={{ color: "var(--text-muted)" }}>No attempts recorded.</p>
      </div>
    );
  }
  return (
    <div className="card" data-testid="compile-attempts-timeline">
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
          {b.message}
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
  );
}
