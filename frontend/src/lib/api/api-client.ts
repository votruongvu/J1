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
import type { AuthConfig, ProjectContext } from "@/types/ui";
import {
  ApiError,
  type IngestionClient,
  type StreamHandle,
  type StreamHandlers,
  type UploadFile,
} from "./client";
import {
  type ApiPlanRecord,
  type ApiProgressEvent,
  type ApiRunRecord,
  eventFromApi,
  planFromApi,
  runFromApi,
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
  // The J1 API does not yet ship a `GET /ingestion-runs` list endpoint.
  // Return an empty page with a hint so the All Runs view renders an
  // explanatory empty state instead of silently displaying nothing.
  async listRuns(_ctx: ProjectContext, _opts?: RunListQuery): Promise<RunListResult> {
    return {
      items: [],
      page: 1,
      pageSize: 0,
      total: 0,
      _liveUnsupported:
        "List view is not available in live mode (the J1 API doesn't ship " +
        "a GET /ingestion-runs list endpoint yet). Use 'New ingestion run' " +
        "to create a run; the run-detail page will work end-to-end.",
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
    fd.append("compilerKind", "mock");
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

  // ---- getEvents ---------------------------------------------------

  async getEvents(runId: string): Promise<ProgressEvent[]> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}/events`), {
      headers: this.headers(),
    });
    const data = await this.json<{ events?: ApiProgressEvent[] }>(resp);
    return (data.events ?? []).map(eventFromApi);
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
        handlers.onClose?.();
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
