/**
 * Pure-logic tests for the macro-stage timeline projection
 * (`timeline-grouping.ts`).
 *
 * Mirrors the backend's `test_macro_event_derivation.py`. The two
 * implementations must agree on the (stage, step, event_type) →
 * macro mapping so a future server-side switch is a no-op.
 *
 * Plain node env (no jsdom) — these are projection helpers, not
 * React components.
 */

import { describe, expect, it } from "vitest";
import { EVENT_TYPES } from "@/lib/constants/events";
import type { ProgressEvent } from "@/types/ingestion";
import {
  classifyMacroStage,
  deriveMacroEventType,
  groupTimelineByMacroStage,
} from "../timeline-grouping";


// Small event-factory so each test case is one line. `data` accepts
// arbitrary keys — we deliberately push through `as never` for the
// document_id field which lives outside the strict
// `ProgressEventData` interface (the runtime payload carries it).
function _event(
  partial: Partial<ProgressEvent> & {
    event: ProgressEvent["event"];
    data?: Record<string, unknown>;
  },
): ProgressEvent {
  return {
    eventId: partial.eventId ?? `evt-${Math.random().toString(36).slice(2, 8)}`,
    event: partial.event,
    ts: partial.ts ?? 0,
    data: {
      runId: "run-1",
      ...(partial.data as never),
    } as never,
  };
}


describe("classifyMacroStage", () => {
  it("classifies compile step as COMPILE", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "COMPILE", step: "compile" },
    });
    expect(classifyMacroStage(event)).toBe("COMPILE");
  });

  it("classifies verify_compile step as VERIFY", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_COMPLETED,
      data: { stage: "VERIFY", step: "verify_compile" },
    });
    expect(classifyMacroStage(event)).toBe("VERIFY");
  });

  it("is case-insensitive on the stage field", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "compile", step: "compile" },
    });
    expect(classifyMacroStage(event)).toBe("COMPILE");
  });

  it("returns null for non-macro stages", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "GRAPH", step: "graph" },
    });
    expect(classifyMacroStage(event)).toBeNull();
  });

  it("classifies enrich_stage step as ENRICH", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "ENRICH", step: "enrich_stage" },
    });
    expect(classifyMacroStage(event)).toBe("ENRICH");
  });

  it("classifies assess_enrichment step as ASSESS_ENRICHMENT", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "ASSESS_ENRICHMENT", step: "assess_enrichment" },
    });
    expect(classifyMacroStage(event)).toBe("ASSESS_ENRICHMENT");
  });

  it("returns null when stage or step is missing", () => {
    const evt1 = _event({ event: EVENT_TYPES.STEP_STARTED, data: {} });
    const evt2 = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "COMPILE" },
    });
    expect(classifyMacroStage(evt1)).toBeNull();
    expect(classifyMacroStage(evt2)).toBeNull();
  });
});


describe("deriveMacroEventType", () => {
  it("projects compile step.started onto compile.started", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "COMPILE", step: "compile" },
    });
    expect(deriveMacroEventType(event)).toBe(EVENT_TYPES.COMPILE_STARTED);
  });

  it("projects compile step.completed onto compile.completed", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_COMPLETED,
      data: { stage: "COMPILE", step: "compile" },
    });
    expect(deriveMacroEventType(event)).toBe(EVENT_TYPES.COMPILE_COMPLETED);
  });

  it("projects compile step.failed onto compile.failed", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_FAILED,
      data: { stage: "COMPILE", step: "compile" },
    });
    expect(deriveMacroEventType(event)).toBe(EVENT_TYPES.COMPILE_FAILED);
  });

  it("projects verify_compile lifecycle onto verification.*", () => {
    const started = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "VERIFY", step: "verify_compile" },
    });
    const completed = _event({
      event: EVENT_TYPES.STEP_COMPLETED,
      data: { stage: "VERIFY", step: "verify_compile" },
    });
    const failed = _event({
      event: EVENT_TYPES.STEP_FAILED,
      data: { stage: "VERIFY", step: "verify_compile" },
    });
    expect(deriveMacroEventType(started)).toBe(EVENT_TYPES.VERIFICATION_STARTED);
    expect(deriveMacroEventType(completed)).toBe(
      EVENT_TYPES.VERIFICATION_COMPLETED,
    );
    expect(deriveMacroEventType(failed)).toBe(EVENT_TYPES.VERIFICATION_FAILED);
  });

  it("returns null for step.progress / step.warning even on macro stages", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_PROGRESS,
      data: { stage: "COMPILE", step: "compile" },
    });
    expect(deriveMacroEventType(event)).toBeNull();
  });

  it("returns null for non-macro stages", () => {
    const event = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "GRAPH", step: "graph" },
    });
    expect(deriveMacroEventType(event)).toBeNull();
  });

  it("projects enrich_stage lifecycle onto enrich.*", () => {
    const started = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "ENRICH", step: "enrich_stage" },
    });
    const completed = _event({
      event: EVENT_TYPES.STEP_COMPLETED,
      data: { stage: "ENRICH", step: "enrich_stage" },
    });
    const failed = _event({
      event: EVENT_TYPES.STEP_FAILED,
      data: { stage: "ENRICH", step: "enrich_stage" },
    });
    const skipped = _event({
      event: EVENT_TYPES.STEP_SKIPPED,
      data: { stage: "ENRICH", step: "enrich_stage" },
    });
    expect(deriveMacroEventType(started)).toBe(EVENT_TYPES.ENRICH_STARTED);
    expect(deriveMacroEventType(completed)).toBe(EVENT_TYPES.ENRICH_COMPLETED);
    expect(deriveMacroEventType(failed)).toBe(EVENT_TYPES.ENRICH_FAILED);
    expect(deriveMacroEventType(skipped)).toBe(EVENT_TYPES.ENRICH_SKIPPED);
  });

  it("projects assess_enrichment started + completed", () => {
    const started = _event({
      event: EVENT_TYPES.STEP_STARTED,
      data: { stage: "ASSESS_ENRICHMENT", step: "assess_enrichment" },
    });
    const completed = _event({
      event: EVENT_TYPES.STEP_COMPLETED,
      data: { stage: "ASSESS_ENRICHMENT", step: "assess_enrichment" },
    });
    expect(deriveMacroEventType(started)).toBe(
      EVENT_TYPES.ASSESS_ENRICHMENT_STARTED,
    );
    expect(deriveMacroEventType(completed)).toBe(
      EVENT_TYPES.ASSESS_ENRICHMENT_COMPLETED,
    );
  });
});


describe("groupTimelineByMacroStage", () => {
  it("returns an empty array for an empty event list", () => {
    expect(groupTimelineByMacroStage([])).toEqual([]);
  });

  it("collapses consecutive compile events for the same doc into one section", () => {
    const events: ProgressEvent[] = [
      _event({
        eventId: "e1",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "e2",
        event: EVENT_TYPES.STEP_PROGRESS,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "e3",
        event: EVENT_TYPES.STEP_COMPLETED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
    ];
    const sections = groupTimelineByMacroStage(events);
    expect(sections).toHaveLength(1);
    const [first] = sections;
    expect(first?.macro).toBe("COMPILE");
    expect(first?.title).toBe("Compile · doc-1");
    expect(first?.events).toHaveLength(3);
    expect(first?.status).toBe("completed");
  });

  it("splits compile sections when document_id changes", () => {
    const events: ProgressEvent[] = [
      _event({
        eventId: "e1",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "e2",
        event: EVENT_TYPES.STEP_COMPLETED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "e3",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-2" },
      }),
    ];
    const sections = groupTimelineByMacroStage(events);
    expect(sections).toHaveLength(2);
    expect(sections[0]?.title).toBe("Compile · doc-1");
    expect(sections[1]?.title).toBe("Compile · doc-2");
    expect(sections[1]?.status).toBe("running");
  });

  it("renders ungrouped events as standalone sections", () => {
    const events: ProgressEvent[] = [
      _event({
        eventId: "e1",
        event: EVENT_TYPES.RUN_CREATED,
        data: {},
      }),
      _event({
        eventId: "e2",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "e3",
        event: EVENT_TYPES.RUN_COMPLETED,
        data: {},
      }),
    ];
    const sections = groupTimelineByMacroStage(events);
    expect(sections).toHaveLength(3);
    expect(sections[0]?.macro).toBeNull();
    expect(sections[0]?.events).toHaveLength(1);
    expect(sections[1]?.macro).toBe("COMPILE");
    expect(sections[2]?.macro).toBeNull();
    expect(sections[2]?.status).toBe("completed");
  });

  it("groups a verify section after a compile section", () => {
    const events: ProgressEvent[] = [
      _event({
        eventId: "c1",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "c2",
        event: EVENT_TYPES.STEP_COMPLETED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "v1",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "VERIFY", step: "verify_compile", document_id: "doc-1" },
      }),
      _event({
        eventId: "v2",
        event: EVENT_TYPES.STEP_FAILED,
        data: { stage: "VERIFY", step: "verify_compile", document_id: "doc-1" },
      }),
    ];
    const sections = groupTimelineByMacroStage(events);
    expect(sections).toHaveLength(2);
    expect(sections[0]?.macro).toBe("COMPILE");
    expect(sections[0]?.status).toBe("completed");
    expect(sections[1]?.macro).toBe("VERIFY");
    expect(sections[1]?.status).toBe("failed");
  });

  it("reports running status when only started events have arrived", () => {
    const events: ProgressEvent[] = [
      _event({
        eventId: "c1",
        event: EVENT_TYPES.STEP_STARTED,
        data: { stage: "COMPILE", step: "compile", document_id: "doc-1" },
      }),
    ];
    const sections = groupTimelineByMacroStage(events);
    expect(sections[0]?.status).toBe("running");
  });
});
