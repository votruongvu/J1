/**
 * Live ApiClient — issues real HTTP requests against the J1 REST
 * surface, parses the SSE stream via `fetch` + `ReadableStream` so
 * we can send custom headers (which native EventSource forbids).
 *
 * Endpoints used:
 *
 *   POST /ingestion-runs                          — upload + create run
 *   GET  /ingestion-runs/{run_id}                  — status snapshot
 *   GET  /ingestion-runs/{run_id}/plan             — execution plan
 *   POST /ingestion-runs/{run_id}/confirm          — confirm plan
 *   GET  /ingestion-runs/{run_id}/events           — historical events
 *   GET  /ingestion-runs/{run_id}/events/stream    — live SSE
 *
 * Tenant + project headers are mandatory on every request. The
 * single header-injection point is `_headers()` — every method
 * routes through it.
 */

import type {
  ExecutionPlan,
  IngestionRun,
  ProgressEvent,
  RunListQuery,
  RunListResult,
} from "@/types/ingestion";
import type {
  GenerateValidationSetRequest,
  ManualTestQueryRequest,
  ManualTestQueryResponse,
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
  StartValidationRunRequest,
  ValidationRun,
  ValidationRunListItem,
  ValidationSet,
  ValidationSetListItem,
} from "@/types/review";
import type { AuthConfig, ProjectContext } from "@/types/ui";
import {
  ApiError,
  type IngestionClient,
  type RunControlResult,
  type StreamHandle,
  type StreamHandlers,
  type UploadFile,
} from "./client";
import {
  type ApiPlanRecord,
  type ApiProgressEvent,
  type ApiRunListItem,
  type ApiRunRecord,
  artifactPageFromApi,
  chunkDetailFromApi,
  chunkPageFromApi,
  eventFromApi,
  graphSnapshotFromApi,
  parseEtag,
  parseFilename,
  planFromApi,
  qualityReportFromApi,
  runFromApi,
  runListItemFromApi,
  runSummaryFromApi,
} from "./translate";
import { readSseStream } from "./sse";

export interface ApiClientOptions {
  baseUrl: string;
  getCtx: () => ProjectContext;
  getAuth: () => AuthConfig;
}

export class ApiClient implements IngestionClient {
  private opts: ApiClientOptions;

  constructor(opts: ApiClientOptions) {
    this.opts = opts;
  }

  // ---- Header injection (single source of truth) -------------------

  private headers(extra?: Record<string, string>): Record<string, string> {
    const ctx = this.opts.getCtx();
    const auth = this.opts.getAuth();
    const h: Record<string, string> = {
      "X-Tenant-Id": ctx.tenant,
      "X-Project-Id": ctx.project,
      ...(extra ?? {}),
    };
    if (auth.value) {
      if (auth.kind === "bearer") h["Authorization"] = `Bearer ${auth.value}`;
      else h["X-API-Key"] = auth.value;
    }
    return h;
  }

  private url(path: string): string {
    const base = (this.opts.baseUrl || "").replace(/\/+$/, "");
    return `${base}${path}`;
  }

  /** Read an envelope-shaped JSON response and unwrap `data`. */
  private async json<T>(resp: Response): Promise<T> {
    const text = await resp.text();
    let parsed: unknown;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      throw new ApiError(resp.status, text || `HTTP ${resp.status}`);
    }
    if (!resp.ok) {
      // J1 envelope: `{ error: { code, message } }`.
      const obj = parsed as { error?: { message?: string }; message?: string };
      const msg = obj?.error?.message ?? obj?.message ?? `HTTP ${resp.status}`;
      throw new ApiError(resp.status, msg);
    }
    // J1 success envelope: `{ requestId, data, meta }`.
    const obj = parsed as { data?: unknown };
    return (obj?.data ?? parsed) as T;
  }

  // ---- listRuns ---------------------------------------------------
  // GET /ingestion-runs — paginated list with optional `?status=`
  // repeats and a `?q=` substring filter (matches `runId` /
  // `documentName`). Tenant + project headers come from `headers()`
  // like every other request.
  async listRuns(_ctx: ProjectContext, opts?: RunListQuery): Promise<RunListResult> {
    const params = new URLSearchParams();
    if (opts?.page != null) params.set("page", String(opts.page));
    if (opts?.pageSize != null) params.set("pageSize", String(opts.pageSize));
    if (opts?.q) params.set("q", opts.q);
    // Map the FE's single-status filter onto the backend's repeated
    // `?status=` query param. The backend uppercase status values
    // are lowercase on the wire (`RunStatus` is a StrEnum); the FE
    // passes them through as-is so the same string the FE filter
    // dropdown carries reaches the server.
    if (opts?.status) params.append("status", opts.status.toLowerCase());
    const qs = params.toString();
    const path = qs ? `/ingestion-runs?${qs}` : "/ingestion-runs";
    const resp = await fetch(this.url(path), { headers: this.headers() });
    const data = await this.json<{
      items?: ApiRunListItem[];
      page?: number;
      pageSize?: number;
      total?: number;
    }>(resp);
    return {
      items: (data.items ?? []).map(runListItemFromApi),
      page: data.page ?? 1,
      pageSize: data.pageSize ?? 20,
      total: data.total ?? 0,
    };
  }

  // ---- upload ------------------------------------------------------

  async upload(file: UploadFile, ctx: ProjectContext): Promise<{ runId: string }> {
    if (!ctx?.tenant || !ctx?.project) {
      throw new ApiError(400, "Tenant and Project are required.");
    }
    const fd = new FormData();
    if (file instanceof File) {
      fd.append("file", file);
    } else {
      // Demo button passes a name-only stub; build a tiny placeholder Blob.
      const blob = new Blob(["demo content"], { type: "text/plain" });
      const filename = (file as { name?: string }).name ?? "demo.txt";
      fd.append("file", blob, filename);
    }
    // `compilerKind` is intentionally OMITTED. The backend resolves it
    // from the deployment's `J1_DEFAULT_COMPILER` (validated against
    // the registered processor kinds at the API boundary). Sending a
    // hard-coded value here would override the deployment default and
    // either silently swap the compiler or fail with INVALID_ARGUMENT
    // when the chosen kind isn't registered. Surface a `compilerKind`
    // selector in the UI before threading a value through here.
    const resp = await fetch(this.url("/ingestion-runs"), {
      method: "POST",
      headers: this.headers(),
      body: fd,
    });
    const data = await this.json<{ runId: string }>(resp);
    return { runId: data.runId };
  }

  // ---- getRun ------------------------------------------------------

  async getRun(runId: string): Promise<IngestionRun> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}`), {
      headers: this.headers(),
    });
    const data = await this.json<ApiRunRecord>(resp);
    return runFromApi(data);
  }

  // ---- getPlan -----------------------------------------------------

  async getPlan(runId: string): Promise<ExecutionPlan> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}/plan`), {
      headers: this.headers(),
    });
    const data = await this.json<ApiPlanRecord>(resp);
    return planFromApi(data);
  }

  // ---- confirm -----------------------------------------------------

  async confirm(runId: string): Promise<{ ok: true }> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}/confirm`), {
      method: "POST",
      headers: this.headers(),
    });
    await this.json(resp);
    return { ok: true };
  }

  // ---- Control actions: pause / resume / cancel --------------------
  // Each posts to `/ingestion-runs/{run_id}/{action}`. The backend
  // flips the run record's status, emits a progress event, and
  // forwards the matching Temporal signal. The returned record is
  // the FE-facing camelCase shape from `IngestionRunControlRecord`.

  private async _control(runId: string, action: "pause" | "resume" | "cancel"):
    Promise<RunControlResult>
  {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/${action}`),
      { method: "POST", headers: this.headers() },
    );
    return this.json<RunControlResult>(resp);
  }

  pauseRun(runId: string): Promise<RunControlResult> {
    return this._control(runId, "pause");
  }

  resumeRun(runId: string): Promise<RunControlResult> {
    return this._control(runId, "resume");
  }

  cancelRun(runId: string): Promise<RunControlResult> {
    return this._control(runId, "cancel");
  }

  // ---- getEvents ---------------------------------------------------

  async getEvents(runId: string): Promise<ProgressEvent[]> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}/events`), {
      headers: this.headers(),
    });
    const data = await this.json<{ events?: ApiProgressEvent[] }>(resp);
    return (data.events ?? []).map(eventFromApi);
  }

  // ---- Result-review surface (Phase 7) -----------------------------
  // Read-only; same envelope + tenant/project header discipline as
  // every other request.

  async getRunSummary(runId: string): Promise<ReviewRunSummary> {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/summary`),
      { headers: this.headers() },
    );
    return runSummaryFromApi(await this.json<unknown>(resp));
  }

  async getRunQualityReport(
    runId: string,
    opts?: { includeRaw?: boolean },
  ): Promise<ReviewQualityReport> {
    const qs = opts?.includeRaw ? "?includeRaw=true" : "";
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/quality-report${qs}`,
      ),
      { headers: this.headers() },
    );
    return qualityReportFromApi(await this.json<unknown>(resp));
  }

  async listRunChunks(
    runId: string,
    opts?: ReviewChunkListQuery,
  ): Promise<ReviewChunkPage> {
    const params = new URLSearchParams();
    if (opts?.page != null) params.set("page", String(opts.page));
    if (opts?.pageSize != null) params.set("pageSize", String(opts.pageSize));
    if (opts?.status) params.set("status", opts.status);
    if (opts?.minConfidence != null) {
      params.set("minConfidence", String(opts.minConfidence));
    }
    const qs = params.toString();
    const path = `/ingestion-runs/${encodeURIComponent(runId)}/chunks${qs ? `?${qs}` : ""}`;
    const resp = await fetch(this.url(path), { headers: this.headers() });
    return chunkPageFromApi(await this.json<unknown>(resp));
  }

  async getRunChunk(
    runId: string, chunkId: string,
  ): Promise<ReviewChunkDetail> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/chunks/${encodeURIComponent(chunkId)}`,
      ),
      { headers: this.headers() },
    );
    return chunkDetailFromApi(await this.json<unknown>(resp));
  }

  async listRunArtifacts(
    runId: string,
    opts?: ReviewArtifactListQuery,
  ): Promise<ReviewArtifactPage> {
    const params = new URLSearchParams();
    if (opts?.kind) params.set("kind", opts.kind);
    if (opts?.page != null) params.set("page", String(opts.page));
    if (opts?.pageSize != null) params.set("pageSize", String(opts.pageSize));
    const qs = params.toString();
    const path = `/ingestion-runs/${encodeURIComponent(runId)}/artifacts${qs ? `?${qs}` : ""}`;
    const resp = await fetch(this.url(path), { headers: this.headers() });
    return artifactPageFromApi(await this.json<unknown>(resp));
  }

  async getRunArtifactContent(
    runId: string, artifactId: string,
  ): Promise<ReviewArtifactContent> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/content`,
      ),
      { headers: this.headers() },
    );
    if (!resp.ok) {
      // Try to surface the standard error envelope; fall back to a
      // plain HTTP message if the response isn't JSON-shaped.
      const text = await resp.text();
      let msg = `HTTP ${resp.status}`;
      try {
        const obj = JSON.parse(text) as {
          error?: { message?: string }; message?: string;
        };
        msg = obj?.error?.message ?? obj?.message ?? msg;
      } catch {
        if (text) msg = text;
      }
      throw new ApiError(resp.status, msg);
    }
    const blob = await resp.blob();
    const contentType =
      resp.headers.get("Content-Type") ?? "application/octet-stream";
    return {
      blob,
      contentType,
      filename: parseFilename(resp.headers.get("Content-Disposition")),
      etag: parseEtag(resp.headers.get("ETag")),
    };
  }

  async getRunGraph(
    runId: string, opts?: ReviewGraphQuery,
  ): Promise<ReviewGraphSnapshot> {
    const params = new URLSearchParams();
    if (opts?.maxNodes != null) params.set("maxNodes", String(opts.maxNodes));
    if (opts?.maxEdges != null) params.set("maxEdges", String(opts.maxEdges));
    const qs = params.toString();
    const path = `/ingestion-runs/${encodeURIComponent(runId)}/graph${qs ? `?${qs}` : ""}`;
    const resp = await fetch(this.url(path), { headers: this.headers() });
    return graphSnapshotFromApi(await this.json<unknown>(resp));
  }

  // ---- runManualTestQuery (Phase 1 validation) ---------------------

  async runManualTestQuery(
    runId: string,
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse> {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/test-query`),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(request),
      },
    );
    // The backend response is camelCase already (CamelModel) so we
    // don't translate field names — same shape as ManualTestQueryResponse.
    return await this.json<ManualTestQueryResponse>(resp);
  }

  // ---- Validation sets + runs (Phase 2) ----------------------------

  async generateValidationSet(
    runId: string,
    request: GenerateValidationSetRequest = {},
  ): Promise<ValidationSet> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/validation-sets/generate`,
      ),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(request),
      },
    );
    return await this.json<ValidationSet>(resp);
  }

  async listValidationSets(runId: string): Promise<ValidationSetListItem[]> {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/validation-sets`),
      { headers: this.headers() },
    );
    const body = await this.json<{ items: ValidationSetListItem[] }>(resp);
    return body.items;
  }

  async getValidationSet(
    runId: string,
    validationSetId: string,
  ): Promise<ValidationSet> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/validation-sets/${encodeURIComponent(validationSetId)}`,
      ),
      { headers: this.headers() },
    );
    return await this.json<ValidationSet>(resp);
  }

  async runValidation(
    runId: string,
    request: StartValidationRunRequest,
  ): Promise<ValidationRun> {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/validation-runs`),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(request),
      },
    );
    return await this.json<ValidationRun>(resp);
  }

  async listValidationRuns(runId: string): Promise<ValidationRunListItem[]> {
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/validation-runs`),
      { headers: this.headers() },
    );
    const body = await this.json<{ items: ValidationRunListItem[] }>(resp);
    return body.items;
  }

  async getValidationRun(
    runId: string,
    validationRunId: string,
  ): Promise<ValidationRun> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/validation-runs/${encodeURIComponent(validationRunId)}`,
      ),
      { headers: this.headers() },
    );
    return await this.json<ValidationRun>(resp);
  }

  // ---- openStream --------------------------------------------------

  openStream(runId: string, handlers: StreamHandlers): StreamHandle {
    const controller = new AbortController();
    const url = this.url(`/ingestion-runs/${encodeURIComponent(runId)}/events/stream`);
    const headers = this.headers({ Accept: "text/event-stream" });
    if (handlers.lastEventId) {
      headers["Last-Event-Id"] = handlers.lastEventId;
    }

    let aborted = false;

    void (async () => {
      let resp: Response;
      try {
        resp = await fetch(url, { headers, signal: controller.signal });
      } catch (err) {
        if (!aborted) handlers.onError?.(err);
        return;
      }
      if (!resp.ok) {
        handlers.onError?.(new ApiError(resp.status, `Stream HTTP ${resp.status}`));
        return;
      }
      handlers.onOpen?.();
      try {
        await readSseStream(resp, controller.signal, (frame) => {
          // Each frame's `data` JSON IS already the ProgressEvent
          // payload from the backend. The SSE `id:` and `event:`
          // fields override the payload's own fields when both are
          // present (matches the mock behaviour).
          const payload = frame.data as Record<string, unknown>;
          const merged = {
            ...payload,
            eventType: frame.event ?? payload["eventType"],
            eventId: frame.id ?? payload["eventId"],
          } as ApiProgressEvent;
          handlers.onEvent?.(eventFromApi(merged));
        });
      } catch (err) {
        if (!aborted) handlers.onError?.(err);
      } finally {
        // Don't fan an `onClose` back to the caller when *they* asked
        // to close — they already know. This keeps the reconnect path
        // in the run-detail page from re-opening a stream we just
        // tore down on unmount or on a terminal event.
        if (!aborted) handlers.onClose?.();
      }
    })();

    return {
      close: () => {
        aborted = true;
        try {
          controller.abort();
        } catch {
          /* ignore */
        }
      },
    };
  }
}
