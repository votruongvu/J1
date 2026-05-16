/**
 * Always-visible state hero rendered directly under the run header.
 *
 * branches on the 6-state UI surface (PENDING /
 * RUNNING / COMPLETED / COMPLETED_WITH_WARNINGS / FAILED / CANCELLED)
 * computed by `projectUiState`. The underlying `INGESTION_STATUS_*`
 * literal (A–F) picks the specific copy + the failure-vs-warning
 * accent so operators see "completed without enrichment" rendered
 * differently from "completed with warnings".
 */

import { useMemo } from "react";

import { Icon } from "@/components/icons";
import { IngestionStepIcon } from "@/components/ingestion-icons";
import { EVENT_TYPES } from "@/lib/constants/events";
import { userFacingStepLabel } from "@/lib/processing-steps";
import { RUN_STATUS } from "@/lib/constants/runStatus";
import {
  INGESTION_STATUS,
  projectUiStateFromReport,
  UI_STATE,
  type EnrichmentSignals,
  type UiRunState,
} from "@/lib/runState";
import type { IngestionRun, ProgressEvent } from "@/types/ingestion";
import type { FinalIngestionReportPayload } from "@/types/review";


interface PrimaryStatusPanelProps {
  run: IngestionRun | null;
  events: ProgressEvent[];
  /**
 * the enrichment overlay snapshot (loaded by the
 * parent page from the `/enrichment-result` endpoint). When
 * present, refines the COMPLETED branch into completed_with /
 * completed_without / completed_with_warnings. When absent,
 * the projector falls back to the run-level status alone.
 */
  enrichmentSignals?: EnrichmentSignals;
  /**
 * the aggregated final-ingestion-report payload, when
 * available. The panel prefers this over the per-artifact
 * signals because the report carries the backend's authoritative
 * `final_status` literal + `final_status_reason` copy. Absent /
 * null falls back to the per-artifact derivation used by.
 */
  finalReport?: FinalIngestionReportPayload | null;
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
    if (e.event !== EVENT_TYPES.STEP_COMPLETED) continue;
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


export function PrimaryStatusPanel({
  run,
  events,
  enrichmentSignals,
  finalReport,
}: PrimaryStatusPanelProps) {
  const outputs = useMemo(() => deriveOutputs(events), [events]);
  // prefer the aggregated report's `final_status` over
  // the per-artifact projection. Falls back when the report is
  // null (pre- runs, in-flight runs).
  const uiState = useMemo<UiRunState | null>(
    () => projectUiStateFromReport(
      run, finalReport ?? null, enrichmentSignals ?? {},
    ),
    [run, finalReport, enrichmentSignals],
  );

  if (!run || !uiState) return null;

  // ---- Cancelled branch ---------------------------------------
  if (uiState.uiState === UI_STATE.CANCELLED) {
    return (
      <div className="psp psp--cancelled" data-testid="psp-cancelled">
        <div className="psp__icon">
          <Icon.X className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Cancelled</div>
          <h2 className="psp__title">Run cancelled</h2>
          <p className="psp__lede">
            This run was cancelled before completion.
          </p>
        </div>
      </div>
    );
  }

  // ---- Review branch (human-review pending) -------------------
  if (
    run.status === RUN_STATUS.AWAITING_HUMAN_REVIEW
    || run.status === RUN_STATUS.REQUIRES_HUMAN_REVIEW
  ) {
    return (
      <div className="psp psp--review" data-testid="psp-review">
        <div className="psp__icon">
          <Icon.UserCheck className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Human review required</div>
          <h2 className="psp__title">
            {run.final?.reason || "Manual review needed before continuing"}
          </h2>
          <p className="psp__lede">
            {run.final?.detail
              || "A reviewer must approve or reject this run before it can proceed."}
          </p>
        </div>
      </div>
    );
  }

  // ---- Pending branch (pre-compile + assess + paused) ---------
  if (uiState.uiState === UI_STATE.PENDING) {
    // If a step.started has fired but the run record hasn't
    // advanced yet, advance the hero copy so the operator sees
    // the first stage immediately.
    const lastStarted = [...events]
      .reverse()
      .find((e) => e.event === EVENT_TYPES.STEP_STARTED);
    if (lastStarted) {
      const step = (lastStarted.data?.step as string | undefined) ?? "—";
      const friendly = userFacingStepLabel(step);
      return (
        <div className="psp psp--running" data-testid="psp-running">
          <div className="psp__icon">
            <IngestionStepIcon step={step} status="running" size="md" />
          </div>
          <div className="psp__body">
            <div className="psp__eyebrow">Running</div>
            <h2 className="psp__title">
              <span className="psp__step">{friendly}</span>
            </h2>
            <p className="psp__lede">
              Streaming events live from the pipeline. Watch the
              timeline on the right for per-step progress.
            </p>
          </div>
        </div>
      );
    }
    return (
      <div className="psp psp--assessing" data-testid="psp-pending">
        <div className="psp__icon">
          <IngestionStepIcon
            step="parse_source_content"
            status="running"
            size="md"
          />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Preparing document</div>
          <h2 className="psp__title">{uiState.headline}</h2>
          <p className="psp__lede">
            Profiling the document to choose a compile strategy.
            Enrichment + finalize decisions are made later from
            compile evidence.
          </p>
        </div>
      </div>
    );
  }

  // ---- Running branch (compile / verify / enrich) -------------
  if (uiState.uiState === UI_STATE.RUNNING) {
    const step = run.current_step || "—";
    const friendly = userFacingStepLabel(step);
    const pct = Math.round(run.progress_pct || 0);
    return (
      <div className="psp psp--running" data-testid="psp-running">
        <div className="psp__icon">
          <IngestionStepIcon step={step} status="running" size="md" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">Running · {pct}%</div>
          <h2 className="psp__title">
            <span className="psp__step">{friendly}</span>
          </h2>
          <p className="psp__lede">{uiState.headline}</p>
          <div className="psp__progress">
            <div
              className="psp__progress-bar"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      </div>
    );
  }

  // ---- Failed branch (compile / enrichment-required /
  // finalization / unknown) ------------------------------------
  if (uiState.uiState === UI_STATE.FAILED) {
    const final = run.final;
    return (
      <div
        className={`psp psp--failed psp--failed-${uiState.underlyingFinalStatus ?? "unknown"}`}
        data-testid={`psp-failed-${uiState.underlyingFinalStatus ?? "unknown"}`}
      >
        <div className="psp__icon">
          <Icon.XCircle className="icon" />
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">
            {failedEyebrow(uiState.underlyingFinalStatus)}
          </div>
          <h2 className="psp__title">
            {failedTitle(uiState.underlyingFinalStatus, run)}
          </h2>
          <p className="psp__lede">
            {failedLede(uiState.underlyingFinalStatus, run)}
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

  // ---- Completed branches (clean / no-enrich /
  // with-warnings) ----------------------------------------------
  if (
    uiState.uiState === UI_STATE.COMPLETED
    || uiState.uiState === UI_STATE.COMPLETED_WITH_WARNINGS
  ) {
    const hasWarnings = uiState.uiState === UI_STATE.COMPLETED_WITH_WARNINGS;
    const metrics: { label: string; value: string }[] = [];
    if (outputs.chunks) metrics.push({ label: "Chunks indexed", value: outputs.chunks });
    if (outputs.nodes) metrics.push({ label: "Graph nodes", value: outputs.nodes });
    if (outputs.edges) metrics.push({ label: "Graph edges", value: outputs.edges });
    if (outputs.sections) metrics.push({ label: "Sections", value: outputs.sections });
    if (outputs.tables) metrics.push({ label: "Tables", value: outputs.tables });
    if (outputs.entities) metrics.push({ label: "Entities", value: outputs.entities });

    return (
      <div
        className={`psp ${hasWarnings ? "psp--warnings" : "psp--success"} psp--${uiState.underlyingFinalStatus ?? "completed"}`}
        data-testid={`psp-${uiState.underlyingFinalStatus ?? "completed"}`}
      >
        <div className="psp__icon">
          {hasWarnings ? (
            <Icon.Alert className="icon" />
          ) : (
            <Icon.CheckCircle className="icon" />
          )}
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">
            {completedEyebrow(uiState.underlyingFinalStatus, run.warning_count || 0)}
          </div>
          <h2 className="psp__title">
            {completedTitle(uiState.underlyingFinalStatus)}
          </h2>
          <p className="psp__lede">
            {completedLede(uiState.underlyingFinalStatus, enrichmentSignals, run)}
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


// ---- Per-state copy helpers (kept inline so the panel renders
// with no extra data round-trips) -------------------------------

function failedEyebrow(finalStatus: string | null | undefined): string {
  switch (finalStatus) {
    case INGESTION_STATUS.FAILED_COMPILE:
      return "Base compile failed";
    case INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED:
      return "Required enrichment did not complete";
    case INGESTION_STATUS.FAILED_FINALIZATION:
      return "Finalize failed";
    default:
      return "Failed";
  }
}


function failedTitle(
  finalStatus: string | null | undefined,
  run: IngestionRun,
): string {
  const final = run.final;
  switch (finalStatus) {
    case INGESTION_STATUS.FAILED_COMPILE:
      return final?.failure_code || "Compile could not produce a usable output";
    case INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED:
      return "Compile succeeded, but required enrichment failed";
    case INGESTION_STATUS.FAILED_FINALIZATION:
      return "Pipeline completed but finalize failed";
    default:
      return final?.failure_code || "Run failed";
  }
}


function failedLede(
  finalStatus: string | null | undefined,
  run: IngestionRun,
): string {
  const final = run.final;
  switch (finalStatus) {
    case INGESTION_STATUS.FAILED_COMPILE:
      return (
        final?.failure_message
        || "The base compile engine couldn't produce a chunked output for this document. Enrichment did not run."
      );
    case INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED:
      return (
        "The base compile output exists and is preserved on disk, but the "
        + "active domain policy requires enrichment to succeed. The run "
        + "is marked failed so downstream consumers don't read a partial overlay."
      );
    case INGESTION_STATUS.FAILED_FINALIZATION:
      return (
        final?.failure_message
        || "Compile + enrichment completed, but the finalize step did not. Previous stage outputs are available."
      );
    default:
      return final?.failure_message || "The run terminated with an error.";
  }
}


function completedEyebrow(
  finalStatus: string | null | undefined,
  warningCount: number,
): string {
  // Run Detail's banner reports the EXECUTION outcome only. Whether
  // post-compile domain enrichment happened or didn't is an
  // active-snapshot concern (Document Detail). The eyebrow no
  // longer says "Completed without enrichment" — that framing
  // confused operators into reading skipped enrichment as a
  // degraded run. The two skipped/with-enrichment variants both
  // collapse to "Compile completed" here, with optional warning
  // counts.
  switch (finalStatus) {
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT:
    case INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT:
      return warningCount > 0
        ? `Compile completed with ${warningCount} warning${warningCount === 1 ? "" : "s"}`
        : "Compile completed";
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS:
      return warningCount > 0
        ? `Completed with ${warningCount} warning${warningCount === 1 ? "" : "s"}`
        : "Completed with warnings";
    default:
      return warningCount > 0 ? "Completed with warnings" : "Completed";
  }
}


function completedTitle(finalStatus: string | null | undefined): string {
  switch (finalStatus) {
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT:
    case INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT:
      return "Compile completed";
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS:
      return "Indexed with warnings";
    default:
      return "Run completed";
  }
}


function completedLede(
  finalStatus: string | null | undefined,
  _enrichment: EnrichmentSignals | undefined,
  run: IngestionRun,
): string {
  switch (finalStatus) {
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT:
    case INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT:
      // Run Detail's narrative ends at compile output. The
      // active-snapshot story (enrichment, Knowledge Memory) is
      // owned by Document Detail; the second sentence points the
      // operator there without re-implying enrichment was missed.
      return (
        "The base Knowledge Index and graph were created "
        + "successfully for this run. Post-compile enrichment "
        + "and Knowledge Memory are managed from the document's "
        + "active knowledge view."
      );
    case INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS:
      return (
        run.final?.warning_summary
        || "The pipeline completed but post-compile enrichment "
          + "flagged issues. Raw compile output remains available "
          + "and unaffected."
      );
    default:
      return "Run completed.";
  }
}
