/**
 * Results section — visible progressively as steps complete.
 *
 * Tabs gate on `summary.availableViews`, which in turn gates on
 * artifact presence in the workspace. The moment chunk artifacts
 * land, the Chunks tab unlocks. The user no longer waits for the
 * workflow to reach a terminal state to inspect partial results.
 *
 * Lazy fetching: the summary loads as soon as the section renders;
 * sub-tab data loads on first activation and is cached for the
 * session. SSE step events refresh the summary so newly-unlocked
 * tabs appear without a manual reload.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type { IngestionRun, ProgressEvent } from "@/types/ingestion";
import { COMPLETED_STATUSES } from "@/lib/constants/runStatus";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ReviewRunSummary } from "@/types/review";
import { ChunksTab } from "./ChunksTab";
import { ManualQueryTraceViewTab } from "./ManualQueryTraceViewTab";
import { OverviewTab } from "./OverviewTab";
import { RawArtifactsTab } from "./RawArtifactsTab";

// Run Detail Results — execution-focused tab set. Active-snapshot
// surfaces (enrichment overlay, quality, validation dashboards)
// moved to `ActiveKnowledgeResultPanel` on Document Detail so
// each surface answers exactly one question:
//
//   * Run Detail  → "What happened in this execution run?"
//   * Document Detail → "What is the current active knowledge state?"
//
// **No `graph` tab here.** The RAGAnything/LightRAG compile DOES
// build the base graph/index — but it lives inside the snapshot's
// LightRAG workspace, not as a registered J1 artifact. Until the
// backend persists a compile graph summary artifact (see the
// audit report's Backend Follow-up Options), Run Detail surfaces
// the situation as a neutral note in Overview rather than a
// permanently-empty tab. The legacy `build_graph` activity that
// would copy LightRAG's workdir into a `graph_json` artifact is
// off by default on `standard` and was renamed/removed from the
// timeline because it was misleading: it doesn't build the
// graph, it just registers files compile already wrote.
type ResultsTab =
  | "overview"
  | "chunks"
  | "raw"
  | "manual-trace";

interface ResultsSectionProps {
  run: IngestionRun | null;
  runId: string;
  // Parent supplies the latest progress event so the Results
  // section can refresh its summary on `step.completed` events.
  // Undefined when no event has fired yet (initial mount).
  latestEvent?: ProgressEvent | null;
}

export function ResultsSection({
  run, runId, latestEvent,
}: ResultsSectionProps) {
  const client = useClient();
  const [tab, setTab] = useState<ResultsTab>("overview");

  const [summary, setSummary] = useState<ReviewRunSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  // Reset cache when the run id changes.
  useEffect(() => {
    setSummary(null);
    setSummaryLoading(false);
    setSummaryError(null);
    setTab("overview");
  }, [runId]);

  // Load the summary as soon as the section mounts. Previous
  // behavior gated this on `isTerminalRunStatus(run?.status)` so
  // the Results section was hidden mid-run; now we render
  // progressively. The summary endpoint already handles in-flight
  // runs — `availableViews` derives from artifacts present at
  // call time, regardless of run status.
  //
  // Deliberately NOT listing `summary` / `summaryLoading` in the
  // deps array: doing so would create a feedback loop where
  // `setSummaryLoading(true)` re-runs the effect, the cleanup fires
  // `cancelled=true` on the prior in-flight fetch, and the FE sits
  // on the loading state forever.
  const loadSummary = useCallback(() => {
    let cancelled = false;
    setSummaryLoading(true);
    setSummaryError(null);
    void (async () => {
      try {
        const result = await client.getRunSummary(runId);
        if (!cancelled) setSummary(result);
      } catch (e) {
        if (!cancelled) {
          setSummaryError(e instanceof Error ? e.message : "Failed to load.");
        }
      } finally {
        if (!cancelled) {
          setSummaryLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId]);

  // Initial load when the run id resolves.
  useEffect(() => {
    if (!run) return;
    return loadSummary();
  }, [run?.runId, loadSummary]);

  // SSE-driven refresh: re-fetch the summary on every step.completed
  // / step.failed / step.skipped, plus the terminal events. The
  // summary is small (one HTTP call returning a few hundred bytes);
  // the cost of re-fetching it on each step boundary is negligible
  // and the UX win is large (newly-unlocked tabs appear without a
  // manual reload).
  useEffect(() => {
    if (!latestEvent) return;
    const refreshOn = new Set<string>([
      EVENT_TYPES.STEP_COMPLETED,
      EVENT_TYPES.STEP_FAILED,
      EVENT_TYPES.STEP_SKIPPED,
    ]);
    if (refreshOn.has(latestEvent.event) || isTerminalEvent(latestEvent.event)) {
      loadSummary();
    }
  }, [latestEvent, loadSummary]);

  // Render the section as long as we have a run reference at all.
  // Hiding it pre-CREATED only — once the run record exists, the
  // Overview tab renders immediately and other tabs unlock as their
  // artifacts land.
  if (!run) {
    return null;
  }

  // availableViews is populated only after summary loads. Until
  // then, every non-Overview tab is disabled.
  const views = summary?.availableViews;

  const tabs: Array<{
    key: ResultsTab;
    label: string;
    available: boolean;
    reason?: string | null;
  }> = [
    // Run Detail Results — execution-focused. Four tabs that
    // describe what happened in THIS execution. Active-snapshot
    // surfaces (enrichment overlay, quality, validation) live on
    // Document Detail's `ActiveKnowledgeResultPanel`.
    //
    //   Overview     → run status, duration, artifacts, warnings,
    //                  + neutral graph/index note (since the base
    //                  graph lives in the LightRAG workspace, not
    //                  in J1's artifact registry).
    //   Chunks       → compiled output produced by this run.
    //   Artifacts    → raw artifact list (operator/dev inspection).
    //   Query Trace  → base/manual query against the run's snapshot.
    //
    // Use safe optional chaining (?. on EVERY field) so a partially-
    // missing `views` object — older API response, in-flight network
    // error retried by the FE, malformed payload — can't crash the
    // tab list rendering.
    { key: "overview", label: "Overview", available: true },
    {
      key: "chunks",
      label: "Chunks",
      available: views?.chunks?.available ?? false,
      reason: views?.chunks?.reason ?? "Loading…",
    },
    {
      key: "raw",
      label: "Artifacts",
      available: views?.rawArtifacts?.available ?? false,
      reason: views?.rawArtifacts?.reason ?? "Loading…",
    },
    {
      // SmartQueryOrchestrator trace against this run's compiled
      // output. Gated on the run reaching a completed status —
      // querying before compile finishes produces a misleading "no
      // answer" result.
      key: "manual-trace",
      label: "Query Trace",
      available:
        run !== null && COMPLETED_STATUSES.has(run.status),
      reason:
        run === null
          ? "Loading run…"
          : COMPLETED_STATUSES.has(run.status)
            ? undefined
            : "Available once the document is processed.",
    },
  ];

  return (
    <section className="results-section" aria-label="Run results">
      <div className="results-section__head">
        <h2 className="results-section__title">Results</h2>
      </div>

      <div className="results-section__tabs" role="tablist">
        {tabs.map((t) => {
          const active = tab === t.key;
          const disabled = !t.available && t.key !== "overview";
          return (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={active}
              aria-disabled={disabled}
              title={disabled && t.reason ? t.reason : undefined}
              className={`results-section__tab ${active ? "is-active" : ""} ${disabled ? "is-disabled" : ""}`}
              onClick={() => {
                if (disabled) return;
                setTab(t.key);
              }}
              data-testid={`results-tab-${t.key}`}
            >
              {t.label}
              {disabled && (
                <span
                  className="results-section__tab-hint"
                  aria-hidden="true"
                >
                  {summariseDisabledReason(t.reason)}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <div className="results-section__body">
        {tab === "overview" && (
          <OverviewTab
            summary={summary}
            loading={summaryLoading}
            error={summaryError}
          />
        )}
        {tab === "chunks" && <ChunksTab runId={runId} />}
        {tab === "raw" && <RawArtifactsTab runId={runId} />}
        {tab === "manual-trace" && (
          <>
            <p
              className="results-section__helper muted"
              data-testid="results-query-trace-helper"
            >
              Tests retrieval against the compiled output for this
              run/snapshot. Post-compile domain enrichment and
              Knowledge Memory are shown on the document&apos;s
              active knowledge view.
            </p>
            <ManualQueryTraceViewTab runId={runId} />
          </>
        )}
      </div>
    </section>
  );
}


/**
 * Project the backend's free-form `availableViews[*].reason` string
 * into a 1-3 word chip the operator can read at a glance on a
 * disabled tab. Hover still surfaces the full reason via the
 * button's `title` attribute.
 *
 * Patterns are intentional — operators previously couldn't tell
 * the difference between "stage skipped by execution profile" and
 * "stage in progress, will unlock soon" without hovering each tab.
 */
export function summariseDisabledReason(
  reason: string | null | undefined,
): string {
  if (!reason) return "";
  const lower = reason.toLowerCase();
  if (lower.includes("disabled by selected execution profile")) {
    return "skipped";
  }
  if (lower.includes("execution profile")) return "skipped";
  if (lower.includes("not provided in request")) return "skipped";
  if (lower.startsWith("loading")) return "loading…";
  if (lower.includes("available once") || lower.includes("once the run")) {
    return "soon";
  }
  if (lower.includes("not yet") || lower.includes("not ready")) {
    return "soon";
  }
  return "n/a";
}
