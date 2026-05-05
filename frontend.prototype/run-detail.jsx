// Run-detail screens: header, plan, timeline, final result.

const { useState: useStateRD, useEffect: useEffectRD, useRef: useRefRD, useMemo: useMemoRD } = React;

// ── Run header (compact + expanded) ────────────────────────────────
function RunHeader({ run, plan, ctx, onBack, onOpenDrawer }) {
  if (!run) return null;
  const summary = plan?.summary;
  const startedMs = run.started_at ? new Date(run.started_at).getTime() : null;
  const endMs = run.completed_at ? new Date(run.completed_at).getTime() : Date.now();
  const durationSec = startedMs ? Math.max(0, Math.round((endMs - startedMs) / 1000)) : null;
  const fmtDur = (s) => s == null ? "—" : s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s/60)}m ${s%60}s` : `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;

  return (
    <div className="run-hero">
      <div className="run-hero__top">
        <div>
          <div className="run-hero__crumb">
            <a href="#" onClick={(e) => { e.preventDefault(); onBack(); }}>
              <Icon.ChevronLeft className="icon-sm" /> All runs
            </a>
            <span>·</span>
            <span className="mono">{ctx.tenant} / {ctx.project}</span>
          </div>
          <h2>
            <span className="run-hero__doc-icon"><Icon.File className="icon" /></span>
            <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{run.document_name}</span>
            <UI.StatusBadge status={run.status} />
          </h2>
          <div className="run-hero__id">{run.runId}</div>
        </div>
        <div className="run-hero__actions">
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
          <label>Total steps</label>
          <div className="v">{summary?.total ?? "—"}</div>
        </div>
        <div className="run-stats__item">
          <label>Run · Skip · Cond</label>
          <div className="vsmall">
            <span style={{ color: "var(--info-fg)" }}>{summary?.run ?? "—"}</span>
            <span style={{ color: "var(--text-subtle)" }}> · </span>
            <span style={{ color: "var(--text-muted)" }}>{summary?.skip ?? "—"}</span>
            <span style={{ color: "var(--text-subtle)" }}> · </span>
            <span style={{ color: "var(--accent-soft-fg)" }}>{summary?.conditional ?? "—"}</span>
          </div>
        </div>
        <div className="run-stats__item">
          <label>Warnings</label>
          <div className="v" style={{ color: run.warning_count > 0 ? "var(--warning-fg)" : "inherit" }}>
            {run.warning_count || 0}
          </div>
        </div>
        <div className="run-stats__item">
          <label>{run.completed_at ? "Duration" : "Elapsed"}</label>
          <div className="vsmall">{fmtDur(durationSec)}</div>
        </div>
      </div>

      {(run.status === "RUNNING" || run.status === "ASSESSING") && (
        <div className="run-progress">
          <div className="run-progress__step">
            <span className="muted">{run.current_stage || "—"} · </span>
            {run.current_step || "Assessing…"}
          </div>
          <div className="run-progress__bar">
            <div className="run-progress__fill" style={{ width: `${run.progress_pct || 0}%` }} />
          </div>
          <div className="run-progress__pct">{run.progress_pct || 0}%</div>
        </div>
      )}
      {run.warning_count > 0 && run.status !== "FAILED" && (
        <div className="run-warning-banner">
          <Icon.Alert className="icon-sm" /> {run.warning_count} warning{run.warning_count === 1 ? "" : "s"} surfaced during this run.
        </div>
      )}
    </div>
  );
}

function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function formatTimeShort(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ── Plan card (grouped by stage) ───────────────────────────────────
function PlanCard({ plan, run, runtimeStepStatus, onConfirm, confirming }) {
  if (!plan) {
    return (
      <div className="card">
        <div className="card__header">
          <div>
            <h3 className="card__title">Execution plan</h3>
            <p className="card__subtitle">Generating plan…</p>
          </div>
        </div>
        <div className="card__body">
          <div style={{ display: "grid", gap: 10 }}>
            {[0,1,2,3].map(i => (
              <div key={i} style={{ height: 56, borderRadius: 8, background: "var(--bg-sunken)" }} />
            ))}
          </div>
        </div>
      </div>
    );
  }

  const showConfirm = run && (run.status === "PLAN_READY" || run.status === "WAITING_FOR_CONFIRMATION");
  const stages = plan.summary.stages;
  const grouped = stages.map(stage => ({
    stage,
    steps: plan.steps.filter(s => s.stage === stage),
  }));

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Execution plan</h3>
          <p className="card__subtitle">
            {plan.summary.total} steps · {plan.summary.run} run · {plan.summary.skip} skip · {plan.summary.conditional} conditional
          </p>
        </div>
        <span className="badge badge--outline mono">{plan.runId}</span>
      </div>

      <div className="card__body">
        {showConfirm && (
          <div className="confirm-bar">
            <div className="confirm-bar__text">
              <strong>Plan is ready.</strong> Review the steps below and confirm to begin execution.
            </div>
            <button className="btn btn--primary" onClick={onConfirm} disabled={confirming}>
              {confirming ? <><Icon.Loader className="icon-sm" /> Confirming…</> : <><Icon.Check className="icon-sm" /> Confirm & run</>}
            </button>
          </div>
        )}

        <div className="plan-summary">
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.run}</span> Run
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.skip}</span> Skip
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <span className="plan-summary__num">{plan.summary.conditional}</span> Conditional
          </div>
          <div className="plan-summary__divider" />
          <div className="plan-summary__item">
            <Icon.Layers className="icon-sm" /> {stages.length} stages
          </div>
        </div>

        {grouped.map(g => (
          <div className="stage-group" key={g.stage}>
            <div className="stage-group__head">
              <span className={`stage-group__chip stage-group__chip--${g.stage.toLowerCase()}`}>{g.stage}</span>
              <div className="stage-group__line" />
              <span className="stage-group__count">{g.steps.length} STEPS</span>
            </div>
            <div className="steps-grid">
              {g.steps.map(step => (
                <PlanStepCard
                  key={step.id}
                  step={step}
                  runtimeStatus={runtimeStepStatus?.[step.id]}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PlanStepCard({ step, runtimeStatus }) {
  const isHighRiskSkip = step.decision === "SKIP" && step.risk_level === "HIGH";
  const cls = [
    "step-card",
    step.decision === "RUN" ? "step-card--run" : "",
    step.decision === "SKIP" ? "step-card--skip" : "",
    step.decision === "CONDITIONAL" ? "step-card--conditional" : "",
    isHighRiskSkip ? "step-card--high-risk-skip" : "",
    runtimeStatus === "running" ? "step-card--running" : "",
    runtimeStatus === "completed" ? "step-card--completed" : "",
    runtimeStatus === "failed" ? "step-card--failed" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={cls}>
      <div className="step-card__head">
        <div className="step-card__name">{step.name}</div>
        <UI.DecisionBadge decision={step.decision} />
      </div>
      <div className="step-card__reason">{step.reason}</div>
      <div className="step-card__meta">
        <UI.RiskBadge level={step.risk_level} />
        <UI.CostBadge tier={step.estimated_cost_tier} />
        <UI.EngineBadge engine={step.expected_engine} provider={step.expected_provider} />
        {runtimeStatus === "running" && (
          <span className="badge badge--info badge--running"><span className="dot" /> Running</span>
        )}
        {runtimeStatus === "completed" && (
          <span className="badge badge--success"><span className="dot" /> Done</span>
        )}
        {runtimeStatus === "failed" && (
          <span className="badge badge--error"><span className="dot" /> Failed</span>
        )}
      </div>
      {step.warning && (
        <div className="step-card__warn">
          <Icon.Alert className="icon-sm" /> {step.warning}
        </div>
      )}
    </div>
  );
}

// ── Live timeline ──────────────────────────────────────────────────
function LiveTimeline({ events, streamStatus, onSelectEvent }) {
  const scrollRef = useRefRD(null);
  useEffectRD(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h3 className="card__title">Live timeline</h3>
          <p className="card__subtitle">{events.length} event{events.length === 1 ? "" : "s"}</p>
        </div>
        <StreamStatus status={streamStatus} />
      </div>
      <div className="card__body" ref={scrollRef} style={{ maxHeight: 520, overflow: "auto" }}>
        {events.length === 0 ? (
          <div className="tl-empty">No events yet. They'll appear here as the run progresses.</div>
        ) : (
          <div className="timeline">
            {events.map(e => (
              <TimelineEventItem key={e.eventId} event={e} onClick={() => onSelectEvent?.(e)} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StreamStatus({ status }) {
  const map = {
    open:         { className: "stream-status--live", label: "Live" },
    reconnecting: { className: "stream-status--reconnecting", label: "Reconnecting…" },
    closed:       { className: "stream-status--closed", label: "Stream closed" },
    idle:         { className: "stream-status--closed", label: "Idle" },
  };
  const m = map[status] || map.idle;
  return (
    <div className={`stream-status ${m.className}`}>
      <span className="dot" /> {m.label}
    </div>
  );
}

function TimelineEventItem({ event, onClick }) {
  const t = event.event;
  const sev = event.data?.severity || "INFO";
  const isProgress = t === "step.progress";
  const isWarning = t === "step.warning" || sev === "WARNING";
  const isError = t === "step.failed" || t === "run.failed" || sev === "ERROR";
  const isReview = t === "human_review.required";
  const isRunning = t === "step.started";
  const isSuccess = t === "step.completed" || t === "run.completed" || t === "assessment.completed";

  let kind = "info";
  if (isError) kind = "error";
  else if (isWarning) kind = "warning";
  else if (isReview) kind = "review";
  else if (isRunning) kind = "running";
  else if (isSuccess) kind = "success";

  return (
    <div className={`tl-item tl-item--${kind}`} onClick={onClick} style={{ cursor: "pointer" }}>
      <div className="tl-item__dot">
        {kind === "success" && <Icon.Check className="icon-sm" />}
        {kind === "warning" && <Icon.Alert className="icon-sm" />}
        {kind === "error" && <Icon.X className="icon-sm" />}
        {kind === "review" && <Icon.UserCheck className="icon-sm" />}
      </div>
      <div className="tl-item__head">
        <span className="tl-item__type">{EventTypeDisplay[t] || t}</span>
        {event.data?.stage && <span className="badge badge--outline mono">{event.data.stage}</span>}
        {event.data?.step && <span className="badge badge--outline mono">{event.data.step}</span>}
        <span className="tl-item__time">{formatTimeShort(event.ts)}</span>
      </div>
      <div className="tl-item__msg">{event.data?.message}</div>

      {isProgress && (
        <div className="tl-progress-card">
          <div className="tl-progress-card__head">
            <span>Progress</span>
            <span className="ct">
              {event.data.current != null && event.data.total != null
                ? `${event.data.current} / ${event.data.total}`
                : `${Math.round((event.data.progress || 0) * 100)}%`}
            </span>
          </div>
          <div className="tl-progress-card__bar">
            <div className="tl-progress-card__fill" style={{ width: `${Math.round((event.data.progress || 0) * 100)}%` }} />
          </div>
          {event.data.message && <div className="tl-progress-card__msg">{event.data.message}</div>}
          {(event.data.engine || event.data.provider) && (
            <div className="tl-progress-card__badges">
              <UI.EngineBadge engine={event.data.engine} provider={event.data.provider} />
            </div>
          )}
        </div>
      )}

      {(event.data?.engine || event.data?.provider) && !isProgress && (
        <div className="tl-item__meta">
          <UI.EngineBadge engine={event.data.engine} provider={event.data.provider} />
        </div>
      )}

      {t === "step.warning" && event.data?.warning && (
        <div className="tl-item__warn">
          <Icon.Alert className="icon-sm" /> {event.data.warning}
        </div>
      )}
      {(t === "step.failed" || t === "run.failed") && event.data?.failure_message && (
        <div className="tl-item__error">
          <strong className="mono">{event.data.failure_code}</strong> · {event.data.failure_message}
        </div>
      )}
      {isReview && event.data?.reason && (
        <div className="tl-item__warn" style={{ background: "var(--accent-soft)", color: "var(--accent-soft-fg)" }}>
          <Icon.UserCheck className="icon-sm" /> {event.data.reason}
        </div>
      )}
    </div>
  );
}

// ── Final result panel ────────────────────────────────────────────
function FinalResult({ run }) {
  if (!run) return null;
  const status = run.status;
  const final = run.final;

  if (status === "COMPLETED") {
    return (
      <div className="final final--success">
        <div className="final__head">
          <div className="final__icon"><Icon.CheckCircle className="icon-lg" /></div>
          <div>
            <h4 className="final__title">Run completed</h4>
            <p className="final__sub">All steps executed successfully.</p>
          </div>
        </div>
      </div>
    );
  }
  if (status === "COMPLETED_WITH_WARNINGS") {
    return (
      <div className="final final--warning">
        <div className="final__head">
          <div className="final__icon"><Icon.Alert className="icon-lg" /></div>
          <div>
            <h4 className="final__title">Completed with {final?.warning_count ?? run.warning_count} warning{(final?.warning_count ?? run.warning_count) === 1 ? "" : "s"}</h4>
            <p className="final__sub">{final?.warning_summary || "Review warnings below."}</p>
          </div>
        </div>
        <div className="final__body">
          <div className="final__row"><label>Warnings</label><span className="v">{final?.warning_count ?? run.warning_count}</span></div>
          <div className="final__row"><label>Status</label><span className="v">COMPLETED_WITH_WARNINGS</span></div>
        </div>
      </div>
    );
  }
  if (status === "FAILED") {
    return (
      <div className="final final--error">
        <div className="final__head">
          <div className="final__icon"><Icon.XCircle className="icon-lg" /></div>
          <div>
            <h4 className="final__title">Run failed</h4>
            <p className="final__sub">{final?.failure_message || "An unexpected failure occurred."}</p>
          </div>
        </div>
        <div className="final__body">
          {final?.failure_code && <div className="final__row"><label>Failure code</label><span className="v">{final.failure_code}</span></div>}
          {final?.failed_step && <div className="final__row"><label>Failed step</label><span className="v">{final.failed_step}</span></div>}
        </div>
      </div>
    );
  }
  if (status === "AWAITING_HUMAN_REVIEW") {
    return (
      <div className="final final--review">
        <div className="final__head">
          <div className="final__icon"><Icon.UserCheck className="icon-lg" /></div>
          <div>
            <h4 className="final__title">Human review required</h4>
            <p className="final__sub">{final?.reason || "Manual review needed before continuing."}</p>
          </div>
        </div>
        <div className="final__body">
          {final?.stage && <div className="final__row"><label>Stage</label><span className="v">{final.stage}</span></div>}
          {final?.step && <div className="final__row"><label>Step</label><span className="v">{final.step}</span></div>}
        </div>
      </div>
    );
  }
  return null;
}

// ── Drawer (technical details) ────────────────────────────────────
function TechDrawer({ open, onClose, run, plan, events, selectedEvent }) {
  const [tab, setTab] = useStateRD("run");
  return (
    <div className={`drawer ${open ? "is-open" : ""}`} role="dialog" aria-hidden={!open}>
      <div className="drawer__head">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Icon.Code className="icon" />
          <strong>Technical details</strong>
        </div>
        <button className="btn btn--ghost btn--sm" onClick={onClose}><Icon.X className="icon-sm" /></button>
      </div>
      <div className="drawer__tabs">
        <button className={`drawer__tab ${tab === "run" ? "is-active" : ""}`} onClick={() => setTab("run")}>Run</button>
        <button className={`drawer__tab ${tab === "plan" ? "is-active" : ""}`} onClick={() => setTab("plan")}>Plan</button>
        <button className={`drawer__tab ${tab === "events" ? "is-active" : ""}`} onClick={() => setTab("events")}>Events ({events?.length || 0})</button>
        <button className={`drawer__tab ${tab === "selected" ? "is-active" : ""}`} onClick={() => setTab("selected")}>Selected</button>
      </div>
      <div className="drawer__body">
        {tab === "run" && <UI.JsonView value={run} />}
        {tab === "plan" && <UI.JsonView value={plan} />}
        {tab === "events" && <UI.JsonView value={events} />}
        {tab === "selected" && (selectedEvent ? <UI.JsonView value={selectedEvent} /> : <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Click any event in the timeline to inspect its raw payload.</div>)}
      </div>
    </div>
  );
}

// ── Primary status panel ──────────────────────────────────────────
// Always-visible state hero shown directly under RunHeader. Adapts to
// every lifecycle state — assessing, awaiting confirmation, running,
// success, warnings, failed, human review, cancelled.
function PrimaryStatusPanel({ run, plan, events }) {
  if (!run) return null;
  const status = run.status;
  const final = run.final;

  // Derive output metrics from completed step events (chunks, nodes, edges, tables, sections)
  const outputs = React.useMemo(() => {
    if (!events) return null;
    const out = {};
    for (const e of events) {
      if (e.event !== "step.completed") continue;
      const msg = e.data?.message || "";
      let m;
      if ((m = msg.match(/(\d[\d,]*)\s+chunks?\s+indexed/i))) out.chunks = m[1];
      if ((m = msg.match(/(\d[\d,]*)\s+nodes?,\s+(\d[\d,]*)\s+edges?/i))) { out.nodes = m[1]; out.edges = m[2]; }
      if ((m = msg.match(/(\d[\d,]*)\s+sections?/i))) out.sections = m[1];
      if ((m = msg.match(/(\d[\d,]*)\s+tables?/i))) out.tables = m[1];
      if ((m = msg.match(/(\d[\d,]*)\s+entities/i))) out.entities = m[1];
    }
    return out;
  }, [events]);

  // Awaiting confirmation — plan ready, gate is open
  if (status === "PLAN_READY" || status === "WAITING_FOR_CONFIRMATION") {
    const totals = plan?.summary?.totals || { run: 0, skip: 0, conditional: 0 };
    return (
      <div className="psp psp--awaiting">
        <div className="psp__icon"><Icon.Alert className="icon" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Awaiting confirmation</div>
          <h2 className="psp__title">Review the plan before execution starts</h2>
          <p className="psp__lede">The assessor identified <strong>{totals.run + totals.skip + totals.conditional}</strong> candidate steps. <strong>{totals.run}</strong> will run, <strong>{totals.skip}</strong> will be skipped, <strong>{totals.conditional}</strong> are conditional. Confirm the plan on the right to begin.</p>
        </div>
      </div>
    );
  }

  // Assessing — generating plan
  if (status === "ASSESSING" || status === "CREATED") {
    return (
      <div className="psp psp--assessing">
        <div className="psp__icon"><Icon.RefreshCw className="icon spin" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Assessing</div>
          <h2 className="psp__title">Building execution plan…</h2>
          <p className="psp__lede">Analyzing document characteristics, language, structure, and content to determine which pipeline steps apply.</p>
        </div>
      </div>
    );
  }

  // Running
  if (status === "RUNNING") {
    const stage = run.current_stage || "—";
    const step = run.current_step || "—";
    const pct = Math.round(run.progress_pct || 0);
    return (
      <div className="psp psp--running">
        <div className="psp__icon"><Icon.RefreshCw className="icon spin" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Running · {pct}%</div>
          <h2 className="psp__title">Executing {stage} · <span className="psp__step">{step}</span></h2>
          <p className="psp__lede">Streaming events live from the pipeline. Watch the timeline on the right for per-step progress.</p>
          <div className="psp__progress"><div className="psp__progress-bar" style={{ width: `${pct}%` }}></div></div>
        </div>
      </div>
    );
  }

  // Failed
  if (status === "FAILED") {
    return (
      <div className="psp psp--failed">
        <div className="psp__icon"><Icon.XCircle className="icon" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Failed</div>
          <h2 className="psp__title">{final?.failure_code || "Run failed"}</h2>
          <p className="psp__lede">{final?.failure_message || "The run terminated with an error."}</p>
          {final?.failed_step && (
            <div className="psp__meta">
              <span className="psp__meta-label">Failed step</span>
              <code className="psp__meta-value">{final.failed_step}</code>
            </div>
          )}
        </div>
      </div>
    );
  }

  // Awaiting human review
  if (status === "AWAITING_HUMAN_REVIEW" || status === "REQUIRES_HUMAN_REVIEW") {
    return (
      <div className="psp psp--review">
        <div className="psp__icon"><Icon.UserCheck className="icon" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Human review required</div>
          <h2 className="psp__title">{final?.reason || "Manual review needed before continuing"}</h2>
          <p className="psp__lede">{final?.detail || "A reviewer must approve or reject this run before it can proceed."}</p>
        </div>
      </div>
    );
  }

  // Cancelled
  if (status === "CANCELLED") {
    return (
      <div className="psp psp--cancelled">
        <div className="psp__icon"><Icon.X className="icon" /></div>
        <div className="psp__body">
          <div className="psp__eyebrow">Cancelled</div>
          <h2 className="psp__title">Run cancelled</h2>
          <p className="psp__lede">This run was cancelled before completion.</p>
        </div>
      </div>
    );
  }

  // Completed (with or without warnings)
  if (status === "COMPLETED" || status === "COMPLETED_WITH_WARNINGS" || status === "SUCCEEDED" || status === "SUCCEEDED_WITH_WARNINGS") {
    const hasWarnings = status === "COMPLETED_WITH_WARNINGS" || status === "SUCCEEDED_WITH_WARNINGS" || (run.warning_count || 0) > 0;
    const metrics = [];
    if (outputs?.chunks) metrics.push({ label: "Chunks indexed", value: outputs.chunks });
    if (outputs?.nodes) metrics.push({ label: "Graph nodes", value: outputs.nodes });
    if (outputs?.edges) metrics.push({ label: "Graph edges", value: outputs.edges });
    if (outputs?.sections) metrics.push({ label: "Sections", value: outputs.sections });
    if (outputs?.tables) metrics.push({ label: "Tables", value: outputs.tables });
    if (outputs?.entities) metrics.push({ label: "Entities", value: outputs.entities });

    return (
      <div className={`psp ${hasWarnings ? "psp--warnings" : "psp--success"}`}>
        <div className="psp__icon">
          {hasWarnings ? <Icon.Alert className="icon" /> : <Icon.CheckCircle className="icon" />}
        </div>
        <div className="psp__body">
          <div className="psp__eyebrow">{hasWarnings ? `Completed with ${run.warning_count || 1} warning${(run.warning_count || 1) === 1 ? "" : "s"}` : "Completed"}</div>
          <h2 className="psp__title">{hasWarnings ? "Indexed with warnings" : "Document indexed and ready to query"}</h2>
          <p className="psp__lede">
            {hasWarnings
              ? (final?.warning_summary || "The pipeline completed but flagged issues that may affect retrieval quality.")
              : "All pipeline stages completed successfully. The document is now searchable across vector, graph, and structured indexes."}
          </p>
          {metrics.length > 0 && (
            <div className="psp__metrics">
              {metrics.map(m => (
                <div key={m.label} className="psp__metric">
                  <span className="psp__metric-value">{m.value}</span>
                  <span className="psp__metric-label">{m.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return null;
}

window.RunHeader = RunHeader;
window.PlanCard = PlanCard;
window.LiveTimeline = LiveTimeline;
window.FinalResult = FinalResult;
window.TechDrawer = TechDrawer;
window.PrimaryStatusPanel = PrimaryStatusPanel;
