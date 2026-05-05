/**
 * Run-detail orchestrator. Owns the run, plan, events, and stream
 * lifecycle. Coordinates the header, primary status panel, plan card,
 * timeline, and technical drawer.
 *
 * Stream resumption: we send the last seen `eventId` as
 * `Last-Event-Id` so reconnects pick up cleanly. A failure triggers a
 * brief retry — the prototype's behaviour preserved verbatim.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type StreamHandle } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import type { ExecutionPlan, IngestionRun, ProgressEvent } from "@/types/ingestion";
import type { ProjectContext, RuntimeStepStatus, StreamStatus, Toast } from "@/types/ui";
import { RunHeader } from "./run-detail/RunHeader";
import { PlanCard } from "./run-detail/PlanCard";
import { LiveTimeline } from "./run-detail/LiveTimeline";
import { PrimaryStatusPanel } from "./run-detail/PrimaryStatusPanel";
import { TechDrawer } from "./run-detail/TechDrawer";

interface RunDetailPageProps {
  runId: string;
  ctx: ProjectContext;
  onBack: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
}

export function RunDetailPage({ runId, ctx, onBack, pushToast }: RunDetailPageProps) {
  const client = useClient();

  const [run, setRun] = useState<IngestionRun | null>(null);
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("idle");
  const [confirming, setConfirming] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<ProgressEvent | null>(null);
  const [runtimeStepStatus, setRuntimeStepStatus] = useState<Record<string, RuntimeStepStatus>>(
    {},
  );

  const streamHandle = useRef<StreamHandle | null>(null);
  const eventIdsRef = useRef<Set<string>>(new Set());
  const lastEventIdRef = useRef<string | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleEvent = useCallback(
    (e: ProgressEvent) => {
      if (eventIdsRef.current.has(e.eventId)) return;
      eventIdsRef.current.add(e.eventId);
      lastEventIdRef.current = e.eventId;
      setEvents((prev) => [...prev, e]);

      void client
        .getRun(runId)
        .then((r) => setRun(r))
        .catch(() => {});

      const t = e.event;
      const stepKey = e.data?.step;
      if (stepKey) {
        if (t === "step.started") setRuntimeStepStatus((s) => ({ ...s, [stepKey]: "running" }));
        if (t === "step.completed")
          setRuntimeStepStatus((s) => ({ ...s, [stepKey]: "completed" }));
        if (t === "step.failed") setRuntimeStepStatus((s) => ({ ...s, [stepKey]: "failed" }));
        if (t === "step.skipped") setRuntimeStepStatus((s) => ({ ...s, [stepKey]: "skipped" }));
      }

      if (t === "plan.generated") {
        void client
          .getPlan(runId)
          .then((p) => setPlan(p))
          .catch(() => {});
      }
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
      onClose: () => setStreamStatus("closed"),
      onError: () => {
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
    setRun(null);
    setPlan(null);
    setEvents([]);
    setRuntimeStepStatus({});

    void (async () => {
      try {
        const r = await client.getRun(runId);
        if (cancelled) return;
        setRun(r);

        // Allow the assessor to populate before fetching the plan.
        setTimeout(async () => {
          try {
            const p = await client.getPlan(runId);
            if (!cancelled) setPlan(p);
          } catch {
            /* plan may not be ready yet — events will trigger refetch */
          }
        }, 600);

        const hist = await client.getEvents(runId);
        if (cancelled) return;
        for (const e of hist) {
          eventIdsRef.current.add(e.eventId);
          lastEventIdRef.current = e.eventId;
        }
        setEvents(hist);
        openStream();
      } catch (e) {
        const apiErr = e instanceof ApiError ? e : null;
        const message = e instanceof Error ? e.message : "Failed to load run.";
        if (apiErr?.status === 404) {
          pushToast({ kind: "error", title: "Run not found" });
        } else {
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
      streamHandle.current?.close();
      streamHandle.current = null;
    };
  }, [runId, client, openStream, pushToast]);

  const onConfirm = async () => {
    setConfirming(true);
    try {
      await client.confirm(runId);
      const r = await client.getRun(runId);
      setRun(r);
      pushToast({
        kind: "success",
        title: "Plan confirmed",
        body: "Execution started.",
      });
    } catch (e) {
      const message = e instanceof Error ? e.message : "Confirm failed.";
      pushToast({ kind: "error", title: "Confirm failed", body: message });
    } finally {
      setConfirming(false);
    }
  };

  const onSelectEvent = (e: ProgressEvent) => {
    setSelectedEvent(e);
    setDrawerOpen(true);
  };

  return (
    <div>
      <RunHeader
        run={run}
        plan={plan}
        ctx={ctx}
        onBack={onBack}
        onOpenDrawer={() => setDrawerOpen(true)}
      />

      <div style={{ marginBottom: 20 }}>
        <PrimaryStatusPanel run={run} plan={plan} events={events} />
      </div>

      <div className="run-body">
        <div className="col">
          <PlanCard
            plan={plan}
            run={run}
            runtimeStepStatus={runtimeStepStatus}
            onConfirm={() => void onConfirm()}
            confirming={confirming}
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

      <TechDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        run={run}
        plan={plan}
        events={events}
        selectedEvent={selectedEvent}
      />
    </div>
  );
}
