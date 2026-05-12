/**
 * Status-aware control buttons for an ingestion run.
 *
 * Phase 8 reorganisation: the document-centric refactor moved
 * destructive lifecycle actions (delete the run, purge the run,
 * re-process from scratch) up to the document layer — users
 * should manage documents, not runs. This component now splits
 * its action set into two tiers:
 *
 *  * **Primary controls** — workflow lifecycle (pause / resume /
 *    cancel), the "Continue from compiled result" affordance, and
 *    rebuild-index. These are run-scoped concerns that legitimately
 *    operate on a specific attempt, not the document as a whole.
 *
 *  * **Advanced (debug)** — the old run-level destructive actions
 *    (re-process / soft-delete / purge). Hidden behind an
 *    `Advanced ▾` disclosure so a normal user reaching for "delete
 *    this run" is gently nudged toward the document-level
 *    Detach / Remove instead.
 *
 * Compact mode (used on the legacy runs list) hides the advanced
 * disclosure entirely — there's no room for a collapsible panel in
 * a list row, and the list view shouldn't encourage run-level
 * destructive ops anyway.
 *
 * UX contract (unchanged):
 * - One in-flight action at a time. While a request is pending,
 * all buttons disable + the active one shows a spinner.
 * - Cancel + advanced destructive actions use `window.confirm`.
 * - Success/failure surface as a toast.
 * - After every attempted action the parent refreshes the run via
 * `onRefresh` so the run record's new status flows back into
 * header/panels.
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

type ControlAction =
  | "pause" | "resume" | "cancel"
  | "reindex" | "delete" | "resumeCheckpoint" | "rebuildIndex" | "purge";

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
  // Disclosure state for the run-level destructive actions
  // (re-process / delete / purge). Hidden by default in detail
  // view; entirely omitted in compact list-row view.
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
        // The document-centric refactor moved primary cleanup
        // up to the document level — Detach / Remove on the
        // document page handles what a normal user wants.
        // Run-level delete still works as a debug affordance but
        // the copy makes the document-level path obvious.
        const ok = window.confirm(
          `Delete THIS RUN of "${run.document_name}"?\n\n` +
            `This soft-deletes ONLY this attempt. The document and ` +
            `its other runs stay attached to the knowledge base.\n\n` +
            `Looking to remove the document from search/answers? ` +
            `Use Detach or Remove on the document page instead.`,
        );
        if (!ok) return;
      }
      if (action === "reindex") {
        const ok = window.confirm(
          `Re-process "${run.document_name}" from scratch?\n\n` +
            `Starts a NEW ingestion run for the same document. The ` +
            `original active run is preserved until the new run ` +
            `reaches a successful terminal state.\n\n` +
            `Tip: "Re-index" on the document page is the document-` +
            `centric equivalent of this action.`,
        );
        if (!ok) return;
      }
      if (action === "resumeCheckpoint") {
        // Operator-friendly relabel per spec: "Continue from
        // compiled result" tells the user exactly where the
        // workflow picks up, vs. the generic "Resume" which
        // collided with the paused→running button.
        const ok = window.confirm(
          `Continue "${run.document_name}" from the compiled result?\n\n` +
            `Skips enrich + graph stages that already completed in ` +
            `the prior run; compile and chunk-generation always re-run. ` +
            `Refused (412) if settings drifted since the prior run — ` +
            `use Re-process in that case.`,
        );
        if (!ok) return;
      }
      if (action === "rebuildIndex") {
        const ok = window.confirm(
          `Rebuild the retrieval index for "${run.document_name}"?\n\n` +
            `Re-runs ONLY the index activity against the chunks the ` +
            `prior run already produced. Use when the vector store ` +
            `was cleared or the embedding model upgraded.\n\n` +
            `Refused (412) if no chunks exist — use Re-process instead.`,
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
        } else if (action === "resumeCheckpoint") {
          const result = await client.resumeFromCheckpoint(run.runId);
          toastTitle = "Resume started";
          const skipped =
            result.resumedSteps.length > 0
              ? ` (skipping ${result.resumedSteps.join(", ")})`
              : "";
          toastBody = `New run ${result.resumeRunId.slice(0, 8)} created${skipped}.`;
          newRunId = result.resumeRunId;
        } else if (action === "rebuildIndex") {
          const result = await client.rebuildIndex(run.runId);
          toastTitle = "Index rebuild started";
          toastBody =
            `New run ${result.rebuildRunId.slice(0, 8)} created — ` +
            `re-indexing ${result.carryForwardChunkCount} chunk(s).`;
          newRunId = result.rebuildRunId;
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
  // ---- Primary controls (always visible on terminal/inflight runs)
  const showPause = PAUSE_FROM.has(status);
  const showResume = RESUME_FROM.has(status);
  const showCancel = CANCEL_FROM.has(status);
  // "Continue from compiled result" — only on FAILED runs (the
  // resume snapshot is persisted on SUCCEEDED / FAILED paths;
  // CANCELLED isn't a meaningful resume point). The backend
  // returns 412 if the snapshot is absent or settings drifted.
  const showResumeCheckpoint = status === RUN_STATUS.FAILED;
  // Rebuild index is a niche-but-legitimate run-level action
  // (run-scoped retry of the index activity only). Stays in the
  // primary surface because there's no document-level equivalent.
  const showRebuildIndex = !isActive && !isDeleted;

  // ---- Advanced (debug) — run-level destructive actions.
  // Document-centric refactor moved these up to the document
  // layer; we keep them here for power users but gate behind a
  // disclosure so a normal user doesn't reach for them by mistake.
  const showAdvancedReindex = !isActive && !isDeleted;
  const showAdvancedDelete = !isActive && !isDeleted;
  const showAdvancedPurge = isDeleted;
  const hasAdvanced =
    !compact && (showAdvancedReindex || showAdvancedDelete || showAdvancedPurge);

  if (
    !showPause && !showResume && !showCancel
    && !showResumeCheckpoint && !showRebuildIndex
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
      {showResumeCheckpoint && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("resumeCheckpoint")}
          aria-label="Continue from compiled result"
          title={
            "Continue from the compiled result — skips enrich + graph " +
            "stages completed by this run. Compile + chunks always re-run."
          }
          data-testid="run-controls-continue"
        >
          {isPending("resumeCheckpoint") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.Play className="icon-sm" />
          )}
          {!compact && (
            <span style={{ marginLeft: 4 }}>Continue from compiled result</span>
          )}
        </button>
      )}
      {showRebuildIndex && (
        <button
          type="button"
          className={btnClass}
          disabled={anyPending}
          onClick={() => void dispatch("rebuildIndex")}
          aria-label="Rebuild retrieval index from existing chunks"
          title={
            "Re-run ONLY the index activity against existing chunks. " +
            "Use after vector store outages or embedding-model upgrades."
          }
        >
          {isPending("rebuildIndex") ? (
            <Icon.RefreshCw className="icon-sm spin" />
          ) : (
            <Icon.Code className="icon-sm" />
          )}
          {!compact && <span style={{ marginLeft: 4 }}>Rebuild index</span>}
        </button>
      )}

      {/* Advanced (debug) — collapsed by default. Detail view
          only — list-row compact mode skips the advanced section
          entirely because there's no room for a disclosure. */}
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
              "Run-level destructive actions. Most users should " +
              "manage documents (Detach / Remove / Re-index) instead."
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
                Looking to remove the document from search/answers?
                Use <strong>Detach</strong> or <strong>Remove</strong>{" "}
                on the document page.
              </p>
              {showAdvancedReindex && (
                <button
                  type="button"
                  className={btnClass}
                  disabled={anyPending}
                  onClick={() => void dispatch("reindex")}
                  aria-label="Re-process from original document"
                  title="Start a new ingestion run for the same document"
                  data-testid="run-controls-reprocess"
                >
                  {isPending("reindex") ? (
                    <Icon.RefreshCw className="icon-sm spin" />
                  ) : (
                    <Icon.RefreshCw className="icon-sm" />
                  )}
                  <span style={{ marginLeft: 4 }}>Re-process</span>
                </button>
              )}
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
