/**
 * Live ApiClient — issues real HTTP requests against the J1 REST
 * surface, parses the SSE stream via `fetch` + `ReadableStream` so
 * we can send custom headers (which native EventSource forbids).
 *
 * Endpoints used:
 *
 * POST /ingestion-runs — upload + create run
 * GET /ingestion-runs/{run_id} — status snapshot
 * GET /ingestion-runs/{run_id}/plan — execution plan
 * POST /ingestion-runs/{run_id}/confirm — confirm plan
 * GET /ingestion-runs/{run_id}/events — historical events
 * GET /ingestion-runs/{run_id}/events/stream — live SSE
 *
 * Tenant + project headers are mandatory on every request. The
 * single header-injection point is `_headers` — every method
 * routes through it.
 */

import type {
  IngestionRun,
  ProgressEvent,
  RunListQuery,
  RunListResult,
} from "@/types/ingestion";
import type {
  ImportedTestCaseExecution,
  ImportedTestCaseSet,
  ManualTestQueryRequest,
  ContentInventory,
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
import type {
  AdvancedAssessmentResponse,
  AssessmentPlanResponse,
  ExecutionProfileId,
  ManualActionDescriptor,
} from "@/types/execution-profile";
import type { AuthConfig, ProjectContext } from "@/types/ui";
import type {
  DocumentDetail,
  DocumentLifecycleResponse,
  DocumentListItem,
  DocumentReindexResponse,
  DocumentRunSummary,
  DocumentSnapshotSummary,
  RunDomainEnrichmentResponse,
  RunRefreshEnrichmentResponse,
} from "@/types/documents";
import {
  ApiError,
  type BatchDetail,
  type BatchUploadResult,
  type CleanUpEligibility,
  type CleanUpRunResult,
  type IngestionClient,
  type LLMHealthStatus,
  type RunControlResult,
  type StreamHandle,
  type StreamHandlers,
  type UploadFile,
} from "./client";
import {
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
  // `documentName`). Tenant + project headers come from `headers`
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

  async upload(
    file: UploadFile,
    ctx: ProjectContext,
    selectedProfile?: ExecutionProfileId,
    assessmentDecisionId?: string | null,
  ): Promise<{ runId: string }> {
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
    //
    // `selectedProfile` IS forwarded when present so the backend can
    // honour the user's profile pick from the AssessmentPlanDialog.
    // Omitting it keeps the legacy "use deployment default" path
    // working for callers that haven't adopted the picker yet.
    if (selectedProfile) {
      fd.append("selectedProfile", selectedProfile);
    }
    // ``assessmentDecisionId`` lets the backend short-circuit its
    // assessment rebuild by consuming the persisted decision the
    // picker just showed. Missing / invalid ids degrade to the
    // workflow's rebuild fallback (server-side warning, not a 4xx).
    if (assessmentDecisionId) {
      fd.append("assessmentDecisionId", assessmentDecisionId);
    }
    const resp = await fetch(this.url("/ingestion-runs"), {
      method: "POST",
      headers: this.headers(),
      body: fd,
    });
    const data = await this.json<{ runId: string }>(resp);
    return { runId: data.runId };
  }

  async getDocumentAssessmentPlan(
    documentId: string,
  ): Promise<AssessmentPlanResponse> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/assessment-plan`),
      { method: "POST", headers: this.headers() },
    );
    return this.json<AssessmentPlanResponse>(resp);
  }

  async runAdvancedAssessment(
    documentId: string,
  ): Promise<AdvancedAssessmentResponse> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/advanced-assessment`,
      ),
      { method: "POST", headers: this.headers() },
    );
    return this.json<AdvancedAssessmentResponse>(resp);
  }

  async listDocumentManualActions(
    documentId: string,
  ): Promise<{ documentId: string; actions: ManualActionDescriptor[] }> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/manual-actions`,
      ),
      { method: "GET", headers: this.headers() },
    );
    return this.json(resp);
  }

  async registerDocument(
    file: UploadFile,
    ctx: ProjectContext,
  ): Promise<{ documentId: string }> {
    if (!ctx?.tenant || !ctx?.project) {
      throw new ApiError(400, "Tenant and Project are required.");
    }
    const fd = new FormData();
    if (file instanceof File) {
      fd.append("file", file);
    } else {
      const blob = new Blob(["demo content"], { type: "text/plain" });
      const filename = (file as { name?: string }).name ?? "demo.txt";
      fd.append("file", blob, filename);
    }
    const resp = await fetch(this.url("/documents"), {
      method: "POST",
      headers: this.headers(),
      body: fd,
    });
    const data = await this.json<{ documentId: string }>(resp);
    return { documentId: data.documentId };
  }

  // ---- getRun ------------------------------------------------------

  async getRun(runId: string): Promise<IngestionRun> {
    const resp = await fetch(this.url(`/ingestion-runs/${encodeURIComponent(runId)}`), {
      headers: this.headers(),
    });
    const data = await this.json<ApiRunRecord>(resp);
    return runFromApi(data);
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

  // ---- Result-review surface -----------------------------
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

  async getRunContentInventory(runId: string): Promise<ContentInventory> {
    // Backend returns the canonical Pydantic camelCase shape via the
    // standard envelope. Type assertion is safe because the
    // `ContentInventory` interface mirrors the BE DTO field-for-field
    // — verified by the contract test in
    // tests/test_rest_run_review.py::test_parsed_content_endpoint.
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/parsed-content`),
      { headers: this.headers() },
    );
    return (await this.json<ContentInventory>(resp));
  }

  async getRunEnrichPlan(runId: string): Promise<RunEnrichPlanResponse> {
    // Reads the `post_compile_enrich_plan` artifact via
    // `IngestionResultReviewService.get_run_enrich_plan`. Plain dict
    // payload (no DTO) — the schema is small and stable; we type
    // the response in the FE for compile-time safety.
    const resp = await fetch(
      this.url(`/ingestion-runs/${encodeURIComponent(runId)}/enrich-plan`),
      { headers: this.headers() },
    );
    return (await this.json<RunEnrichPlanResponse>(resp));
  }

  async getRunInitialExecutionPlan(
    runId: string,
  ): Promise<RunInitialExecutionPlanResponse> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/initial-execution-plan`,
      ),
      { headers: this.headers() },
    );
    return (await this.json<RunInitialExecutionPlanResponse>(resp));
  }

  async getRunCompileResult(
    runId: string,
  ): Promise<RunCompileResultResponse> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/compile-result`,
      ),
      { headers: this.headers() },
    );
    return (await this.json<RunCompileResultResponse>(resp));
  }

  async getRunEnrichmentResult(
    runId: string,
  ): Promise<RunEnrichmentResultResponse> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/enrichment-result`,
      ),
      { headers: this.headers() },
    );
    return (await this.json<RunEnrichmentResultResponse>(resp));
  }

  async getRunFinalIngestionReport(
    runId: string,
  ): Promise<FinalIngestionReportResponse> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/final-ingestion-report`,
      ),
      { headers: this.headers() },
    );
    return (await this.json<FinalIngestionReportResponse>(resp));
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

  // ---- runManualTestQuery ( validation) ---------------------

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

  async runDocumentTestQuery(
    documentId: string,
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/test-query`,
      ),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(request),
      },
    );
    return await this.json<ManualTestQueryResponse>(resp);
  }

  async runProjectQuery(
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse> {
    // Project id is in the path; the backend cross-checks against
    // ``X-Project-Id`` (header set by ``this.headers()``).
    const ctx = this.opts.getCtx();
    const resp = await fetch(
      this.url(
        `/projects/${encodeURIComponent(ctx.project)}/query`,
      ),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(request),
      },
    );
    return await this.json<ManualTestQueryResponse>(resp);
  }

  // ---- runQueryTrace (raw orchestrator trace) ---------------------
  //
  // Hits POST /dev/query-trace and returns the full ``QueryTrace``
  // JSON verbatim. The trace exposes EVERY orchestrator stage so an
  // operator can answer "why did the query fail" without
  // instrumentation: plan, routes executed, candidates with
  // kept/dropped reasons, evidence groups, llm_evidence, citations,
  // and gate results. Used by ManualQueryTraceView (Validation
  // tab's "Trace" sub-panel).
  //
  // Returns 503 when no orchestrator is wired at the backend —
  // callers should fall back to runManualTestQuery in that case.
  async runQueryTrace(
    runId: string,
    question: string,
  ): Promise<QueryTracePayload> {
    const resp = await fetch(
      this.url(`/dev/query-trace`),
      {
        method: "POST",
        headers: this.headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ question, run_id: runId }),
      },
    );
    const wrapped = await this.json<{ data?: QueryTracePayload } | QueryTracePayload>(
      resp,
    );
    // The endpoint uses the envelope() wrapper so the body is
    // {requestId, data: {...}}. Unwrap when present.
    if (wrapped && typeof wrapped === "object" && "data" in wrapped) {
      return (wrapped as { data: QueryTracePayload }).data;
    }
    return wrapped as QueryTracePayload;
  }

  // ---- Validation sets + runs ----------------------------

  // ---- Imported test cases (auxiliary Validation Tab helper) ----

  async importTestCases(
    documentId: string,
    file: File,
  ): Promise<ImportedTestCaseSet> {
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/imported-test-cases/import`,
      ),
      {
        method: "POST",
        // NOTE: no Content-Type header — fetch sets the multipart
        // boundary itself when given a FormData body.
        headers: this.headers(),
        body: form,
      },
    );
    return await this.json<ImportedTestCaseSet>(resp);
  }

  async getImportedTestCases(
    documentId: string,
  ): Promise<ImportedTestCaseSet | null> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/imported-test-cases`,
      ),
      { headers: this.headers() },
    );
    if (resp.status === 404) {
      return null;
    }
    return await this.json<ImportedTestCaseSet>(resp);
  }

  async deleteImportedTestCases(documentId: string): Promise<void> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/imported-test-cases`,
      ),
      { method: "DELETE", headers: this.headers() },
    );
    if (!resp.ok && resp.status !== 204) {
      throw new ApiError(resp.status, `HTTP ${resp.status}`);
    }
  }

  async executeImportedTestCases(
    documentId: string,
  ): Promise<ImportedTestCaseExecution> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/imported-test-cases/execute`,
      ),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<ImportedTestCaseExecution>(resp);
  }

  async getImportedTestCaseExecution(
    documentId: string,
  ): Promise<ImportedTestCaseExecution | null> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}/imported-test-cases/execution`,
      ),
      { headers: this.headers() },
    );
    if (resp.status === 404) {
      return null;
    }
    return await this.json<ImportedTestCaseExecution>(resp);
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

  async getLLMHealth(): Promise<LLMHealthStatus> {
    return this._llmHealthCall("GET", "/healthz/llm");
  }

  async refreshLLMHealth(): Promise<LLMHealthStatus> {
    // Synchronous re-probe — backend bounds it by the configured
    // probe deadline (default 5s per role), so the worst case is
    // ~15s if all three roles are down. The banner's button shows
    // a spinner during the call.
    return this._llmHealthCall("POST", "/healthz/llm/refresh");
  }

  private async _llmHealthCall(
    method: "GET" | "POST",
    path: string,
  ): Promise<LLMHealthStatus> {
    const res = await fetch(this.url(path), {
      method,
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      throw new ApiError(res.status, `${method} ${path} → ${res.status}`);
    }
    const body = await res.json();
    const data = body?.data ?? body;
    return {
      // Default to UNHEALTHY when the field is missing — fail-closed
      // so a malformed response doesn't silently hide real LLM
      // outages from the admin banner. The catch path on a network
      // error already does the same; this aligns the success-but-
      // malformed path with that contract.
      healthy: Boolean(data?.healthy ?? false),
      checkedAt: data?.checkedAt ?? null,
      results: Array.isArray(data?.results)
        ? data.results.map((r: Record<string, unknown>) => ({
            role: String(r.role ?? ""),
            ok: Boolean(r.ok),
            provider: r.provider == null ? null : String(r.provider),
            model: r.model == null ? null : String(r.model),
            error: r.error == null ? null : String(r.error),
          }))
        : [],
    };
  }

  // ---- Operational actions --------------------------------------

  async getCleanupEligibility(runId: string): Promise<CleanUpEligibility> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/cleanup-eligibility`,
      ),
      { headers: this.headers() },
    );
    return await this.json<CleanUpEligibility>(resp);
  }

  async cleanUpRun(runId: string): Promise<CleanUpRunResult> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/clean-up`,
      ),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<CleanUpRunResult>(resp);
  }

  async uploadBatch(
    files: File[],
    _ctx: ProjectContext,
  ): Promise<BatchUploadResult> {
    const form = new FormData();
    for (const f of files) {
      form.append("files", f, f.name);
    }
    const resp = await fetch(this.url("/ingestion-batches"), {
      method: "POST",
      // Don't add Content-Type — FormData sets its own
      // multipart boundary header automatically.
      headers: this.headers(),
      body: form,
    });
    const data = await this.json<Record<string, unknown>>(resp);
    return {
      batchRunId: String(data.batchRunId ?? ""),
      fileCount: Number(data.fileCount ?? files.length),
      runIds: Array.isArray(data.runIds) ? data.runIds.map(String) : [],
      status: String(data.status ?? "running"),
      startedAt: String(data.startedAt ?? ""),
    };
  }

  async getBatch(batchRunId: string): Promise<BatchDetail> {
    const resp = await fetch(
      this.url(`/ingestion-batches/${encodeURIComponent(batchRunId)}`),
      { headers: this.headers() },
    );
    const data = await this.json<Record<string, unknown>>(resp);
    const runs = Array.isArray(data.runs)
      ? data.runs.map((r: Record<string, unknown>) => ({
          runId: String(r.runId ?? ""),
          documentId: r.documentId == null ? null : String(r.documentId),
          filename: r.filename == null ? null : String(r.filename),
          status: String(r.status ?? ""),
          currentStage: r.currentStage == null ? null : String(r.currentStage),
          currentStep: r.currentStep == null ? null : String(r.currentStep),
          progressPercent: Number(r.progressPercent ?? 0),
        }))
      : [];
    return {
      batchRunId: String(data.batchRunId ?? batchRunId),
      status: String(data.status ?? "running"),
      startedAt: String(data.startedAt ?? ""),
      fileCount: Number(data.fileCount ?? 0),
      completedCount: Number(data.completedCount ?? 0),
      failedCount: Number(data.failedCount ?? 0),
      currentRunId: data.currentRunId == null ? null : String(data.currentRunId),
      runs,
    };
  }

  // ---- Document-centric surface --------------------------------
  //
  // Mirrors the Phase 6 read endpoints + Phase 3/4 write endpoints.
  // Each call uses the standard `headers()` (which carries tenant
  // + project) and the envelope-extractor `this.json()`. Wire
  // shape is already camelCase server-side so no field-name
  // translation needed.

  async listDocuments(
    opts?: { includeRemoved?: boolean },
  ): Promise<DocumentListItem[]> {
    const params = new URLSearchParams();
    if (opts?.includeRemoved) params.set("includeRemoved", "true");
    const qs = params.toString();
    const path = qs ? `/documents?${qs}` : "/documents";
    const resp = await fetch(this.url(path), { headers: this.headers() });
    const data = await this.json<{ documents?: DocumentListItem[] }>(resp);
    return data.documents ?? [];
  }

  async getDocumentDetail(documentId: string): Promise<DocumentDetail> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/detail`),
      { headers: this.headers() },
    );
    return await this.json<DocumentDetail>(resp);
  }

  async listDocumentRuns(documentId: string): Promise<DocumentRunSummary[]> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/runs`),
      { headers: this.headers() },
    );
    const data = await this.json<{ runs?: DocumentRunSummary[] }>(resp);
    return data.runs ?? [];
  }

  async listDocumentSnapshots(
    documentId: string,
  ): Promise<DocumentSnapshotSummary[]> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/snapshots`),
      { headers: this.headers() },
    );
    // 503 → snapshot service not wired in this deployment. Treat as
    // "no snapshot detail available" and let the caller render
    // gracefully (the Candidate Knowledge section already handles
    // empty snapshot data).
    if (resp.status === 503) return [];
    const data = await this.json<{
      snapshots?: DocumentSnapshotSummary[];
    }>(resp);
    return data.snapshots ?? [];
  }

  async attachDocument(documentId: string): Promise<DocumentLifecycleResponse> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/attach`),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<DocumentLifecycleResponse>(resp);
  }

  async detachDocument(documentId: string): Promise<DocumentLifecycleResponse> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/detach`),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<DocumentLifecycleResponse>(resp);
  }

  async removeDocument(documentId: string): Promise<DocumentLifecycleResponse> {
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/remove`),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<DocumentLifecycleResponse>(resp);
  }

  async reindexDocument(
    documentId: string,
    selectedProfile?: ExecutionProfileId,
  ): Promise<DocumentReindexResponse> {
    const headers: Record<string, string> = { ...this.headers() };
    let init: RequestInit = { method: "POST", headers };
    if (selectedProfile) {
      headers["Content-Type"] = "application/json";
      init = {
        ...init,
        body: JSON.stringify({ selectedProfile }),
      };
    }
    const resp = await fetch(
      this.url(`/documents/${encodeURIComponent(documentId)}/reindex`),
      init,
    );
    return await this.json<DocumentReindexResponse>(resp);
  }

  async refreshRunEnrichment(
    runId: string,
  ): Promise<RunRefreshEnrichmentResponse> {
    const resp = await fetch(
      this.url(
        `/ingestion-runs/${encodeURIComponent(runId)}/refresh-enrichment`,
      ),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<RunRefreshEnrichmentResponse>(resp);
  }

  async runDomainEnrichment(
    documentId: string,
  ): Promise<RunDomainEnrichmentResponse> {
    const resp = await fetch(
      this.url(
        `/documents/${encodeURIComponent(documentId)}`
        + `/manual-actions/run-domain-enrichment`,
      ),
      { method: "POST", headers: this.headers() },
    );
    return await this.json<RunDomainEnrichmentResponse>(resp);
  }
}
