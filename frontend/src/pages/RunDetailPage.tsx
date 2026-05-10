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
import { EnrichPlanPanel } from "./run-detail/EnrichPlanPanel";

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
  // them into one `getRun()` per ~250ms keeps the request count
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
        //   - terminal event arrived → backend ended the generator,
        //   - or the backend hit its 1h max-duration timeout while
        //     the run is still in flight.
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
        <PrimaryStatusPanel run={run} events={events} />
      </div>

      {/* Assessment Plan FIRST — shows the rule-based assessor's
          mode + confidence + capabilities + reason at the top of
          the page so operators see WHICH compile strategy J1
          picked before scanning compile output / timeline. Reads
          the same `compile_strategy_report` artifact that the
          CompileStrategyPanel below renders. */}
      <div style={{ marginBottom: 20 }}>
        <AssessmentPlanPanel runId={runId} />
      </div>

      <div className="run-body">
        <div className="col">
          <CompileStrategyPanel runId={runId} />
          <EnrichPlanPanel runId={runId} />
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
