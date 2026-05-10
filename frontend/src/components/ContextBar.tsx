/**
 * Top app bar — tenant / project inputs, auth button, theme toggle.
 * Owns no state; the parent (`App.tsx`) drives every value via props.
 */

import type { AuthConfig, ProjectContext, Theme } from "@/types/ui";
import { Icon } from "./icons";

interface ContextBarProps {
  ctx: ProjectContext;
  setCtx: (next: ProjectContext) => void;
  auth: AuthConfig;
  onAuthClick: () => void;
  theme: Theme;
  onThemeToggle: () => void;
}

export function ContextBar({
  ctx,
  setCtx,
  auth,
  onAuthClick,
  theme,
  onThemeToggle,
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
