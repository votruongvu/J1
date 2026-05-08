/**
 * Live timeline — auto-scrolling stream of progress events. Each event
 * is colour-coded by kind (running / success / warning / error /
 * review / info) and clickable for detail inspection.
 */

import { useEffect, useRef } from "react";
import { Icon } from "@/components/icons";
import { EngineBadge } from "@/components/badges";
import { EVENT_TYPES } from "@/lib/constants/events";
import { eventTypeLabel } from "@/lib/display";
import type { ProgressEvent } from "@/types/ingestion";
import type { StreamStatus as StreamStatusKind } from "@/types/ui";

interface LiveTimelineProps {
  events: ProgressEvent[];
  streamStatus: StreamStatusKind;
  onSelectEvent?: (event: ProgressEvent) => void;
}

export function LiveTimeline({ events, streamStatus, onSelectEvent }: LiveTimelineProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Live timeline</h3>
          <p className="card__subtitle">
            {events.length} event{events.length === 1 ? "" : "s"}
          </p>
        </div>
        <StreamStatus status={streamStatus} />
      </div>
      <div className="card__body" ref={scrollRef} style={{ maxHeight: 520, overflow: "auto" }}>
        {events.length === 0 ? (
          <div className="tl-empty">
            No events yet. They&apos;ll appear here as the run progresses.
          </div>
        ) : (
          <div className="timeline">
            {events.map((e) => (
              <TimelineEventItem key={e.eventId} event={e} onClick={() => onSelectEvent?.(e)} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const STREAM_LABEL: Record<StreamStatusKind, { className: string; label: string }> = {
  open: { className: "stream-status--live", label: "Live" },
  reconnecting: { className: "stream-status--reconnecting", label: "Reconnecting…" },
  closed: { className: "stream-status--closed", label: "Stream closed" },
  idle: { className: "stream-status--closed", label: "Idle" },
};

function StreamStatus({ status }: { status: StreamStatusKind }) {
  const m = STREAM_LABEL[status] ?? STREAM_LABEL.idle;
  return (
    <div className={`stream-status ${m.className}`}>
      <span className="dot" /> {m.label}
    </div>
  );
}

function formatTimeShort(ts: number | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

interface TimelineEventItemProps {
  event: ProgressEvent;
  onClick: () => void;
}

function TimelineEventItem({ event, onClick }: TimelineEventItemProps) {
  const t = event.event;
  const sev = event.data?.severity || "INFO";
  const isProgress = t === EVENT_TYPES.STEP_PROGRESS;
  const isWarning = t === EVENT_TYPES.STEP_WARNING || sev === "WARNING";
  const isError = t === EVENT_TYPES.STEP_FAILED || t === EVENT_TYPES.RUN_FAILED || sev === "ERROR";
  const isReview = t === EVENT_TYPES.HUMAN_REVIEW_REQUIRED;
  const isRunning = t === EVENT_TYPES.STEP_STARTED;
  const isSuccess =
    t === EVENT_TYPES.STEP_COMPLETED ||
    t === EVENT_TYPES.RUN_COMPLETED ||
    t === EVENT_TYPES.ASSESSMENT_COMPLETED;

  let kind: "info" | "warning" | "error" | "review" | "running" | "success" = "info";
  if (isError) kind = "error";
  else if (isWarning) kind = "warning";
  else if (isReview) kind = "review";
  else if (isRunning) kind = "running";
  else if (isSuccess) kind = "success";

  return (
    <div className={`tl-item tl-item--${kind}`} onClick={onClick} style={{ cursor: "pointer" }}>
      <div className="tl-item__dot">
        {kind === "success" && <Icon.Check className="icon-sm" />}
        {kind === "warning" && <Icon.Alert className="icon-sm" />}
        {kind === "error" && <Icon.X className="icon-sm" />}
        {kind === "review" && <Icon.UserCheck className="icon-sm" />}
      </div>
      <div className="tl-item__head">
        <span className="tl-item__type">{eventTypeLabel(t)}</span>
        {event.data?.stage && (
          <span className="badge badge--outline mono">{event.data.stage}</span>
        )}
        {event.data?.step && (
          <span className="badge badge--outline mono">{event.data.step}</span>
        )}
        <span className="tl-item__time">{formatTimeShort(event.ts)}</span>
      </div>
      <div className="tl-item__msg">{event.data?.message}</div>

      {isProgress && (
        <div className="tl-progress-card">
          <div className="tl-progress-card__head">
            <span>Progress</span>
            <span className="ct">
              {event.data.current != null && event.data.total != null
                ? `${event.data.current} / ${event.data.total}`
                : `${Math.round((event.data.progress || 0) * 100)}%`}
            </span>
          </div>
          <div className="tl-progress-card__bar">
            <div
              className="tl-progress-card__fill"
              style={{ width: `${Math.round((event.data.progress || 0) * 100)}%` }}
            />
          </div>
          {event.data.message && (
            <div className="tl-progress-card__msg">{event.data.message}</div>
          )}
          {(event.data.engine || event.data.provider) && (
            <div className="tl-progress-card__badges">
              <EngineBadge engine={event.data.engine} provider={event.data.provider} />
            </div>
          )}
        </div>
      )}

      {(event.data?.engine || event.data?.provider) && !isProgress && (
        <div className="tl-item__meta">
          <EngineBadge engine={event.data.engine} provider={event.data.provider} />
        </div>
      )}

      {t === EVENT_TYPES.STEP_WARNING && event.data?.warning && (
        <div className="tl-item__warn">
          <Icon.Alert className="icon-sm" /> {event.data.warning}
        </div>
      )}
      {(t === EVENT_TYPES.STEP_FAILED || t === EVENT_TYPES.RUN_FAILED) && event.data?.failure_message && (
        <div className="tl-item__error">
          <strong className="mono">{event.data.failure_code}</strong> ·{" "}
          {event.data.failure_message}
        </div>
      )}
      {isReview && event.data?.reason && (
        <div
          className="tl-item__warn"
          style={{ background: "var(--accent-soft)", color: "var(--accent-soft-fg)" }}
        >
          <Icon.UserCheck className="icon-sm" /> {event.data.reason}
        </div>
      )}
    </div>
  );
}
