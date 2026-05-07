/**
 * Results section — visible only when the run is terminal.
 *
 * Owns data fetching for the Results sub-tabs. As of Phase 10:
 * every Results sub-tab ships. Each tab button is gated by
 * `summary.availableViews` so disabled tabs render with a tooltip
 * carrying the unavailable reason.
 *
 * Lazy fetching: the summary loads as soon as the section renders;
 * sub-tab data loads on first activation and is cached for the
 * session.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type { IngestionRun } from "@/types/ingestion";
import type { ReviewQualityReport, ReviewRunSummary } from "@/types/review";
import { AssetsTab } from "./AssetsTab";
import { ChunksTab } from "./ChunksTab";
import { GraphTab } from "./GraphTab";
import { isTerminalRunStatus } from "./lifecycle";
import { OverviewTab } from "./OverviewTab";
import { QualityTab } from "./QualityTab";
import { RawArtifactsTab } from "./RawArtifactsTab";
import { ValidationTab } from "./ValidationTab";

type ResultsTab =
  | "overview"
  | "chunks"
  | "assets"
  | "graph"
  | "quality"
  | "raw"
  | "validation";

interface ResultsSectionProps {
  run: IngestionRun | null;
  runId: string;
}

export function ResultsSection({ run, runId }: ResultsSectionProps) {
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

  // Auto-load summary when the run flips to terminal. Sub-tab data
  // is loaded lazily on first activation.
  //
  // Deliberately NOT listing `summary` / `summaryLoading` in the
  // deps array: doing so would create a feedback loop where
  // `setSummaryLoading(true)` re-runs the effect, the cleanup fires
  // `cancelled=true` on the prior in-flight fetch, and the FE sits
  // on the loading state forever. Caught by the Phase 11 e2e smoke.
  useEffect(() => {
    if (!isTerminalRunStatus(run?.status)) return;
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
  }, [client, run?.status, runId]);

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

  // Hide entirely while the run is still running. Reviewers shouldn't
  // see partial-state Results until the workflow has terminated.
  if (!isTerminalRunStatus(run?.status)) {
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
        {tab === "chunks" && <ChunksTab runId={runId} />}
        {tab === "assets" && <AssetsTab runId={runId} />}
        {tab === "graph" && <GraphTab runId={runId} />}
        {tab === "raw" && <RawArtifactsTab runId={runId} />}
        {tab === "validation" && <ValidationTab runId={runId} />}
      </div>
    </section>
  );
}
