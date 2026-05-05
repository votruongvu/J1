/**
 * UI-only type definitions: app-level state, theme, auth, mock scenarios.
 */

/** Tenant + project headers required on every J1 REST request. */
export interface ProjectContext {
  tenant: string;
  project: string;
}

/** Frontend auth configuration — Bearer token or API key. */
export type AuthKind = "bearer" | "apiKey";

export interface AuthConfig {
  kind: AuthKind;
  /** Empty string means "not authenticated"; the UI treats this as "no auth header sent". */
  value: string;
}

/** Data-source mode for the active client. */
export type Mode = "mock" | "live";

/** Light / dark theme, persisted to localStorage. */
export type Theme = "light" | "dark";

/** Mock-mode scenario for the prototype's scripted timeline. */
export type MockScenario = "warnings" | "failure" | "review";

/** SSE stream lifecycle. */
export type StreamStatus = "idle" | "open" | "reconnecting" | "closed";

/** Per-step runtime status used to overlay run progress on plan cards. */
export type RuntimeStepStatus = "running" | "completed" | "failed" | "skipped";

/** App-level toast notification. */
export interface Toast {
  id: string;
  kind?: "success" | "error" | "warning" | "info";
  title: string;
  body?: string;
}

/** Routing state. Three top-level views. */
export type Route = { name: "list" } | { name: "upload" } | { name: "run"; runId: string };
