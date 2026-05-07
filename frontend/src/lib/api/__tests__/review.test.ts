/**
 * Tests for the Phase 7 review surface in the live ApiClient and the
 * `runSummaryFromApi` / `qualityReportFromApi` translators.
 *
 * Parallel to `api-client.test.ts`'s style: stub `fetch`, verify the
 * URL + headers + envelope unwrapping for the new endpoints, and
 * verify the translator's defensive normalisation independently.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api-client";
import { ApiError } from "../client";
import {
  artifactPageFromApi,
  chunkDetailFromApi,
  chunkPageFromApi,
  graphSnapshotFromApi,
  parseEtag,
  parseFilename,
  qualityReportFromApi,
  runSummaryFromApi,
} from "../translate";

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

function makeClient() {
  return new ApiClient({
    baseUrl: "https://api.test.j1.local",
    getCtx: () => ({ tenant: "acme", project: "alpha" }),
    getAuth: () => ({ kind: "bearer", value: "tok-123" }),
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

// ---- ApiClient: getRunSummary --------------------------------------

describe("ApiClient.getRunSummary", () => {
  it("hits /summary with tenant + project + auth headers", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          runId: "run-1",
          status: "succeeded",
          documentIds: ["doc-A"],
          steps: [],
          artifactCounts: {},
          totalBytes: 0,
          warnings: [],
          availableViews: {
            chunks: { available: false },
            assets: { available: false },
            graph: { available: false, reason: "skipped" },
            quality: { available: false },
            rawArtifacts: { available: false },
          },
        },
      }),
    );

    const summary = await makeClient().getRunSummary("run-1");

    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/summary",
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Authorization"]).toBe("Bearer tok-123");
    expect(summary.runId).toBe("run-1");
    expect(summary.status).toBe("succeeded");
    expect(summary.availableViews.graph.reason).toBe("skipped");
  });

  it("encodes the run id correctly for path-unsafe characters", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          runId: "weird/id",
          status: "succeeded",
          documentIds: [],
          steps: [],
          artifactCounts: {},
          totalBytes: 0,
          warnings: [],
          availableViews: {
            chunks: { available: false },
            assets: { available: false },
            graph: { available: false },
            quality: { available: false },
            rawArtifacts: { available: false },
          },
        },
      }),
    );
    await makeClient().getRunSummary("weird/id");
    expect(calls[0]!.url).toContain("/ingestion-runs/weird%2Fid/summary");
  });
});

// ---- ApiClient: getRunQualityReport --------------------------------

describe("ApiClient.getRunQualityReport", () => {
  it("hits /quality-report without includeRaw by default", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          overallConfidence: 0.7,
          modalityConfidences: [],
          warnings: [],
          skippedSteps: [],
          failedOptionalSteps: [],
          lowConfidenceFindings: [],
          rawDebug: null,
        },
      }),
    );
    await makeClient().getRunQualityReport("run-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/quality-report",
    );
  });

  it("appends ?includeRaw=true when the caller opts in", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          overallConfidence: null,
          modalityConfidences: [],
          warnings: [],
          skippedSteps: [],
          failedOptionalSteps: [],
          lowConfidenceFindings: [],
          rawDebug: { confidence_assessment: [] },
        },
      }),
    );
    const report = await makeClient().getRunQualityReport("run-1", {
      includeRaw: true,
    });
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/quality-report?includeRaw=true",
    );
    expect(report.rawDebug).toEqual({ confidence_assessment: [] });
  });
});

// ---- Translator: runSummaryFromApi ---------------------------------

describe("runSummaryFromApi", () => {
  it("normalises a fully-populated response", () => {
    const summary = runSummaryFromApi({
      runId: "r1",
      status: "succeeded",
      durationMs: 1234,
      documentIds: ["doc-A"],
      steps: [
        {
          step: "compile",
          status: "completed",
          required: true,
          source: "caller",
          durationMs: 500,
          artifactCount: 3,
          metadata: { engine: "vlm" },
        },
      ],
      artifactCounts: { chunk: 2 },
      totalBytes: 1024,
      warnings: [
        {
          code: "step.warning",
          message: "page 7 OCR low",
          severity: "warning",
          step: "EXTRACT_TABLES",
          page: 7,
        },
      ],
      qualitySummary: {
        overallConfidence: 0.81,
        warningCount: 1,
        lowConfidenceCount: 2,
      },
      availableViews: {
        chunks: { available: true, reason: null },
        assets: { available: true, reason: null },
        graph: { available: false, reason: "skipped by policy" },
        quality: { available: true, reason: null },
        rawArtifacts: { available: true, reason: null },
      },
    });

    expect(summary.runId).toBe("r1");
    expect(summary.status).toBe("succeeded");
    expect(summary.durationMs).toBe(1234);
    expect(summary.documentIds).toEqual(["doc-A"]);
    expect(summary.steps[0]!.step).toBe("compile");
    expect(summary.steps[0]!.metadata).toEqual({ engine: "vlm" });
    expect(summary.warnings[0]!.page).toBe(7);
    expect(summary.qualitySummary?.overallConfidence).toBe(0.81);
    expect(summary.availableViews.graph).toEqual({
      available: false,
      reason: "skipped by policy",
    });
    expect(summary.availableViews.rawArtifacts.available).toBe(true);
  });

  it("survives a sparse response with default-fills", () => {
    // Backend can legitimately return an empty-ish summary for a run
    // that produced nothing. The translator should not throw.
    const summary = runSummaryFromApi({
      runId: "r2",
      status: "succeeded",
      availableViews: {},
    });

    expect(summary.runId).toBe("r2");
    expect(summary.documentIds).toEqual([]);
    expect(summary.steps).toEqual([]);
    expect(summary.warnings).toEqual([]);
    expect(summary.totalBytes).toBe(0);
    expect(summary.artifactCounts).toEqual({});
    expect(summary.qualitySummary).toBeNull();
    // availableViews defaults to { available: false, reason: null }
    // for every missing key — keeps the FE tab gating safe.
    expect(summary.availableViews.chunks.available).toBe(false);
  });

  it("ignores malformed steps/warnings entries", () => {
    // An array with mixed valid/garbage entries should still produce
    // a usable response — defensive translator, not a strict schema.
    const summary = runSummaryFromApi({
      runId: "r3",
      status: "succeeded",
      steps: [{ step: "compile", status: "completed" }, "not-an-object"],
      warnings: [null, { code: "x", message: "ok", severity: "info" }],
      availableViews: {},
    });
    expect(summary.steps).toHaveLength(2);
    // The garbage entry coerces to defaults rather than throwing.
    expect(summary.steps[1]!.step).toBe("");
    expect(summary.warnings).toHaveLength(2);
    expect(summary.warnings[1]!.message).toBe("ok");
  });
});

// ---- Translator: qualityReportFromApi ------------------------------

describe("qualityReportFromApi", () => {
  it("normalises a fully-populated quality report", () => {
    const report = qualityReportFromApi({
      overallConfidence: 0.65,
      modalityConfidences: [
        { modality: "tables", confidence: 0.8, sampleCount: 5 },
        { modality: "ocr", confidence: 0.5 },
      ],
      warnings: [
        { code: "step.warning", message: "ocr low", severity: "warning",
          page: 9 },
      ],
      skippedSteps: [{ step: "graph", reason: "policy", policy: "policy" }],
      failedOptionalSteps: [
        { step: "enrich", reason: "vision down", errorType: "VisionUnavailable" },
      ],
      lowConfidenceFindings: [
        { score: 0.4, category: "low_confidence", page: 7,
          chunkId: "ch-3", artifactId: "art-1" },
      ],
      rawDebug: null,
    });

    expect(report.overallConfidence).toBe(0.65);
    expect(report.modalityConfidences[0]!.sampleCount).toBe(5);
    expect(report.modalityConfidences[1]!.sampleCount).toBeNull();
    expect(report.skippedSteps[0]!.policy).toBe("policy");
    expect(report.failedOptionalSteps[0]!.errorType).toBe("VisionUnavailable");
    expect(report.lowConfidenceFindings[0]!.chunkId).toBe("ch-3");
    expect(report.rawDebug).toBeNull();
  });

  it("survives a fully empty quality report", () => {
    const report = qualityReportFromApi({});
    expect(report.overallConfidence).toBeNull();
    expect(report.modalityConfidences).toEqual([]);
    expect(report.warnings).toEqual([]);
    expect(report.skippedSteps).toEqual([]);
    expect(report.failedOptionalSteps).toEqual([]);
    expect(report.lowConfidenceFindings).toEqual([]);
    expect(report.rawDebug).toBeNull();
  });

  it("preserves rawDebug when included", () => {
    const report = qualityReportFromApi({
      rawDebug: {
        confidence_assessment: [{ x: 1 }],
        consistency_findings: [],
      },
    });
    expect(report.rawDebug).toEqual({
      confidence_assessment: [{ x: 1 }],
      consistency_findings: [],
    });
  });
});

// ---- ApiClient: listRunChunks --------------------------------------

describe("ApiClient.listRunChunks", () => {
  it("hits /chunks with default query when no opts", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 1, pageSize: 50, total: 0 },
      }),
    );
    await makeClient().listRunChunks("run-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/chunks",
    );
  });

  it("encodes pagination + filters into the query string", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 2, pageSize: 10, total: 25 },
      }),
    );
    await makeClient().listRunChunks("run-1", {
      page: 2,
      pageSize: 10,
      minConfidence: 0.6,
      status: "approved",
    });
    expect(calls[0]!.url).toContain(
      "/ingestion-runs/run-1/chunks?page=2&pageSize=10&status=approved&minConfidence=0.6",
    );
  });

  it("encodes the run id correctly for path-unsafe characters", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 1, pageSize: 50, total: 0 },
      }),
    );
    await makeClient().listRunChunks("weird/id");
    expect(calls[0]!.url).toContain("/ingestion-runs/weird%2Fid/chunks");
  });

  it("propagates tenant + project + auth headers", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 1, pageSize: 50, total: 0 },
      }),
    );
    await makeClient().listRunChunks("run-1");
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-Id"]).toBe("acme");
    expect(headers["X-Project-Id"]).toBe("alpha");
    expect(headers["Authorization"]).toBe("Bearer tok-123");
  });
});

// ---- ApiClient: getRunChunk ----------------------------------------

describe("ApiClient.getRunChunk", () => {
  it("encodes both the run id and chunk id", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          chunkId: "ch-1",
          body: "body text",
          metadata: {},
          linkedAssets: [],
          lineage: {},
        },
      }),
    );
    await makeClient().getRunChunk("run-1", "ch/with/slash");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/chunks/ch%2Fwith%2Fslash",
    );
  });
});

// ---- Translator: chunkPageFromApi ----------------------------------

describe("chunkPageFromApi", () => {
  it("normalises a fully-populated page", () => {
    const page = chunkPageFromApi({
      items: [
        {
          chunkId: "ch-1",
          preview: "preview text",
          pageStart: 1,
          pageEnd: 1,
          section: "Intro",
          tokenCount: 42,
          confidence: 0.81,
          metadata: { status: "approved" },
          linkedAssets: [
            { artifactId: "tab-1", kind: "enriched.tables" },
            { artifactId: "img-1" },
          ],
          sourceArtifactId: "art-compile-1",
        },
      ],
      page: 2,
      pageSize: 25,
      total: 73,
    });
    expect(page.page).toBe(2);
    expect(page.pageSize).toBe(25);
    expect(page.total).toBe(73);
    expect(page.items[0]!.chunkId).toBe("ch-1");
    expect(page.items[0]!.tokenCount).toBe(42);
    expect(page.items[0]!.linkedAssets).toEqual([
      { artifactId: "tab-1", kind: "enriched.tables" },
      { artifactId: "img-1", kind: null },
    ]);
    expect(page.items[0]!.sourceArtifactId).toBe("art-compile-1");
    expect(page.items[0]!.metadata).toEqual({ status: "approved" });
  });

  it("survives sparse / missing fields with defaults", () => {
    const page = chunkPageFromApi({});
    expect(page.items).toEqual([]);
    expect(page.page).toBe(1);
    expect(page.pageSize).toBe(50);
    expect(page.total).toBe(0);
  });

  it("drops malformed linked-asset entries", () => {
    const page = chunkPageFromApi({
      items: [
        {
          chunkId: "x",
          preview: "p",
          linkedAssets: [
            { artifactId: "ok" },
            "not-a-dict",
            { kind: "missing-id" }, // dropped — no artifactId
          ],
        },
      ],
      page: 1,
      pageSize: 50,
      total: 1,
    });
    expect(page.items[0]!.linkedAssets).toEqual([
      { artifactId: "ok", kind: null },
    ]);
  });
});

// ---- Translator: chunkDetailFromApi --------------------------------

describe("chunkDetailFromApi", () => {
  it("normalises full body + lineage", () => {
    const detail = chunkDetailFromApi({
      chunkId: "ch-2",
      body: "the full body text",
      pageStart: 5,
      pageEnd: 5,
      section: "Section 3",
      title: "Subtitle",
      tokenCount: 120,
      confidence: 0.7,
      metadata: { status: "needs_review" },
      linkedAssets: [{ artifactId: "img-1", kind: "enriched.visuals" }],
      sourceArtifactId: "art-9",
      lineage: {
        documentIds: ["doc-A"],
        sourceArtifactId: "art-9",
        stage: "compile",
      },
    });
    expect(detail.body).toBe("the full body text");
    expect(detail.confidence).toBe(0.7);
    expect(detail.lineage).toEqual({
      documentIds: ["doc-A"],
      sourceArtifactId: "art-9",
      stage: "compile",
    });
    expect(detail.linkedAssets[0]!.artifactId).toBe("img-1");
  });

  it("survives a missing-body response", () => {
    const detail = chunkDetailFromApi({ chunkId: "ch-3" });
    expect(detail.chunkId).toBe("ch-3");
    expect(detail.body).toBe("");
    expect(detail.lineage).toEqual({});
  });
});

// ---- ApiClient: listRunArtifacts -----------------------------------

describe("ApiClient.listRunArtifacts", () => {
  it("hits /artifacts with no query when no opts", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 1, pageSize: 50, total: 0 },
      }),
    );
    await makeClient().listRunArtifacts("run-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/artifacts",
    );
  });

  it("encodes kind + pagination into the query string", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 2, pageSize: 25, total: 99 },
      }),
    );
    await makeClient().listRunArtifacts("run-1", {
      kind: "enriched.tables",
      page: 2,
      pageSize: 25,
    });
    expect(calls[0]!.url).toContain(
      "/ingestion-runs/run-1/artifacts?kind=enriched.tables&page=2&pageSize=25",
    );
  });

  it("encodes path-unsafe characters in the run id", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: { items: [], page: 1, pageSize: 50, total: 0 },
      }),
    );
    await makeClient().listRunArtifacts("weird/id");
    expect(calls[0]!.url).toContain("/ingestion-runs/weird%2Fid/artifacts");
  });
});

// ---- ApiClient: getRunArtifactContent ------------------------------

describe("ApiClient.getRunArtifactContent", () => {
  it("returns blob + content-type + filename + etag", async () => {
    const { calls } = withFetch(
      () =>
        new Response("the-bytes", {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "Content-Disposition": 'attachment; filename="abc.json"',
            ETag: '"sha256-deadbeef"',
          },
        }),
    );
    const result = await makeClient().getRunArtifactContent(
      "run-1",
      "art-1",
    );
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/artifacts/art-1/content",
    );
    expect(result.contentType).toBe("application/json");
    expect(result.filename).toBe("abc.json");
    expect(result.etag).toBe("sha256-deadbeef");
    // Blob is a real one — round-trip via text() to confirm.
    expect(await result.blob.text()).toBe("the-bytes");
  });

  it("falls back to default content-type when header is missing", async () => {
    withFetch(() => new Response("body"));
    const result = await makeClient().getRunArtifactContent("run-1", "art-1");
    // Real Response defaults Content-Type to text/plain;charset=UTF-8
    // for string bodies — what matters here is that the helper never
    // returns null for contentType.
    expect(typeof result.contentType).toBe("string");
    expect(result.contentType.length).toBeGreaterThan(0);
  });

  it("encodes both run + artifact id", async () => {
    const { calls } = withFetch(() => new Response("x", { status: 200 }));
    await makeClient().getRunArtifactContent("run/with/slash", "art/with/slash");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run%2Fwith%2Fslash/artifacts/art%2Fwith%2Fslash/content",
    );
  });

  it("surfaces 404 with envelope error message as ApiError", async () => {
    withFetch(() =>
      jsonResponse(
        { error: { code: "REVIEW_NOT_FOUND", message: "artifact missing" } },
        404,
      ),
    );
    await expect(
      makeClient().getRunArtifactContent("run-1", "missing"),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      message: "artifact missing",
    });
  });

  it("surfaces non-JSON 5xx body as a plain ApiError", async () => {
    withFetch(
      () => new Response("upstream timeout", { status: 504 }),
    );
    const err = await makeClient()
      .getRunArtifactContent("run-1", "x")
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(504);
    expect((err as ApiError).message).toContain("upstream timeout");
  });
});

// ---- Translator: artifactPageFromApi -------------------------------

describe("artifactPageFromApi", () => {
  it("normalises a fully-populated page", () => {
    const page = artifactPageFromApi({
      items: [
        {
          artifactId: "a1",
          kind: "chunk",
          location: "compiled/a1.json",
          contentHash: "sha256:abc",
          byteSize: 412,
          status: "succeeded",
          reviewStatus: "not_required",
          version: 1,
          createdAt: "2026-05-07T10:00:00Z",
          updatedAt: "2026-05-07T10:00:00Z",
          sourceDocumentIds: ["doc-A"],
          sourceArtifactIds: [],
          metadata: { run_id: "run-1" },
        },
      ],
      page: 1,
      pageSize: 25,
      total: 1,
    });
    expect(page.items[0]!.artifactId).toBe("a1");
    expect(page.items[0]!.byteSize).toBe(412);
    expect(page.items[0]!.sourceDocumentIds).toEqual(["doc-A"]);
    expect(page.items[0]!.metadata).toEqual({ run_id: "run-1" });
  });

  it("survives sparse / missing fields with defaults", () => {
    const page = artifactPageFromApi({});
    expect(page.items).toEqual([]);
    expect(page.page).toBe(1);
    expect(page.pageSize).toBe(50);
    expect(page.total).toBe(0);
  });
});

// ---- parseFilename / parseEtag ------------------------------------

describe("parseFilename", () => {
  it("extracts a quoted filename", () => {
    expect(
      parseFilename('attachment; filename="hello world.png"'),
    ).toBe("hello world.png");
  });

  it("extracts an unquoted filename", () => {
    expect(parseFilename("attachment; filename=plain.txt")).toBe("plain.txt");
  });

  it("returns null when the header is absent", () => {
    expect(parseFilename(null)).toBeNull();
  });

  it("returns null when the header has no filename token", () => {
    expect(parseFilename("inline")).toBeNull();
  });
});

describe("parseEtag", () => {
  it("strips surrounding quotes", () => {
    expect(parseEtag('"sha256-deadbeef"')).toBe("sha256-deadbeef");
  });

  it("returns the raw value when unquoted", () => {
    expect(parseEtag("W/foo")).toBe("W/foo");
  });

  it("returns null when the header is absent", () => {
    expect(parseEtag(null)).toBeNull();
  });
});

// ---- ApiClient: getRunGraph ----------------------------------------

describe("ApiClient.getRunGraph", () => {
  it("hits /graph with no query when no opts", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          stats: { entityCount: 0, relationCount: 0, sourceArtifactIds: [] },
          entities: [],
          relations: [],
          truncated: {
            entities: false,
            relations: false,
            limits: { maxNodes: 5000, maxEdges: 5000 },
          },
          unavailable: null,
        },
      }),
    );
    await makeClient().getRunGraph("run-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/graph",
    );
  });

  it("encodes maxNodes + maxEdges into the query string", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          stats: { entityCount: 0, relationCount: 0, sourceArtifactIds: [] },
          entities: [],
          relations: [],
          truncated: {
            entities: false,
            relations: false,
            limits: { maxNodes: 100, maxEdges: 200 },
          },
          unavailable: null,
        },
      }),
    );
    await makeClient().getRunGraph("run-1", {
      maxNodes: 100,
      maxEdges: 200,
    });
    expect(calls[0]!.url).toContain(
      "/ingestion-runs/run-1/graph?maxNodes=100&maxEdges=200",
    );
  });

  it("encodes path-unsafe characters in the run id", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        data: {
          stats: { entityCount: 0, relationCount: 0, sourceArtifactIds: [] },
          entities: [],
          relations: [],
          truncated: {
            entities: false,
            relations: false,
            limits: { maxNodes: 5000, maxEdges: 5000 },
          },
          unavailable: null,
        },
      }),
    );
    await makeClient().getRunGraph("weird/id");
    expect(calls[0]!.url).toContain("/ingestion-runs/weird%2Fid/graph");
  });
});

// ---- Translator: graphSnapshotFromApi ------------------------------

describe("graphSnapshotFromApi", () => {
  it("normalises a fully-populated snapshot", () => {
    const snap = graphSnapshotFromApi({
      stats: {
        entityCount: 2,
        relationCount: 1,
        sourceArtifactIds: ["art-graph"],
      },
      entities: [
        {
          id: "e1",
          label: "Alice",
          type: "PERSON",
          description: "Lead",
          sourceChunkIds: ["ch-1"],
          sourceArtifactIds: ["art-graph"],
          metadata: { rank: 1 },
        },
        {
          id: "e2",
          label: "Bob",
          type: null,
          description: null,
          sourceChunkIds: [],
          sourceArtifactIds: [],
          metadata: {},
        },
      ],
      relations: [
        {
          id: "r1",
          sourceEntityId: "e1",
          targetEntityId: "e2",
          label: "knows",
          type: null,
          description: "Alice knows Bob",
          weight: 0.85,
          sourceChunkIds: ["ch-1"],
          sourceArtifactIds: ["art-graph"],
          metadata: {},
        },
      ],
      truncated: {
        entities: false,
        relations: false,
        limits: { maxNodes: 5000, maxEdges: 5000 },
      },
      unavailable: null,
    });

    expect(snap.stats.entityCount).toBe(2);
    expect(snap.stats.relationCount).toBe(1);
    expect(snap.entities[0]!.label).toBe("Alice");
    expect(snap.entities[0]!.type).toBe("PERSON");
    expect(snap.entities[0]!.metadata).toEqual({ rank: 1 });
    expect(snap.entities[1]!.type).toBeNull();
    expect(snap.relations[0]!.weight).toBe(0.85);
    expect(snap.relations[0]!.sourceEntityId).toBe("e1");
    expect(snap.truncated.limits.maxNodes).toBe(5000);
    expect(snap.unavailable).toBeNull();
  });

  it("preserves the unavailable reason", () => {
    const snap = graphSnapshotFromApi({
      stats: { entityCount: 0, relationCount: 0, sourceArtifactIds: [] },
      entities: [],
      relations: [],
      truncated: {
        entities: false,
        relations: false,
        limits: { maxNodes: 5000, maxEdges: 5000 },
      },
      unavailable: { reason: "Graph generation was skipped by policy." },
    });
    expect(snap.unavailable).toEqual({
      reason: "Graph generation was skipped by policy.",
    });
  });

  it("propagates per-list truncation flags", () => {
    const snap = graphSnapshotFromApi({
      stats: {
        entityCount: 9999,
        relationCount: 50,
        sourceArtifactIds: ["a"],
      },
      entities: [{ id: "e1", label: "E1" }],
      relations: [],
      truncated: {
        entities: true,
        relations: false,
        limits: { maxNodes: 100, maxEdges: 5000 },
      },
      unavailable: null,
    });
    expect(snap.truncated.entities).toBe(true);
    expect(snap.truncated.relations).toBe(false);
    expect(snap.truncated.limits.maxNodes).toBe(100);
  });

  it("survives a sparse / missing-fields response", () => {
    const snap = graphSnapshotFromApi({});
    expect(snap.stats.entityCount).toBe(0);
    expect(snap.stats.relationCount).toBe(0);
    expect(snap.stats.sourceArtifactIds).toEqual([]);
    expect(snap.entities).toEqual([]);
    expect(snap.relations).toEqual([]);
    expect(snap.truncated.entities).toBe(false);
    expect(snap.truncated.limits.maxNodes).toBe(5000);
    expect(snap.unavailable).toBeNull();
  });

  it("falls back to id when label is missing", () => {
    const snap = graphSnapshotFromApi({
      stats: { entityCount: 1, relationCount: 0, sourceArtifactIds: [] },
      entities: [{ id: "only-id" }],
      relations: [],
      truncated: {
        entities: false,
        relations: false,
        limits: { maxNodes: 5000, maxEdges: 5000 },
      },
    });
    expect(snap.entities[0]!.label).toBe("only-id");
  });
});
