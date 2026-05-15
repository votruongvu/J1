/**
 * HomeDashboard — the new landing page. Answers "is the
 * knowledge base ready and healthy, and can I ask it now?"
 * in one screen.
 *
 * Layout (top → bottom):
 *
 *   1. GlobalSearchCard      — the most prominent action
 *   2. NeedsAttentionPanel   — only renders when applicable
 *   3. SystemStatusSummary   — five compact counters
 *   4. RecentRunsPanel       — flat table of recent ingests
 *
 * Data source: a single `listDocuments()` call. Every panel
 * is fed from that one fetch via the helper functions in
 * `home-dashboard-helpers.ts`. Adding a new panel ideally means
 * extending the helper, not adding a new endpoint.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { Banner } from "@/components/Banner";
import { useClient } from "@/lib/hooks/useClient";
import type { DocumentListItem } from "@/types/documents";
import type { ProjectContext } from "@/types/ui";

import { GlobalSearchCard } from "./GlobalSearchCard";
import { NeedsAttentionPanel } from "./NeedsAttentionPanel";
import { RecentRunsPanel } from "./RecentRunsPanel";
import { SystemStatusSummary } from "./SystemStatusSummary";
import {
  aggregateDocumentStatus,
  collectRecentRuns,
  computeNeedsAttention,
} from "./home-dashboard-helpers";


interface HomeDashboardProps {
  ctx: ProjectContext;
  /** Hand off the search query to the Global Search page.
   * Owner (App.tsx) navigates via setRoute. */
  onSearch: (query: string) => void;
  /** Open the Run Detail page from the Recent Runs panel. */
  onOpenRun: (runId: string) => void;
}


type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; documents: DocumentListItem[] }
  | { kind: "error"; message: string };


export function HomeDashboard({
  ctx,
  onSearch,
  onOpenRun,
}: HomeDashboardProps) {
  const client = useClient();
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const ready = !!ctx.tenant && !!ctx.project;

  const reload = useCallback(() => {
    if (!ready) return;
    let cancelled = false;
    setState({ kind: "loading" });
    void (async () => {
      try {
        const documents = await client.listDocuments();
        if (cancelled) return;
        setState({ kind: "ready", documents });
      } catch (e) {
        if (cancelled) return;
        setState({
          kind: "error",
          message: e instanceof Error ? e.message : "Could not load documents.",
        });
      }
    })();
    return () => { cancelled = true; };
  }, [client, ready]);

  useEffect(() => {
    return reload();
  }, [reload]);

  // Memoize `documents` itself so each derived `useMemo` has a
  // stable reference identity. The ternary built a fresh `[]`
  // on every render which thrashed the downstream memos.
  const documents = useMemo<DocumentListItem[]>(
    () => (state.kind === "ready" ? state.documents : []),
    [state],
  );
  const summary = useMemo(
    () => aggregateDocumentStatus(documents),
    [documents],
  );
  const recentRuns = useMemo(
    () => collectRecentRuns(documents, 5),
    [documents],
  );
  const attention = useMemo(
    () => computeNeedsAttention(documents, summary),
    [documents, summary],
  );

  // Disable the search card when there's nothing indexed to
  // search. The NeedsAttention panel already names the cause —
  // the card's hint stays short.
  const searchDisabled = state.kind === "ready" && summary.indexed === 0;

  return (
    <div className="home-dashboard" data-testid="home-dashboard">
      <div className="page-header">
        <div>
          <h1>Home</h1>
          <p>Search across the knowledge base and monitor system health.</p>
        </div>
      </div>

      {!ready && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="warn" title="Tenant and Project are required">
            Set Tenant ID and Project ID in the context bar above to load
            the dashboard.
          </Banner>
        </div>
      )}

      {state.kind === "error" && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="err" title="Could not load dashboard">
            {state.message}
          </Banner>
        </div>
      )}

      <GlobalSearchCard
        onSubmit={onSearch}
        disabled={searchDisabled || !ready}
        disabledHint={
          !ready
            ? "Set tenant + project to enable search."
            : undefined
        }
      />

      <NeedsAttentionPanel items={attention} />

      <SystemStatusSummary
        summary={summary}
        loading={state.kind === "loading"}
      />

      <RecentRunsPanel
        rows={recentRuns}
        loading={state.kind === "loading"}
        onOpenRun={onOpenRun}
      />
    </div>
  );
}
