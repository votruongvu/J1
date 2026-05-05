/**
 * Right-side technical drawer that surfaces raw run / plan / event /
 * selected-event JSON for engineers diagnosing a run.
 */

import { useState } from "react";
import { Icon } from "@/components/icons";
import { JsonView } from "@/components/JsonView";
import type { ExecutionPlan, IngestionRun, ProgressEvent } from "@/types/ingestion";

interface TechDrawerProps {
  open: boolean;
  onClose: () => void;
  run: IngestionRun | null;
  plan: ExecutionPlan | null;
  events: ProgressEvent[];
  selectedEvent: ProgressEvent | null;
}

type Tab = "run" | "plan" | "events" | "selected";

export function TechDrawer({
  open,
  onClose,
  run,
  plan,
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
          className={`drawer__tab ${tab === "plan" ? "is-active" : ""}`}
          onClick={() => setTab("plan")}
        >
          Plan
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
        {tab === "plan" && <JsonView value={plan} />}
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
