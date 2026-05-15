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

/** Routing state. Six top-level views:
 *
 *   `home`      — dashboard landing page (default). Carries the
 *                 Global Search card, system status summary, and
 *                 recent-runs / needs-attention panels.
 *   `search`    — dedicated Global Search page; optionally
 *                 preloaded with a `query` from the home search card.
 *   `documents` — document list (existing surface, no longer the
 *                 default since `home` exists).
 *   `document`  — single-document detail.
 *   `upload`    — upload + ingest dialog.
 *   `run`       — run-detail page.
 *
 * The `origin` field on the `run` route records where the operator
 * arrived from so the back link can return them to the same page
 * (documents list vs. a specific document's detail vs. home) rather
 * than always dropping them on the documents list.
 */
export type Route =
  | { name: "home" }
  | { name: "search"; query?: string }
  | { name: "upload" }
  | { name: "run"; runId: string; origin?: RunOrigin }
  | { name: "documents" }                              // documents list
  | { name: "document"; documentId: string };          // document detail

export type RunOrigin =
  | { name: "home" }
  | { name: "documents" }
  | { name: "document"; documentId: string };
