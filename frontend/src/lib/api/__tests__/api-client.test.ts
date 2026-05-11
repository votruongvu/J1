/**
 * Guard tests for the live `ApiClient`. We verify the bits that
 * caused real integration bugs:
 *
 * 1. Every request carries `X-Tenant-Id`, `X-Project-Id`, and the
 * auth header for the configured scheme.
 * 2. Multipart upload sends a `file` field (matching the backend's
 * `UploadFile` parameter name) and DOES NOT inject a hard-coded
 * `compilerKind` form field that would override the deployment
 * default.
 * 3. The success envelope (`{ requestId, data, meta }`) is unwrapped
 * so callers see the raw `data` payload.
 * 4. Error envelopes (`{ error: { message } }`) surface as
 * `ApiError` with a useful message.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api-client";
import { ApiError } from "../client";

interface CapturedCall {
  url: string;
  init: RequestInit;
}

function withFetch(responder: (call: CapturedCall) => Response | Promise<Response>): {
  calls: CapturedCall[];
} {
  const calls: CapturedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const call = { url: String(input), init };
      calls.push(call);
      return responder(call);
    }),
  );
  return { calls };
}

function makeClient(
  overrides: Partial<{
    tenant: string;
    project: string;
    auth: { kind: "bearer" | "apiKey"; value: string };
  }> = {},
) {
  return new ApiClient({
    baseUrl: "https://api.test.j1.local",
    getCtx: () => ({
      tenant: overrides.tenant ?? "acme",
      project: overrides.project ?? "alpha",
    }),
    getAuth: () => overrides.auth ?? { kind: "bearer", value: "tok-123" },
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ApiClient header injection", () => {
  it("sends Tenant + Project + Bearer on JSON GETs", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          runId: "run-1",
          documentId: "doc-1",
          workflowId: "run-1",
          status: "running",
          startedAt: "2026-05-01T00:00:00Z",
          updatedAt: "2026-05-01T00:00:01Z",
          progressPercent: 12,
          warningCount: 0,
        },
      }),
    );

    const run = await makeClient().getRun("run-1");

    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("https://api.test.j1.local/ingestion-runs/run-1");
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Authorization"]).toBe("Bearer tok-123");
    expect(headers["X-API-Key"]).toBeUndefined();
    expect(run.runId).toBe("run-1");
    expect(run.status).toBe("RUNNING");
    expect(run.progress_pct).toBe(12);
  });

  it("sends X-API-Key when the auth scheme is apiKey", async () => {
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r", events: [] } }));
    await makeClient({ auth: { kind: "apiKey", value: "sk_abc" } }).getEvents("r");
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-API-Key"]).toBe("sk_abc");
    expect(headers["Authorization"]).toBeUndefined();
  });

  it("includes Last-Event-Id when caller passes a resume cursor", async () => {
    const { calls } = withFetch(
      () =>
        new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    const handle = makeClient().openStream("run-1", { lastEventId: "evt-42" });
    // Allow the microtask that issues the fetch to run.
    await Promise.resolve();
    await Promise.resolve();
    handle.close();
    expect(calls[0]!.url).toBe("https://api.test.j1.local/ingestion-runs/run-1/events/stream");
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Last-Event-Id"]).toBe("evt-42");
    expect(headers["Accept"]).toBe("text/event-stream");
  });
});

describe("ApiClient.upload", () => {
  it("posts multipart with a `file` field and Tenant/Project headers", async () => {
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "run-new" } }, 201));

    const file = new File(["hello"], "report.pdf", { type: "application/pdf" });
    const out = await makeClient().upload(file, { tenant: "acme", project: "alpha" });

    expect(out).toEqual({ runId: "run-new" });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.init.method).toBe("POST");

    const body = calls[0]!.init.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    const fileEntry = body.get("file");
    expect(fileEntry).toBeInstanceOf(Blob);
    expect((fileEntry as File).name).toBe("report.pdf");

    // The frontend must NOT hard-code a compilerKind — the backend
    // resolves the deployment default. Sending one would silently
    // override that default and either swap compilers or 400.
    expect(body.get("compilerKind")).toBeNull();
    expect(body.get("policy")).toBeNull();

    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Authorization"]).toBe("Bearer tok-123");
    // `fetch` must compute its own multipart boundary; we never set
    // Content-Type on a FormData POST.
    expect(headers["Content-Type"]).toBeUndefined();
  });

  it("rejects with a 400 ApiError when tenant/project are missing", async () => {
    withFetch(() => jsonResponse({}, 200));
    const client = makeClient({ tenant: "" });
    await expect(
      client.upload(new File([""], "x.txt"), { tenant: "", project: "alpha" }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("ApiClient.listRuns", () => {
  it("forwards page/pageSize/q to the query string and translates items", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          items: [
            {
              runId: "r1",
              documentId: "d1",
              documentName: "earnings.pdf",
              status: "running",
              startedAt: "2026-05-01T00:00:00Z",
              updatedAt: "2026-05-01T00:00:01Z",
              progressPercent: 30,
              warningCount: 0,
            },
          ],
          page: 2,
          pageSize: 10,
          total: 42,
        },
      }),
    );
    const result = await makeClient().listRuns(
      { tenant: "acme", project: "alpha" },
      { page: 2, pageSize: 10, q: "earnings" },
    );
    expect(calls).toHaveLength(1);
    const url = new URL(calls[0]!.url);
    expect(url.pathname).toBe("/ingestion-runs");
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("pageSize")).toBe("10");
    expect(url.searchParams.get("q")).toBe("earnings");
    expect(result.total).toBe(42);
    expect(result.items).toHaveLength(1);
    expect(result.items[0]!.documentName).toBe("earnings.pdf");
    expect(result.items[0]!.status).toBe("RUNNING");
  });

  it("appends ?status=<lowercase> for the single-status filter", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { items: [], page: 1, pageSize: 20, total: 0 } }),
    );
    await makeClient().listRuns({ tenant: "acme", project: "alpha" }, { status: "RUNNING" });
    const url = new URL(calls[0]!.url);
    // Backend's RunStatus is lowercase on the wire (StrEnum). The
    // FE filter dropdown surfaces UPPERCASE values; the api-client
    // lowercases on the way out.
    expect(url.searchParams.get("status")).toBe("running");
  });
});

describe("ApiClient envelope unwrap", () => {
  it("unwraps the success envelope's `data` field", async () => {
    withFetch(() =>
      jsonResponse({
        requestId: "r2",
        data: { runId: "x", events: [] },
        meta: {},
      }),
    );
    const events = await makeClient().getEvents("x");
    expect(events).toEqual([]);
  });

  it("surfaces the error envelope message as an ApiError", async () => {
    withFetch(() =>
      jsonResponse({ error: { code: "INVALID_ARGUMENT", message: "tenant required" } }, 400),
    );
    await expect(makeClient().getRun("x")).rejects.toMatchObject({
      status: 400,
      message: "tenant required",
    });
  });
});

describe("ApiClient run controls", () => {
  it("pauseRun POSTs to /ingestion-runs/{id}/pause and returns the control record", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r3",
        data: {
          runId: "run-1",
          action: "pause",
          status: "paused",
          stage: "COMPILE",
          message: "Pause requested.",
          updatedAt: "2026-05-01T00:00:00Z",
        },
      }),
    );
    const result = await makeClient().pauseRun("run-1");
    expect(result.status).toBe("paused");
    expect(result.action).toBe("pause");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/pause",
    );
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("resumeRun POSTs to /ingestion-runs/{id}/resume", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { runId: "r", action: "resume", status: "running" } }),
    );
    await makeClient().resumeRun("r");
    expect(calls[0]!.url).toBe("https://api.test.j1.local/ingestion-runs/r/resume");
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("cancelRun POSTs to /ingestion-runs/{id}/cancel", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { runId: "r", action: "cancel", status: "cancelling" } }),
    );
    const result = await makeClient().cancelRun("r");
    expect(result.status).toBe("cancelling");
    expect(calls[0]!.url).toBe("https://api.test.j1.local/ingestion-runs/r/cancel");
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("propagates 409 errors from invalid state transitions as ApiError", async () => {
    withFetch(() =>
      jsonResponse({
        error: {
          code: "INVALID_STATE",
          message: "cannot pause a terminal run (status=succeeded)",
        },
      }, 409),
    );
    await expect(makeClient().pauseRun("done")).rejects.toMatchObject({
      status: 409,
    });
  });

  it("encodes the runId so special characters round-trip safely", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { runId: "x", action: "pause", status: "paused" } }),
    );
    await makeClient().pauseRun("a/b c");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/a%2Fb%20c/pause",
    );
  });
});

describe("ApiClient.runManualTestQuery", () => {
  // Stable response payload used across the wire-shape assertions
  // below. Mirrors the live backend's CamelModel envelope verbatim.
  const _OK_PAYLOAD = {
    data: {
      requestId: "tq-abc",
      runId: "run-1",
      question: "hello",
      answer: "world",
      modeUsed: "knowledge_first",
      retrievedChunks: [
        {
          artifactId: "a-1",
          chunkId: "c-1",
          runId: "run-1",
          documentId: "doc-1",
          sourceLocation: "p.1",
          score: 0.9,
          preview: "demo",
        },
      ],
      citations: [
        {
          artifactId: "a-1",
          artifactType: "chunk",
          sourceDocumentId: "doc-1",
          sourceLocation: "p.1",
          chunkId: "c-1",
          runId: "run-1",
        },
      ],
      checks: [
        { name: "answer_non_empty", severity: "required", passed: true },
      ],
      validationStatus: "passed",
      evidenceFlags: { graphUsed: false, tablesUsed: false, imagesUsed: false },
      rawResponse: null,
    },
  };

  it("POSTs to the run-scoped test-query endpoint with tenant/project + auth headers", async () => {
    const { calls } = withFetch(() => jsonResponse(_OK_PAYLOAD));

    await makeClient().runManualTestQuery("run-1", {
      question: "hello",
      topK: 5,
      citationRequired: true,
    });

    expect(calls).toHaveLength(1);
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/test-query",
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Authorization"]).toBe("Bearer tok-123");
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("forwards request fields verbatim in the JSON body", async () => {
    const { calls } = withFetch(() => jsonResponse(_OK_PAYLOAD));
    await makeClient().runManualTestQuery("run-1", {
      question: "hello",
      topK: 7,
      mode: "knowledge_first",
      citationRequired: true,
      includeRaw: true,
    });
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body.question).toBe("hello");
    expect(body.topK).toBe(7);
    expect(body.mode).toBe("knowledge_first");
    expect(body.citationRequired).toBe(true);
    expect(body.includeRaw).toBe(true);
  });

  it("unwraps the envelope and returns server-derived chunkId/runId on citations", async () => {
    withFetch(() => jsonResponse(_OK_PAYLOAD));
    const out = await makeClient().runManualTestQuery("run-1", {
      question: "hello",
    });
    // Wire-shape regression — every field surfaces.
    expect(out.requestId).toBe("tq-abc");
    expect(out.runId).toBe("run-1");
    expect(out.validationStatus).toBe("passed");
    expect(out.checks).toHaveLength(1);
    expect(out.citations[0]!.chunkId).toBe("c-1");
    expect(out.citations[0]!.runId).toBe("run-1");
    expect(out.retrievedChunks[0]!.chunkId).toBe("c-1");
  });

  it("encodes the runId for safe URL transport", async () => {
    const { calls } = withFetch(() => jsonResponse(_OK_PAYLOAD));
    await makeClient().runManualTestQuery("a/b c", { question: "x" });
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/a%2Fb%20c/test-query",
    );
  });

  it("surfaces 4xx error envelopes as ApiError", async () => {
    withFetch(() =>
      jsonResponse({ error: { code: "REVIEW_NOT_FOUND", message: "missing" } }, 404),
    );
    await expect(
      makeClient().runManualTestQuery("ghost", { question: "x" }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("ApiClient validation set methods ", () => {
  const _SET_PAYLOAD = {
    data: {
      validationSetId: "vs-1",
      runId: "run-1",
      documentIds: ["doc-1"],
      source: "generated",
      status: "draft",
      createdAt: "2026-05-07T10:00:00Z",
      createdBy: null,
      generatorVersion: "v1",
      artifactsContentHash: "sha256:abcd",
      testCases: [],
      metadata: {},
    },
  };

  const _RUN_PAYLOAD = {
    data: {
      validationRunId: "vrun-1",
      validationSetId: "vs-1",
      runId: "run-1",
      executionStatus: "completed",
      validationStatus: "passed",
      startedAt: "2026-05-07T10:00:00Z",
      completedAt: "2026-05-07T10:00:05Z",
      actor: "tester",
      summary: {
        total: 0, passed: 0, warning: 0, failed: 0, skipped: 0,
        coverage: { byType: {}, byPriority: {}, bySection: {} },
        mainIssues: [],
        recommendedAction: "ready",
      },
      results: [],
      failureMessage: null,
      metadata: {},
    },
  };

  it("POSTs generate to the run-scoped path with auth headers", async () => {
    const { calls } = withFetch(() => jsonResponse(_SET_PAYLOAD, 201));
    await makeClient().generateValidationSet("run-1", { force: true });

    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/validation-sets/generate",
    );
    expect(calls[0]!.init.method).toBe("POST");
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body.force).toBe(true);
  });

  it("listValidationSets unwraps the items array", async () => {
    withFetch(() =>
      jsonResponse({
        data: {
          items: [
            {
              validationSetId: "vs-1",
              runId: "run-1",
              source: "generated",
              status: "draft",
              createdAt: "2026-05-07T10:00:00Z",
              createdBy: null,
              caseCount: 3,
            },
          ],
        },
      }),
    );
    const items = await makeClient().listValidationSets("run-1");
    expect(items).toHaveLength(1);
    expect(items[0]!.validationSetId).toBe("vs-1");
    expect(items[0]!.caseCount).toBe(3);
  });

  it("getValidationSet returns the full set", async () => {
    withFetch(() => jsonResponse(_SET_PAYLOAD));
    const out = await makeClient().getValidationSet("run-1", "vs-1");
    expect(out.validationSetId).toBe("vs-1");
    expect(out.source).toBe("generated");
  });

  it("runValidation POSTs the validationSetId in the body", async () => {
    const { calls } = withFetch(() => jsonResponse(_RUN_PAYLOAD, 201));
    await makeClient().runValidation("run-1", { validationSetId: "vs-1" });
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/validation-runs",
    );
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body.validationSetId).toBe("vs-1");
  });

  it("runValidation returns the terminal snapshot with split status", async () => {
    withFetch(() =>
      jsonResponse({
        data: {
          ..._RUN_PAYLOAD.data,
          executionStatus: "completed",
          validationStatus: "failed",
        },
      }, 201),
    );
    const out = await makeClient().runValidation("run-1", {
      validationSetId: "vs-1",
    });
    // Critical: split status must round-trip end-to-end so the FE
    // can render `validationStatus=failed` even when HTTP=201.
    expect(out.executionStatus).toBe("completed");
    expect(out.validationStatus).toBe("failed");
  });

  it("listValidationRuns unwraps items + getValidationRun returns full payload", async () => {
    withFetch(() =>
      jsonResponse({
        data: {
          items: [
            {
              validationRunId: "vrun-1",
              validationSetId: "vs-1",
              runId: "run-1",
              executionStatus: "completed",
              validationStatus: "passed",
              startedAt: "2026-05-07T10:00:00Z",
              completedAt: "2026-05-07T10:00:05Z",
              summary: {
                total: 0, passed: 0, warning: 0, failed: 0, skipped: 0,
                coverage: { byType: {}, byPriority: {}, bySection: {} },
                mainIssues: [], recommendedAction: "ready",
              },
            },
          ],
        },
      }),
    );
    const items = await makeClient().listValidationRuns("run-1");
    expect(items).toHaveLength(1);
    expect(items[0]!.validationRunId).toBe("vrun-1");
  });

  it("404 envelopes from validation endpoints surface as ApiError", async () => {
    withFetch(() =>
      jsonResponse({ error: { code: "REVIEW_NOT_FOUND", message: "missing" } }, 404),
    );
    await expect(
      makeClient().getValidationSet("run-1", "vs-ghost"),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("ApiClient tester verdict + report ", () => {
  const _RUN_PAYLOAD = {
    data: {
      validationRunId: "vrun-1",
      validationSetId: "vs-1",
      runId: "run-1",
      executionStatus: "completed",
      validationStatus: "failed",
      startedAt: "2026-05-07T10:00:00Z",
      completedAt: "2026-05-07T10:00:05Z",
      actor: "tester",
      summary: {
        total: 1, passed: 0, warning: 0, failed: 1, skipped: 0,
        coverage: { byType: {}, byPriority: {}, bySection: {} },
        mainIssues: [], recommendedAction: "block release until resolved",
      },
      results: [
        {
          resultId: "vr-1",
          testCaseId: "tc-1",
          status: "failed",
          question: "?",
          answer: "",
          retrievedChunks: [],
          citations: [],
          checks: [],
          testerVerdict: "pass",
          testerNotes: "ok",
        },
      ],
      failureMessage: null,
      metadata: {},
    },
  };

  it("recordTesterVerdict POSTs to the verdict path with JSON body", async () => {
    const { calls } = withFetch(() => jsonResponse(_RUN_PAYLOAD));
    await makeClient().recordTesterVerdict(
      "run-1", "vrun-1", "vr-1",
      { verdict: "pass", notes: "ok" },
    );
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/validation-runs/vrun-1/results/vr-1/verdict",
    );
    expect(calls[0]!.init.method).toBe("POST");
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body).toEqual({ verdict: "pass", notes: "ok" });
  });

  it("recordTesterVerdict returns the updated run with verdict surfaced", async () => {
    withFetch(() => jsonResponse(_RUN_PAYLOAD));
    const out = await makeClient().recordTesterVerdict(
      "run-1", "vrun-1", "vr-1",
      { verdict: "pass" },
    );
    // Critical wire-shape regression — the FE relies on the
    // response containing the updated verdict so it can
    // re-render without an extra GET.
    expect(out.results[0]!.testerVerdict).toBe("pass");
    // Auto status must NOT change on a verdict POST.
    expect(out.results[0]!.status).toBe("failed");
  });

  it("downloadValidationReport returns text body + filename + mediaType", async () => {
    const markdownBody = "# Validation Report\n";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(markdownBody, {
          status: 200,
          headers: {
            "Content-Type": "text/markdown",
            "Content-Disposition": 'attachment; filename="validation-vrun-1.md"',
          },
        }),
      ),
    );
    const out = await makeClient().downloadValidationReport(
      "run-1", "vrun-1", "markdown",
    );
    expect(out.content).toBe(markdownBody);
    expect(out.mediaType).toBe("text/markdown");
    expect(out.filename).toBe("validation-vrun-1.md");
  });

  it("downloadValidationReport surfaces 4xx envelopes as ApiError", async () => {
    withFetch(() =>
      jsonResponse({ error: { code: "REVIEW_NOT_FOUND", message: "missing" } }, 404),
    );
    await expect(
      makeClient().downloadValidationReport("run-1", "vrun-ghost"),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("downloadValidationReport falls back to a sensible filename if the header is absent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response("# x", {
          status: 200,
          headers: { "Content-Type": "text/markdown" },
        }),
      ),
    );
    const out = await makeClient().downloadValidationReport(
      "run-1", "vrun-1",
    );
    // Defensive default: the FE should never render an empty
    // download filename. Locked here so a producer that drops the
    // header doesn't silently break the download UX.
    expect(out.filename).toBe("validation-vrun-1.md");
  });
});
