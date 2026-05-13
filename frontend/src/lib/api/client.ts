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
  ContentInventory,
  GenerateValidationSetRequest,
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
  StartValidationRunRequest,
  ValidationRun,
  ValidationRunListItem,
  ValidationSet,
  ValidationSetListItem,
} from "@/types/review";
import type { ProjectContext } from "@/types/ui";
import type {
  DocumentDetail,
  DocumentLifecycleResponse,
  DocumentListItem,
  DocumentRefreshEnrichResponse,
  DocumentReindexResponse,
  DocumentRunSummary,
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

  /** POST a new run. Returns the assigned run id. */
  upload(file: UploadFile, ctx: ProjectContext): Promise<{ runId: string }>;

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

  // ---- Validation sets + runs --------------------------

  /**
 * Generate a validation set from this run's chunks. Idempotent on
 * `(runId, hash)` — repeated calls with the same chunks return
 * the same set unless `force` is set.
 */
  generateValidationSet(
    runId: string,
    request?: GenerateValidationSetRequest,
  ): Promise<ValidationSet>;

  /** List validation sets for this run (lightweight projections). */
  listValidationSets(runId: string): Promise<ValidationSetListItem[]>;

  /** Fetch one set with its full test_cases array. */
  getValidationSet(runId: string, validationSetId: string): Promise<ValidationSet>;

  /**
 * POST to execute a validation set against this run. Synchronous
 * in v1. Returns the terminal snapshot — `executionStatus`
 * (`completed`/`failed`) and `validationStatus` (the answer
 * outcome) are independent fields.
 */
  runValidation(
    runId: string,
    request: StartValidationRunRequest,
  ): Promise<ValidationRun>;

  /** List validation runs for this run (lightweight projections). */
  listValidationRuns(runId: string): Promise<ValidationRunListItem[]>;

  /** Fetch one validation run with its full per-case results array. */
  getValidationRun(
    runId: string,
    validationRunId: string,
  ): Promise<ValidationRun>;

  // ---- tester verdict + report --------------------------

  /**
 * Record a human override on a single validation result. The
 * automated `status` is unchanged — verdict is a separate
 * signal recorded on the result. Returns the full updated
 * validation run snapshot so the caller can refresh local
 * state without an extra GET.
 */
  recordTesterVerdict(
    runId: string,
    validationRunId: string,
    resultId: string,
    body: { verdict: "pass" | "warning" | "fail"; notes?: string | null },
  ): Promise<ValidationRun>;

  /**
 * Download a validation run report. Returns the raw text body
 * (Markdown or JSON depending on `format`) plus the suggested
 * filename from the backend's Content-Disposition header. The
 * caller decides whether to render inline or trigger a download.
 */
  downloadValidationReport(
    runId: string,
    validationRunId: string,
    format?: "markdown" | "json",
  ): Promise<{ content: string; mediaType: string; filename: string }>;

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
 * DELETE an ingestion run (soft tombstone). Backend marks the
 * run + its artifacts deleted; subsequent reads exclude them.
 * Idempotent — `wasAlreadyDeleted=true` on the second call.
 * Throws ApiError(409) when the run is still active.
 */
  deleteRun(runId: string): Promise<DeleteRunResult>;

  /**
 * POST purge — physically remove a soft-deleted run, its
 * artifacts (files + registry records), and any validation
 * sets/runs that referenced it. Audit log stays intact.
 *
 * By default the backend requires the run to already be
 * soft-deleted (DELETE first). Pass `force=true` to bypass that
 * gate for admin tooling.
 *
 * Throws `ApiError(409)` when the run is still active OR when
 * the operator hasn't soft-deleted it first (and `force` is
 * false).
 */
  purgeRun(runId: string, opts?: { force?: boolean }): Promise<PurgeRunResult>;

  /**
 * POST full re-index — start a NEW run for the same document_id
 * as the referenced run. Returns the new `reindexRunId`.
 * Throws ApiError(409) when the original run is still active.
 */
  fullReindexRun(runId: string): Promise<FullReindexResult>;

  /**
 * POST resume-from-checkpoint — start a NEW run for the same
 * document_id, skipping LLM-cost stages that already completed
 * in the prior run (currently enrich + graph). Compile and
 * chunk-generation always re-run.
 *
 * Throws `ApiError(409)` when the original run is still active,
 * `ApiError(412)` when the prior run has no resume snapshot
 * (legacy run / cancelled), and `ApiError(412)` with
 * `RESUME_INCOMPATIBLE` code + `details.diff` when settings
 * drifted since the prior run finished.
 */
  resumeFromCheckpoint(runId: string): Promise<ResumeFromCheckpointResult>;

  /**
 * POST rebuild-index — start a NEW run that ONLY runs the index
 * activity against the prior run's chunk artifacts. Use when the
 * vector store was cleared, the embedding model upgraded, or the
 * index got corrupted while chunks themselves are still valid.
 *
 * Throws `ApiError(409)` when the original run is still active,
 * `ApiError(412)` when the prior run has no resume snapshot or
 * never produced chunk artifacts (use full-reindex instead).
 */
  rebuildIndex(runId: string): Promise<RebuildIndexResult>;

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
 * knowledge. Clears active_run_id; the document is hidden from
 * the normal list. Re-attach requires re-upload.
 */
  removeDocument(documentId: string): Promise<DocumentLifecycleResponse>;

  /**
 * POST /documents/{id}/reindex — start a new ingestion attempt.
 * The new run carries `runType="reindex"`. The document's
 * activeRunId only flips when the new run reaches a usable
 * terminal state — a failed reindex preserves the previous good
 * result.
 */
  reindexDocument(documentId: string): Promise<DocumentReindexResponse>;

  /**
 * POST /documents/{id}/refresh-enrich — start a candidate run
 * that REUSES the previous active run's compile output and re-
 * runs only enrichment + graph + index. Promotion to
 * `activeRunId` is gated on terminal success (same CAS rule as
 * reindex), so a failed refresh preserves the previous active.
 */
  refreshEnrichDocument(
    documentId: string,
  ): Promise<DocumentRefreshEnrichResponse>;
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

/** Result envelope from `DELETE /ingestion-runs/{id}`. */
export interface DeleteRunResult {
  runId: string;
  status: string;
  tombstonedArtifactCount: number;
  wasAlreadyDeleted: boolean;
  deletedAt: string;
}

/** Result envelope from `POST /ingestion-runs/{id}/purge`. */
export interface PurgeRunResult {
  runId: string;
  /** Registry records removed. */
  artifactsPurged: number;
  /** Files actually unlinked from disk. */
  filesDeleted: number;
  /** Files that were already missing (idempotent path). */
  filesMissing: number;
  /** JSONL snapshots of the run record removed. */
  snapshotsRemoved: number;
  validationSetsRemoved: number;
  validationRunsRemoved: number;
  purgedAt: string;
}

/** Result envelope from `POST /ingestion-runs/{id}/full-reindex`. */
export interface FullReindexResult {
  originalRunId: string;
  reindexRunId: string;
  workflowId: string;
  documentId: string;
  status: string;
}

/** Result envelope from `POST /ingestion-runs/{id}/resume-from-checkpoint`. */
export interface ResumeFromCheckpointResult {
  originalRunId: string;
  resumeRunId: string;
  workflowId: string;
  documentId: string;
  status: string;
  /** Step names the new run will skip (subset of enrich, graph). */
  resumedSteps: string[];
  /** Number of artifacts seeded from the prior run. */
  carryForwardArtifactCount: number;
}

/** Result envelope from `POST /ingestion-runs/{id}/rebuild-index`. */
export interface RebuildIndexResult {
  originalRunId: string;
  rebuildRunId: string;
  workflowId: string;
  documentId: string;
  status: string;
  /** Number of chunk artifacts seeded from the prior run. */
  carryForwardChunkCount: number;
  /** The indexer kind the new run will use. */
  indexerKind: string;
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
