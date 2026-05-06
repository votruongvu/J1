/**
 * Status-aware control buttons for an ingestion run — pause / resume /
 * cancel. Each button only renders for statuses where the action is
 * legal (the backend also enforces this and returns 409 if it isn't).
 *
 * UX contract:
 *   - One in-flight action at a time. While a request is pending,
 *     all buttons disable + the active one shows a spinner.
 *   - Cancel uses `window.confirm` because it's irreversible.
 *   - Success/failure surface as a toast.
 *   - After every attempted action the parent refreshes the run via
 *     `onRefresh()` so the run record's new status flows back into
 *     header/panels (the backend flips status synchronously, but the
 *     SSE/polling channels reconverge anyway).
 */

import { useCallback, useState } from "react";
import { ApiError, type RunControlResult } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import type { IngestionRun, RunStatus } from "@/types/ingestion";
import type { Toast } from "@/types/ui";
import { Icon } from "@/components/icons";

type ControlAction = "pause" | "resume" | "cancel";

interface RunControlsProps {
  run: IngestionRun | null;
  onRefresh: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  /** Compact list-row variant — narrower buttons, no labels. */
  compact?: boolean;
}

const PAUSE_FROM: ReadonlySet<RunStatus> = new Set(["RUNNING", "ASSESSING"]);
const RESUME_FROM: ReadonlySet<RunStatus> = new Set(["PAUSED"]);
const CANCEL_FROM: ReadonlySet<RunStatus> = new Set([
  "RUNNING",
  "ASSESSING",
  "PAUSED",
  "PLAN_READY",
  "WAITING_FOR_CONFIRMATION",
]);

export function RunControls({ run, onRefresh, pushToast, compact = false }: RunControlsProps) {
  const client = useClient();
  const [pending, setPending] = useState<ControlAction | null>(null);

  const dispatch = useCallback(
    async (action: ControlAction) => {
      if (!run || pending) return;
      if (action === "cancel") {
        const ok = window.confirm(
          `Cancel "${run.document_name}"? In-flight stages will wind down; ` +
            `the run will land at CANCELLED.`,
        );
        if (!ok) return;
      }
      setPending(action);
      try {
        let result: RunControlResult;
        if (action === "pause") result = await client.pauseRun(run.runId);
        else if (action === "resume") result = await client.resumeRun(run.runId);
        else result = await client.cancelRun(run.runId);
        pushToast({
          kind: "success",
          title: `${action[0].toUpperCase()}${action.slice(1)} requested`,
          body: result.message ?? `Run is now ${result.status}.`,
        });
      } catch (err) {
        const status = err instanceof ApiError ? err.status : undefined;
        const message =
          err instanceof Error ? err.message : `Failed to ${action} the run.`;
        pushToast({
          kind: "error",
          title: `${action[0].toUpperCase()}${action.slice(1)} failed${
            status != null ? ` (HTTP ${status})` : ""
          }`,
          body: message,
        });
      } finally {
        setPending(null);
        onRefresh();
      }
    },
    [client, run, pending, onRefresh, pushToast],
  );

  if (!run) return null;
  const status = run.status;
  const showPause = PAUSE_FROM.has(status);
  const showResume = RESUME_FROM.has(status);
  const showCancel = CANCEL_FROM.has(status);
  if (!showPause && !showResume && !showCancel && status !== "CANCELLING") {
    return null;
  }
  if (status === "CANCELLING") {
    return (
      <span className="run-controls__cancelling" aria-live="polite">
        <Icon.RefreshCw className="icon-sm spin" /> Cancelling…
      </span>
    );
  }
  const btnClass = compact ? "btn btn--xs" : "btn btn--sm";
  const isPending = (a: ControlAction) => pending === a;
  const anyPending = pending !== null;

  return (
    <div className="run-controls" role="group" aria-label="Run controls">
      {showPause && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("pause")}
          aria-label="Pause run"
        >
          {isPending("pause") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.Pause className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Pause</span>}
        </button>
      )}
      {showResume && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("resume")}
          aria-label="Resume run"
        >
          {isPending("resume") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.Play className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Resume</span>}
        </button>
      )}
      {showCancel && (
        <button
          type="button"
          className={`${btnClass} btn--danger`}
          disabled={anyPending}
          onClick={() => void dispatch("cancel")}
          aria-label="Cancel run"
        >
          {isPending("cancel") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.XCircle className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Cancel</span>}
        </button>
      )}
    </div>
  );
}
