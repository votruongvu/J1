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

  it("forwards selectedProfile in the FormData when provided", async () => {
    // The whole point of the new two-step flow: the dialog's profile
    // choice must reach the backend. Without this field on the wire,
    // `minimum_queryable` becomes a UI fiction.
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r" } }));
    const file = new File(["x"], "x.txt");
    await makeClient().upload(
      file,
      { tenant: "acme", project: "alpha" },
      "minimum_queryable",
    );
    const body = calls[0]!.init.body as FormData;
    expect(body.get("selectedProfile")).toBe("minimum_queryable");
  });

  it("omits selectedProfile when no profile was selected", async () => {
    // Legacy callers (none today, but future automation) skip the
    // picker entirely and rely on the backend default. Pin so a
    // future refactor doesn't accidentally hard-code a profile.
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r" } }));
    const file = new File(["x"], "x.txt");
    await makeClient().upload(file, { tenant: "acme", project: "alpha" });
    const body = calls[0]!.init.body as FormData;
    expect(body.get("selectedProfile")).toBeNull();
  });

  it("forwards assessmentDecisionId in the FormData when provided", async () => {
    // Without this field, the backend recomputes the assessment
    // downstream and the FE-shown recommendation never reaches the
    // workflow. Pin so a regression silently demotes every run to
    // ``rebuilt_fallback``.
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r" } }));
    const file = new File(["x"], "x.txt");
    await makeClient().upload(
      file,
      { tenant: "acme", project: "alpha" },
      "standard",
      "ad-abc123",
    );
    const body = calls[0]!.init.body as FormData;
    expect(body.get("assessmentDecisionId")).toBe("ad-abc123");
    expect(body.get("selectedProfile")).toBe("standard");
  });

  it("does not call /advanced-assessment automatically on upload", async () => {
    // Hard regression: the default Index path is lightweight. The
    // LLM Advanced Assessment is an EXPLICIT operator action — the
    // upload endpoint must never trigger it as a side effect.
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r" } }));
    const file = new File(["x"], "x.txt");
    await makeClient().upload(file, { tenant: "acme", project: "alpha" });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).not.toContain("/advanced-assessment");
    expect(calls[0]!.url).not.toContain("/manual-actions");
  });

  it("omits assessmentDecisionId when null / undefined", async () => {
    // Either skipping the picker entirely (legacy) or hitting a
    // deployment without the decision store wired (the endpoint
    // returns ``assessmentDecisionId: null``). The form field must
    // be absent so the backend treats the run as decision-less.
    const { calls } = withFetch(() => jsonResponse({ data: { runId: "r" } }));
    const file = new File(["x"], "x.txt");
    await makeClient().upload(
      file,
      { tenant: "acme", project: "alpha" },
      "standard",
      null,
    );
    const body = calls[0]!.init.body as FormData;
    expect(body.get("assessmentDecisionId")).toBeNull();
  });
});

describe("ApiClient.getDocumentAssessmentPlan", () => {
  it("POSTs to /documents/{id}/assessment-plan and unwraps the envelope", async () => {
    const planPayload = {
      documentId: "doc-1",
      recommendedProfile: "advanced",
      availableProfiles: [
        {
          id: "minimum_queryable",
          label: "Minimum Queryable",
          queryable: true,
          expected_speed: "fast",
          expected_llm_usage: "none_or_minimal",
          graph_enabled: false,
          multimodal_processing: false,
          enrichment_enabled: false,
          domain_enrichment_enabled: false,
          validation_enabled: false,
          compile_lightrag_extraction: false,
        },
      ],
      reasons: ["Document contains tables."],
      assessment: { mode: "standard" },
      warnings: [],
    };
    const { calls } = withFetch(() =>
      jsonResponse({ data: planPayload }),
    );
    const out = await makeClient().getDocumentAssessmentPlan("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/assessment-plan",
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(out.recommendedProfile).toBe("advanced");
    expect(out.availableProfiles).toHaveLength(1);
    expect(out.reasons).toEqual(["Document contains tables."]);
  });

  it("URL-encodes the document id", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { documentId: "x" } }),
    );
    await makeClient().getDocumentAssessmentPlan("doc with spaces/and-slash");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc%20with%20spaces%2Fand-slash/assessment-plan",
    );
  });
});

describe("ApiClient.runAdvancedAssessment", () => {
  it("POSTs to /documents/{id}/advanced-assessment + returns the result", async () => {
    const payload = {
      data: {
        documentId: "doc-1",
        assessmentDecisionId: "ad-llm-1",
        result: {
          status: "ok",
          refusalReason: null,
          message: null,
          documentComplexity: "complex",
          recommendedProfile: "deep_knowledge_index",
          confidence: "medium",
          detectedSignals: { likely_tables: "likely" },
          recommendedNextSteps: ["run_domain_enrichment"],
          reasoningSummary: ["RFP-shaped"],
          warnings: ["LLM estimate from sampled content."],
        },
      },
    };
    const { calls } = withFetch(() => jsonResponse(payload));
    const out = await makeClient().runAdvancedAssessment("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/advanced-assessment",
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(out.assessmentDecisionId).toBe("ad-llm-1");
    expect(out.result.status).toBe("ok");
    expect(out.result.recommendedProfile).toBe("deep_knowledge_index");
  });

  it("returns the refusal payload verbatim when guardrails trip", async () => {
    // Server returns a structured refusal — NOT a 4xx. The FE
    // surfaces ``message`` to the user instead of throwing.
    const refusal = {
      data: {
        documentId: "doc-1",
        assessmentDecisionId: null,
        result: {
          status: "refused",
          refusalReason: "document_too_large",
          message:
            "This document is too large for Advanced Assessment. "
            + "Please choose a profile manually based on visible "
            + "document complexity.",
          documentComplexity: null,
          recommendedProfile: null,
          confidence: null,
          detectedSignals: {},
          recommendedNextSteps: [],
          reasoningSummary: [],
          warnings: ["This document is too large…"],
        },
      },
    };
    withFetch(() => jsonResponse(refusal));
    const out = await makeClient().runAdvancedAssessment("doc-1");
    expect(out.result.status).toBe("refused");
    expect(out.result.refusalReason).toBe("document_too_large");
    expect(out.assessmentDecisionId).toBeNull();
  });
});

describe("ApiClient.listDocumentManualActions", () => {
  it("GETs the manual-actions vocabulary for a document", async () => {
    const payload = {
      data: {
        documentId: "doc-1",
        actions: [
          {
            id: "run_llm_advanced_assessment",
            label: "Run Advanced Assessment",
            description: "...",
            costNote: "Uses one LLM call.",
            status: "available",
          },
          {
            id: "run_domain_enrichment",
            label: "Run Domain Enrichment",
            description: "...",
            costNote: "Multiple LLM calls.",
            status: "not_implemented",
          },
        ],
      },
    };
    const { calls } = withFetch(() => jsonResponse(payload));
    const out = await makeClient().listDocumentManualActions("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/manual-actions",
    );
    expect(calls[0]!.init.method).toBe("GET");
    expect(out.actions).toHaveLength(2);
    expect(out.actions[0]!.id).toBe("run_llm_advanced_assessment");
    expect(out.actions[1]!.status).toBe("not_implemented");
  });
});

describe("ApiClient.registerDocument", () => {
  it("posts multipart to /documents and returns the documentId", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ data: { documentId: "doc-new" } }, 201),
    );
    const file = new File(["x"], "report.pdf");
    const out = await makeClient().registerDocument(file, {
      tenant: "acme",
      project: "alpha",
    });
    expect(out.documentId).toBe("doc-new");
    expect(calls[0]!.url).toBe("https://api.test.j1.local/documents");
    expect(calls[0]!.init.method).toBe("POST");
    const body = calls[0]!.init.body as FormData;
    expect(body.get("file")).toBeInstanceOf(Blob);
  });

  it("rejects with a 400 ApiError when tenant/project are missing", async () => {
    withFetch(() => jsonResponse({}, 200));
    await expect(
      makeClient({ tenant: "" }).registerDocument(new File([""], "x.txt"), {
        tenant: "",
        project: "alpha",
      }),
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

  it("serialises a document_run scope verbatim (Run Detail wire shape)", async () => {
    // Run Detail's "Produced snapshot" choice sends document_run so
    // the server can resolve identity via the run store and bypass
    // active-snapshot eligibility. Pin the camelCase body shape —
    // a typo here would silently re-route to project-active rules.
    const { calls } = withFetch(() => jsonResponse(_OK_PAYLOAD));
    await makeClient().runManualTestQuery("run-1", {
      question: "is the candidate snapshot answering correctly?",
      scope: {
        type: "document_run",
        documentId: "doc-1",
        runId: "run-1",
      },
    });
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body.scope).toEqual({
      type: "document_run",
      documentId: "doc-1",
      runId: "run-1",
    });
  });
});


// ---- runQueryTrace (SmartQueryOrchestrator dev endpoint) ----

describe("ApiClient.runQueryTrace", () => {
  const _TRACE_PAYLOAD = {
    data: {
      final_status: "passed",
      answer: "demo",
      message: null,
      trace: {
        question: "q",
        normalized_question: "q",
        plan: {
          normalized_question: "q",
          intent: "stage_progression",
          anchors: ["60%"],
          requested_fields: ["deliverables"],
          answer_shape: "stage_by_stage_table",
          synthesis_mode: "project_structured",
          retrieval_jobs: [],
          required_groups: [],
          sufficiency: {
            min_required_groups: 3,
            min_total_blocks: 3,
            fail_when_no_candidates: true,
          },
          quality: {
            required_fields: [],
            answer_shape: "stage_by_stage_table",
            fail_on_refusal: true,
          },
          intent_confidence: 0.9,
          domain_id: "",
        },
        routes_executed: [],
        all_candidates: [],
        selected: [],
        dropped: [],
        groups_covered: ["60%"],
        groups_missing: [],
        llm_evidence: [],
        answer: "demo",
        citations: [],
        gate_results: [],
        final_status: "passed",
        duration_ms: 42,
      },
    },
  };

  it("POSTs to /dev/query-trace with question + run_id", async () => {
    const { calls } = withFetch(() => jsonResponse(_TRACE_PAYLOAD));
    await makeClient().runQueryTrace("run-1", "what is X?");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/dev/query-trace",
    );
    const body = JSON.parse(calls[0]!.init.body as string);
    expect(body).toEqual({ question: "what is X?", run_id: "run-1" });
  });

  it("unwraps the envelope and returns the trace payload verbatim", async () => {
    withFetch(() => jsonResponse(_TRACE_PAYLOAD));
    const out = await makeClient().runQueryTrace("run-1", "q");
    expect(out.final_status).toBe("passed");
    expect(out.trace.plan.intent).toBe("stage_progression");
    expect(out.trace.duration_ms).toBe(42);
  });

  it("surfaces a 503 (orchestrator not wired) as ApiError", async () => {
    withFetch(() =>
      jsonResponse(
        {
          error: {
            code: "HTTP_503",
            message: "smart_query_orchestrator not configured",
          },
        },
        503,
      ),
    );
    await expect(
      makeClient().runQueryTrace("run-1", "q"),
    ).rejects.toBeInstanceOf(ApiError);
  });
});


describe("ApiClient imported test cases ", () => {
  const _SET_PAYLOAD = {
    data: {
      documentId: "doc-1",
      importedAt: "2026-05-14T10:00:00Z",
      sourceFilename: "tests.csv",
      cases: [
        {
          testCaseId: "itc-1",
          question: "What is X?",
          expectedAnswer: null,
          expectedSources: [],
          testType: null,
          notes: null,
        },
      ],
    },
  };

  const _EXECUTION_PAYLOAD = {
    data: {
      documentId: "doc-1",
      executedAt: "2026-05-14T10:05:00Z",
      runId: "run-1",
      summary: {
        total: 1, answered: 1, withSources: 1, scopeIssues: 0,
        errors: 0, overall: "good",
      },
      results: [
        {
          testCaseId: "itc-1",
          question: "What is X?",
          status: "answered",
          hasSources: true,
          scopeOk: true,
          error: null,
          runId: "run-1",
        },
      ],
    },
  };

  it("importTestCases POSTs multipart to the document-scoped path", async () => {
    const { calls } = withFetch(() => jsonResponse(_SET_PAYLOAD, 201));
    const file = new File(["question\nWhat?\n"], "tests.csv", {
      type: "text/csv",
    });
    const set = await makeClient().importTestCases("doc-1", file);
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/imported-test-cases/import",
    );
    expect(calls[0]!.init.method).toBe("POST");
    // No Content-Type header — fetch + FormData set the boundary.
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["Content-Type"]).toBeUndefined();
    expect(set.documentId).toBe("doc-1");
    expect(set.cases).toHaveLength(1);
  });

  it("getImportedTestCases returns null on 404", async () => {
    withFetch(() =>
      jsonResponse(
        { error: { code: "HTTP_404", message: "missing" } }, 404,
      ),
    );
    const out = await makeClient().getImportedTestCases("doc-1");
    expect(out).toBeNull();
  });

  it("getImportedTestCases returns the set on 200", async () => {
    withFetch(() => jsonResponse(_SET_PAYLOAD));
    const out = await makeClient().getImportedTestCases("doc-1");
    expect(out?.cases[0]!.testCaseId).toBe("itc-1");
  });

  it("deleteImportedTestCases issues DELETE and tolerates 204", async () => {
    const { calls } = withFetch(() =>
      new Response(null, { status: 204 }),
    );
    await makeClient().deleteImportedTestCases("doc-1");
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/imported-test-cases",
    );
  });

  it("executeImportedTestCases POSTs to the execute path", async () => {
    const { calls } = withFetch(() => jsonResponse(_EXECUTION_PAYLOAD, 201));
    const execution = await makeClient().executeImportedTestCases("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/imported-test-cases/execute",
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(execution.summary.overall).toBe("good");
    expect(execution.results[0]!.status).toBe("answered");
  });

  it("getImportedTestCaseExecution returns null on 404", async () => {
    withFetch(() =>
      jsonResponse(
        { error: { code: "HTTP_404", message: "missing" } }, 404,
      ),
    );
    const out = await makeClient().getImportedTestCaseExecution("doc-1");
    expect(out).toBeNull();
  });

  it("getImportedTestCaseExecution returns the snapshot on 200", async () => {
    withFetch(() => jsonResponse(_EXECUTION_PAYLOAD));
    const out = await makeClient().getImportedTestCaseExecution("doc-1");
    expect(out?.summary.total).toBe(1);
    expect(out?.runId).toBe("run-1");
  });
});
