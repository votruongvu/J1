/**
 * Run header — breadcrumb, document name, status, and a strip of
 * top-level run stats (mode / policy / warnings / duration). Pure
 * presentation; the parent passes data.
 *
 * Compile-first: per-step counts (total / run / skip / cond) used
 * to come from the IngestPlan, which the workflow no longer
 * produces. Stage-by-stage detail lives in the LiveTimeline +
 * EnrichPlanPanel; the header keeps only run-scope identity.
 */

import type { IngestionRun } from "@/types/ingestion";
import type { ProjectContext, Toast } from "@/types/ui";
import { Icon } from "@/components/icons";
import { StatusBadge } from "@/components/badges";
import { RunControls } from "./RunControls";

interface RunHeaderProps {
  run: IngestionRun | null;
  ctx: ProjectContext;
  onBack: () => void;
  onOpenDrawer: () => void;
  onRefresh: () => void;
  pushToast: (toast: Omit<Toast, "id">) => void;
  /** Forwarded to RunControls so the page can navigate after
   * Re-process (→ new run) / Delete (→ list). */
  onAfterAction?: (
    action:
      | "pause" | "resume" | "cancel"
      | "reindex" | "delete" | "resumeCheckpoint" | "rebuildIndex" | "purge",
    newRunId: string | null,
  ) => void;
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function RunHeader({
  run, ctx, onBack, onOpenDrawer, onRefresh, pushToast, onAfterAction,
}: RunHeaderProps) {
  if (!run) return null;
  const startedMs = run.started_at ? new Date(run.started_at).getTime() : null;
  const endMs = run.completed_at ? new Date(run.completed_at).getTime() : Date.now();
  const durationSec = startedMs ? Math.max(0, Math.round((endMs - startedMs) / 1000)) : null;

  return (
    <div className="run-hero">
      <div className="run-hero__top">
        <div>
          <div className="run-hero__crumb">
            <a
              href="#"
              onClick={(e) => {
                e.preventDefault();
                onBack();
              }}
            >
              <Icon.ChevronLeft className="icon-sm" /> All runs
            </a>
            <span>·</span>
            <span className="mono">
              {ctx.tenant} / {ctx.project}
            </span>
          </div>
          <h2>
            <span className="run-hero__doc-icon">
              <Icon.File className="icon" />
            </span>
            <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
              {run.document_name}
            </span>
            <StatusBadge status={run.status} />
          </h2>
          <div className="run-hero__id">{run.runId}</div>
        </div>
        <div className="run-hero__actions">
          <RunControls
            run={run}
            onRefresh={onRefresh}
            pushToast={pushToast}
            onAfterAction={onAfterAction}
          />
          <button className="btn btn--sm" onClick={onOpenDrawer}>
            <Icon.Code className="icon-sm" /> Technical details
          </button>
        </div>
      </div>

      <div className="run-stats">
        <div className="run-stats__item">
          <label>Mode</label>
          <div className="vsmall">{run.mode}</div>
        </div>
        <div className="run-stats__item">
          <label>Policy</label>
          <div className="v mono">{run.policy}</div>
        </div>
        <div className="run-stats__item">
          <label>Warnings</label>
          <div
            className="v"
            style={{
              color: run.warning_count > 0 ? "var(--warning-fg)" : "inherit",
            }}
          >
            {run.warning_count || 0}
          </div>
        </div>
        <div className="run-stats__item">
          <label>{run.completed_at ? "Duration" : "Elapsed"}</label>
          <div className="vsmall">{formatDuration(durationSec)}</div>
        </div>
      </div>

      {(run.status === "RUNNING" || run.status === "ASSESSING") && (
        <div className="run-progress">
          <div className="run-progress__step">
            <span className="muted">{run.current_stage || "—"} · </span>
            {run.current_step || "Assessing…"}
          </div>
          <div className="run-progress__bar">
            <div
              className="run-progress__fill"
              style={{ width: `${run.progress_pct || 0}%` }}
            />
          </div>
          <div className="run-progress__pct">{run.progress_pct || 0}%</div>
        </div>
      )}
      {run.warning_count > 0 && run.status !== "FAILED" && (
        <div className="run-warning-banner">
          <Icon.Alert className="icon-sm" /> {run.warning_count} warning
          {run.warning_count === 1 ? "" : "s"} surfaced during this run.
        </div>
      )}
    </div>
  );
}
