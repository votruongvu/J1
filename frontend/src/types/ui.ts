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

/** Routing state. Five top-level views — runs (legacy) + documents
 * (the new document-centric surface from the Phase 7 refactor) +
 * upload. The two list views coexist during the migration so
 * operators can switch between them via the main nav tab. */
export type Route =
  | { name: "list" }                                   // legacy: runs list
  | { name: "upload" }
  | { name: "run"; runId: string }
  | { name: "documents" }                              // new: documents list
  | { name: "document"; documentId: string };          // new: document detail
