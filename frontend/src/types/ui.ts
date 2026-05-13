/**
 * UI-only type definitions: app-level state, theme, auth.
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

/** Light / dark theme, persisted to localStorage. */
export type Theme = "light" | "dark";

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

/** Routing state. Four top-level views — documents (the
 * document-centric surface), document detail, upload, and the
 * shared run detail page.
 *
 * The `origin` field on the `run` route records where the operator
 * arrived from so the back link can return them to the same page
 * (documents list vs. a specific document's detail) rather than
 * always dropping them on the documents list.
 */
export type Route =
  | { name: "upload" }
  | { name: "run"; runId: string; origin?: RunOrigin }
  | { name: "documents" }                              // documents list
  | { name: "document"; documentId: string };          // document detail

export type RunOrigin =
  | { name: "documents" }
  | { name: "document"; documentId: string };
