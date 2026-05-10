/**
 * Assessment Plan panel — surfaced FIRST on the run detail page so
 * operators see exactly which compile strategy J1 picked before
 * diving into outputs.
 *
 * Source of truth: the `compile_strategy_report` artifact. The
 * AssessmentPlan is built pre-compile (cheap deterministic profile
 * + `DefaultAssessmentPlanner`) and persisted as part of the
 * report once compile finishes. While compile is in flight, this
 * panel shows a loading state — the assessment phase shows up on
 * the LiveTimeline as `assess_compile_strategy` so operators
 * still see progress.
 *
 * Information surfaced (from richest to terse):
 *   * Selected mode (big badge) + one-line description of what
 *     that mode does.
 *   * Confidence (badge color-coded by bucket).
 *   * Document type + complexity (signals the planner picked up).
 *   * Required + optional capabilities (pills).
 *   * Risk flags (warning style).
 *   * Reason the planner chose this mode.
 *   * Resolved compile config (parse_method actually sent to the
 *     parser).
 *   * Mode escalation hint when the safety retry layer changed the
 *     mode mid-flight (initial → final).
 *   * Unhandled capabilities the deployment couldn't honour.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import {
  COMPILE_STRATEGY_REPORT_KIND,
  capabilityLabel,
  confidenceBucket,
  formatConfidence,
  hasModeEscalation,
  isFallbackOnly,
  modeDescription,
  resolvedCompileConfig,
  type AssessmentPlanPayload,
  type CompileStrategyReport,
} from "./compile-strategy-helpers";

interface AssessmentPlanPanelProps {
  runId: string;
  /**
   * Latest SSE event from the parent page. When relevant events
   * arrive (step lifecycle, run terminal) the panel re-fetches its
   * artifact so a panel that mounted BEFORE compile finished
   * eventually picks up the report once it lands. Without this the
   * panel sticks at "missing" forever for runs the user navigated
   * into mid-flight.
   */
  latestEvent?: ProgressEvent | null;
}

export function AssessmentPlanPanel({
  runId, latestEvent,
}: AssessmentPlanPanelProps) {
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
          // Don't downgrade an already-loaded report on a transient
          // refetch miss — only flip to "missing" if we never had
          // data in the first place.
          if (!cancelled) {
            setState((prev) => prev.kind === "ready" ? prev : { kind: "missing" });
          }
          return;
        }
        const content = await client.getRunArtifactContent(
          runId, artifact.artifactId,
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

  // Initial load on mount / runId change.
  useEffect(() => {
    setState({ kind: "loading" });
    return loadReport();
  }, [loadReport]);

  // SSE-driven refresh: re-fetch when compile-completion-adjacent
  // events arrive. Cheap (one HTTP call returning a few KB) and lets
  // a panel mounted mid-flight pick up the report once it lands.
  useEffect(() => {
    if (!latestEvent) return;
    const refreshOn = new Set<string>([
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
      <div className="card" data-testid="assessment-plan-loading">
        <h3>Assessment Plan</h3>
        <p style={{ color: "var(--text-muted)" }}>
          Profiling document and building AssessmentPlan…
        </p>
      </div>
    );
  }
  if (state.kind === "missing") {
    return (
      <div className="card" data-testid="assessment-plan-missing">
        <h3>Assessment Plan</h3>
        <p style={{ color: "var(--text-muted)" }}>
          No assessment data available for this run yet. The plan is
          persisted as part of the compile-strategy report once compile
          completes.
        </p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="card" data-testid="assessment-plan-error">
        <h3>Assessment Plan</h3>
        <p style={{ color: "var(--error-fg)" }}>
          Couldn't load assessment plan: {state.message}
        </p>
      </div>
    );
  }
  return <AssessmentPlanContent report={state.report} />;
}

function AssessmentPlanContent({ report }: { report: CompileStrategyReport }) {
  const plan = report.assessment_plan;
  const initialPlan = report.initial_assessment_plan;
  const fallback = isFallbackOnly(report);
  const escalated = hasModeEscalation(report);
  const mappedConfig = resolvedCompileConfig(report);
  const required = plan.required_capabilities ?? [];
  const optional = plan.optional_capabilities ?? [];
  const risk = plan.risk_flags ?? [];
  const unhandled = report.unhandled_capabilities ?? [];
  const planWarnings = report.plan_warnings ?? [];
  const confBucket = confidenceBucket(plan.confidence);

  return (
    <div
      className="card assessment-plan-panel"
      data-testid="assessment-plan-panel"
    >
      <div className="assessment-plan-panel__head">
        <h3>Assessment Plan</h3>
        <span
          className="assessment-plan-panel__source"
          data-testid="assessment-plan-source"
        >
          {fallback
            ? "fallback (no AssessmentPlan attached)"
            : "rule-based assessor"}
        </span>
      </div>

      {/* Hero: mode + confidence side by side */}
      <div className="assessment-plan-panel__hero">
        <div className="assessment-plan-panel__hero-item">
          <div className="assessment-plan-panel__label">Selected mode</div>
          <span
            className={`badge mode-badge mode-badge--lg mode-badge--${plan.mode ?? "unknown"}`}
            data-testid="assessment-plan-mode"
          >
            {plan.mode ?? "—"}
          </span>
          <div
            className="assessment-plan-panel__hint"
            data-testid="assessment-plan-mode-description"
          >
            {modeDescription(plan.mode)}
          </div>
        </div>
        <div className="assessment-plan-panel__hero-item">
          <div className="assessment-plan-panel__label">Confidence</div>
          <span
            className={`badge confidence-badge confidence-badge--${confBucket}`}
            data-testid="assessment-plan-confidence"
          >
            {formatConfidence(plan.confidence)}
          </span>
          <div className="assessment-plan-panel__hint">
            {confBucket === "low" && "Operator review recommended."}
            {confBucket === "medium" && "Some signals were ambiguous."}
            {confBucket === "high" && "Strong signals; planner is confident."}
            {confBucket === "unknown" && "Confidence not reported."}
          </div>
        </div>
      </div>

      {escalated && (
        <div
          className="assessment-plan-panel__escalation"
          data-testid="assessment-plan-escalation"
        >
          <strong>Compile-safety retry escalated mode:</strong>{" "}
          <span className="mono">{report.initial_mode}</span> →{" "}
          <span className="mono">{report.final_mode}</span>
          {initialPlan?.mode && initialPlan.mode !== plan.mode && (
            <span className="muted">
              {" "}(initial assessor confidence{" "}
              {formatConfidence(initialPlan.confidence)})
            </span>
          )}
        </div>
      )}

      {/* Profile signals the assessor used */}
      <dl className="kv assessment-plan-panel__kv">
        <dt>Document type</dt>
        <dd>{plan.document_type ?? "—"}</dd>
        <dt>Complexity</dt>
        <dd>{plan.complexity ?? "—"}</dd>
        <dt>Fallback policy</dt>
        <dd className="mono">{plan.fallback_policy ?? "—"}</dd>
        <dt>Reason</dt>
        <dd data-testid="assessment-plan-reason">
          {plan.reason || "—"}
        </dd>
      </dl>

      {/* Capabilities */}
      <div className="assessment-plan-panel__caps">
        <div>
          <div className="assessment-plan-panel__label">
            Required capabilities ({required.length})
          </div>
          {required.length === 0 ? (
            <span className="muted">—</span>
          ) : (
            <CapabilityPills caps={required} variant="required" />
          )}
        </div>
        <div>
          <div className="assessment-plan-panel__label">
            Optional capabilities ({optional.length})
          </div>
          {optional.length === 0 ? (
            <span className="muted">—</span>
          ) : (
            <CapabilityPills caps={optional} variant="optional" />
          )}
        </div>
      </div>

      {/* Risk flags */}
      {risk.length > 0 && (
        <div
          className="assessment-plan-panel__risks"
          data-testid="assessment-plan-risks"
        >
          <div className="assessment-plan-panel__label">
            Risk flags ({risk.length})
          </div>
          <ul className="bullet-list">
            {risk.map((r, i) => <li key={i}>{capabilityLabel(r)}</li>)}
          </ul>
        </div>
      )}

      {/* Resolved compile config */}
      <div
        className="assessment-plan-panel__config"
        data-testid="assessment-plan-config"
      >
        <div className="assessment-plan-panel__label">
          Resolved compile config
        </div>
        <dl className="kv">
          <dt>parse_method</dt>
          <dd className="mono">{mappedConfig.parse_method ?? "—"}</dd>
          <dt>assessment_mode</dt>
          <dd className="mono">{mappedConfig.assessment_mode ?? "—"}</dd>
        </dl>
      </div>

      {unhandled.length > 0 && (
        <div
          className="assessment-plan-panel__unhandled"
          data-testid="assessment-plan-unhandled"
        >
          <div className="assessment-plan-panel__label">
            Unhandled capabilities ({unhandled.length})
          </div>
          <p className="muted">
            The deployment couldn't honour these requested capabilities.
            Compile may have produced lower-quality output for these areas.
          </p>
          <CapabilityPills caps={unhandled} variant="unhandled" />
        </div>
      )}

      {planWarnings.length > 0 && (
        <div
          className="assessment-plan-panel__warnings"
          data-testid="assessment-plan-warnings"
        >
          <div className="assessment-plan-panel__label">
            Plan warnings ({planWarnings.length})
          </div>
          <ul className="bullet-list">
            {planWarnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function CapabilityPills({
  caps,
  variant,
}: {
  caps: string[];
  variant: "required" | "optional" | "unhandled";
}) {
  return (
    <div className="cap-pills">
      {caps.map((c) => (
        <span
          key={c}
          className={`badge cap-pill cap-pill--${variant}`}
        >
          {capabilityLabel(c)}
        </span>
      ))}
    </div>
  );
}

// Re-export the panel type so tests can reference it.
export type { AssessmentPlanPayload };
