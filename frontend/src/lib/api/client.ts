/**
 * Single integration surface — the contract every IngestionClient
 * must satisfy. The mock and live clients implement the same
 * interface so component code never branches on data origin.
 */

import type {
  ExecutionPlan,
  IngestionRun,
  ProgressEvent,
  RunListQuery,
  RunListResult,
} from "@/types/ingestion";
import type {
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
} from "@/types/review";
import type { ProjectContext } from "@/types/ui";

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
 * their local cache without a follow-up `getRun()`.
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

  /** GET the execution plan for a run. */
  getPlan(runId: string): Promise<ExecutionPlan>;

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

  // ---- Result review (Phase 7) ------------------------------------
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
   * via `URL.createObjectURL`, JSON via `blob.text()` + parse, etc.)
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
