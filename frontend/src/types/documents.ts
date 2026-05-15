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
  | "validation"
  | "refresh_enrich"
  | "run_domain_enrichment";

/**
 * Server-computed action permission. The FE iterates this array
 * to decide which buttons to render — never compares against the
 * `knowledgeState` directly. Adding a new action server-side means
 * the FE picks it up automatically as long as it's rendered.
 *
 * Run-scoped actions (delete-run, refresh-enrichment, run-enrichment)
 * are NOT on this enum — they're driven by capability flags carried
 * on each ``DocumentRunSummary``.
 */
export type DocumentAction =
  | "view"
  | "reindex"
  | "detach"
  | "attach"
  | "remove";

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
  /**
   * The snapshot this run produced (or was allocated to build).
   * ``null`` on legacy runs predating the snapshot model. The
   * Document Detail page uses this to render the "Produced
   * snapshot" column and to identify the active snapshot's
   * producing run (the run whose ``targetSnapshotId`` equals
   * ``DocumentDetail.activeSnapshotId``).
   */
  targetSnapshotId: string | null;
  /**
   * Operator-facing version chip in ``DDMMYYYY-NN`` format (per
   * document, per day). ``null`` for legacy runs created before
   * the dev-mode refactor — the FE renders nothing for those
   * rather than showing an empty placeholder.
   */
  displayVersion: string | null;
  /**
   * Run-level capability flags. Computed server-side; the FE MUST
   * NOT recompute these locally. Drive every Run Detail action.
   */
  isOnlyRun: boolean;
  canDeleteRun: boolean;
  canRefreshEnrichment: boolean;
  canRunEnrichment: boolean;
}

/** List-view projection — one per document in `GET /documents`. */
export interface DocumentListItem {
  documentId: string;
  displayName: string;
  knowledgeState: KnowledgeState;
  /**
   * The canonical visibility key: which DocumentSnapshot is currently
   * "live" for this document. ``null`` when no snapshot has been
   * promoted yet (just uploaded; first ingestion still queued; or
   * removed and the snapshot pointer cleared).
   */
  activeSnapshotId: string | null;
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
 *
 * Snapshot-centric model:
 *
 *   Document         = source file + lifecycle
 *   Snapshot         = queryable knowledge version (activeSnapshotId)
 *   Run              = workflow execution that produced a snapshot
 *
 * The "active run" the UI displays is derived from
 * ``runHistory.find(r => r.targetSnapshotId === activeSnapshotId)``
 * — that's the run that *produced* the active snapshot, NOT the
 * latest succeeded run (the two can diverge, e.g. a later run
 * that succeeded but didn't promote). Falls back to
 * ``isActive`` when ``targetSnapshotId`` is unavailable (legacy
 * runs predating the snapshot model).
 */
export interface DocumentDetail {
  documentId: string;
  displayName: string;
  knowledgeState: KnowledgeState;
  activeSnapshotId: string | null;
  latestVersionId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  removedAt: string | null;
  currentResultSummary: DocumentResultSummary;
  availableActions: DocumentAction[];
  runHistory: DocumentRunSummary[];
}

/**
 * Snapshot lifecycle states (mirror of
 * ``j1.documents.snapshot.SnapshotState``). Renders as a colored
 * pill on the Candidate Knowledge section + the Active Knowledge
 * panel so users can tell whether a snapshot is being built, ready
 * for promotion, already published, or has failed.
 */
export type SnapshotState =
  | "building"
  | "ready"
  | "superseded"
  | "failed";

/** One row of ``GET /documents/{id}/snapshots``. */
export interface DocumentSnapshotSummary {
  snapshotId: string;
  documentId: string;
  createdByRunId: string | null;
  state: SnapshotState | null;
  createdAt: string | null;
  promotedAt: string | null;
  supersededAt: string | null;
  /** True iff this snapshot id matches ``DocumentDetail.activeSnapshotId``. */
  isActive: boolean;
  /** Index kinds attached (vector / graph / evidence / rag). */
  indexKinds: string[];
}

/** Minimal payload returned by attach / detach / remove. */
export interface DocumentLifecycleResponse {
  documentId: string;
  knowledgeState: KnowledgeState;
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

/**
 * Response from ``POST /ingestion-runs/{run_id}/refresh-enrichment``.
 * The endpoint allocates a new candidate run that reuses the active
 * run's compile output and re-runs only enrichment + graph + index.
 * Promotion to ``activeSnapshotId`` is CAS-on-terminal-success.
 */
export interface RunRefreshEnrichmentResponse {
  documentId: string;
  refreshRunId: string;
  parentRunId: string;
  workflowId: string;
  runType: RunType;
  reusedCompileFromRunId: string;
}

/**
 * Response from
 * ``POST /documents/{id}/manual-actions/run-domain-enrichment``.
 *
 * The endpoint allocates a candidate snapshot + a new
 * ``run_type="run_domain_enrichment"`` run. The run REUSES the active
 * snapshot's compile artifacts and writes fresh ``enriched.*``
 * artifacts under the candidate; promotion to ``activeSnapshotId`` is
 * CAS-on-terminal-success — a failed manual action preserves the
 * previous active.
 */
export interface RunDomainEnrichmentResponse {
  documentId: string;
  manualAction: "run_domain_enrichment";
  manualActionRunId: string;
  runType: RunType;
  parentRunId: string;
  sourceRunId: string;
  sourceSnapshotId: string;
  targetSnapshotId: string | null;
  workflowId: string;
  status: "queued";
}
