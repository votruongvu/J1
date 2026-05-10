/**
 * Right-side technical drawer that surfaces raw run / event /
 * selected-event JSON for engineers diagnosing a run.
 *
 * Compile-first: there is no longer a single canonical IngestPlan
 * to render — the AssessmentPlan + post-compile enrich plan are
 * artifact-backed and have their own UI panels (Compile Strategy,
 * Enrich Plan). Operators wanting raw JSON for those should use
 * the Raw artifacts result tab.
 */

import { useState } from "react";
import { Icon } from "@/components/icons";
import { JsonView } from "@/components/JsonView";
import type { IngestionRun, ProgressEvent } from "@/types/ingestion";

interface TechDrawerProps {
  open: boolean;
  onClose: () => void;
  run: IngestionRun | null;
  events: ProgressEvent[];
  selectedEvent: ProgressEvent | null;
}

type Tab = "run" | "events" | "selected";

export function TechDrawer({
  open,
  onClose,
  run,
  events,
  selectedEvent,
}: TechDrawerProps) {
  const [tab, setTab] = useState<Tab>("run");

  return (
    <div className={`drawer ${open ? "is-open" : ""}`} role="dialog" aria-hidden={!open}>
      <div className="drawer__head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Icon.Code className="icon" />
          <strong>Technical details</strong>
        </div>
        <button className="btn btn--ghost btn--sm" onClick={onClose}>
          <Icon.X className="icon-sm" />
        </button>
      </div>
      <div className="drawer__tabs">
        <button
          className={`drawer__tab ${tab === "run" ? "is-active" : ""}`}
          onClick={() => setTab("run")}
        >
          Run
        </button>
        <button
          className={`drawer__tab ${tab === "events" ? "is-active" : ""}`}
          onClick={() => setTab("events")}
        >
          Events ({events.length})
        </button>
        <button
          className={`drawer__tab ${tab === "selected" ? "is-active" : ""}`}
          onClick={() => setTab("selected")}
        >
          Selected
        </button>
      </div>
      <div className="drawer__body">
        {tab === "run" && <JsonView value={run} />}
        {tab === "events" && <JsonView value={events} />}
        {tab === "selected" &&
          (selectedEvent ? (
            <JsonView value={selectedEvent} />
          ) : (
            <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
              Click any event in the timeline to inspect its raw payload.
            </div>
          ))}
      </div>
    </div>
  );
}
