/**
 * Always-visible state hero rendered directly under the run header.
 * Renders a different panel for every lifecycle status — assessing,
 * awaiting confirmation, running, success, warnings, failed, human
 * review, and cancelled. Output metrics on the success state are
 * scraped from completed step messages (chunks indexed, graph nodes,
 * etc.).
 */

import { useMemo } from "react";
import { Icon } from "@/components/icons";
import type { ExecutionPlan, IngestionRun, ProgressEvent } from "@/types/ingestion";

interface PrimaryStatusPanelProps {
  run: IngestionRun | null;
  plan: ExecutionPlan | null;
  events: ProgressEvent[];
}

interface OutputMetrics {
  chunks?: string;
  nodes?: string;
  edges?: string;
  sections?: string;
  tables?: string;
  entities?: string;
}

function deriveOutputs(events: ProgressEvent[]): OutputMetrics {
  const out: OutputMetrics = {};
  for (const e of events) {
    if (e.event !== "step.completed") continue;
    const msg = e.data?.message || "";
    let m: RegExpMatchArray | null;
    if ((m = msg.match(/(\d[\d,]*)\s+chunks?\s+indexed/i))) out.chunks = m[1];
    if ((m = msg.match(/(\d[\d,]*)\s+nodes?,\s+(\d[\d,]*)\s+edges?/i))) {
      out.nodes = m[1];
      out.edges = m[2];
    }
    if ((m = msg.match(/(\d[\d,]*)\s+sections?/i))) out.sections = m[1];
    if ((m = msg.match(/(\d[\d,]*)\s+tables?/i))) out.tables = m[1];
    if ((m = msg.match(/(\d[\d,]*)\s+entities/i))) out.entities = m[1];
  }
  return out;
}

export function PrimaryStatusPanel({ run, plan, events }: PrimaryStatusPanelProps) {
  const outputs = useMemo(() => deriveOutputs(events), [events]);

  if (!run) return null;
  const status = run.status;
  const final = run.final;

  if (status === "PLAN_READY" || status === "WAITING_FOR_CONFIRMATION") {
    const summary = plan?.summary;
    const total = (summary?.run ?? 0) + (summary?.skip ?? 0) + (summary?.conditional ?? 0);
    return (
      <div className="psp psp--awaiting">
        <div className="psp__icon">
          <Icon.Alert className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Awaiting confirmation</div>
          <h2 className="psp__title">Review the plan before execution starts</h2>
          <p className="psp__lede">
            The assessor identified <strong>{total}</strong> candidate steps.{" "}
            <strong>{summary?.run ?? 0}</strong> will run, <strong>{summary?.skip ?? 0}</strong>{" "}
            will be skipped, <strong>{summary?.conditional ?? 0}</strong> are conditional.
            Confirm the plan on the right to begin.
          </p>
        </div>
      </div>
    );
  }

  if (status === "ASSESSING" || status === "CREATED") {
    return (
      <div className="psp psp--assessing">
        <div className="psp__icon">
          <Icon.RefreshCw className="icon spin" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Assessing</div>
          <h2 className="psp__title">Building execution plan…</h2>
          <p className="psp__lede">
            Analyzing document characteristics, language, structure, and content to determine
            which pipeline steps apply.
          </p>
        </div>
      </div>
    );
  }

  if (status === "RUNNING") {
    const stage = run.current_stage || "—";
    const step = run.current_step || "—";
    const pct = Math.round(run.progress_pct || 0);
    return (
      <div className="psp psp--running">
        <div className="psp__icon">
          <Icon.RefreshCw className="icon spin" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Running · {pct}%</div>
          <h2 className="psp__title">
            Executing {stage} · <span className="psp__step">{step}</span>
          </h2>
          <p className="psp__lede">
            Streaming events live from the pipeline. Watch the timeline on the right for
            per-step progress.
          </p>
          <div className="psp__progress">
            <div className="psp__progress-bar" style={{ width: `${pct}%` }} />
          </div>
        </div>
      </div>
    );
  }

  if (status === "FAILED") {
    return (
      <div className="psp psp--failed">
        <div className="psp__icon">
          <Icon.XCircle className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Failed</div>
          <h2 className="psp__title">{final?.failure_code || "Run failed"}</h2>
          <p className="psp__lede">
            {final?.failure_message || "The run terminated with an error."}
          </p>
          {final?.failed_step && (
            <div className="psp__meta">
              <span className="psp__meta-label">Failed step</span>
              <code className="psp__meta-value">{final.failed_step}</code>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (status === "AWAITING_HUMAN_REVIEW" || status === "REQUIRES_HUMAN_REVIEW") {
    return (
      <div className="psp psp--review">
        <div className="psp__icon">
          <Icon.UserCheck className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Human review required</div>
          <h2 className="psp__title">
            {final?.reason || "Manual review needed before continuing"}
          </h2>
          <p className="psp__lede">
            {final?.detail ||
              "A reviewer must approve or reject this run before it can proceed."}
          </p>
        </div>
      </div>
    );
  }

  if (status === "CANCELLED") {
    return (
      <div className="psp psp--cancelled">
        <div className="psp__icon">
          <Icon.X className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Cancelled</div>
          <h2 className="psp__title">Run cancelled</h2>
          <p className="psp__lede">This run was cancelled before completion.</p>
        </div>
      </div>
    );
  }

  if (
    status === "COMPLETED" ||
    status === "COMPLETED_WITH_WARNINGS" ||
    status === "SUCCEEDED" ||
    status === "SUCCEEDED_WITH_WARNINGS"
  ) {
    const hasWarnings =
      status === "COMPLETED_WITH_WARNINGS" ||
      status === "SUCCEEDED_WITH_WARNINGS" ||
      (run.warning_count || 0) > 0;
    const metrics: { label: string; value: string }[] = [];
    if (outputs.chunks) metrics.push({ label: "Chunks indexed", value: outputs.chunks });
    if (outputs.nodes) metrics.push({ label: "Graph nodes", value: outputs.nodes });
    if (outputs.edges) metrics.push({ label: "Graph edges", value: outputs.edges });
    if (outputs.sections) metrics.push({ label: "Sections", value: outputs.sections });
    if (outputs.tables) metrics.push({ label: "Tables", value: outputs.tables });
    if (outputs.entities) metrics.push({ label: "Entities", value: outputs.entities });

    const wc = run.warning_count || 1;
    return (
      <div className={`psp ${hasWarnings ? "psp--warnings" : "psp--success"}`}>
        <div className="psp__icon">
          {hasWarnings ? (
            <Icon.Alert className="icon" />
          ) : (
            <Icon.CheckCircle className="icon" />
          )}
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">
            {hasWarnings ? `Completed with ${wc} warning${wc === 1 ? "" : "s"}` : "Completed"}
          </div>
          <h2 className="psp__title">
            {hasWarnings ? "Indexed with warnings" : "Document indexed and ready to query"}
          </h2>
          <p className="psp__lede">
            {hasWarnings
              ? final?.warning_summary ||
                "The pipeline completed but flagged issues that may affect retrieval quality."
              : "All pipeline stages completed successfully. The document is now searchable across vector, graph, and structured indexes."}
          </p>
          {metrics.length > 0 && (
            <div className="psp__metrics">
              {metrics.map((m) => (
                <div key={m.label} className="psp__metric">
                  <span className="psp__metric-value">{m.value}</span>
                  <span className="psp__metric-label">{m.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return null;
}
