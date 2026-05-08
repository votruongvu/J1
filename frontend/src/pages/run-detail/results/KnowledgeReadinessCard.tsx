/**
 * Knowledge Readiness card — shown at the top of the Validation tab.
 *
 * Surfaces the latest validation run's status + summary counts and
 * exposes the Generate / Run buttons.
 *
 * Honours the executionStatus / validationStatus split: the badge
 * shows the test outcome (`validationStatus`), not the job state.
 * A `completed` + `failed` pair renders as a red FAILED badge with
 * a "ran successfully but cases failed" subtitle.
 */

import { useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import { ApiError } from "@/lib/api/client";
import { validationStatusMeta } from "@/lib/display";
import type {
  ValidationRun,
  ValidationSetListItem,
  ValidationStatus,
} from "@/types/review";

interface KnowledgeReadinessCardProps {
  /** Most-recent terminal validation run for this ingestion run. */
  latestRun: ValidationRun | null;
  /** Latest set, used to enable / disable the Run button. */
  setItem: ValidationSetListItem | null;
  /** Lifecycle flags from the parent (ValidationTab). */
  running: boolean;
  generating: boolean;
  onGenerate: () => void;
  onRun: () => void;
  /** Run id of the parent ingestion run — needed for the report
   * download. We pass it down so the card can issue the request
   * without going back through ValidationTab. */
  runId: string;
}

export function KnowledgeReadinessCard({
  latestRun,
  setItem,
  running,
  generating,
  onGenerate,
  onRun,
  runId,
}: KnowledgeReadinessCardProps) {
  const client = useClient();
  const [downloading, setDownloading] = useState<"markdown" | "json" | null>(
    null,
  );
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const status: ValidationStatus | "not_run" =
    latestRun?.validationStatus ?? "not_run";
  const summary = latestRun?.summary ?? null;
  const subtitle = _buildSubtitle(latestRun, setItem);

  const downloadReport = async (format: "markdown" | "json") => {
    if (!latestRun) return;
    setDownloading(format);
    setDownloadError(null);
    try {
      const { content, mediaType, filename } =
        await client.downloadValidationReport(
          runId, latestRun.validationRunId, format,
        );
      // Trigger a browser download via an in-memory Blob URL. The
      // backend's `Content-Disposition: attachment` header is
      // ignored by `fetch` callers — we have to build the link
      // ourselves. `revokeObjectURL` after a tick so the browser
      // has time to start the download.
      const blob = new Blob([content], { type: mediaType });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 100);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Download failed (${e.status}): ${e.message}`
          : e instanceof Error
            ? e.message
            : "Download failed.";
      setDownloadError(msg);
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Knowledge readiness</h3>
          <p className="card__subtitle">{subtitle}</p>
        </div>
        <div>
          <span
            className={`validation-status ${validationStatusMeta(status).className}`}
            aria-label={`Validation status: ${validationStatusMeta(status).label}`}
          >
            {validationStatusMeta(status).label}
          </span>
        </div>
      </div>

      <div
        className="card__body"
        style={{ display: "grid", gap: 12 }}
      >
        {summary && (
          <div
            className="readiness-counters"
            style={{
              display: "flex",
              gap: 16,
              flexWrap: "wrap",
              fontVariantNumeric: "tabular-nums",
            }}
          >
            <Counter label="Total" value={summary.total} />
            <Counter label="Passed" value={summary.passed} tone="ok" />
            <Counter label="Warning" value={summary.warning} tone="warn" />
            <Counter label="Failed" value={summary.failed} tone="fail" />
            <Counter label="Skipped" value={summary.skipped} />
          </div>
        )}

        {summary?.recommendedAction && (
          <p style={{ fontStyle: "italic", color: "var(--fg-muted)" }}>
            Recommendation: {summary.recommendedAction}
          </p>
        )}

        {summary?.mainIssues && summary.mainIssues.length > 0 && (
          <div>
            <strong>Main issues:</strong>
            <ul style={{ marginTop: 4 }}>
              {summary.mainIssues.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          </div>
        )}

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onGenerate}
            disabled={generating || running}
          >
            {generating ? "Generating…" : "Generate Test Set"}
          </button>
          <button
            type="button"
            className="btn"
            onClick={onRun}
            disabled={running || generating || !setItem}
            title={!setItem ? "Generate a set first" : undefined}
          >
            {running ? "Running…" : "Run Validation"}
          </button>
          {/* Phase 5 download buttons. Disabled until a run exists
              so testers don't try to download an empty report. */}
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void downloadReport("markdown")}
            disabled={!latestRun || downloading !== null}
            title={
              !latestRun ? "Run validation first" : "Download as Markdown"
            }
          >
            {downloading === "markdown" ? "Downloading…" : "Download .md"}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void downloadReport("json")}
            disabled={!latestRun || downloading !== null}
            title={
              !latestRun ? "Run validation first" : "Download as JSON"
            }
          >
            {downloading === "json" ? "Downloading…" : "Download .json"}
          </button>
        </div>
        {downloadError && (
          <div
            className="banner banner--err"
            role="alert"
            style={{ marginTop: 8 }}
          >
            {downloadError}
          </div>
        )}
      </div>
    </div>
  );
}

interface CounterProps {
  label: string;
  value: number;
  tone?: "ok" | "warn" | "fail";
}

function Counter({ label, value, tone }: CounterProps) {
  const color =
    tone === "ok"
      ? "var(--ok, #2a7d2e)"
      : tone === "warn"
        ? "var(--warn, #c98a00)"
        : tone === "fail"
          ? "var(--err, #c0392b)"
          : "var(--fg)";
  return (
    <div style={{ display: "grid", gap: 0 }}>
      <span style={{ color, fontSize: 22, fontWeight: 600 }}>{value}</span>
      <span style={{ fontSize: 12, color: "var(--fg-muted)" }}>{label}</span>
    </div>
  );
}

function _buildSubtitle(
  latestRun: ValidationRun | null,
  setItem: ValidationSetListItem | null,
): string {
  if (latestRun) {
    // Surface the executionStatus when it conflicts with the
    // outcome — operators need to know if the job didn't finish
    // cleanly even when the validationStatus says "failed". For
    // the common "completed + failed" case we don't repeat
    // ourselves; the badge already says failed.
    if (
      latestRun.executionStatus !== "completed"
      && latestRun.executionStatus !== "running"
    ) {
      return `Job ${latestRun.executionStatus} at ${_fmt(latestRun.completedAt ?? latestRun.startedAt)}.`;
    }
    return `Last validated ${_fmt(latestRun.completedAt ?? latestRun.startedAt)}.`;
  }
  if (setItem) {
    return `${setItem.caseCount} test cases generated. Click Run Validation to execute.`;
  }
  return (
    "Validation has not been run yet. Generate a test set or use the manual " +
    "console below to verify the index can answer questions from this document."
  );
}

function _fmt(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
