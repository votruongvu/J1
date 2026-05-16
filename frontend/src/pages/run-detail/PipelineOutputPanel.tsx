/**
 * One unified panel for the post-pipeline stage outputs (Assessment
 * Plan → Compile Strategy → Compile Result → Enrich Plan → Enrichment
 * Result).
 *
 * Replaces the previous five-separate-cards layout with a single
 * bordered frame and a single scroll surface, modeled after the
 * Claude Code chat tab: each stage's output renders as a "message"
 * block that lands in order as the backend produces it, and the
 * view auto-scrolls to the newest content.
 *
 * Why a wrapper instead of merging the five panel files: each child
 * still owns its own SSE-driven fetch logic, missing/error states,
 * and tests. We get the unified UX without rewriting any of them —
 * the visual flattening (stripping their inner `.card` chrome) is
 * scoped via CSS to `.pipeline-stream__body`.
 */

import { useEffect, useRef } from "react";
import { AssessmentPlanPanel } from "./AssessmentPlanPanel";
import { CompileStrategyPanel } from "./CompileStrategyPanel";
import { CompileResultPanel } from "./CompileResultPanel";
import { EVENT_TYPES } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import type { StreamStatus as StreamStatusKind } from "@/types/ui";

// Run Detail's Pipeline Output is execution-focused — Assessment
// Plan + Compile only. `EnrichPlanPanel` + `EnrichmentResultPanel`
// were dropped here because their content is active-snapshot
// scoped and now lives on Document Detail's
// `ActiveKnowledgeResultPanel`. The leaf files themselves are
// kept (they're still rendered by Document Detail's panel
// indirectly via the `AssetsTab` artifact view).

interface PipelineOutputPanelProps {
  runId: string;
  events: ProgressEvent[];
  streamStatus: StreamStatusKind;
}

export function PipelineOutputPanel({
  runId, events, streamStatus,
}: PipelineOutputPanelProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when a NEW stage-completion event lands.
  // We don't scroll on every event (step.progress fires many times
  // per stage and would yank the viewport mid-read); only on the
  // events that materially change which panels show new content.
  const lastStageEventIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!events.length) return;
    // Walk from the tail to find the latest stage-meaningful event.
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (!e) continue;
      const t = e.event;
      if (
        t === EVENT_TYPES.STEP_STARTED
        || t === EVENT_TYPES.STEP_COMPLETED
        || t === EVENT_TYPES.STEP_FAILED
        || t === EVENT_TYPES.STEP_SKIPPED
      ) {
        if (lastStageEventIdRef.current === e.eventId) return;
        lastStageEventIdRef.current = e.eventId;
        // Defer one frame so the child panels have finished their
        // refetch + render before we measure scrollHeight.
        requestAnimationFrame(() => {
          const el = scrollRef.current;
          if (!el) return;
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        });
        return;
      }
    }
  }, [events]);

  const latestEvent = events.length > 0 ? events[events.length - 1] : null;
  const stageCount = events.filter(
    (e) =>
      e.event === EVENT_TYPES.STEP_COMPLETED
      || e.event === EVENT_TYPES.STEP_FAILED
      || e.event === EVENT_TYPES.STEP_SKIPPED,
  ).length;

  return (
    <div className="pipeline-stream">
      <div className="pipeline-stream__header">
        <div>
          <h3 className="pipeline-stream__title">Pipeline output</h3>
          <p className="pipeline-stream__subtitle">
            {stageCount === 0
              ? "Waiting for the first stage to complete…"
              : `${stageCount} stage${stageCount === 1 ? "" : "s"} complete · auto-scrolling to newest`}
          </p>
        </div>
        {(streamStatus === "open" || streamStatus === "reconnecting") && (
          <StreamPill status={streamStatus} />
        )}
      </div>

      <div ref={scrollRef} className="pipeline-stream__body">
        <AssessmentPlanPanel runId={runId} latestEvent={latestEvent} />
        <CompileStrategyPanel runId={runId} latestEvent={latestEvent} />
        <CompileResultPanel runId={runId} latestEvent={latestEvent} />
        <p
          className="pipeline-stream__footer-note muted"
          data-testid="pipeline-stream-active-snapshot-note"
        >
          Post-compile actions (domain enrichment, Knowledge
          Memory, validation) are managed from the document&apos;s
          active knowledge view.
        </p>
      </div>
    </div>
  );
}

const STREAM_PILL_META: Record<StreamStatusKind, { className: string; label: string }> = {
  open: { className: "stream-status--live", label: "Live" },
  reconnecting: { className: "stream-status--reconnecting", label: "Reconnecting…" },
  closed: { className: "stream-status--closed", label: "Stream closed" },
  idle: { className: "stream-status--closed", label: "Idle" },
};

function StreamPill({ status }: { status: StreamStatusKind }) {
  const meta = STREAM_PILL_META[status] ?? STREAM_PILL_META.idle;
  return (
    <div className={`stream-status ${meta.className}`}>
      <span className="dot" /> {meta.label}
    </div>
  );
}
