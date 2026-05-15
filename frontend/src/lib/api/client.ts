/**
 * Single integration surface — the contract every IngestionClient
 * must satisfy. The mock and live clients implement the same
 * interface so component code never branches on data origin.
 */

import type {
  IngestionRun,
  ProgressEvent,
  RunListQuery,
  RunListResult,
} from "@/types/ingestion";
import type {
  AssessmentPlanResponse,
  ExecutionProfileId,
} from "@/types/execution-profile";
import type {
  ContentInventory,
  ImportedTestCaseExecution,
  ImportedTestCaseSet,
  ManualTestQueryRequest,
  ManualTestQueryResponse,
  QueryTracePayload,
  ReviewArtifactContent,
  ReviewArtifactListQuery,
  ReviewArtifactPage,
  ReviewChunkDetail,
  ReviewChunkListQuery,
  ReviewChunkPage,
  ReviewGraphQuery,
  ReviewGraphSnapshot,
  ReviewQualityReport,
  ReviewRunSummary,
  FinalIngestionReportResponse,
  RunCompileResultResponse,
  RunEnrichPlanResponse,
  RunEnrichmentResultResponse,
  RunInitialExecutionPlanResponse,
} from "@/types/review";
import type { ProjectContext } from "@/types/ui";
import type {
  DocumentDetail,
  DocumentLifecycleResponse,
  DocumentListItem,
  RunRefreshEnrichmentResponse,
  DocumentReindexResponse,
  DocumentRunSummary,
  DocumentSnapshotSummary,
} from "@/types/documents";

/** Handlers passed to `openStream`. */
export interface StreamHandlers {
  onOpen?: () => void;
  onEvent?: (event: ProgressEvent) => void;
  onError?: (err: unknown) => void;
  onClose?: () => void;
  /** Resume cursor — sent as `Last-Event-Id` to the live API. */
  lastEventId?: string;
}

export interface StreamHandle {
  close: () => void;
}

/** Stub of an uploaded file — covers both real `File` objects from the
 * dropzone and the `{ name }` placeholder the demo button uses. */
export type UploadFile = File | { name?: string };

/**
 * Response shape for control actions (`pauseRun` / `resumeRun` / `cancelRun`).
 * Mirrors the backend's `IngestionRunControlRecord` so callers can update
 * their local cache without a follow-up `getRun`.
 */
export interface RunControlResult {
  runId: string;
  action: "pause" | "resume" | "cancel";
  /** New run status post-action — typically PAUSED / RUNNING / CANCELLING. */
  status: string;
  /** Current stage if known. */
  stage?: string | null;
  /** Short message suitable for a toast. */
  message?: string | null;
  /** ISO 8601 server timestamp of the update. */
  updatedAt?: string | null;
}

export interface IngestionClient {
  /** GET list of runs (live mode may return an empty page with `_liveUnsupported`). */
  listRuns(ctx: ProjectContext, opts?: RunListQuery): Promise<RunListResult>;

  /** POST a new run. Returns the assigned run id.
   * When `selectedProfile` is supplied (typically from the
   * `AssessmentPlanDialog` picker), it becomes the authoritative
   * gate for downstream stage decisions. Omitting it preserves
   * legacy behaviour — the backend's `DEFAULT_PROFILE` kicks in. */
  upload(
    file: UploadFile,
    ctx: ProjectContext,
    selectedProfile?: ExecutionProfileId,
  ): Promise<{ runId: string }>;

  /** POST `/documents/{id}/assessment-plan`. Synchronous, no
   * workflow dispatch. Returns the recommended profile + the
   * catalogue the picker renders. Caller must have already
   * registered the document (typically via the preceding
   * `registerDocument` call from the upload flow). */
  getDocumentAssessmentPlan(
    documentId: string,
  ): Promise<AssessmentPlanResponse>;

  /** POST `/documents`. Registers a document WITHOUT starting an
   * ingestion run, so the two-step upload flow can call
   * `getDocumentAssessmentPlan` before the user commits to a
   * profile. Returns the document id (idempotent on checksum;
   * duplicate uploads return the existing record). */
  registerDocument(
    file: UploadFile,
    ctx: ProjectContext,
  ): Promise<{ documentId: string }>;

  /** GET a single run snapshot. */
  getRun(runId: string): Promise<IngestionRun>;

  /** POST confirm — transitions a run from PLAN_READY → RUNNING. */
  confirm(runId: string): Promise<{ ok: true }>;

  /** POST pause — transitions a RUNNING run to PAUSED. */
  pauseRun(runId: string): Promise<RunControlResult>;

  /** POST resume — transitions a PAUSED run back to RUNNING. */
  resumeRun(runId: string): Promise<RunControlResult>;

  /** POST cancel — flips run record to CANCELLING; workflow lands at CANCELLED at terminal. */
  cancelRun(runId: string): Promise<RunControlResult>;

  /** GET historical events. */
  getEvents(runId: string): Promise<ProgressEvent[]>;

  /** Open the SSE stream and call back on each event. */
  openStream(runId: string, handlers: StreamHandlers): StreamHandle;

  // ---- Result review ------------------------------------
  // Read-only review surface for completed runs. Each method returns
  // a neutral DTO mirroring `j1.ingestion_review.dtos`.

  /** GET the review summary (Overview tab, drives availableViews). */
  getRunSummary(runId: string): Promise<ReviewRunSummary>;

  /** GET the neutral quality report (Quality tab). */
  getRunQualityReport(
    runId: string,
    opts?: { includeRaw?: boolean },
  ): Promise<ReviewQualityReport>;

  /** GET the run's chunks — paginated, filterable. */
  listRunChunks(
    runId: string,
    opts?: ReviewChunkListQuery,
  ): Promise<ReviewChunkPage>;

  /** GET one chunk in detail (full body + lineage). */
  getRunChunk(runId: string, chunkId: string): Promise<ReviewChunkDetail>;

  /** GET the run's artifacts — paginated, kind-filterable. */
  listRunArtifacts(
    runId: string,
    opts?: ReviewArtifactListQuery,
  ): Promise<ReviewArtifactPage>;

  /**
 * GET the bytes for one artifact (run-scoped).
 *
 * Returns a `Blob` — caller decides whether to render inline (image
 * via `URL.createObjectURL`, JSON via `blob.text` + parse, etc.)
 * or trigger a download. Component code MUST `URL.revokeObjectURL`
 * any object URLs it creates.
 */
  getRunArtifactContent(
    runId: string,
    artifactId: string,
  ): Promise<ReviewArtifactContent>;

  /**
 * GET the neutral graph snapshot for the run. When the run produced
 * no graph data, the snapshot's `unavailable.reason` is populated
 * and entities/relations are empty.
 */
  getRunGraph(
    runId: string,
    opts?: ReviewGraphQuery,
  ): Promise<ReviewGraphSnapshot>;

  /**
 * GET the parsed-content manifest projection. Returns
 * `status="unavailable"` with a reason when no manifest exists
 * (legacy / mid-compile / failed-compile runs). Currently only
 * surfaced via the Compile Strategy panel — the standalone tab
 * was removed in the compile-first refactor.
 */
  getRunContentInventory(runId: string): Promise<ContentInventory>;

  /**
 * GET the post-compile rule-based enrich plan (Enrich Plan card).
 * Returns `status="unavailable"` with a reason when the run hasn't
 * reached post-compile yet or the artifact wasn't persisted.
 */
  getRunEnrichPlan(runId: string): Promise<RunEnrichPlanResponse>;

  /**
 * GET the pre-compile initial execution plan. Returns the
 * resolved domain pack, enrichment policy, candidate modules + cheap
 * signals. Status is `"unavailable"` when the artifact wasn't
 * persisted yet (legacy / pre-compile / persist failure).
 */
  getRunInitialExecutionPlan(
    runId: string,
  ): Promise<RunInitialExecutionPlanResponse>;

  /**
 * GET the typed NormalizedCompileResult. Returns chunks +
 * detected tables/images + retry history + quality signals + raw
 * artifact refs. Status `"unavailable"` when the run didn't reach
 * the post-compile normalize step.
 */
  getRunCompileResult(runId: string): Promise<RunCompileResultResponse>;

  /**
 * GET the typed EnrichmentResult overlay (/8). Returns
 * per-module outcomes + document-metadata + terminology + validation
 * + warnings/errors. Status `"unavailable"` when enrichment hasn't
 * run yet (or was skipped before the artifact wrote).
 */
  getRunEnrichmentResult(runId: string): Promise<RunEnrichmentResultResponse>;

  /**
 * GET the aggregated final ingestion report. This is
 * the single source of truth the run-detail page prefers when
 * present; the FE falls back to per-artifact endpoints when the
 * envelope returns `status="unavailable"` (pre- runs,
 * in-flight runs, persistence failures).
 */
  getRunFinalIngestionReport(
    runId: string,
  ): Promise<FinalIngestionReportResponse>;

  // ---- Validation ---------------------------------------

  /**
 * POST a single manual test query against an ingested run.
 *
 * Synchronous: blocks until the answer engine has run + the
 * deterministic checks have aggregated. Throws `ApiError` on
 * transport errors. The returned body's `validationStatus` is
 * INDEPENDENT of the HTTP outcome — a 200 with
 * `validationStatus="failed"` is the canonical 'job ran but the
 * answer didn't pass' case.
 */
  runManualTestQuery(
    runId: string,
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse>;

  /**
   * Snapshot-centric document query — the backend resolves the
   * document's ``active_snapshot_id`` from ``document_id`` alone.
   * No producing run id required. Use this for Document Detail's
   * "Test Active Knowledge" / Manual Query Trace surfaces.
   *
   * Default scope is ``{ type: "document_active", documentId }``.
   * Callers MAY pass an explicit ``scope`` to validate a specific
   * candidate snapshot tied to this document via
   * ``{ type: "snapshot_explicit", snapshotIds }``.
   */
  runDocumentTestQuery(
    documentId: string,
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse>;

  /**
   * Snapshot-centric project query — every attached document's
   * active snapshot. The backend resolves eligibility internally
   * (detached / removed documents and building / failed /
   * superseded snapshots are excluded). Use this for the Home /
   * Ask Knowledge Base flow.
   *
   * Default scope is ``{ type: "project_active" }``. Callers MAY
   * override with ``{ type: "snapshot_explicit", snapshotIds }``
   * to query a fixed allowlist across the project.
   */
  runProjectQuery(
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse>;

  /**
   * Run a question through SmartQueryOrchestrator and return the
   * full QueryTrace JSON. Operator/developer surface — exposes
   * every stage of the new pipeline (plan, routes, candidates,
   * dropped reasons, gates) so testers can diagnose "why did
   * this query fail" without instrumentation. Returns 503 when
   * no orchestrator is wired at the backend.
   */
  runQueryTrace(
    runId: string,
    question: string,
  ): Promise<QueryTracePayload>;

  // ---- Imported test cases (auxiliary Validation Tab helper) -

  /**
   * Upload a CSV per document. Replaces the prior set + the prior
   * execution snapshot. Required CSV column: ``question``. Optional
   * columns: ``expected_answer``, ``expected_sources``, ``test_type``,
   * ``notes``.
   */
  importTestCases(
    documentId: string,
    file: File,
  ): Promise<ImportedTestCaseSet>;

  /**
   * Get the current imported set for a document, or null when none
   * has been imported.
   */
  getImportedTestCases(
    documentId: string,
  ): Promise<ImportedTestCaseSet | null>;

  /**
   * Remove the imported set + its execution snapshot. Idempotent.
   */
  deleteImportedTestCases(documentId: string): Promise<void>;

  /**
   * Run every imported question against the document's latest
   * succeeded run. Returns the execution snapshot (summary cards +
   * per-question status).
   */
  executeImportedTestCases(
    documentId: string,
  ): Promise<ImportedTestCaseExecution>;

  /**
   * Get the latest imported-test-case execution snapshot, or null
   * when no execution has been run yet.
   */
  getImportedTestCaseExecution(
    documentId: string,
  ): Promise<ImportedTestCaseExecution | null>;

  /**
 * GET cached LLM connectivity status from the API. Drives the
 * top-of-screen "LLM unreachable" banner + disables the upload
 * button when any required role is down — so users don't kick
 * off uploads that are guaranteed to fail mid-pipeline.
 *
 * Backend reads from a process-local cache populated at startup
 * (no upstream LLM call per request), safe to poll on a short
 * interval.
 */
  getLLMHealth(): Promise<LLMHealthStatus>;

  /**
 * POST a synchronous re-probe and return the fresh snapshot. Used
 * by the banner's "Retry now" button so admins can verify the LLM
 * is back immediately after restarting it, instead of waiting up
 * to 30s for the next background poll.
 */
  refreshLLMHealth(): Promise<LLMHealthStatus>;

  // ---- Operational actions ---------------------------------------

  /**
 * DELETE an ingestion run — single-step hard delete. Physically
 * removes the run record, every artifact (file on disk + registry
 * record), every JSONL snapshot of the run, and cascades to any
 * validation sets/runs that referenced it. Audit log stays intact.
 *
 * Throws `ApiError(409)` when the run is still active, when the
 * run is the document's currently active run, or when the run is
 * the document's only run. Throws `ApiError(404)` when the run
 * doesn't exist (also returned on a second delete — the operation
 * is idempotent in the sense that retries are safe).
 */
  /**
   * Pre-flight check for the Clean Up Run action. Lets the FE
   * render the button in the right state (enabled / disabled with
   * reason) without trying the action. The server uses the same
   * helper internally on ``cleanUpRun``, so the UI never drifts
   * from the API on eligibility rules.
   */
  getCleanupEligibility(runId: string): Promise<CleanUpEligibility>;

  /**
   * Snapshot-centric replacement for ``deleteRun``: clean up all
   * data produced by a non-active run. HTTP is always 200 — the
   * ``cleaned`` flag carries the outcome. Refusal (``cleaned=false``)
   * is NOT an error; the FE renders the server's ``message`` and
   * leaves the run intact.
   */
  cleanUpRun(runId: string): Promise<CleanUpRunResult>;

  /**
 * POST a multi-upload batch. Backend registers each file as a
 * child ingestion run, returns the batch_run_id + child run_ids.
 * Max files is enforced server-side (default 5 via
 * `J1_INGESTION_BATCH_MAX_FILES`).
 */
  uploadBatch(
    files: File[],
    ctx: ProjectContext,
  ): Promise<BatchUploadResult>;

  /** GET batch detail — aggregate status + per-file run rows. */
  getBatch(batchRunId: string): Promise<BatchDetail>;

  // ---- Document-centric surface (Phase 6+ of the refactor) -----
  //
  // The user manages documents, not runs. Each document carries a
  // knowledge state (attached / detached / removed) and a pointer
  // to its current "usable" run. These methods are the FE's window
  // into that surface; the older run-centric methods above remain
  // for the run-detail page during the migration.

  /**
 * GET /documents — list documents in the project. `includeRemoved`
 * defaults to false; pass true for admin/history views.
 */
  listDocuments(opts?: { includeRemoved?: boolean }): Promise<DocumentListItem[]>;

  /** GET /documents/{id}/detail — single document with full run history. */
  getDocumentDetail(documentId: string): Promise<DocumentDetail>;

  /** GET /documents/{id}/runs — run history only (most recent first). */
  listDocumentRuns(documentId: string): Promise<DocumentRunSummary[]>;

  /**
   * GET /documents/{id}/snapshots — per-snapshot rows for the
   * Candidate Knowledge / Active Knowledge sections on Document
   * Detail. Most recent first. The active snapshot is flagged
   * via ``isActive``. Returns 503 if the deployment doesn't wire
   * the snapshot service.
   */
  listDocumentSnapshots(
    documentId: string,
  ): Promise<DocumentSnapshotSummary[]>;

  /**
 * POST /documents/{id}/attach — restore knowledge usage. 409
 * when the document was previously removed (must re-upload).
 */
  attachDocument(documentId: string): Promise<DocumentLifecycleResponse>;

  /**
 * POST /documents/{id}/detach — stop using the document for
 * retrieval / search / validation / answers. Preserves
 * everything on disk so the user can re-attach later.
 */
  detachDocument(documentId: string): Promise<DocumentLifecycleResponse>;

  /**
 * POST /documents/{id}/remove — disown the document's generated
 * knowledge. Clears active_snapshot_id; the document is hidden
 * from the normal list. Re-attach requires re-upload.
 */
  removeDocument(documentId: string): Promise<DocumentLifecycleResponse>;

  /**
 * POST /documents/{id}/reindex — start a new ingestion attempt.
 * The new run carries `runType="reindex"`. The document's
 * activeSnapshotId only flips when the new run reaches a usable
 * terminal state — a failed reindex preserves the previous good
 * result.
 */
  reindexDocument(documentId: string): Promise<DocumentReindexResponse>;

  /**
 * POST /ingestion-runs/{run_id}/refresh-enrichment — start a new
 * candidate run that REUSES this run's compile output and re-runs
 * only enrichment + graph + index. ``run_id`` MUST be the
 * document's currently active run; the server rejects non-active
 * runs with HTTP 409.
 *
 * Promotion to ``activeSnapshotId`` is CAS-on-terminal-success —
 * a failed refresh preserves the previous active.
 */
  refreshRunEnrichment(runId: string): Promise<RunRefreshEnrichmentResponse>;
}

/** Per-role probe result from `/healthz/llm`. */
export interface LLMHealthRole {
  role: string;
  ok: boolean;
  provider: string | null;
  model: string | null;
  error: string | null;
}

/** Aggregate LLM health status surfaced by `/healthz/llm`. */
export interface LLMHealthStatus {
  healthy: boolean;
  checkedAt: string | null;
  results: LLMHealthRole[];
}

/**
 * Reason code on a Clean Up Run eligibility / result response. The
 * server is the source of truth; the FE maps these to copy without
 * re-deriving the rules. ``OK`` is the only "yes you can" code.
 */
export type CleanUpRunReason =
  | "OK"
  | "RUN_NOT_FOUND"
  | "PROCESSING_RUN"
  | "ACTIVE_RUN"
  | "ONLY_RUN";

/** Result of ``GET /ingestion-runs/{id}/cleanup-eligibility``. */
export interface CleanUpEligibility {
  runId: string;
  allowed: boolean;
  reason: CleanUpRunReason;
  message: string;
  /** Specific blocking ids (e.g. ``activeRunId``, ``documentId``)
   *  so the UI can render precise diagnostics. */
  blockingReferences: Record<string, unknown>;
}

/**
 * Result of ``POST /ingestion-runs/{id}/clean-up``. HTTP status is
 * always 200 — ``cleaned`` carries the outcome. On ``cleaned=false``
 * the ``reason`` is one of the refusal codes; the FE renders the
 * server's ``message`` verbatim so users see the canonical copy.
 */
export interface CleanUpRunResult {
  runId: string;
  cleaned: boolean;
  reason: CleanUpRunReason;
  message: string;
  deletedCounts: {
    artifacts: number;
    chunks: number;
    enrichments: number;
    validationResults: number;
    snapshots: number;
    workspaceFiles: number;
  };
  deletedAt: string | null;
}

/** Result envelope from `POST /ingestion-batches`. */
export interface BatchUploadResult {
  batchRunId: string;
  fileCount: number;
  runIds: string[];
  status: string;
  startedAt: string;
}

/** One row in `BatchDetail.runs`. */
export interface BatchChildRun {
  runId: string;
  documentId: string | null;
  filename: string | null;
  status: string;
  currentStage: string | null;
  currentStep: string | null;
  progressPercent: number;
}

/** Aggregate view returned by `GET /ingestion-batches/{id}`. */
export interface BatchDetail {
  batchRunId: string;
  status: string;
  startedAt: string;
  fileCount: number;
  completedCount: number;
  failedCount: number;
  currentRunId: string | null;
  runs: BatchChildRun[];
}

/** Sentinel error type the UI can surface as 4xx / 5xx differently. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}
