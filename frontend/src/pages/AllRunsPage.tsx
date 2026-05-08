/**
 * All Runs page — paginated list of every ingestion run for the
 * current tenant/project, with search, status, stage, and quick-tab
 * filters. The data is fetched once (with `pageSize=1000`) and
 * filtered / paginated client-side so the prototype's interactive
 * feel survives the migration. Server-side filtering will kick in
 * once the dataset grows past a few hundred runs per project.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "@/lib/api/client";
import { Banner } from "@/components/Banner";
import { Icon } from "@/components/icons";
import { StatusBadge } from "@/components/badges";
import { useClient } from "@/lib/hooks/useClient";
import { StatusDisplay } from "@/lib/display";
import { RunControls } from "./run-detail/RunControls";
import {
  AWAITING_STATUSES,
  COMPLETED_STATUSES,
  LIST_STATUSES,
  RUN_STATUS,
  RUNNING_STATUSES,
} from "@/lib/constants/runStatus";
import { relativeTime } from "@/lib/format";
import type { RunListItem, RunListResult, RunStatus, Stage } from "@/types/ingestion";
import type { ProjectContext, Toast } from "@/types/ui";

const LIST_STAGES: Stage[] = ["COMPILE", "ENRICH", "GRAPH", "INDEX"];

type QuickFilter =
  | "all"
  | "running"
  | "awaiting"
  | "warnings"
  | "failed"
  | "review"
  | "completed";

interface ListError {
  status: number;
  message: string;
}

interface AllRunsPageProps {
  ctx: ProjectContext;
  onOpenRun: (runId: string) => void;
  onNewRun: () => void;
  pushToast?: (toast: Omit<Toast, "id">) => void;
}

const QUICK_PREDICATES: Record<QuickFilter, (x: RunListItem) => boolean> = {
  all: () => true,
  running: (x) => RUNNING_STATUSES.has(x.status),
  awaiting: (x) => AWAITING_STATUSES.has(x.status),
  warnings: (x) => x.status === RUN_STATUS.SUCCEEDED_WITH_WARNINGS || (x.warningCount ?? 0) > 0,
  failed: (x) => x.status === RUN_STATUS.FAILED,
  review: (x) => x.status === RUN_STATUS.REQUIRES_HUMAN_REVIEW,
  completed: (x) => COMPLETED_STATUSES.has(x.status),
};

export function AllRunsPage({ ctx, onOpenRun, onNewRun, pushToast }: AllRunsPageProps) {
  const client = useClient();
  const ready = !!ctx.tenant && !!ctx.project;

  const [page, setPage] = useState(1);
  const pageSize = 8;
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<RunStatus | "">("");
  const [stageFilter, setStageFilter] = useState<Stage | "">("");
  const [quickFilter, setQuickFilter] = useState<QuickFilter>("all");
  const [allData, setAllData] = useState<RunListResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ListError | null>(null);

  const load = useCallback(async () => {
    if (!ready) {
      setAllData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // Server cap is 200 (`adapters/rest/app.py:1059`). Pull the
      // full page so quick-filter chip counts and client-side
      // pagination reflect the actual run set; without this the
      // dashboard silently truncates above ~10 runs.
      const all = await client.listRuns(ctx, { page: 1, pageSize: 200 });
      setAllData(all);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 500;
      const message = e instanceof Error ? e.message : "Failed to load runs.";
      setError({ status, message });
    } finally {
      setLoading(false);
    }
  }, [client, ctx, ready]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setPage(1);
  }, [q, statusFilter, stageFilter, quickFilter]);

  // Auto-refresh while there's a live run.
  useEffect(() => {
    if (!allData) return;
    const hasLive = allData.items.some(
      (x) => RUNNING_STATUSES.has(x.status) || AWAITING_STATUSES.has(x.status),
    );
    if (!hasLive) return;
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, [allData, load]);

  const filtered = useMemo<RunListItem[]>(() => {
    const items = allData?.items ?? [];
    const qLower = q.trim().toLowerCase();
    const pred = QUICK_PREDICATES[quickFilter];
    return items.filter((x) => {
      if (!pred(x)) return false;
      if (statusFilter && x.status !== statusFilter) return false;
      if (stageFilter && x.currentStage !== stageFilter) return false;
      if (qLower) {
        const hay = `${x.documentName} ${x.runId}`.toLowerCase();
        if (!hay.includes(qLower)) return false;
      }
      return true;
    });
  }, [allData, q, statusFilter, stageFilter, quickFilter]);

  const total = filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const data = useMemo(() => {
    const start = (page - 1) * pageSize;
    return { items: filtered.slice(start, start + pageSize), total };
  }, [filtered, page, total]);

  const stats = useMemo(() => {
    const items = allData?.items ?? [];
    return {
      total: items.length,
      running: items.filter((x) => RUNNING_STATUSES.has(x.status)).length,
      awaiting: items.filter((x) => AWAITING_STATUSES.has(x.status)).length,
      warnings: items.filter(
        (x) => x.status === RUN_STATUS.SUCCEEDED_WITH_WARNINGS || (x.warningCount ?? 0) > 0,
      ).length,
      failed: items.filter((x) => x.status === RUN_STATUS.FAILED).length,
      review: items.filter((x) => x.status === RUN_STATUS.REQUIRES_HUMAN_REVIEW).length,
      completed: items.filter((x) => COMPLETED_STATUSES.has(x.status)).length,
    };
  }, [allData]);

  return (
    <div>
      <div className="page-header">
        <div>
          <span className="page-header__eyebrow">Operations · J1 Pipeline</span>
          <h1>Ingestion runs</h1>
          <p>
            {ready
              ? `${total} run${total === 1 ? "" : "s"} in ${ctx.tenant} / ${ctx.project}`
              : "Set Tenant and Project to view ingestion runs."}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn"
            onClick={() => void load()}
            disabled={!ready || loading}
            title="Refresh"
          >
            <Icon.RefreshCw className={"icon-sm" + (loading ? " spin" : "")} /> Refresh
          </button>
          <button className="btn btn--primary" onClick={onNewRun} disabled={!ready}>
            <Icon.Upload className="icon-sm" /> New ingestion run
          </button>
        </div>
      </div>

      {ready && allData && (
        <div className="summary-pills">
          {(
            [
              {
                key: "running",
                label: "Running",
                value: stats.running,
                sub: "in flight right now",
              },
              {
                key: "awaiting",
                label: "Awaiting",
                value: stats.awaiting,
                sub: "confirmation or review",
              },
              {
                key: "warnings",
                label: "Warnings",
                value: stats.warnings,
                sub: "runs with warnings",
              },
              { key: "failed", label: "Failed", value: stats.failed, sub: "need attention" },
            ] as const
          ).map((p) => (
            <button
              key={p.key}
              className={`summary-pill summary-pill--${p.key}`}
              onClick={() => setQuickFilter(quickFilter === p.key ? "all" : p.key)}
            >
              <span className="summary-pill__label">{p.label}</span>
              <span className="summary-pill__value">{p.value}</span>
              <span className="summary-pill__sub">{p.sub}</span>
            </button>
          ))}
        </div>
      )}

      {!ready && (
        <Banner kind="warn" title="Tenant and Project are required">
          Set Tenant and Project in the context bar above to view ingestion runs.
        </Banner>
      )}

      {ready && error && error.status === 400 && (
        <Banner kind="warn" title="Tenant and Project are required">
          {error.message}
        </Banner>
      )}
      {ready && error && (error.status === 401 || error.status === 403) && (
        <Banner
          kind="err"
          title="Unauthorized"
          action={<button className="btn btn--sm">Authorize</button>}
        >
          {error.message || "Authentication required."}
        </Banner>
      )}
      {ready && error && error.status >= 500 && (
        <Banner
          kind="err"
          title="Server error"
          action={
            <button className="btn btn--sm" onClick={() => void load()}>
              Retry
            </button>
          }
        >
          {error.message}
        </Banner>
      )}

      {ready && (
        <div className="quick-filters" role="tablist" aria-label="Quick filter by status">
          {(
            [
              { key: "all", label: "All", count: stats.total, mod: "" },
              {
                key: "running",
                label: "Running",
                count: stats.running,
                mod: "quick-chip--running",
              },
              {
                key: "awaiting",
                label: "Awaiting",
                count: stats.awaiting,
                mod: "quick-chip--awaiting",
              },
              {
                key: "warnings",
                label: "Warnings",
                count: stats.warnings,
                mod: "quick-chip--warnings",
              },
              {
                key: "failed",
                label: "Failed",
                count: stats.failed,
                mod: "quick-chip--failed",
              },
              {
                key: "review",
                label: "Human review",
                count: stats.review,
                mod: "quick-chip--review",
              },
              {
                key: "completed",
                label: "Completed",
                count: stats.completed,
                mod: "quick-chip--completed",
              },
            ] as const
          ).map((c) => (
            <button
              key={c.key}
              role="tab"
              aria-selected={quickFilter === c.key}
              className={`quick-chip ${c.mod}${quickFilter === c.key ? " is-active" : ""}`}
              onClick={() => setQuickFilter(c.key)}
            >
              {c.key !== "all" && <span className="quick-chip__dot" aria-hidden />}
              <span>{c.label}</span>
            </button>
          ))}
        </div>
      )}

      {ready && (
        <div className="filters">
          <div className="filters__search">
            <Icon.Eye className="icon-sm" />
            <input
              className="input"
              type="search"
              placeholder="Search by document name or run ID…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              style={{
                border: "none",
                height: 32,
                paddingLeft: 0,
                background: "transparent",
                flex: 1,
              }}
            />
          </div>
          <select
            className="input"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as RunStatus | "")}
            style={{ height: 36 }}
          >
            <option value="">All statuses</option>
            {LIST_STATUSES.map((s) => (
              <option key={s} value={s}>
                {StatusDisplay[s]?.label ?? s}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={stageFilter}
            onChange={(e) => setStageFilter(e.target.value as Stage | "")}
            style={{ height: 36 }}
          >
            <option value="">All stages</option>
            {LIST_STAGES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {(q || statusFilter || stageFilter || quickFilter !== "all") && (
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => {
                setQ("");
                setStatusFilter("");
                setStageFilter("");
                setQuickFilter("all");
              }}
            >
              Clear filters
            </button>
          )}
        </div>
      )}

      {ready && !error && (
        <div className="run-list">
          {loading && data.items.length === 0 && (
            <div style={{ display: "grid", gap: 8 }}>
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  style={{ height: 92, background: "var(--bg-sunken)", borderRadius: 12 }}
                />
              ))}
            </div>
          )}

          {!loading && data.items.length === 0 && (
            <div className="empty">
              {q || statusFilter || stageFilter ? (
                <>
                  <Icon.Eye className="icon-lg" />
                  <h3>No runs match the current filters.</h3>
                  <p>Try clearing the search or filters.</p>
                  <button
                    className="btn btn--sm"
                    onClick={() => {
                      setQ("");
                      setStatusFilter("");
                      setStageFilter("");
                    }}
                  >
                    Clear filters
                  </button>
                </>
              ) : (
                <>
                  <Icon.Upload className="icon-lg" />
                  <h3>No ingestion runs yet.</h3>
                  <p>Upload a document to start.</p>
                  <button className="btn btn--primary btn--sm" onClick={onNewRun}>
                    <Icon.Upload className="icon-sm" /> New ingestion run
                  </button>
                </>
              )}
            </div>
          )}

          {data.items.map((item) => (
            <RunRow
              key={item.runId}
              item={item}
              onClick={() => onOpenRun(item.runId)}
              onRefresh={load}
              pushToast={pushToast}
            />
          ))}

          {totalPages > 1 && (
            <div className="pagination">
              <span className="pagination__info">
                Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} of{" "}
                {total}
              </span>
              <div style={{ display: "flex", gap: 4 }}>
                <button
                  className="btn btn--sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page === 1}
                >
                  <Icon.ChevronLeft className="icon-sm" /> Prev
                </button>
                <span className="pagination__pages">
                  Page {page} of {totalPages}
                </span>
                <button
                  className="btn btn--sm"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                >
                  Next <Icon.ChevronRight className="icon-sm" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface RunRowProps {
  item: RunListItem;
  onClick: () => void;
  onRefresh: () => void;
  pushToast?: (toast: Omit<Toast, "id">) => void;
}

function RunRow({ item, onClick, onRefresh, pushToast }: RunRowProps) {
  const isRunning = RUNNING_STATUSES.has(item.status);
  const isFailed = item.status === RUN_STATUS.FAILED;

  const accent =
    item.status === RUN_STATUS.FAILED
      ? "run-row--failed"
      : item.status === RUN_STATUS.SUCCEEDED_WITH_WARNINGS
        ? "run-row--warnings"
        : item.status === RUN_STATUS.SUCCEEDED
          ? "run-row--succeeded"
          : item.status === RUN_STATUS.REQUIRES_HUMAN_REVIEW
            ? "run-row--review"
            : AWAITING_STATUSES.has(item.status)
              ? "run-row--awaiting"
              : isRunning
                ? "run-row--running"
                : item.status === RUN_STATUS.CANCELLED
                  ? "run-row--cancelled"
                  : "";

  return (
    <div
      className={`run-row ${accent}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter") onClick();
      }}
    >
      <div className="run-row__left">
        <div className="run-row__icon">
          <Icon.File className="icon" />
        </div>
        <div style={{ minWidth: 0 }}>
          <div className="run-row__head">
            <span className="run-row__name">{item.documentName}</span>
            <StatusBadge status={item.status} />
            {item.warningCount > 0 && (
              <span className="badge badge--warning">
                <Icon.Alert className="icon-sm" /> {item.warningCount}
              </span>
            )}
          </div>
          <div className="run-row__meta">
            <span className="mono">{item.runId}</span>
            <span className="run-row__sep">·</span>
            <span>{item.mode}</span>
            <span className="run-row__sep">·</span>
            <span className="mono">{item.policy}</span>
            {item.currentStage && (
              <>
                <span className="run-row__sep">·</span>
                <span>
                  {item.currentStage}
                  {item.currentStep ? ` / ${item.currentStep}` : ""}
                </span>
              </>
            )}
          </div>
          {isRunning && (
            <div className="run-row__progress">
              <div className="run-row__progress-bar">
                <div
                  className="run-row__progress-fill"
                  style={{ width: `${item.progressPercent || 0}%` }}
                />
              </div>
              <span
                className="mono"
                style={{
                  fontSize: 11,
                  color: "var(--text-muted)",
                  minWidth: 36,
                  textAlign: "right",
                }}
              >
                {item.progressPercent || 0}%
              </span>
            </div>
          )}
          {isFailed && item.failureMessage && (
            <div className="run-row__failure">
              <span className="mono">{item.failureCode}</span> · {item.failureMessage}
            </div>
          )}
        </div>
      </div>
      <div className="run-row__right">
        <div className="run-row__time">
          <label>Started</label>
          <span>{relativeTime(item.startedAt)}</span>
        </div>
        <div className="run-row__time">
          <label>{item.completedAt ? "Completed" : "Updated"}</label>
          <span>{relativeTime(item.completedAt ?? item.updatedAt)}</span>
        </div>
        {pushToast && (
          <div
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            <RunControls
              compact
              run={{
                runId: item.runId,
                document_name: item.documentName,
                mode: item.mode,
                policy: item.policy,
                status: item.status,
                started_at: item.startedAt,
                completed_at: item.completedAt,
                progress_pct: item.progressPercent,
                warning_count: item.warningCount,
                current_stage: item.currentStage,
                current_step: item.currentStep,
              }}
              onRefresh={onRefresh}
              pushToast={pushToast}
            />
          </div>
        )}
        <Icon.ChevronRight className="icon" />
      </div>
    </div>
  );
}
