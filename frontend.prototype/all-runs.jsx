// All Runs / Job List page.

const { useState: useStateAR, useEffect: useEffectAR, useCallback: useCBAR, useMemo: useMemoAR } = React;

const LIST_STATUSES = [
  "CREATED",
  "ASSESSING",
  "PLAN_READY",
  "WAITING_FOR_CONFIRMATION",
  "RUNNING",
  "SUCCEEDED",
  "SUCCEEDED_WITH_WARNINGS",
  "FAILED",
  "CANCELLED",
  "REQUIRES_HUMAN_REVIEW",
];
const LIST_STAGES = ["COMPILE", "ENRICH", "GRAPH", "INDEX"];

function relativeTime(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function AllRunsPage({ ctx, onOpenRun, onNewRun, pushToast }) {
  const ready = !!ctx.tenant && !!ctx.project;
  const [page, setPage] = useStateAR(1);
  const [pageSize] = useStateAR(8);
  const [q, setQ] = useStateAR("");
  const [statusFilter, setStatusFilter] = useStateAR("");
  const [stageFilter, setStageFilter] = useStateAR("");
  const [quickFilter, setQuickFilter] = useStateAR("all");
  const [allData, setAllData] = useStateAR(null);
  const [loading, setLoading] = useStateAR(false);
  const [error, setError] = useStateAR(null);

  const load = useCBAR(async () => {
    if (!ready) { setAllData(null); return; }
    setLoading(true);
    setError(null);
    try {
      const all = await window.client.listRuns(ctx, { page: 1, pageSize: 10 });
      setAllData(all);
    } catch (e) {
      setError({ status: e.status, message: e.message });
    } finally {
      setLoading(false);
    }
  }, [ctx.tenant, ctx.project, ready]);

  useEffectAR(() => { load(); }, [load]);
  // Reset page when filters change
  useEffectAR(() => { setPage(1); }, [q, statusFilter, stageFilter, quickFilter]);

  // Auto-refresh while there's a running/assessing item
  useEffectAR(() => {
    if (!allData) return;
    const hasLive = allData.items.some(x => ["RUNNING","ASSESSING","PLAN_READY","WAITING_FOR_CONFIRMATION"].includes(x.status));
    if (!hasLive) return;
    const t = setInterval(() => load(), 4000);
    return () => clearInterval(t);
  }, [allData, load]);

  // Quick-filter predicates (multi-status mapping)
  const quickPredicate = (key) => {
    if (!key || key === "all") return () => true;
    const matches = {
      running:   x => ["RUNNING","ASSESSING"].includes(x.status),
      awaiting:  x => ["PLAN_READY","WAITING_FOR_CONFIRMATION"].includes(x.status),
      warnings:  x => x.status === "SUCCEEDED_WITH_WARNINGS" || (x.warningCount && x.warningCount > 0),
      failed:    x => x.status === "FAILED",
      review:    x => x.status === "REQUIRES_HUMAN_REVIEW",
      completed: x => ["SUCCEEDED","SUCCEEDED_WITH_WARNINGS"].includes(x.status),
    };
    return matches[key] || (() => true);
  };

  // Filtered + paginated view, derived client-side
  const filtered = useMemoAR(() => {
    const items = allData?.items || [];
    const qLower = q.trim().toLowerCase();
    const pred = quickPredicate(quickFilter);
    const out = items.filter(x => {
      if (!pred(x)) return false;
      if (statusFilter && x.status !== statusFilter) return false;
      if (stageFilter && x.currentStage !== stageFilter) return false;
      if (qLower) {
        const hay = `${x.documentName} ${x.runId}`.toLowerCase();
        if (!hay.includes(qLower)) return false;
      }
      return true;
    });
    return out;
  }, [allData, q, statusFilter, stageFilter, quickFilter]);

  const total = filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const data = useMemoAR(() => {
    const start = (page - 1) * pageSize;
    return { items: filtered.slice(start, start + pageSize), total };
  }, [filtered, page, pageSize, total]);

  // Summary stats across all runs (unfiltered)
  const stats = useMemoAR(() => {
    const items = allData?.items || [];
    return {
      total: items.length,
      running: items.filter(x => ["RUNNING","ASSESSING"].includes(x.status)).length,
      awaiting: items.filter(x => ["PLAN_READY","WAITING_FOR_CONFIRMATION"].includes(x.status)).length,
      warnings: items.filter(x => x.status === "SUCCEEDED_WITH_WARNINGS" || (x.warningCount && x.warningCount > 0)).length,
      failed: items.filter(x => x.status === "FAILED").length,
      review: items.filter(x => x.status === "REQUIRES_HUMAN_REVIEW").length,
      completed: items.filter(x => ["SUCCEEDED","SUCCEEDED_WITH_WARNINGS"].includes(x.status)).length,
    };
  }, [allData]);

  return (
    <div>
      <div className="page-header">
        <div>
          <span className="page-header__eyebrow">Operations · J1 Pipeline</span>
          <h1>Ingestion runs</h1>
          <p>{ready ? `${total} run${total === 1 ? "" : "s"} in ${ctx.tenant} / ${ctx.project}` : "Set Tenant and Project to view ingestion runs."}</p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={load} disabled={!ready || loading} title="Refresh">
            <Icon.RefreshCw className={"icon-sm" + (loading ? " spin" : "")} /> Refresh
          </button>
          <button className="btn btn--primary" onClick={onNewRun} disabled={!ready}>
            <Icon.Upload className="icon-sm" /> New ingestion run
          </button>
        </div>
      </div>

      {ready && allData && (
        <div className="summary-pills">
          <button className="summary-pill summary-pill--running" onClick={() => setQuickFilter(quickFilter === "running" ? "all" : "running")}>
            <span className="summary-pill__label">Running</span>
            <span className="summary-pill__value">{stats.running}</span>
            <span className="summary-pill__sub">in flight right now</span>
          </button>
          <button className="summary-pill summary-pill--awaiting" onClick={() => setQuickFilter(quickFilter === "awaiting" ? "all" : "awaiting")}>
            <span className="summary-pill__label">Awaiting</span>
            <span className="summary-pill__value">{stats.awaiting}</span>
            <span className="summary-pill__sub">confirmation or review</span>
          </button>
          <button className="summary-pill summary-pill--warnings" onClick={() => setQuickFilter(quickFilter === "warnings" ? "all" : "warnings")}>
            <span className="summary-pill__label">Warnings</span>
            <span className="summary-pill__value">{stats.warnings}</span>
            <span className="summary-pill__sub">runs with warnings</span>
          </button>
          <button className="summary-pill summary-pill--failed" onClick={() => setQuickFilter(quickFilter === "failed" ? "all" : "failed")}>
            <span className="summary-pill__label">Failed</span>
            <span className="summary-pill__value">{stats.failed}</span>
            <span className="summary-pill__sub">need attention</span>
          </button>
        </div>
      )}

      {!ready && (
        <UI.Banner kind="warn" title="Tenant and Project are required">
          Set Tenant and Project in the context bar above to view ingestion runs.
        </UI.Banner>
      )}

      {ready && error && error.status === 400 && (
        <UI.Banner kind="warn" title="Tenant and Project are required">{error.message}</UI.Banner>
      )}
      {ready && error && (error.status === 401 || error.status === 403) && (
        <UI.Banner kind="err" title="Unauthorized" action={<button className="btn btn--sm">Authorize</button>}>
          {error.message || "Authentication required."}
        </UI.Banner>
      )}
      {ready && error && error.status >= 500 && (
        <UI.Banner kind="err" title="Server error" action={<button className="btn btn--sm" onClick={load}>Retry</button>}>
          {error.message}
        </UI.Banner>
      )}

      {ready && (
        <div className="quick-filters" role="tablist" aria-label="Quick filter by status">
          {[
            { key: "all",       label: "All",          count: stats.total,     mod: "" },
            { key: "running",   label: "Running",      count: stats.running,   mod: "quick-chip--running" },
            { key: "awaiting",  label: "Awaiting",     count: stats.awaiting,  mod: "quick-chip--awaiting" },
            { key: "warnings",  label: "Warnings",     count: stats.warnings,  mod: "quick-chip--warnings" },
            { key: "failed",    label: "Failed",       count: stats.failed,    mod: "quick-chip--failed" },
            { key: "review",    label: "Human review", count: stats.review,    mod: "quick-chip--review" },
            { key: "completed", label: "Completed",    count: stats.completed, mod: "quick-chip--completed" },
          ].map(c => (
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
              style={{ border: "none", height: 32, paddingLeft: 0, background: "transparent", flex: 1 }}
            />
          </div>
          <select className="input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ height: 36 }}>
            <option value="">All statuses</option>
            {LIST_STATUSES.map(s => <option key={s} value={s}>{StatusDisplay[s]?.label || s}</option>)}
          </select>
          <select className="input" value={stageFilter} onChange={(e) => setStageFilter(e.target.value)} style={{ height: 36 }}>
            <option value="">All stages</option>
            {LIST_STAGES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          {(q || statusFilter || stageFilter || quickFilter !== "all") && (
            <button className="btn btn--ghost btn--sm" onClick={() => { setQ(""); setStatusFilter(""); setStageFilter(""); setQuickFilter("all"); }}>
              Clear filters
            </button>
          )}
        </div>
      )}

      {ready && !error && (
        <div className="run-list">
          {loading && !data && (
            <div style={{ display: "grid", gap: 8 }}>
              {[0,1,2].map(i => <div key={i} style={{ height: 92, background: "var(--bg-sunken)", borderRadius: 12 }} />)}
            </div>
          )}

          {data && data.items.length === 0 && (
            <div className="empty">
              {(q || statusFilter || stageFilter) ? (
                <>
                  <Icon.Eye className="icon-lg" />
                  <h3>No runs match the current filters.</h3>
                  <p>Try clearing the search or filters.</p>
                  <button className="btn btn--sm" onClick={() => { setQ(""); setStatusFilter(""); setStageFilter(""); }}>Clear filters</button>
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

          {data && data.items.map(item => (
            <RunRow key={item.runId} item={item} onClick={() => onOpenRun(item.runId)} />
          ))}

          {data && totalPages > 1 && (
            <div className="pagination">
              <span className="pagination__info">
                Showing {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} of {total}
              </span>
              <div style={{ display: "flex", gap: 4 }}>
                <button className="btn btn--sm" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1}>
                  <Icon.ChevronLeft className="icon-sm" /> Prev
                </button>
                <span className="pagination__pages">Page {page} of {totalPages}</span>
                <button className="btn btn--sm" onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages}>
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

function RunRow({ item, onClick }) {
  const isRunning = ["RUNNING","ASSESSING"].includes(item.status);
  const isFailed = item.status === "FAILED";

  const accent =
    item.status === "FAILED" ? "run-row--failed" :
    item.status === "SUCCEEDED_WITH_WARNINGS" ? "run-row--warnings" :
    item.status === "SUCCEEDED" ? "run-row--succeeded" :
    item.status === "REQUIRES_HUMAN_REVIEW" ? "run-row--review" :
    item.status === "WAITING_FOR_CONFIRMATION" || item.status === "PLAN_READY" ? "run-row--awaiting" :
    isRunning ? "run-row--running" :
    item.status === "CANCELLED" ? "run-row--cancelled" : "";

  return (
    <div className={`run-row ${accent}`} onClick={onClick} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === "Enter") onClick(); }}>
      <div className="run-row__left">
        <div className="run-row__icon">
          <Icon.File className="icon" />
        </div>
        <div style={{ minWidth: 0 }}>
          <div className="run-row__head">
            <span className="run-row__name">{item.documentName}</span>
            <UI.StatusBadge status={item.status} />
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
                <span>{item.currentStage}{item.currentStep ? ` / ${item.currentStep}` : ""}</span>
              </>
            )}
          </div>
          {isRunning && (
            <div className="run-row__progress">
              <div className="run-row__progress-bar">
                <div className="run-row__progress-fill" style={{ width: `${item.progressPercent || 0}%` }} />
              </div>
              <span className="mono" style={{ fontSize: 11, color: "var(--text-muted)", minWidth: 36, textAlign: "right" }}>
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
          <span>{relativeTime(item.completedAt || item.updatedAt)}</span>
        </div>
        <Icon.ChevronRight className="icon" />
      </div>
    </div>
  );
}

window.AllRunsPage = AllRunsPage;
