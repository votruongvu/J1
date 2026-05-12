/**
 * Live timeline — auto-scrolling stream of progress events. Each event
 * is colour-coded by kind (running / success / warning / error /
 * review / info) and clickable for detail inspection.
 */

import { useEffect, useMemo, useRef } from "react";
import { Icon } from "@/components/icons";
import { EngineBadge } from "@/components/badges";
import {
  IngestionStepIcon,
  type StepStatus,
} from "@/components/ingestion-icons";
import { EVENT_TYPES } from "@/lib/constants/events";
import { eventTypeLabel } from "@/lib/display";
import {
  internalStepToUserFacing,
  processingStepById,
  userFacingStepLabel,
} from "@/lib/processing-steps";
import type { ProgressEvent } from "@/types/ingestion";
import type { StreamStatus as StreamStatusKind } from "@/types/ui";
import {
  groupTimelineByMacroStage,
  type TimelineSection,
} from "./timeline-grouping";

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

  // Hide legacy IngestPlanner-era events from the user-facing
  // timeline. The compile-first workflow no longer emits these:
  // pre-compile work surfaces as `assess_compile_strategy` step
  // events, post-compile as `assess_enrichment`. The raw plan.*
  // events still ship through SSE for diagnostic consumers, but
  // they only appear on legacy runs replayed from history.
  const visibleEvents = events.filter(
    (e) =>
      e.event !== EVENT_TYPES.PLAN_GENERATED
      && e.event !== EVENT_TYPES.PLAN_REVISED
      && e.event !== EVENT_TYPES.PLAN_CONFIRMED,
  );

  // project the flat event list into macro-stage sections
  // so the timeline can draw "Compile" / "Verification" headers
  // around the operator-detail step rows. Pure helper — cheap to
  // recompute on each event arrival.
  const sections = useMemo(
    () => groupTimelineByMacroStage(visibleEvents),
    [visibleEvents],
  );

  return (
    <div className="card live-timeline-card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Live timeline</h3>
          <p className="card__subtitle">
            {visibleEvents.length} event{visibleEvents.length === 1 ? "" : "s"}
          </p>
        </div>
        <StreamStatus status={streamStatus} />
      </div>
      <div className="card__body" ref={scrollRef}>
        {visibleEvents.length === 0 ? (
          <div className="tl-empty">
            No events yet. They&apos;ll appear here as the run progresses.
          </div>
        ) : (
          <div className="timeline">
            {sections.map((section) => (
              <TimelineSectionView
                key={section.key}
                section={section}
                onSelectEvent={onSelectEvent}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

interface TimelineSectionViewProps {
  section: TimelineSection;
  onSelectEvent?: (event: ProgressEvent) => void;
}

function TimelineSectionView({ section, onSelectEvent }: TimelineSectionViewProps) {
  // Ungrouped section: one event, render directly without a header.
  // Preserves the existing flat-row look for plan.* / run.* / finalize
  // events that don't belong under a macro stage.
  if (section.macro === null) {
    const event = section.events[0];
    if (!event) return null;
    return (
      <TimelineEventItem
        event={event}
        onClick={() => onSelectEvent?.(event)}
      />
    );
  }
  return (
    <>
      <MacroStageHeader section={section} />
      {section.events.map((e) => (
        <TimelineEventItem
          key={e.eventId}
          event={e}
          onClick={() => onSelectEvent?.(e)}
        />
      ))}
    </>
  );
}

function MacroStageHeader({ section }: { section: TimelineSection }) {
  const kind =
    section.status === "failed"
      ? "error"
      : section.status === "completed"
        ? "success"
        : section.status === "running"
          ? "running"
          : "info";
  return (
    <div className={`tl-section tl-section--${kind}`}>
      <div className="tl-section__dot" aria-hidden />
      <div className="tl-section__head">
        <span className="tl-section__title">{section.title}</span>
        <span className="tl-section__badge mono">
          {section.events.length} event{section.events.length === 1 ? "" : "s"}
        </span>
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

  // Project the timeline event onto a step-icon status so the row
  // shows the matching pixel icon instead of a generic dot. Only
  // step.* events carry a `step` field; non-step events fall back
  // to the existing dot/icon glyph so the column shape stays
  // consistent.
  const stepRaw = (event.data?.step as string | null | undefined) ?? null;
  const stepId = internalStepToUserFacing(stepRaw);
  const iconStatus: StepStatus | null = (() => {
    if (!stepId) return null;
    if (t === EVENT_TYPES.STEP_STARTED) return "running";
    if (t === EVENT_TYPES.STEP_COMPLETED) return "completed";
    if (t === EVENT_TYPES.STEP_FAILED) return "failed";
    if (t === EVENT_TYPES.STEP_SKIPPED) return "skipped";
    if (t === EVENT_TYPES.STEP_PROGRESS) return "running";
    return null;
  })();
  // Operator-readable description of what this step does. Pulled
  // from the canonical PROCESSING_STEPS table so the timeline row
  // explains "Assess Compile Strategy" / "Assess Enrichment" in
  // plain language without needing per-event backend copy.
  const stepDescription =
    stepId && t === EVENT_TYPES.STEP_STARTED
      ? processingStepById(stepId).description
      : null;

  return (
    <div className={`tl-item tl-item--${kind}`} onClick={onClick} style={{ cursor: "pointer" }}>
      <div className="tl-item__dot">
        {iconStatus !== null ? (
          <IngestionStepIcon
            step={stepId}
            status={iconStatus}
            size="xs"
            ariaLabel={`${userFacingStepLabel(stepRaw)} — ${iconStatus}`}
          />
        ) : (
          <>
            {kind === "success" && <Icon.Check className="icon-sm" />}
            {kind === "warning" && <Icon.Alert className="icon-sm" />}
            {kind === "error" && <Icon.X className="icon-sm" />}
            {kind === "review" && <Icon.UserCheck className="icon-sm" />}
          </>
        )}
      </div>
      <div className="tl-item__head">
        <span className="tl-item__type">{eventTypeLabel(t)}</span>
        {event.data?.step ? (
          // Single inline stage tag. Earlier rows carried three
          // overlapping tags (user-facing label + internal step
          // name + internal stage label); the internal pair was
          // diagnostic noise once macro grouping landed, so the
          // timeline now shows just the operator-facing label.
          <span className="badge">
            {userFacingStepLabel(event.data.step as string)}
          </span>
        ) : null}
        <span className="tl-item__time">{formatTimeShort(event.ts)}</span>
      </div>
      <div className="tl-item__msg">{event.data?.message}</div>
      {stepDescription && !event.data?.message && (
        <div className="tl-item__step-desc">{stepDescription}</div>
      )}

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
