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
import {
  ACTIVE_STATUSES,
  CANCELLABLE_STATUSES,
  PAUSABLE_STATUSES,
  RESUMABLE_STATUSES,
  RUN_STATUS,
} from "@/lib/constants/runStatus";
import type { IngestionRun } from "@/types/ingestion";
import type { Toast } from "@/types/ui";
import { Icon } from "@/components/icons";

type ControlAction = "pause" | "resume" | "cancel" | "reindex" | "delete";

interface RunControlsProps {
  run: IngestionRun | null;
  onRefresh: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  /** Compact list-row variant — narrower buttons, no labels. */
  compact?: boolean;
  /** Optional: invoked after a successful Re-process / Delete with
   * the resulting (new run id | null). Lets the parent navigate
   * away (e.g. to the new reindex run, or back to the list after a
   * delete). */
  onAfterAction?: (action: ControlAction, newRunId: string | null) => void;
}

const PAUSE_FROM = PAUSABLE_STATUSES;
const RESUME_FROM = RESUMABLE_STATUSES;
const CANCEL_FROM = CANCELLABLE_STATUSES;

export function RunControls({
  run, onRefresh, pushToast, compact = false, onAfterAction,
}: RunControlsProps) {
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
      if (action === "delete") {
        const ok = window.confirm(
          `Delete "${run.document_name}"?\n\n` +
            `This soft-deletes the run + every artifact it produced. ` +
            `Tombstoned records stay on disk for audit but no longer ` +
            `appear in the UI. The action is reversible only by an admin.`,
        );
        if (!ok) return;
      }
      if (action === "reindex") {
        const ok = window.confirm(
          `Re-process "${run.document_name}" from scratch?\n\n` +
            `Starts a NEW ingestion run for the same document. The ` +
            `original run stays visible until you delete it explicitly.`,
        );
        if (!ok) return;
      }
      setPending(action);
      try {
        let toastTitle = "";
        let toastBody = "";
        let newRunId: string | null = null;
        if (action === "pause" || action === "resume" || action === "cancel") {
          let result: RunControlResult;
          if (action === "pause") result = await client.pauseRun(run.runId);
          else if (action === "resume") result = await client.resumeRun(run.runId);
          else result = await client.cancelRun(run.runId);
          const verb = action.charAt(0).toUpperCase() + action.slice(1);
          toastTitle = `${verb} requested`;
          toastBody = result.message ?? `Run is now ${result.status}.`;
        } else if (action === "delete") {
          const result = await client.deleteRun(run.runId);
          toastTitle = result.wasAlreadyDeleted
            ? "Already deleted" : "Deleted";
          toastBody = result.wasAlreadyDeleted
            ? "This run was already tombstoned."
            : `Tombstoned ${result.tombstonedArtifactCount} artifact(s).`;
        } else if (action === "reindex") {
          const result = await client.fullReindexRun(run.runId);
          toastTitle = "Re-process started";
          toastBody = `New run ${result.reindexRunId.slice(0, 8)} created.`;
          newRunId = result.reindexRunId;
        }
        pushToast({ kind: "success", title: toastTitle, body: toastBody });
        if (onAfterAction) onAfterAction(action, newRunId);
      } catch (err) {
        const status = err instanceof ApiError ? err.status : undefined;
        const message =
          err instanceof Error ? err.message : `Failed to ${action} the run.`;
        const verb = action.charAt(0).toUpperCase() + action.slice(1);
        pushToast({
          kind: "error",
          title: `${verb} failed${status != null ? ` (HTTP ${status})` : ""}`,
          body: message,
        });
      } finally {
        setPending(null);
        onRefresh();
      }
    },
    [client, run, pending, onRefresh, pushToast, onAfterAction],
  );

  if (!run) return null;
  const status = run.status;
  const showPause = PAUSE_FROM.has(status);
  const showResume = RESUME_FROM.has(status);
  const showCancel = CANCEL_FROM.has(status);
  // Re-process + Delete are only legal on terminal runs (the
  // backend returns 409 otherwise). Show whenever the run is NOT
  // active. Hide on already-deleted runs (status === "deleted")
  // — there's nothing more to delete and the document is gone
  // from the active KB.
  const isActive = ACTIVE_STATUSES.has(status);
  const isDeleted = status === RUN_STATUS.DELETED;
  const showReindex = !isActive && !isDeleted;
  const showDelete = !isActive && !isDeleted;
  if (!showPause && !showResume && !showCancel && !showReindex && !showDelete && status !== RUN_STATUS.CANCELLING) {
    return null;
  }
  if (status === RUN_STATUS.CANCELLING) {
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
      {showReindex && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("reindex")}
          aria-label="Re-process from original document"
          title="Start a new ingestion run for the same document"
        >
          {isPending("reindex") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.RefreshCw className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Re-process</span>}
        </button>
      )}
      {showDelete && (
        <button
          type="button"
          className={`${btnClass} btn--danger`}
          disabled={anyPending}
          onClick={() => void dispatch("delete")}
          aria-label="Delete run from knowledge base"
          title="Soft-delete the run + its artifacts"
        >
          {isPending("delete") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.XCircle className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Delete</span>}
        </button>
      )}
    </div>
  );
}
