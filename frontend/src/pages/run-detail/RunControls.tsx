/**
 * Status-aware control buttons for an ingestion run.
 *
 * A run is an immutable execution record: nothing here ever restarts
 * or re-indexes the run. Re-processing a document is a document-level
 * action — see the "Re-Index Document" button on the Document page.
 *
 * What this component still exposes:
 *
 *   * **Cancel** — stop an in-flight workflow. Lets operators stop a
 *     stuck or unwanted run; the run still becomes an immutable
 *     CANCELLED record.
 *   * **Pause** — pause an in-flight workflow at the next
 *     pause-checkpoint. Operator escape hatch; resume is intentionally
 *     not surfaced (a paused run can only be cancelled, then the user
 *     starts a fresh re-index from the document).
 *   * **Advanced → Delete / Purge** — soft-delete the run record or
 *     hard-purge it from disk. These never start a new run; they only
 *     remove historical data.
 *
 * What's removed:
 *
 *   * "Re-process" / "Re-index" on a run — only available at the
 *     document level now.
 *   * "Resume" of a stopped/paused run — only document-level
 *     re-index can produce a new run.
 *   * "Continue from compiled result" / "Rebuild index" — same reason.
 */

import { useCallback, useState } from "react";
import { ApiError, type RunControlResult } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import {
  ACTIVE_STATUSES,
  CANCELLABLE_STATUSES,
  PAUSABLE_STATUSES,
  RUN_STATUS,
} from "@/lib/constants/runStatus";
import type { IngestionRun } from "@/types/ingestion";
import type { Toast } from "@/types/ui";
import { Icon } from "@/components/icons";

type ControlAction = "pause" | "cancel" | "delete" | "purge";

interface RunControlsProps {
  run: IngestionRun | null;
  onRefresh: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  /** Compact list-row variant — narrower buttons, no labels. */
  compact?: boolean;
  /** Optional: invoked after a successful Delete with `null` so the
   * parent can navigate away (back to the list). */
  onAfterAction?: (action: ControlAction, newRunId: string | null) => void;
}

const PAUSE_FROM = PAUSABLE_STATUSES;
const CANCEL_FROM = CANCELLABLE_STATUSES;

export function RunControls({
  run, onRefresh, pushToast, compact = false, onAfterAction,
}: RunControlsProps) {
  const client = useClient();
  const [pending, setPending] = useState<ControlAction | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);

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
          `Delete THIS RUN of "${run.document_name}"?\n\n` +
            `This soft-deletes ONLY this attempt. The document and ` +
            `its other runs stay attached to the knowledge base.\n\n` +
            `Looking to remove the document from search/answers? ` +
            `Use Detach or Remove on the document page instead.`,
        );
        if (!ok) return;
      }
      if (action === "purge") {
        const ok = window.confirm(
          `PERMANENTLY purge "${run.document_name}"?\n\n` +
            `This physically deletes the run record + every artifact ` +
            `file on disk + cascades to validation sets/runs. The ` +
            `audit log stays intact for compliance, but the run + its ` +
            `outputs are GONE.\n\n` +
            `This action CANNOT be undone.`,
        );
        if (!ok) return;
      }
      setPending(action);
      try {
        let toastTitle = "";
        let toastBody = "";
        const newRunId: string | null = null;
        if (action === "pause" || action === "cancel") {
          let result: RunControlResult;
          if (action === "pause") result = await client.pauseRun(run.runId);
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
        } else if (action === "purge") {
          const result = await client.purgeRun(run.runId);
          toastTitle = "Purged";
          toastBody =
            `${result.filesDeleted} file(s) + ${result.artifactsPurged} ` +
            `artifact record(s) removed.`;
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
  const isActive = ACTIVE_STATUSES.has(status);
  const isDeleted = status === RUN_STATUS.DELETED;
  const showPause = PAUSE_FROM.has(status);
  const showCancel = CANCEL_FROM.has(status);
  const showAdvancedDelete = !isActive && !isDeleted;
  const showAdvancedPurge = isDeleted;
  const hasAdvanced =
    !compact && (showAdvancedDelete || showAdvancedPurge);

  if (
    !showPause && !showCancel
    && !hasAdvanced
    && status !== RUN_STATUS.CANCELLING
  ) {
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
      {hasAdvanced && (
        <div className="run-controls__advanced">
          <button
            type="button"
            className={`${btnClass} btn--ghost run-controls__advanced-toggle`}
            onClick={() => setAdvancedOpen((v) => !v)}
            aria-expanded={advancedOpen}
            aria-controls="run-controls-advanced-panel"
            data-testid="run-controls-advanced-toggle"
            title={
              "Run-level destructive actions. Re-processing a document " +
              "is a document-level action (Re-Index Document)."
            }
          >
            Advanced {advancedOpen ? "▴" : "▾"}
          </button>
          {advancedOpen && (
            <div
              id="run-controls-advanced-panel"
              className="run-controls__advanced-panel"
              role="group"
              aria-label="Advanced run actions"
            >
              <p className="run-controls__advanced-note">
                These act on this specific run, not the document.
                To re-process the document from scratch, use{" "}
                <strong>Re-Index Document</strong> on the document
                page.
              </p>
              {showAdvancedDelete && (
                <button
                  type="button"
                  className={`${btnClass} btn--danger`}
                  disabled={anyPending}
                  onClick={() => void dispatch("delete")}
                  aria-label="Delete this run only"
                  title="Soft-delete this run + its artifacts only"
                  data-testid="run-controls-delete"
                >
                  {isPending("delete") ? (
                    <Icon.RefreshCw className="icon-sm spin" />
                  ) : (
                    <Icon.XCircle className="icon-sm" />
                  )}
                  <span style={{ marginLeft: 4 }}>Delete this run</span>
                </button>
              )}
              {showAdvancedPurge && (
                <button
                  type="button"
                  className={`${btnClass} btn--danger`}
                  disabled={anyPending}
                  onClick={() => void dispatch("purge")}
                  aria-label="Permanently purge run + artifacts"
                  title={
                    "Physically delete the run + every artifact file on disk. " +
                    "Audit log stays intact. CANNOT be undone."
                  }
                  data-testid="run-controls-purge"
                >
                  {isPending("purge") ? (
                    <Icon.RefreshCw className="icon-sm spin" />
                  ) : (
                    <Icon.XCircle className="icon-sm" />
                  )}
                  <span style={{ marginLeft: 4 }}>Purge</span>
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
