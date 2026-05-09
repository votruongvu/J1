/**
 * Results section — visible progressively as steps complete.
 *
 * Tabs gate on `summary.availableViews`, which in turn gates on
 * artifact presence in the workspace. As soon as the compile
 * activity emits the parsed-content manifest, the Content Inventory
 * tab unlocks; the moment chunk artifacts land, the Chunks tab
 * unlocks. The user no longer waits for the workflow to reach a
 * terminal state to inspect partial results.
 *
 * Lazy fetching: the summary loads as soon as the section renders;
 * sub-tab data loads on first activation and is cached for the
 * session. SSE step events refresh the summary so newly-unlocked
 * tabs appear without a manual reload.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type { IngestionRun, ProgressEvent } from "@/types/ingestion";
import { EVENT_TYPES, isTerminalEvent } from "@/lib/constants/events";
import type { ReviewQualityReport, ReviewRunSummary } from "@/types/review";
import { AssetsTab } from "./AssetsTab";
import { ChunksTab } from "./ChunksTab";
import { ContentInventoryTab } from "./ContentInventoryTab";
import { GraphTab } from "./GraphTab";
import { OverviewTab } from "./OverviewTab";
import { QualityTab } from "./QualityTab";
import { RawArtifactsTab } from "./RawArtifactsTab";
import { ValidationTab } from "./ValidationTab";

type ResultsTab =
  | "overview"
  | "parsedContent"
  | "chunks"
  | "assets"
  | "graph"
  | "quality"
  | "raw"
  | "validation";

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

  const [quality, setQuality] = useState<ReviewQualityReport | null>(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualityError, setQualityError] = useState<string | null>(null);

  // Reset cache when the run id changes.
  useEffect(() => {
    setSummary(null);
    setSummaryLoading(false);
    setSummaryError(null);
    setQuality(null);
    setQualityLoading(false);
    setQualityError(null);
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
        if (!cancelled) setSummaryLoading(false);
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
  // and on the terminal events. The summary is small (one HTTP call
  // returning a few hundred bytes); the cost of re-fetching it on
  // each step boundary is negligible and the UX win is large
  // (newly-unlocked tabs appear without a manual reload).
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

  // Lazy quality-report load — only on first Quality tab open.
  const loadQuality = useCallback(() => {
    if (quality || qualityLoading) return;
    setQualityLoading(true);
    setQualityError(null);
    void (async () => {
      try {
        const r = await client.getRunQualityReport(runId);
        setQuality(r);
      } catch (e) {
        setQualityError(e instanceof Error ? e.message : "Failed to load.");
      } finally {
        setQualityLoading(false);
      }
    })();
  }, [client, runId, quality, qualityLoading]);

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
    { key: "overview", label: "Overview", available: true },
    {
      key: "parsedContent",
      label: "Content Inventory",
      // `parsedContent` is optional on older API responses — fall
      // back to false + a generic reason when absent so an old
      // bundle running against a newer backend (or vice-versa)
      // doesn't crash on `undefined.available`.
      available: views?.parsedContent?.available ?? false,
      reason:
        views?.parsedContent?.reason ??
        (views ? "Waiting for parser to finish." : "Loading…"),
    },
    {
      key: "chunks",
      label: "Chunks",
      available: views?.chunks.available ?? false,
      reason: views?.chunks.reason ?? "Loading…",
    },
    {
      key: "assets",
      label: "Assets",
      available: views?.assets.available ?? false,
      reason: views?.assets.reason ?? "Loading…",
    },
    {
      key: "graph",
      label: "Graph",
      available: views?.graph.available ?? false,
      reason: views?.graph.reason ?? "Loading…",
    },
    {
      key: "quality",
      label: "Quality",
      available: views?.quality.available ?? false,
      reason: views?.quality.reason ?? "Loading…",
    },
    {
      key: "raw",
      label: "Raw artifacts",
      available: views?.rawArtifacts.available ?? false,
      reason: views?.rawArtifacts.reason ?? "Loading…",
    },
    {
      key: "validation",
      label: "Validation",
      available: views?.validation.available ?? false,
      reason: views?.validation.reason ?? "Loading…",
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
                if (t.key === "quality") loadQuality();
              }}
            >
              {t.label}
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
        {tab === "quality" && (
          <QualityTab
            report={quality}
            loading={qualityLoading}
            error={qualityError}
          />
        )}
        {tab === "parsedContent" && <ContentInventoryTab runId={runId} />}
        {tab === "chunks" && <ChunksTab runId={runId} />}
        {tab === "assets" && <AssetsTab runId={runId} />}
        {tab === "graph" && <GraphTab runId={runId} />}
        {tab === "raw" && <RawArtifactsTab runId={runId} />}
        {tab === "validation" && <ValidationTab runId={runId} />}
      </div>
    </section>
  );
}
