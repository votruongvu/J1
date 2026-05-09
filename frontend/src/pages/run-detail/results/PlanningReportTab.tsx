/**
 * Results > Planning Report tab.
 *
 * Renders the Planning Report from
 * `GET /ingestion-runs/{id}/planning`. Available as soon as the
 * planner emits a `plan.generated` event, even while downstream
 * stages are still running.
 *
 * Three states:
 *   * `status="completed"` → assessment + decisions + (optional) digest.
 *   * `status="unavailable"` → backend's `unavailableReason` copy.
 *   * load error → inline alert.
 */

import { useEffect, useMemo, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type {
  PlanningResult,
  PlanningStepDecision,
} from "@/types/review";

interface PlanningReportTabProps {
  runId: string;
}

export function PlanningReportTab({ runId }: PlanningReportTabProps) {
  const client = useClient();
  const [report, setReport] = useState<PlanningResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await client.getRunPlanning(runId);
        if (!cancelled) setReport(result);
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof Error ? e.message : "Failed to load planning report.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  if (error) {
    return (
      <div className="results__empty" role="alert">
        <strong>Couldn&apos;t load planning report.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{error}</div>
      </div>
    );
  }
  if (loading || !report) {
    return (
      <div className="results__empty" aria-busy="true">
        Loading planning report…
      </div>
    );
  }

  if (report.status === "unavailable") {
    return (
      <div className="results__empty">
        <strong>Planning report not available</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 6 }}>
          {report.unavailableReason ??
            "No plan has been generated for this run yet."}
        </div>
      </div>
    );
  }

  const { assessment, digest, llmRecommendation, decisions } = report;

  return (
    <div className="results-content-inventory">
      {/* Header strip — document + plan timestamp */}
      <section className="results-content-inventory__source">
        <SourceField label="Document">
          {report.documentName ?? report.documentId ?? "—"}
        </SourceField>
        <SourceField label="Generated">
          {formatTimestamp(report.generatedAt)}
          {report.revised ? (
            <span
              className="results-tag results-tag--info"
              style={{ marginLeft: 6 }}
            >
              revised
            </span>
          ) : null}
        </SourceField>
      </section>

      {/* Assessment scorecards */}
      {assessment ? (
        <>
          <section className="results-content-inventory__summary">
            <SummaryCard label="Mode" value={assessment.mode || "—"} />
            <SummaryCard label="Policy" value={assessment.policy || "—"} />
            <SummaryCard
              label="Confidence"
              value={formatConfidence(assessment.confidence)}
            />
            <SummaryCard
              label="Cost level"
              value={assessment.estimatedCostLevel || "—"}
            />
            <SummaryCard
              label="Vision"
              value={assessment.requiresVision ? "yes" : "no"}
            />
            <SummaryCard
              label="Premium LLM"
              value={assessment.requiresPremiumLlm ? "yes" : "no"}
              tone={assessment.requiresPremiumLlm ? "accent" : undefined}
            />
          </section>

          {assessment.reasons.length > 0 ? (
            <section className="results-content-inventory__items">
              <h4 style={{ margin: "8px 0 6px" }}>Why this plan</h4>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {assessment.reasons.map((reason, idx) => (
                  <li key={idx} style={{ color: "var(--text-muted)" }}>
                    {reason}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {assessment.warnings.length > 0 ? (
            <section className="results-content-inventory__items">
              <h4 style={{ margin: "8px 0 6px" }}>Warnings</h4>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {assessment.warnings.map((warning, idx) => (
                  <li
                    key={idx}
                    style={{ color: "var(--text-warning, #b8860b)" }}
                  >
                    {warning}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      ) : null}

      {/* Per-step decisions table */}
      {decisions.length > 0 ? (
        <section className="results-content-inventory__items">
          <h4 style={{ margin: "8px 0 6px" }}>Per-step decisions</h4>
          <table className="results-graph__table" role="table">
            <thead>
              <tr>
                <th>Stage</th>
                <th>Decision</th>
                <th>Source</th>
                <th>Cost</th>
                <th>LLM</th>
                <th>Risk</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => (
                <DecisionRow key={d.stepId} decision={d} />
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {/* Content digest panel — only when the manifest is available. */}
      {digest ? (
        <section className="results-content-inventory__items">
          <h4 style={{ margin: "8px 0 6px" }}>Content digest</h4>
          <div className="results-content-inventory__summary">
            <SummaryCard label="Pages" value={digest.pageCount ?? "—"} />
            <SummaryCard label="Text blocks" value={digest.textBlockCount} />
            <SummaryCard label="Tables" value={digest.tableCount} />
            <SummaryCard label="Images" value={digest.imageCount} />
            <SummaryCard label="Formulas" value={digest.formulaCount} />
            <SummaryCard
              label="Sampled blocks"
              value={`${digest.sampledBlockCount}/${digest.textBlockCount}`}
            />
            <SummaryCard
              label="Preview cap"
              value={`${digest.maxPreviewChars} chars`}
            />
          </div>
          <p
            style={{
              color: "var(--text-muted)",
              fontSize: 12,
              marginTop: 6,
            }}
          >
            Privacy: when LLM-assisted planning is enabled, only this
            sampled digest is sent to the planner. The full document is
            never transmitted.
          </p>
        </section>
      ) : null}

      {/* LLM recommendation block */}
      <section className="results-content-inventory__items">
        <h4 style={{ margin: "8px 0 6px" }}>LLM-assisted planning</h4>
        <LLMRecommendation rec={llmRecommendation} />
      </section>
    </div>
  );
}

function SourceField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="results-content-inventory__source-field">
      <span className="results-content-inventory__source-label">{label}</span>
      <span className="results-content-inventory__source-value">{children}</span>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string | null | undefined;
  tone?: "accent";
}) {
  return (
    <div
      className={`results-conf-card${tone === "accent" ? " results-conf-card--good" : ""}`}
    >
      <div className="results-conf-card__label">{label}</div>
      <div className="results-conf-card__value">{value ?? "—"}</div>
    </div>
  );
}

function DecisionRow({ decision }: { decision: PlanningStepDecision }) {
  const decisionTone = useMemo(() => {
    if (decision.decision === "RUN") return "info";
    if (decision.decision === "CONDITIONAL") return "warning";
    return "muted";
  }, [decision.decision]);
  const riskTone = useMemo(() => {
    if (decision.riskLevel === "high") return "warning";
    if (decision.riskLevel === "medium") return "info";
    return "muted";
  }, [decision.riskLevel]);
  return (
    <tr>
      <td>
        <strong>{decision.stage || decision.stepId}</strong>
        {decision.required ? (
          <span
            className="results-tag results-tag--info"
            style={{ marginLeft: 6 }}
          >
            required
          </span>
        ) : null}
      </td>
      <td>
        <span className={`results-tag results-tag--${decisionTone}`}>
          {decision.decision}
        </span>
      </td>
      <td>{decision.source}</td>
      <td>{decision.estimatedCostTier}</td>
      <td>{decision.llmClass}</td>
      <td>
        <span className={`results-tag results-tag--${riskTone}`}>
          {decision.riskLevel}
        </span>
      </td>
      <td className="results-graph__desc">
        {decision.reason ?? (
          <span className="results-graph__placeholder">—</span>
        )}
      </td>
    </tr>
  );
}

function LLMRecommendation({
  rec,
}: {
  rec: PlanningResult["llmRecommendation"];
}) {
  if (rec.status === "disabled") {
    return (
      <div style={{ color: "var(--text-muted)" }}>
        LLM-assisted planning is disabled. Rule-based planning was used.
      </div>
    );
  }
  if (rec.status === "failed") {
    return (
      <div role="alert">
        <strong>LLM planning failed.</strong>
        <div style={{ color: "var(--text-muted)", marginTop: 4 }}>
          {rec.failureReason ?? "Rule-based decision was retained."}
        </div>
      </div>
    );
  }
  return (
    <div>
      <div>
        <span className="results-tag results-tag--info">{rec.status}</span>
        {rec.modelProfile ? (
          <span style={{ marginLeft: 8, color: "var(--text-muted)" }}>
            ({rec.modelProfile})
          </span>
        ) : null}
      </div>
      {rec.summary ? (
        <p style={{ marginTop: 6, color: "var(--text-muted)" }}>
          {rec.summary}
        </p>
      ) : null}
    </div>
  );
}

function formatConfidence(value: number): string {
  if (Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleString();
}
