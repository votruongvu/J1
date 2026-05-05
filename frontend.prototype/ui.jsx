// Reusable UI primitives.

const { useState, useEffect, useRef, useMemo, useCallback } = React;

function StatusBadge({ status }) {
  const meta = StatusDisplay[status] || { label: status, tone: "neutral", pulse: false };
  const cls = `badge badge--${meta.tone}` + (meta.pulse ? " badge--running" : "");
  return (
    <span className={cls}>
      <span className="dot" /> {meta.label}
    </span>
  );
}

function DecisionBadge({ decision }) {
  const meta = DecisionDisplay[decision] || { label: decision, className: "decision--skip" };
  return <span className={`decision ${meta.className}`}>{meta.label}</span>;
}

function RiskBadge({ level }) {
  const tone = level === "HIGH" ? "warning" : level === "MEDIUM" ? "info" : "neutral";
  return <span className={`badge badge--${tone}`}>Risk · {level?.toLowerCase()}</span>;
}

function CostBadge({ tier }) {
  const map = { S: "Cost · S", M: "Cost · M", L: "Cost · L" };
  return <span className="badge badge--outline">{map[tier] || `Cost · ${tier}`}</span>;
}

function EngineBadge({ engine, provider }) {
  if (!engine && !provider) return null;
  const text = provider ? `${engine} · ${provider}` : engine;
  return <span className="badge badge--outline mono">{text}</span>;
}

function ProgressBar({ value, current, total }) {
  return (
    <div className="tl-item__progress">
      <div className="tl-item__progress-bar">
        <div className="tl-item__progress-fill" style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      <div className="tl-item__progress-text">
        {current != null && total != null ? `${current}/${total}` : `${Math.round(value * 100)}%`}
      </div>
    </div>
  );
}

function Modal({ open, onClose, title, children, footer }) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal__head">
          <h3 className="modal__title">{title}</h3>
          <button className="btn btn--ghost btn--sm" onClick={onClose} aria-label="Close">
            <Icon.X className="icon-sm" />
          </button>
        </div>
        <div className="modal__body">{children}</div>
        {footer && <div className="modal__foot">{footer}</div>}
      </div>
    </div>
  );
}

function ToastHost({ toasts, onDismiss }) {
  return (
    <div className="toast-host">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast--${t.kind || ""}`} onClick={() => onDismiss(t.id)}>
          <strong style={{ display: "block", marginBottom: 2 }}>{t.title}</strong>
          {t.body && <span style={{ color: "var(--text-muted)" }}>{t.body}</span>}
        </div>
      ))}
    </div>
  );
}

function jsonHighlight(obj) {
  const json = JSON.stringify(obj, null, 2);
  return json
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/("(?:\\.|[^"\\])*")(\s*:)/g, '<span class="k">$1</span>$2')
    .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="s">$1</span>')
    .replace(/:\s*(true|false)/g, ': <span class="b">$1</span>')
    .replace(/:\s*(-?\d+(?:\.\d+)?)/g, ': <span class="n">$1</span>');
}

function JsonView({ value }) {
  return <pre className="json" dangerouslySetInnerHTML={{ __html: jsonHighlight(value || {}) }} />;
}

function Banner({ kind = "info", title, children, action }) {
  const IconEl = kind === "err" ? Icon.XCircle : kind === "warn" ? Icon.Alert : Icon.Info;
  return (
    <div className={`banner banner--${kind}`}>
      <IconEl className="icon" />
      <div style={{ flex: 1 }}>
        <p className="banner__title">{title}</p>
        {children && <p className="banner__body">{children}</p>}
      </div>
      {action}
    </div>
  );
}

window.UI = { StatusBadge, DecisionBadge, RiskBadge, CostBadge, EngineBadge, ProgressBar, Modal, ToastHost, JsonView, Banner };
