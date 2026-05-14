/**
 * Run-level action buttons.
 *
 * A run is an immutable execution record. Re-processing a document is
 * a document-level action — see "Re-Index Document" on the Document
 * page. This component only exposes actions whose scope is THIS run:
 *
 *   * **Pause / Cancel** — operator workflow control while the run is
 *     in-flight.
 *   * **Run enrichment / Refresh enrichment** — active-run only.
 *     Label flips based on whether enrichment artifacts already
 *     exist on the active run.
 *   * **Delete Run** — hard delete this single run + its artifacts /
 *     chunks / enrichment / graph / index / validation outputs. Only
 *     surfaced on non-active runs that are NOT the document's only
 *     run. The only-run case shows helper text pointing at Remove
 *     Knowledge instead.
 *
 * Every action gate comes from server-side capability flags on the
 * ``DocumentRunSummary`` (``isActive``, ``isOnlyRun``,
 * ``canDeleteRun``, ``canRefreshEnrichment``, ``canRunEnrichment``).
 * The frontend MUST NOT recompute these locally — the server is the
 * source of truth, including for the active-run identity.
 */

import { useCallback, useState } from "react";
import { ApiError, type RunControlResult } from "@/lib/api/client";
import { useClient } from "@/lib/hooks/useClient";
import {
  CANCELLABLE_STATUSES,
  PAUSABLE_STATUSES,
  RUN_STATUS,
} from "@/lib/constants/runStatus";
import type { IngestionRun } from "@/types/ingestion";
import type { DocumentRunSummary } from "@/types/documents";
import type { Toast } from "@/types/ui";
import { Icon } from "@/components/icons";

type ControlAction =
  | "pause"
  | "cancel"
  | "delete"
  | "refresh_enrichment"
  | "run_enrichment";

interface RunControlsProps {
  run: IngestionRun | null;
  /**
   * Server-computed capability flags for THIS run. Coming from the
   * document detail's ``runHistory``. When absent, only the in-flight
   * actions (pause / cancel) are surfaced — destructive and
   * enrichment actions stay hidden until the parent loads it.
   */
  capability?: Pick<
    DocumentRunSummary,
    | "isActive"
    | "isOnlyRun"
    | "canDeleteRun"
    | "canRefreshEnrichment"
    | "canRunEnrichment"
  > | null;
  onRefresh: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  /** Compact list-row variant — narrower buttons, no labels. */
  compact?: boolean;
  /** Invoked after a successful action so the parent can navigate
   *  (e.g. back to the run list after a delete, or to the new
   *  enrichment-refresh run). */
  onAfterAction?: (action: ControlAction, newRunId: string | null) => void;
}

const PAUSE_FROM = PAUSABLE_STATUSES;
const CANCEL_FROM = CANCELLABLE_STATUSES;

export function RunControls({
  run,
  capability = null,
  onRefresh,
  pushToast,
  compact = false,
  onAfterAction,
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
          `Delete this run?\n\n` +
            `This permanently deletes this run and all run-scoped ` +
            `artifacts, chunks, enrichment, validation outputs, and ` +
            `index data.\n\n` +
            `This cannot be undone.`,
        );
        if (!ok) return;
      }
      setPending(action);
      try {
        let toastTitle = "";
        let toastBody = "";
        let newRunId: string | null = null;
        if (action === "pause" || action === "cancel") {
          let result: RunControlResult;
          if (action === "pause") result = await client.pauseRun(run.runId);
          else result = await client.cancelRun(run.runId);
          const verb = action.charAt(0).toUpperCase() + action.slice(1);
          toastTitle = `${verb} requested`;
          toastBody = result.message ?? `Run is now ${result.status}.`;
        } else if (action === "delete") {
          const result = await client.deleteRun(run.runId);
          toastTitle = "Run deleted";
          toastBody =
            `${result.filesDeleted} file(s) + ${result.artifactsDeleted} ` +
            `artifact record(s) removed.`;
        } else if (
          action === "refresh_enrichment" || action === "run_enrichment"
        ) {
          const result = await client.refreshRunEnrichment(run.runId);
          toastTitle =
            action === "refresh_enrichment"
              ? "Refresh enrichment started"
              : "Enrichment started";
          toastBody = `New run: ${result.refreshRunId.slice(0, 12)}`;
          newRunId = result.refreshRunId;
        }
        pushToast({ kind: "success", title: toastTitle, body: toastBody });
        if (onAfterAction) onAfterAction(action, newRunId);
      } catch (err) {
        const status = err instanceof ApiError ? err.status : undefined;
        const message =
          err instanceof Error ? err.message : `Failed to ${action} the run.`;
        const verbLabel: Record<ControlAction, string> = {
          pause: "Pause",
          cancel: "Cancel",
          delete: "Delete",
          refresh_enrichment: "Refresh enrichment",
          run_enrichment: "Run enrichment",
        };
        pushToast({
          kind: "error",
          title:
            `${verbLabel[action]} failed` +
            (status != null ? ` (HTTP ${status})` : ""),
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
  const showCancel = CANCEL_FROM.has(status);
  const canDeleteRun = capability?.canDeleteRun ?? false;
  const canRefreshEnrichment = capability?.canRefreshEnrichment ?? false;
  const canRunEnrichment = capability?.canRunEnrichment ?? false;
  const isOnlyRun = capability?.isOnlyRun ?? false;
  const isActive = capability?.isActive ?? false;
  const showOnlyRunHint = !compact && isOnlyRun && !isActive;

  const anyButton =
    showPause ||
    showCancel ||
    canDeleteRun ||
    canRefreshEnrichment ||
    canRunEnrichment;

  if (
    !anyButton
    && !showOnlyRunHint
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
      {canRefreshEnrichment && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("refresh_enrichment")}
          aria-label="Refresh enrichment for active snapshot"
          title={
            "Build a new candidate snapshot that reuses this run's " +
            "compile output and re-runs enrichment + graph + index. " +
            "Current active knowledge stays live until the new " +
            "candidate is promoted."
          }
          data-testid="run-controls-refresh-enrichment"
        >
          {isPending("refresh_enrichment") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.RefreshCw className="icon-sm" />
          )}
          {!compact && (
            <span style={{ marginLeft: 4 }}>
              Refresh enrichment for active snapshot
            </span>
          )}
        </button>
      )}
      {canRunEnrichment && (
        <button
          type="button"
          className={`${btnClass} btn--primary`}
          disabled={anyPending}
          onClick={() => void dispatch("run_enrichment")}
          aria-label="Run enrichment for this run"
          title={
            "Run enrichment on this run for the first time. Re-uses this " +
            "run's compile output."
          }
          data-testid="run-controls-run-enrichment"
        >
          {isPending("run_enrichment") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.RefreshCw className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Run enrichment</span>}
        </button>
      )}
      {canDeleteRun && (
        <button
          type="button"
          className={`${btnClass} btn--danger`}
          disabled={anyPending}
          onClick={() => void dispatch("delete")}
          aria-label="Delete this processing run"
          title={
            "Permanently delete this processing run and the candidate " +
            "snapshot it produced (artifacts, chunks, enrichment, " +
            "validation outputs, index data). The active knowledge " +
            "snapshot is never deletable from this control."
          }
          data-testid="run-controls-delete"
        >
          {isPending("delete") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.XCircle className="icon-sm" />
          )}
          {!compact && (
            <span style={{ marginLeft: 4 }}>Delete Processing Run</span>
          )}
        </button>
      )}
      {showOnlyRunHint && (
        <p
          className="run-controls__only-run-hint"
          data-testid="run-controls-only-run-hint"
        >
          This is the only run for this document. Use{" "}
          <strong>Remove Knowledge</strong> on the document page to
          delete the document and all related data.
        </p>
      )}
    </div>
  );
}
