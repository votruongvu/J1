/**
 * Guards for the document-centric `ApiClient` methods (Phase 7).
 *
 * Each test verifies the request shape (URL, method, headers) plus
 * the response unwrap (envelope → typed list/detail/lifecycle).
 * Combined with the backend's REST tests, this gives us end-to-end
 * coverage of the wire contract without a real network round-trip.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api-client";

interface CapturedCall {
  url: string;
  init: RequestInit;
}

function withFetch(
  responder: (call: CapturedCall) => Response | Promise<Response>,
): { calls: CapturedCall[] } {
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

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeClient() {
  return new ApiClient({
    baseUrl: "https://api.test.j1.local",
    getCtx: () => ({ tenant: "acme", project: "alpha" }),
    getAuth: () => ({ kind: "bearer", value: "tok-123" }),
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});


describe("listDocuments", () => {
  it("GETs /documents and unwraps the envelope", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          documents: [
            {
              documentId: "doc-1",
              displayName: "Bridge Report.pdf",
              knowledgeState: "attached",
              latestVersionId: null,
              createdAt: "2026-05-12T00:00:00Z",
              updatedAt: null,
              removedAt: null,
              currentResultSummary: {
                status: "succeeded",
                compileStatus: "completed",
                enrichmentStatus: "completed",
                validationStatus: "completed",
                failureCode: null,
              },
              availableActions: ["view", "reindex", "detach", "remove"],
              runHistorySummary: [],
            },
          ],
        },
      }),
    );
    const docs = await makeClient().listDocuments();
    expect(calls[0]!.url).toBe("https://api.test.j1.local/documents");
    expect(docs).toHaveLength(1);
    expect(docs[0]!.documentId).toBe("doc-1");
    expect(docs[0]!.knowledgeState).toBe("attached");
    expect(docs[0]!.availableActions).toContain("detach");
  });

  it("passes includeRemoved=true as a query param when requested", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ requestId: "r1", data: { documents: [] } }),
    );
    await makeClient().listDocuments({ includeRemoved: true });
    expect(calls[0]!.url).toContain("includeRemoved=true");
  });

  it("returns an empty array when the backend returns no documents key", async () => {
    withFetch(() => jsonResponse({ requestId: "r1", data: {} }));
    const docs = await makeClient().listDocuments();
    expect(docs).toEqual([]);
  });

  it("forwards the X-Tenant-Id + X-Project-Id headers", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ requestId: "r1", data: { documents: [] } }),
    );
    await makeClient().listDocuments();
    const headers = new Headers(calls[0]!.init.headers as HeadersInit);
    expect(headers.get("X-Tenant-Id")).toBe("acme");
    expect(headers.get("X-Project-Id")).toBe("alpha");
  });
});


describe("getDocumentDetail", () => {
  it("GETs /documents/{id}/detail and returns the typed detail", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          documentId: "doc-1",
          displayName: "Bridge Report.pdf",
          knowledgeState: "attached",
          latestVersionId: null,
          createdAt: "2026-05-12T00:00:00Z",
          updatedAt: null,
          removedAt: null,
          currentResultSummary: {
            status: "succeeded",
            compileStatus: "completed",
            enrichmentStatus: null,
            validationStatus: null,
            failureCode: null,
          },
          availableActions: ["view", "reindex", "detach", "remove"],
          runHistory: [
            {
              runId: "r-1",
              runType: "initial",
              status: "succeeded",
              startedAt: "2026-05-12T00:00:00Z",
              completedAt: "2026-05-12T00:01:00Z",
              failureCode: null,
              isActive: true,
            },
          ],
        },
      }),
    );
    const detail = await makeClient().getDocumentDetail("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/detail",
    );
    expect(detail.runHistory).toHaveLength(1);
    expect(detail.runHistory[0]!.isActive).toBe(true);
  });

  it("URI-encodes document_id with special characters", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({ requestId: "r1", data: {} }),
    );
    await makeClient().getDocumentDetail("doc/with slash").catch(() => {});
    expect(calls[0]!.url).toContain("doc%2Fwith%20slash");
  });
});


describe("listDocumentRuns", () => {
  it("GETs /documents/{id}/runs and unwraps the runs list", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          runs: [
            {
              runId: "r-2",
              runType: "reindex",
              status: "succeeded",
              startedAt: "2026-05-12T01:00:00Z",
              completedAt: null,
              failureCode: null,
              isActive: true,
            },
          ],
        },
      }),
    );
    const runs = await makeClient().listDocumentRuns("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/runs",
    );
    expect(runs).toHaveLength(1);
    expect(runs[0]!.runType).toBe("reindex");
  });

  it("returns an empty array when no runs key is present", async () => {
    withFetch(() => jsonResponse({ requestId: "r1", data: {} }));
    const runs = await makeClient().listDocumentRuns("doc-1");
    expect(runs).toEqual([]);
  });
});


describe("lifecycle actions (attach/detach/remove)", () => {
  function lifecycleResponse() {
    return {
      requestId: "r1",
      data: {
        documentId: "doc-1",
        knowledgeState: "detached",
        latestVersionId: null,
        removedAt: null,
        updatedAt: "2026-05-12T01:00:00Z",
      },
    };
  }

  it("POSTs /documents/{id}/attach", async () => {
    const { calls } = withFetch(() => jsonResponse(lifecycleResponse()));
    await makeClient().attachDocument("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/attach",
    );
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("POSTs /documents/{id}/detach", async () => {
    const { calls } = withFetch(() => jsonResponse(lifecycleResponse()));
    const r = await makeClient().detachDocument("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/detach",
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(r.knowledgeState).toBe("detached");
  });

  it("POSTs /documents/{id}/remove", async () => {
    const { calls } = withFetch(() => jsonResponse(lifecycleResponse()));
    await makeClient().removeDocument("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/remove",
    );
    expect(calls[0]!.init.method).toBe("POST");
  });

  it("surfaces 409 conflicts as ApiError", async () => {
    withFetch(() =>
      new Response(
        JSON.stringify({
          requestId: "r1",
          error: { message: "document has been removed; re-upload" },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(makeClient().attachDocument("doc-1")).rejects.toMatchObject({
      status: 409,
    });
  });
});


describe("reindexDocument", () => {
  it("POSTs /documents/{id}/reindex and returns the new run details", async () => {
    const { calls } = withFetch(() =>
      jsonResponse({
        requestId: "r1",
        data: {
          documentId: "doc-1",
          reindexRunId: "r-new",
          parentRunId: "r-prev",
          workflowId: "wf-r-new",
          runType: "reindex",
        },
      }),
    );
    const r = await makeClient().reindexDocument("doc-1");
    expect(calls[0]!.url).toBe(
      "https://api.test.j1.local/documents/doc-1/reindex",
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(r.reindexRunId).toBe("r-new");
    expect(r.parentRunId).toBe("r-prev");
    expect(r.runType).toBe("reindex");
  });

  it("surfaces 409 when document is detached", async () => {
    withFetch(() =>
      new Response(
        JSON.stringify({
          requestId: "r1",
          error: { message: "document 'doc-1' is detached" },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(makeClient().reindexDocument("doc-1")).rejects.toMatchObject({
      status: 409,
    });
  });
});
