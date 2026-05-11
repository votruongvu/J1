/**
 * Run-detail orchestrator. Owns the run, events, and stream
 * lifecycle. Coordinates the header, primary status panel, live
 * timeline, compile-strategy panel, enrich-plan panel, and
 * technical drawer.
 *
 * Stream resumption: we send the last seen `eventId` as
 * `Last-Event-Id` so reconnects pick up cleanly. A failure triggers a
 * brief retry — the prototype's behaviour preserved verbatim.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type StreamHandle } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import {
  type IngestionRun,
  type ProgressEvent,
  isTerminalEvent,
} from "@/types/ingestion";
import type { ProjectContext, StreamStatus, Toast } from "@/types/ui";
import { Banner } from "@/components/Banner";
import { RunHeader } from "./run-detail/RunHeader";
import { LiveTimeline } from "./run-detail/LiveTimeline";
import { PrimaryStatusPanel } from "./run-detail/PrimaryStatusPanel";
import { ResultsSection } from "./run-detail/results";
import { TechDrawer } from "./run-detail/TechDrawer";
import { AssessmentPlanPanel } from "./run-detail/AssessmentPlanPanel";
import { CompileStrategyPanel } from "./run-detail/CompileStrategyPanel";
import { CompileResultPanel } from "./run-detail/CompileResultPanel";
import { EnrichPlanPanel } from "./run-detail/EnrichPlanPanel";
import { EnrichmentResultPanel } from "./run-detail/EnrichmentResultPanel";
import type {
  EnrichmentResultPayload,
  FinalIngestionReportPayload,
  InitialExecutionPlanPayload,
} from "@/types/review";
import type { EnrichmentSignals } from "@/lib/runState";

interface RunDetailPageProps {
  runId: string;
  ctx: ProjectContext;
  onBack: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
}

export function RunDetailPage({ runId, ctx, onBack, pushToast }: RunDetailPageProps) {
  const client = useClient();

  const [run, setRun] = useState<IngestionRun | null>(null);
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("idle");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<ProgressEvent | null>(null);
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);
  // typed artifact snapshots threaded into
  // PrimaryStatusPanel so the success-branch copy refines per the
  // (A–F) underlying-final-status surface. Both load lazily off the
  // new endpoints; absent values fall back to the run-level signals.
  const [initialPlan, setInitialPlan] =
    useState<InitialExecutionPlanPayload | null>(null);
  const [enrichmentResult, setEnrichmentResult] =
    useState<EnrichmentResultPayload | null>(null);
  // aggregated final-ingestion-report. Preferred over
  // per-artifact projection when present; pre- runs return
  // `null` here and the FE falls back to the existing per-artifact
  // signals (initialPlan + enrichmentResult above).
  const [finalReport, setFinalReport] =
    useState<FinalIngestionReportPayload | null>(null);

  const streamHandle = useRef<StreamHandle | null>(null);
  const eventIdsRef = useRef<Set<string>>(new Set());
  const lastEventIdRef = useRef<string | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Set true once we observe `run.completed` / `run.failed` /
  // `run.cancelled` / `human_review.required`. Drives the close
  // handler: terminal close → stay closed; idle close (backend's 1h
  // max-duration timeout) → reconnect with the last-seen event id so
  // we don't miss events.
  const terminalRef = useRef(false);
  // Debounce window for run-snapshot refreshes. A single fast run
  // can emit dozens of `step.progress` events per second; coalescing
  // them into one `getRun` per ~250ms keeps the request count
  // manageable while still feeling realtime. Terminal events bypass
  // the debounce (we want the authoritative final snapshot).
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const RUN_REFRESH_DEBOUNCE_MS = 250;

  const handleEvent = useCallback(
    (e: ProgressEvent) => {
      if (eventIdsRef.current.has(e.eventId)) return;
      eventIdsRef.current.add(e.eventId);
      lastEventIdRef.current = e.eventId;
      setEvents((prev) => [...prev, e]);

      const t = e.event;
      const isTerminal = isTerminalEvent(t);
      if (isTerminal) terminalRef.current = true;

      // Refresh the run snapshot so header/status panels reflect the
      // new state. Debounce non-terminal refreshes — a fast pipeline
      // can fan out tens of `step.progress` events per second, and
      // we don't need to round-trip the run record for every one.
      // Terminal events bypass the debounce so the user sees the
      // final state immediately.
      const refresh = () => {
        refreshTimerRef.current = null;
        void client
          .getRun(runId)
          .then((r) => setRun(r))
          .catch(() => {});
      };
      if (isTerminal) {
        if (refreshTimerRef.current) {
          clearTimeout(refreshTimerRef.current);
          refreshTimerRef.current = null;
        }
        refresh();
      } else if (refreshTimerRef.current == null) {
        refreshTimerRef.current = setTimeout(refresh, RUN_REFRESH_DEBOUNCE_MS);
      }

      // Compile-first: there is no IngestPlan to refresh on
      // plan.generated/plan.revised. Compile evidence + the
      // post-compile enrich plan are the source of truth; the
      // EnrichPlanPanel pulls its own data from the artifact.
      // Step lifecycle telemetry is rendered directly off the
      // event stream by `LiveTimeline` — no separate runtime-step
      // map is needed at this layer.
    },
    [client, runId],
  );

  const openStream = useCallback(() => {
    if (streamHandle.current) {
      streamHandle.current.close();
      streamHandle.current = null;
    }
    setStreamStatus("open");
    streamHandle.current = client.openStream(runId, {
      lastEventId: lastEventIdRef.current ?? undefined,
      onOpen: () => setStreamStatus("open"),
      onEvent: handleEvent,
      onClose: () => {
        // The api-client only fires onClose on a *backend-initiated*
        // close (caller-initiated aborts are suppressed). So we're
        // here either because:
        //  - terminal event arrived → backend ended the generator,
        //  - or the backend hit its 1h max-duration timeout while
        //  the run is still in flight.
        // Reconnect in the second case to avoid losing events.
        if (terminalRef.current) {
          setStreamStatus("closed");
          return;
        }
        setStreamStatus("reconnecting");
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = setTimeout(openStream, 1500);
      },
      onError: () => {
        if (terminalRef.current) {
          setStreamStatus("closed");
          return;
        }
        setStreamStatus("reconnecting");
        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = setTimeout(openStream, 1500);
      },
    });
  }, [client, runId, handleEvent]);

  // Initial load — fetch run + plan + history, then open the stream.
  useEffect(() => {
    let cancelled = false;
    eventIdsRef.current = new Set();
    lastEventIdRef.current = null;
    terminalRef.current = false;
    if (refreshTimerRef.current) {
      clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    setRun(null);
    setEvents([]);
    setLoadError(null);

    void (async () => {
      try {
        const r = await client.getRun(runId);
        if (cancelled) return;
        setRun(r);

        const hist = await client.getEvents(runId);
        if (cancelled) return;
        for (const e of hist) {
          eventIdsRef.current.add(e.eventId);
          lastEventIdRef.current = e.eventId;
        }
        setEvents(hist);
        openStream();
      } catch (e) {
        if (cancelled) return;
        const status = e instanceof ApiError ? e.status : 500;
        const message = e instanceof Error ? e.message : "Failed to load run.";
        setLoadError({ status, message });
        // Toasts dismiss themselves; we ALSO render an inline banner
        // (below) for the auth/missing-context cases so the page
        // doesn't look blank with a fading toast as the only signal.
        if (status === 404) {
          pushToast({ kind: "error", title: "Run not found" });
        } else if (status !== 401 && status !== 403 && status !== 400) {
          pushToast({ kind: "error", title: "Failed to load run", body: message });
        }
      }
    })();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
      streamHandle.current?.close();
      streamHandle.current = null;
    };
  }, [runId, client, openStream, pushToast]);

  // load the typed artifacts (initial execution
  // plan + enrichment result overlay) so PrimaryStatusPanel can
  // branch on the underlying INGESTION_STATUS_* literal instead of
  // the coarse RUN_STATUS. Both panels also fetch their own copies
  // — duplicating one round-trip is preferable to introducing a
  // new aggregate endpoint just to share state.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    void (async () => {
      try {
        const resp = await client.getRunInitialExecutionPlan(runId);
        if (cancelled) return;
        setInitialPlan(resp.status === "completed" ? resp.plan : null);
      } catch {
        if (!cancelled) setInitialPlan(null);
      }
    })();
    void (async () => {
      try {
        const resp = await client.getRunEnrichmentResult(runId);
        if (cancelled) return;
        setEnrichmentResult(resp.status === "completed" ? resp.plan : null);
      } catch {
        if (!cancelled) setEnrichmentResult(null);
      }
    })();
    // also try the aggregated final-ingestion-report. The
    // FE prefers this when available; falls back to the per-artifact
    // signals above when the report is unavailable (pre-
    // runs, in-flight runs, persist failures).
    void (async () => {
      try {
        const resp = await client.getRunFinalIngestionReport(runId);
        if (cancelled) return;
        setFinalReport(resp.status === "completed" ? resp.report : null);
      } catch {
        if (!cancelled) setFinalReport(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, runId, events.length]);

  const enrichmentSignals: EnrichmentSignals = (() => {
    // prefer the report's enrichment summary when present.
    // It's the authoritative aggregated view (uses post-compile plan
    // override when the resolved policy differs from initial-plan).
    if (finalReport?.enrichment_summary) {
      const summary = finalReport.enrichment_summary;
      const status = summary.enrichment_status ?? undefined;
      return {
        status: status ?? undefined,
        skippedReason: summary.skipped_reason ?? undefined,
        requireEnrichmentSuccess:
          summary.require_enrichment_success ?? undefined,
      };
    }
    // Fallback for pre- runs: derive from per-artifact data
    // the FE already fetched. Same semantics, slightly cruder
    // (`requireEnrichmentSuccess` comes from the initial plan
    // rather than the resolved post-compile policy).
    if (!enrichmentResult) return {};
    return {
      status: enrichmentResult.status,
      skippedReason:
        enrichmentResult.status === "skipped"
          ? enrichmentResult.reason
          : undefined,
      requireEnrichmentSuccess: initialPlan?.require_enrichment_success
        ?? undefined,
    };
  })();

  const onSelectEvent = (e: ProgressEvent) => {
    setSelectedEvent(e);
    setDrawerOpen(true);
  };

  return (
    <div>
      <RunHeader
        run={run}
        ctx={ctx}
        onBack={onBack}
        onOpenDrawer={() => setDrawerOpen(true)}
        onRefresh={() => {
          void client
            .getRun(runId)
            .then((r) => setRun(r))
            .catch(() => {});
        }}
        pushToast={pushToast}
        onAfterAction={(action, newRunId) => {
          // Delete → back to runs list; Re-process / Resume → jump
          // to the new run if one was created. Other actions
          // (pause / resume / cancel) just refresh in-place via
          // onRefresh.
          if (action === "delete" || action === "purge") {
            // Soft-delete and purge both remove the run from view —
            // bounce back to the list so the user doesn't sit on a
            // page for something that no longer exists.
            onBack();
          } else if (
            (action === "reindex"
              || action === "resumeCheckpoint"
              || action === "rebuildIndex")
            && newRunId
          ) {
            try {
              window.history.pushState({}, "", `?run=${newRunId}`);
            } catch {
              /* ignore */
            }
            onBack();
          }
        }}
      />

      {loadError && loadError.status === 400 && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="warn" title="Tenant and Project are required">
            {loadError.message}
          </Banner>
        </div>
      )}
      {loadError && (loadError.status === 401 || loadError.status === 403) && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="err" title="Unauthorized">
            {loadError.message || "Authorize via the context bar to load this run."}
          </Banner>
        </div>
      )}
      {loadError && loadError.status === 404 && (
        <div style={{ marginBottom: 20 }}>
          <Banner kind="err" title="Run not found">
            {loadError.message}
          </Banner>
        </div>
      )}

      <div style={{ marginBottom: 20 }}>
        <PrimaryStatusPanel
          run={run}
          events={events}
          enrichmentSignals={enrichmentSignals}
          finalReport={finalReport}
        />
      </div>

      {/* Assessment Plan — shows the rule-based assessor's mode +
 confidence + capabilities + reason at the top of the page
 so operators see WHICH compile strategy J1 picked before
 scanning compile output / timeline. */}
      <div style={{ marginBottom: 20 }}>
        <AssessmentPlanPanel
          runId={runId}
          latestEvent={events.length > 0 ? events[events.length - 1] : null}
        />
      </div>

      <div className="run-body">
        <div className="col">
          <CompileStrategyPanel
            runId={runId}
            latestEvent={events.length > 0 ? events[events.length - 1] : null}
          />
          {/* typed compile-result summary (chunks,
 detected tables, retry history, raw refs). Surfaces
 the same data the workflow used to decide whether
 enrichment runs. */}
          <CompileResultPanel
            runId={runId}
            latestEvent={events.length > 0 ? events[events.length - 1] : null}
          />
          <EnrichPlanPanel
            runId={runId}
            latestEvent={events.length > 0 ? events[events.length - 1] : null}
          />
          {/* typed enrichment overlay. Renders
 skipped/succeeded/warning/failed states with operator-
 readable copy + per-module outcomes. */}
          <EnrichmentResultPanel
            runId={runId}
            latestEvent={events.length > 0 ? events[events.length - 1] : null}
          />
        </div>
        <div className="col">
          <LiveTimeline
            events={events}
            streamStatus={streamStatus}
            onSelectEvent={onSelectEvent}
          />
        </div>
      </div>

      {/* Results section — visible progressively as steps complete.
 The component handles the visibility check internally so
 the page tree stays declarative. `latestEvent` lets the
 section refresh its summary on step.completed events,
 unlocking newly-available result tabs without a manual
 reload. */}
      <ResultsSection
        run={run}
        runId={runId}
        latestEvent={events.length > 0 ? events[events.length - 1] : null}
      />

      <TechDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        run={run}
        events={events}
        selectedEvent={selectedEvent}
      />
    </div>
  );
}
