/**
 * Document-centric type definitions.
 *
 * Mirrors the wire shape returned by the backend's Phase 6 read
 * endpoints (GET /documents, GET /documents/{id}/detail,
 * GET /documents/{id}/runs) — see `j1.documents.projector` for the
 * server-side source of truth. Phase 4's `POST /documents/{id}/
 * reindex` returns the lighter `DocumentReindexResponse` shape
 * defined at the bottom.
 *
 * The action-matrix literal `DocumentAction` matches the spec's
 * section-8 contract: the server computes the allowed actions
 * server-side and the FE renders whatever's in the array — no
 * client-side rule duplication.
 */

/**
 * Document knowledge state. Drives the action matrix + the
 * retrieval filter:
 *
 *  * `attached` — usable as knowledge (default).
 *  * `detached` — preserved on disk, excluded from retrieval/
 *    search/validation/answer generation. Re-attachable.
 *  * `removed`  — knowledge has been disowned. Hidden from the
 *    normal list. Re-upload required to bring it back.
 */
export type KnowledgeState = "attached" | "detached" | "removed";

/**
 * Question/run-type tag the FE uses to color-code rows in the
 * run-history panel. Matches `j1.runs.models.RunType`.
 */
export type RunType =
  | "initial"
  | "reindex"
  | "resume"
  | "retry"
  | "validation";

/**
 * Server-computed action permission. The FE iterates this array
 * to decide which buttons to render — never compares against the
 * `knowledgeState` directly. Adding a new action server-side means
 * the FE picks it up automatically as long as it's rendered.
 */
export type DocumentAction =
  | "view"
  | "reindex"
  | "detach"
  | "attach"
  | "remove"
  | "resume";

/** Roll-up of the document's *current usable* result (the active run). */
export interface DocumentResultSummary {
  status: string;            // "succeeded" | "failed" | "running" | "none" | ...
  compileStatus: string | null;
  enrichmentStatus: string | null;
  validationStatus: string | null;
  failureCode: string | null;
}

/** Compact per-run row used in the document's run-history panel. */
export interface DocumentRunSummary {
  runId: string;
  runType: RunType;
  status: string;             // RunStatus enum value
  startedAt: string | null;   // ISO 8601
  completedAt: string | null;
  failureCode: string | null;
  isActive: boolean;
}

/** List-view projection — one per document in `GET /documents`. */
export interface DocumentListItem {
  documentId: string;
  displayName: string;
  knowledgeState: KnowledgeState;
  activeRunId: string | null;
  latestVersionId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  removedAt: string | null;
  currentResultSummary: DocumentResultSummary;
  availableActions: DocumentAction[];
  runHistorySummary: DocumentRunSummary[];
}

/**
 * Detail-view projection — what `GET /documents/{id}/detail`
 * returns. Same shape as the list item but with the full
 * run history (uncapped) instead of just a 3-row tail.
 */
export interface DocumentDetail {
  documentId: string;
  displayName: string;
  knowledgeState: KnowledgeState;
  activeRunId: string | null;
  latestVersionId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  removedAt: string | null;
  currentResultSummary: DocumentResultSummary;
  availableActions: DocumentAction[];
  runHistory: DocumentRunSummary[];
}

/** Minimal payload returned by attach / detach / remove. */
export interface DocumentLifecycleResponse {
  documentId: string;
  knowledgeState: KnowledgeState;
  activeRunId: string | null;
  latestVersionId: string | null;
  removedAt: string | null;
  updatedAt: string | null;
}

/** Response from `POST /documents/{id}/reindex`. */
export interface DocumentReindexResponse {
  documentId: string;
  reindexRunId: string;
  parentRunId: string | null;
  workflowId: string;
  runType: RunType;
}
