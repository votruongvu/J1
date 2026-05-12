/**
 * Macro-stage projection for the LiveTimeline.
 *
 * The backend emits flat `step.*` events tagged with `stage` and
 * `step` fields; today there's no server-emitted macro-stage event
 * vocabulary. This module derives the canonical macro names * client-side so the timeline can render macro section headers
 * around the operator-detail step rows.
 *
 * Pure / deterministic: the helpers take an event in and return a
 * label out — they don't subscribe to anything or mutate state.
 * Mirrors the backend's `derive_macro_event_type` in
 * `src/j1/runs/reporter.py`.
 */

import { EVENT_TYPES } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";

/**
 * Coarse macro-stage classification for an event. `null` means the
 * event isn't yet folded under a macro stage and should render as a
 * standalone row (assess_compile_strategy, finalize, etc.).
 */
export type MacroStage =
  | "COMPILE"
  | "ASSESS_ENRICHMENT"
  | "ENRICH"
  | null;

/** Stage strings the backend emits — uppercase, snake_cased. */
const STAGE_COMPILE = "COMPILE";
const STAGE_ASSESS_ENRICHMENT = "ASSESS_ENRICHMENT";
const STAGE_ENRICH = "ENRICH";

const COMPILE_STEPS = new Set(["compile"]);
const ASSESS_ENRICHMENT_STEPS = new Set(["assess_enrichment"]);
const ENRICH_STEPS = new Set(["enrich_stage"]);

/**
 * Classify a progress event into one of the macro stages.
 * Case-insensitive on the `stage` field — legacy lowercase emitters
 * still match. Returns null for events that don't belong to a macro
 * stage (the FE renders them as ungrouped rows).
 */
export function classifyMacroStage(event: ProgressEvent): MacroStage {
  const stage = event.data?.stage ?? "";
  const step = event.data?.step ?? "";
  const stageUpper = stage.toUpperCase();
  if (stageUpper === STAGE_COMPILE && COMPILE_STEPS.has(step)) {
    return "COMPILE";
  }
  if (stageUpper === STAGE_ASSESS_ENRICHMENT && ASSESS_ENRICHMENT_STEPS.has(step)) {
    return "ASSESS_ENRICHMENT";
  }
  if (stageUpper === STAGE_ENRICH && ENRICH_STEPS.has(step)) {
    return "ENRICH";
  }
  return null;
}

/**
 * Extract the document_id off an event without forcing every
 * consumer to widen `ProgressEventData`. The backend writes this
 * key on per-document step events; absent on run-level events.
 */
function eventDocumentId(event: ProgressEvent): string {
  const data = event.data as unknown as Record<string, unknown> | undefined;
  const raw = data?.document_id;
  return typeof raw === "string" ? raw : "";
}

/**
 * Project a `step.*` event onto its canonical macro event name, or
 * null when the event doesn't sit under a macro stage. Mirrors the
 * backend's `derive_macro_event_type`.
 */
export function deriveMacroEventType(event: ProgressEvent): string | null {
  const macro = classifyMacroStage(event);
  if (macro === null) return null;
  const t = event.event;
  if (macro === "COMPILE") {
    if (t === EVENT_TYPES.STEP_STARTED) return EVENT_TYPES.COMPILE_STARTED;
    if (t === EVENT_TYPES.STEP_COMPLETED) return EVENT_TYPES.COMPILE_COMPLETED;
    if (t === EVENT_TYPES.STEP_FAILED) return EVENT_TYPES.COMPILE_FAILED;
  }
  if (macro === "ASSESS_ENRICHMENT") {
    if (t === EVENT_TYPES.STEP_STARTED) {
      return EVENT_TYPES.ASSESS_ENRICHMENT_STARTED;
    }
    if (t === EVENT_TYPES.STEP_COMPLETED) {
      return EVENT_TYPES.ASSESS_ENRICHMENT_COMPLETED;
    }
    if (t === EVENT_TYPES.STEP_SKIPPED) {
      return EVENT_TYPES.ASSESS_ENRICHMENT_SKIPPED;
    }
  }
  if (macro === "ENRICH") {
    if (t === EVENT_TYPES.STEP_STARTED) return EVENT_TYPES.ENRICH_STARTED;
    if (t === EVENT_TYPES.STEP_COMPLETED) return EVENT_TYPES.ENRICH_COMPLETED;
    if (t === EVENT_TYPES.STEP_FAILED) return EVENT_TYPES.ENRICH_FAILED;
    if (t === EVENT_TYPES.STEP_SKIPPED) return EVENT_TYPES.ENRICH_SKIPPED;
  }
  return null;
}

/**
 * One grouped section of the timeline. Either a macro stage with
 * its sub-step events ("Compile" with the per-attempt step.* rows
 * below), or a single ungrouped event (PlanGenerated, finalize…).
 *
 * `status` is aggregated from the section's events:
 * - "running" — at least one started, none completed/failed
 * - "completed" — last terminal event was a completion
 * - "failed" — last terminal event was a failure
 *
 * `lastEventAt` is the millisecond timestamp of the most recent
 * event in the section; used for stable ordering when one macro
 * stage's events interleave with another's (multi-doc runs).
 */
export interface TimelineSection {
  key: string;
  macro: MacroStage;
  title: string;
  status: "running" | "completed" | "failed" | "info";
  events: ProgressEvent[];
  lastEventAt: number;
}

const MACRO_TITLE: Record<NonNullable<MacroStage>, string> = {
  COMPILE: "Compile",
  ASSESS_ENRICHMENT: "Enrichment Assessment",
  ENRICH: "Enrichment",
};

function macroStatus(
  events: ProgressEvent[],
): TimelineSection["status"] {
  let last: TimelineSection["status"] = "info";
  for (const e of events) {
    if (
      e.event === EVENT_TYPES.STEP_STARTED
      || e.event === EVENT_TYPES.STEP_PROGRESS
    ) {
      last = "running";
    } else if (
      e.event === EVENT_TYPES.STEP_COMPLETED
      || e.event === EVENT_TYPES.STEP_SKIPPED
    ) {
      // Skipped is a clean terminal for the macro section — the
      // FE renders "Enrichment skipped" with the same neutral
      // styling as a completion, not a failure.
      last = "completed";
    } else if (e.event === EVENT_TYPES.STEP_FAILED) {
      last = "failed";
    }
  }
  return last;
}

/**
 * Group a flat event list into macro-stage sections (Compile,
 * Verification) interleaved with ungrouped rows in original order.
 *
 * Grouping rule: consecutive events that share a macro classification
 * AND the same `document_id` collapse into one section. A run that
 * processes a single doc produces one Compile section and one
 * Verification section; multi-doc runs interleave naturally.
 *
 * The returned sections preserve the original event order — sections
 * appear in the timeline in the order their first event arrived,
 * not in macro-stage canonical order. (The backend's per-document
 * loop already emits in canonical order; we don't re-sort.)
 */
export function groupTimelineByMacroStage(
  events: ProgressEvent[],
): TimelineSection[] {
  const sections: TimelineSection[] = [];
  let current: TimelineSection | null = null;
  for (const event of events) {
    const macro = classifyMacroStage(event);
    const docId = eventDocumentId(event);
    if (macro === null) {
      if (current !== null) {
        current.status = macroStatus(current.events);
        current.lastEventAt = lastTs(current.events);
        sections.push(current);
        current = null;
      }
      sections.push({
        key: event.eventId,
        macro: null,
        title: event.event,
        status: ungroupedStatus(event),
        events: [event],
        lastEventAt: event.ts ?? 0,
      });
      continue;
    }
    if (
      current !== null
      && current.macro === macro
      && current.events[0]
      && eventDocumentId(current.events[0]) === docId
    ) {
      current.events.push(event);
      continue;
    }
    if (current !== null) {
      current.status = macroStatus(current.events);
      current.lastEventAt = lastTs(current.events);
      sections.push(current);
    }
    current = {
      key: `${macro}:${docId}:${event.eventId}`,
      macro,
      title: docId ? `${MACRO_TITLE[macro]} · ${docId}` : MACRO_TITLE[macro],
      status: "running",
      events: [event],
      lastEventAt: event.ts ?? 0,
    };
  }
  if (current !== null) {
    current.status = macroStatus(current.events);
    current.lastEventAt = lastTs(current.events);
    sections.push(current);
  }
  return sections;
}

function ungroupedStatus(event: ProgressEvent): TimelineSection["status"] {
  if (event.event === EVENT_TYPES.STEP_FAILED) return "failed";
  if (event.event === EVENT_TYPES.RUN_FAILED) return "failed";
  if (event.event === EVENT_TYPES.STEP_COMPLETED) return "completed";
  if (event.event === EVENT_TYPES.RUN_COMPLETED) return "completed";
  if (event.event === EVENT_TYPES.STEP_STARTED) return "running";
  return "info";
}

function lastTs(events: ProgressEvent[]): number {
  let last = 0;
  for (const e of events) {
    if ((e.ts ?? 0) > last) last = e.ts ?? 0;
  }
  return last;
}
