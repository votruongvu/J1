/**
 * Top app bar — tenant / project inputs, mode toggle, auth button,
 * theme toggle. Owns no state; the parent (`App.tsx`) drives every
 * value via props.
 */

import type { AuthConfig, Mode, ProjectContext, Theme } from "@/types/ui";
import { Icon } from "./icons";

interface ContextBarProps {
  ctx: ProjectContext;
  setCtx: (next: ProjectContext) => void;
  auth: AuthConfig;
  onAuthClick: () => void;
  theme: Theme;
  onThemeToggle: () => void;
  mode: Mode;
  onModeToggle: () => void;
}

export function ContextBar({
  ctx,
  setCtx,
  auth,
  onAuthClick,
  theme,
  onThemeToggle,
  mode,
  onModeToggle,
}: ContextBarProps) {
  const ok = !!ctx.tenant && !!ctx.project;
  const authed = !!auth.value;

  return (
    <div className="context-bar">
      <div className="context-bar__inner">
        <div className="brand">
          <div className="brand__mark">J1</div>
          <span className="brand__name">Execution Console</span>
          <span className="brand__tag">Ingestion</span>
        </div>

        <div className="ctx-fields">
          <div className="ctx-field">
            <label>Tenant</label>
            <input
              value={ctx.tenant}
              onChange={(e) => setCtx({ ...ctx, tenant: e.target.value })}
              placeholder="tenant-id"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <div className="ctx-field">
            <label>Project</label>
            <input
              value={ctx.project}
              onChange={(e) => setCtx({ ...ctx, project: e.target.value })}
              placeholder="project-id"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <div className={`ctx-status ${ok ? "ctx-status--ok" : "ctx-status--warn"}`}>
            <span className="dot" />
            {ok ? "Context set" : "Context required"}
          </div>
        </div>

        <button
          className="btn btn--sm"
          onClick={onModeToggle}
          aria-label="Toggle data source"
          title={mode === "live" ? "Switch to mock data" : "Switch to live API"}
          style={{
            background:
              mode === "live" ? "var(--success-soft, #d1fae5)" : "var(--warning-soft, #fef3c7)",
            color:
              mode === "live" ? "var(--success-fg, #065f46)" : "var(--warning-fg, #92400e)",
          }}
        >
          {mode === "live" ? (
            <Icon.Cpu className="icon-sm" />
          ) : (
            <Icon.Spark className="icon-sm" />
          )}
          {mode === "live" ? "Live API" : "Mock mode"}
        </button>

        <button className="btn btn--sm" onClick={onAuthClick}>
          {authed ? <Icon.Lock className="icon-sm" /> : <Icon.Unlock className="icon-sm" />}
          {authed ? `${auth.kind === "bearer" ? "Bearer" : "API key"} set` : "Authorize"}
        </button>

        <button
          className="theme-toggle"
          onClick={onThemeToggle}
          aria-label="Toggle theme"
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
        >
          {theme === "dark" ? (
            <Icon.Sun className="icon-sm" />
          ) : (
            <Icon.Moon className="icon-sm" />
          )}
        </button>
      </div>
    </div>
  );
}
