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
  PlanningContentReport,
  PlanningDocumentUnderstanding,
  PlanningExecutionPlan,
  PlanningQualityReport,
  PlanningResult,
  PlanningRuleBasedComparison,
  PlanningStepDecision,
  PlanningStepEntry,
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

  const {
    assessment, digest, llmRecommendation, decisions,
    documentUnderstanding, contentReport, qualityReport,
    executionPlan, ruleBasedComparison, decisionSummary,
    nextActions, warnings,
  } = report;
  const isPostCompile = report.planningPhase === "post_compile";

  return (
    <div className="results-content-inventory">
      {/* Header strip — document + plan timestamp + source */}
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
        {report.source ? (
          <SourceField label="Source">
            <span className="results-tag results-tag--info">
              {report.source}
            </span>
          </SourceField>
        ) : null}
        {isPostCompile ? (
          <SourceField label="Phase">
            <span className="results-tag results-tag--info">post-compile</span>
          </SourceField>
        ) : null}
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

      {/* Document Understanding (post-compile only) */}
      {documentUnderstanding ? (
        <DocumentUnderstandingPanel data={documentUnderstanding} />
      ) : null}

      {/* Content Report (post-compile only) */}
      {contentReport ? <ContentReportPanel data={contentReport} /> : null}

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

      {/* Execution Plan with selective scopes (post-compile only) */}
      {executionPlan ? <ExecutionPlanPanel plan={executionPlan} /> : null}

      {/* Quality Report (post-compile only) */}
      {qualityReport ? <QualityReportPanel data={qualityReport} /> : null}

      {/* Decision summary main reasoning */}
      {decisionSummary?.mainReasoning &&
       decisionSummary.mainReasoning.length > 0 ? (
        <section className="results-content-inventory__items">
          <h4 style={{ margin: "8px 0 6px" }}>Decision summary</h4>
          {decisionSummary.overallAssessment ? (
            <p style={{ marginTop: 0 }}>{decisionSummary.overallAssessment}</p>
          ) : null}
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {decisionSummary.mainReasoning.map((reason, idx) => (
              <li key={idx} style={{ color: "var(--text-muted)" }}>
                {reason}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* Rule-based vs LLM comparison */}
      {ruleBasedComparison &&
       (ruleBasedComparison.acceptedRuleRecommendations?.length ||
        ruleBasedComparison.overriddenRuleRecommendations?.length) ? (
        <RuleBasedComparisonPanel data={ruleBasedComparison} />
      ) : null}

      {/* Warnings & next actions */}
      {warnings && warnings.length > 0 ? (
        <section className="results-content-inventory__items">
          <h4 style={{ margin: "8px 0 6px" }}>Warnings</h4>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {warnings.map((w, idx) => (
              <li key={idx} style={{ color: "var(--text-warning, #b8860b)" }}>
                {w}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {nextActions && nextActions.length > 0 ? (
        <section className="results-content-inventory__items">
          <h4 style={{ margin: "8px 0 6px" }}>Next actions</h4>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {nextActions.map((a, idx) => (
              <li key={idx}>{a}</li>
            ))}
          </ul>
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

// ---- Post-compile section components ---------------------------

function DocumentUnderstandingPanel({
  data,
}: {
  data: PlanningDocumentUnderstanding;
}) {
  const bias = data.recommendedAnalysisBias;
  return (
    <section className="results-content-inventory__items">
      <h4 style={{ margin: "8px 0 6px" }}>Document understanding</h4>
      <div className="results-content-inventory__summary">
        <SummaryCard label="Type" value={data.documentType ?? "—"} />
        <SummaryCard
          label="Type confidence"
          value={
            data.documentTypeConfidence != null
              ? `${Math.round((data.documentTypeConfidence ?? 0) * 100)}%`
              : "—"
          }
        />
        <SummaryCard
          label="Title quality"
          value={data.titleQuality ?? "—"}
          tone={data.titleQuality === "clear" ? "accent" : undefined}
        />
        <SummaryCard label="Importance" value={data.documentImportance ?? "—"} />
        <SummaryCard label="Audience" value={data.intendedAudience ?? "—"} />
        <SummaryCard label="Domain" value={data.businessDomain || "—"} />
      </div>
      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "max-content 1fr",
          gap: "4px 12px",
          margin: "8px 0 0",
        }}
      >
        <dt style={{ color: "var(--text-muted)" }}>Detected title</dt>
        <dd style={{ margin: 0 }}>{data.detectedTitle || "—"}</dd>
        <dt style={{ color: "var(--text-muted)" }}>Primary topic</dt>
        <dd style={{ margin: 0 }}>{data.primaryTopic || "—"}</dd>
        <dt style={{ color: "var(--text-muted)" }}>Document purpose</dt>
        <dd style={{ margin: 0 }}>{data.documentPurpose || "—"}</dd>
      </dl>
      {bias?.reason ? (
        <p style={{ marginTop: 8, color: "var(--text-muted)" }}>
          <strong>Analysis bias:</strong> {bias.reason}
        </p>
      ) : null}
      {data.expectedInformationTypes &&
       data.expectedInformationTypes.length > 0 ? (
        <p style={{ marginTop: 4 }}>
          <strong>Expected information:</strong>{" "}
          {data.expectedInformationTypes.map((t, i) => (
            <span
              key={i}
              className="results-tag results-tag--info"
              style={{ marginRight: 4 }}
            >
              {t}
            </span>
          ))}
        </p>
      ) : null}
      {data.evidence && data.evidence.length > 0 ? (
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: "pointer" }}>
            Evidence ({data.evidence.length})
          </summary>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {data.evidence.map((e, idx) => (
              <li key={idx}>
                <code className="results-graph__id">{e.source}</code>
                {e.page != null ? <span> — page {e.page}</span> : null}
                {e.reason ? (
                  <span style={{ color: "var(--text-muted)" }}>
                    {" "}— {e.reason}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function ContentReportPanel({ data }: { data: PlanningContentReport }) {
  return (
    <section className="results-content-inventory__items">
      <h4 style={{ margin: "8px 0 6px" }}>Content report</h4>
      <div className="results-content-inventory__summary">
        <SummaryCard label="Pages" value={data.pageCount ?? "—"} />
        <SummaryCard label="Structure" value={data.structureQuality ?? "—"} />
        <SummaryCard label="Layout" value={data.layoutComplexity ?? "—"} />
        <SummaryCard label="Density" value={data.contentDensity ?? "—"} />
        <SummaryCard
          label="Tables"
          value={data.hasTables ? "yes" : "no"}
        />
        <SummaryCard
          label="Images"
          value={data.hasImages ? "yes" : "no"}
        />
        <SummaryCard
          label="OCR pages"
          value={data.hasOcrPages ? "yes" : "no"}
        />
      </div>
      {data.importantObservations && data.importantObservations.length > 0 ? (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {data.importantObservations.map((o, idx) => (
            <li key={idx} style={{ color: "var(--text-muted)" }}>
              {o}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function ExecutionPlanPanel({ plan }: { plan: PlanningExecutionPlan }) {
  const steps = plan.steps ?? {};
  const ordered = Object.entries(steps);
  const chunking = steps.chunking;
  return (
    <section className="results-content-inventory__items">
      <h4 style={{ margin: "8px 0 6px" }}>Execution plan</h4>
      <div className="results-content-inventory__summary">
        <SummaryCard label="Estimated time" value={plan.estimatedTime ?? "—"} />
        <SummaryCard label="Estimated cost" value={plan.estimatedCost ?? "—"} />
        {chunking?.strategy ? (
          <SummaryCard label="Chunking" value={chunking.strategy} />
        ) : null}
      </div>
      {ordered.length > 0 ? (
        <table className="results-graph__table" role="table">
          <thead>
            <tr>
              <th>Step</th>
              <th>Enabled</th>
              <th>Scope</th>
              <th>Pages</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {ordered.map(([name, entry]) => (
              <ExecutionStepRow key={name} name={name} entry={entry} />
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}

function ExecutionStepRow({
  name, entry,
}: { name: string; entry: PlanningStepEntry }) {
  const pages = entry.pages ?? [];
  return (
    <tr>
      <td>
        <strong>{name}</strong>
        {entry.strategy ? (
          <span
            className="results-tag results-tag--info"
            style={{ marginLeft: 6 }}
          >
            {entry.strategy}
          </span>
        ) : null}
      </td>
      <td>
        <span
          className={`results-tag results-tag--${entry.enabled ? "info" : "muted"}`}
        >
          {entry.enabled ? "RUN" : "SKIP"}
        </span>
      </td>
      <td>{entry.scope ?? "—"}</td>
      <td>
        {pages.length === 0 ? (
          <span className="results-graph__placeholder">—</span>
        ) : pages.length <= 8 ? (
          pages.join(", ")
        ) : (
          `${pages.slice(0, 8).join(", ")}… (+${pages.length - 8})`
        )}
      </td>
      <td className="results-graph__desc">
        {entry.reason ?? <span className="results-graph__placeholder">—</span>}
      </td>
    </tr>
  );
}

function QualityReportPanel({ data }: { data: PlanningQualityReport }) {
  return (
    <section className="results-content-inventory__items">
      <h4 style={{ margin: "8px 0 6px" }}>Parse quality &amp; risk</h4>
      <div className="results-content-inventory__summary">
        <SummaryCard
          label="Parse confidence"
          value={data.parseConfidence ?? "—"}
        />
        <SummaryCard label="Risk" value={data.riskLevel ?? "—"} />
        <SummaryCard
          label="Manual review"
          value={data.manualReviewRequired ? "required" : "no"}
          tone={data.manualReviewRequired ? "accent" : undefined}
        />
      </div>
      {data.detectedIssues && data.detectedIssues.length > 0 ? (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {data.detectedIssues.map((iss, idx) => (
            <li key={idx}>
              <span
                className={`results-tag results-tag--${iss.severity === "high" ? "warning" : "info"}`}
                style={{ marginRight: 6 }}
              >
                {iss.severity}
              </span>
              <strong>{iss.issue}</strong>
              {iss.recommendation ? (
                <div style={{ color: "var(--text-muted)", marginTop: 2 }}>
                  {iss.recommendation}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {data.manualReviewCandidates && data.manualReviewCandidates.length > 0 ? (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: "pointer" }}>
            Review candidates ({data.manualReviewCandidates.length})
          </summary>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {data.manualReviewCandidates.map((c, idx) => (
              <li key={idx}>
                <strong>page {c.page}</strong> — {c.reason}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function RuleBasedComparisonPanel({
  data,
}: {
  data: PlanningRuleBasedComparison;
}) {
  return (
    <section className="results-content-inventory__items">
      <h4 style={{ margin: "8px 0 6px" }}>Rule-based vs LLM</h4>
      {data.acceptedRuleRecommendations &&
       data.acceptedRuleRecommendations.length > 0 ? (
        <p style={{ marginTop: 0 }}>
          <strong>Accepted:</strong>{" "}
          {data.acceptedRuleRecommendations.map((r) => (
            <span
              key={r}
              className="results-tag results-tag--info"
              style={{ marginRight: 4 }}
            >
              {r}
            </span>
          ))}
        </p>
      ) : null}
      {data.overriddenRuleRecommendations &&
       data.overriddenRuleRecommendations.length > 0 ? (
        <table className="results-graph__table" role="table">
          <thead>
            <tr>
              <th>Step</th>
              <th>Rule-based</th>
              <th>LLM</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {data.overriddenRuleRecommendations.map((o, idx) => (
              <tr key={idx}>
                <td>
                  <strong>{o.rule}</strong>
                </td>
                <td>{o.originalRecommendation}</td>
                <td>{o.llmRecommendation}</td>
                <td className="results-graph__desc">{o.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
