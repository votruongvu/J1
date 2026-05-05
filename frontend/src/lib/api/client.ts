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

  /** GET historical events. */
  getEvents(runId: string): Promise<ProgressEvent[]>;

  /** Open the SSE stream and call back on each event. */
  openStream(runId: string, handlers: StreamHandlers): StreamHandle;
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
