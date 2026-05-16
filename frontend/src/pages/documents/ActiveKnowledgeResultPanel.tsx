/**
 * ActiveKnowledgeResultPanel — Document Detail full active-snapshot
 * result surface.
 *
 * Answers the question "what is the current active knowledge state
 * of this document?" — the counterpart to Run Detail's simplified
 * Results panel which answers "what happened in this run?".
 *
 * The panel reuses the per-tab leaf components (AssetsTab,
 * GraphTab, QualityTab, ValidationTab) but composes its OWN tab
 * shell + copy so the surface tells the operator they're looking
 * at the active snapshot, not a one-off run inspection. The
 * active producing run's id is threaded into each tab — every
 * leaf component is keyed off `runId` and the BE's
 * `getRunSummary` already projects all the active-snapshot views
 * from that run's artifacts.
 *
 * Tabs:
 *
 *   Overview     — active-snapshot status + KPI strip + warnings.
 *   Enrichment   — post-compile domain enrichment overlay assets.
 *   Knowledge Graph — graph stats and ego-graph viewer (when the
 *                  run produced graph_json — needs `advanced`
 *                  profile or a graph re-run).
 *   Quality      — confidence assessment + readiness gates.
 *   Validation   — imported test cases + manual test query.
 *
 * Knowledge Memory is intentionally NOT a tab here — it's a
 * sibling panel (`KnowledgeMemoryStatusPanel`) on Document Detail
 * so its status remains independently scannable.
 */

import { useCallback, useEffect, useState } from "react";
import { useClient } from "@/lib/hooks/useClient";
import type { ReviewQualityReport, ReviewRunSummary } from "@/types/review";
import { AssetsTab } from "../run-detail/results/AssetsTab";
import { GraphTab } from "../run-detail/results/GraphTab";
import { OverviewTab } from "../run-detail/results/OverviewTab";
import { QualityTab } from "../run-detail/results/QualityTab";
import { ValidationTab } from "../run-detail/results/ValidationTab";
import { summariseDisabledReason } from "../run-detail/results";


type ActiveKnowledgeTab =
  | "overview"
  | "enrichment"
  | "graph"
  | "quality"
  | "validation";


export interface ActiveKnowledgeResultPanelProps {
  /** Run id of the run that produced the active snapshot. The
   * leaf tabs key off this id; the BE's run-summary projection
   * derives every active-snapshot artifact view from the run's
   * artifact set. */
  activeRunId: string;
  /** Document id this active snapshot belongs to. Used by the
   * Validation tab to scope `document_active` queries. */
  documentId: string;
  /** Active snapshot id. Passed through to the Validation tab so
   * `snapshot_explicit` queries hit the right snapshot. */
  activeSnapshotId: string | null;
}


export function ActiveKnowledgeResultPanel({
  activeRunId,
  documentId,
  activeSnapshotId,
}: ActiveKnowledgeResultPanelProps) {
  const client = useClient();
  const [tab, setTab] = useState<ActiveKnowledgeTab>("overview");

  // The Overview reuses the run-summary projection so the active-
  // snapshot status + KPI strip + warnings rendering stays
  // byte-identical to Run Detail. The summary is also the source
  // of truth for which sibling tabs unlock.
  const [summary, setSummary] = useState<ReviewRunSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [quality, setQuality] = useState<ReviewQualityReport | null>(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualityError, setQualityError] = useState<string | null>(null);

  useEffect(() => {
    setSummary(null);
    setSummaryError(null);
    setQuality(null);
    setQualityError(null);
    setTab("overview");
  }, [activeRunId]);

  const loadSummary = useCallback(() => {
    let cancelled = false;
    setSummaryLoading(true);
    setSummaryError(null);
    void (async () => {
      try {
        const result = await client.getRunSummary(activeRunId);
        if (!cancelled) setSummary(result);
      } catch (e) {
        if (!cancelled) {
          setSummaryError(
            e instanceof Error ? e.message : "Failed to load.",
          );
        }
      } finally {
        if (!cancelled) setSummaryLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [client, activeRunId]);

  useEffect(() => loadSummary(), [loadSummary]);

  const loadQuality = useCallback(() => {
    if (quality || qualityLoading) return;
    setQualityLoading(true);
    setQualityError(null);
    void (async () => {
      try {
        const r = await client.getRunQualityReport(activeRunId);
        setQuality(r);
      } catch (e) {
        setQualityError(
          e instanceof Error ? e.message : "Failed to load.",
        );
      } finally {
        setQualityLoading(false);
      }
    })();
  }, [client, activeRunId, quality, qualityLoading]);

  const views = summary?.availableViews;

  const tabs: Array<{
    key: ActiveKnowledgeTab;
    label: string;
    available: boolean;
    reason?: string | null;
  }> = [
    { key: "overview", label: "Overview", available: true },
    {
      key: "enrichment",
      label: "Enrichment",
      available: views?.assets?.available ?? false,
      reason: views?.assets?.reason ?? "Loading…",
    },
    {
      key: "graph",
      label: "Knowledge Graph",
      available: views?.graph?.available ?? false,
      reason: views?.graph?.reason ?? "Loading…",
    },
    {
      key: "quality",
      label: "Quality",
      available: views?.quality?.available ?? false,
      reason: views?.quality?.reason ?? "Loading…",
    },
    {
      key: "validation",
      label: "Validation",
      available: views?.validation?.available ?? false,
      reason: views?.validation?.reason ?? "Loading…",
    },
  ];

  return (
    <section
      className="results-section active-knowledge-result-panel"
      aria-label="Active knowledge result"
      data-testid="active-knowledge-result-panel"
    >
      <div className="results-section__head">
        <h2 className="results-section__title">Active Knowledge Result</h2>
        <p
          className="results-section__helper muted"
          data-testid="active-knowledge-result-panel-helper"
        >
          This view reflects the document&apos;s active snapshot.
          Post-compile domain enrichment, Knowledge Memory, and
          validation diagnostics all read from the same snapshot.
        </p>
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
              className={
                `results-section__tab `
                + `${active ? "is-active" : ""} `
                + `${disabled ? "is-disabled" : ""}`
              }
              onClick={() => {
                if (disabled) return;
                setTab(t.key);
                if (t.key === "quality") loadQuality();
              }}
              data-testid={`active-knowledge-tab-${t.key}`}
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
        {tab === "enrichment" && <AssetsTab runId={activeRunId} />}
        {tab === "graph" && <GraphTab runId={activeRunId} />}
        {tab === "quality" && (
          <QualityTab
            report={quality}
            loading={qualityLoading}
            error={qualityError}
          />
        )}
        {tab === "validation" && (
          <ValidationTab
            runId={activeRunId}
            documentId={documentId}
            targetSnapshotId={activeSnapshotId}
          />
        )}
      </div>
    </section>
  );
}
