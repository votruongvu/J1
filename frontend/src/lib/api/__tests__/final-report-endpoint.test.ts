/**
 * API client wire-contract test for
 * `getRunFinalIngestionReport`.
 *
 * Verifies:
 * (a) the client hits `/ingestion-runs/{id}/final-ingestion-report`,
 * (b) the envelope is unwrapped so callers see the raw
 * `FinalIngestionReportResponse` payload,
 * (c) the `"unavailable"` sentinel is passed through verbatim so
 * the FE state machine renders the fallback branch (pre-
 * runs, in-flight runs).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api-client";


function withFetch(responder: (url: string) => Response): {
  calls: string[];
} {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      return responder(url);
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


function envelope(data: unknown): Response {
  return new Response(JSON.stringify({ requestId: "r1", data, meta: {} }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}


afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});


describe("getRunFinalIngestionReport", () => {
  it("calls /ingestion-runs/{id}/final-ingestion-report", async () => {
    const { calls } = withFetch(() =>
      envelope({
        runId: "run-1",
        documentId: "doc-1",
        documentName: "spec.pdf",
        status: "completed",
        unavailableReason: null,
        artifactId: "art-fir-1",
        report: {
          schema_version: "1.0",
          run_id: "run-1",
          final_status: "completed_with_enrichment",
          final_status_reason: "enrichment overlay produced",
          stages: [
            {
              stage_id: "compile",
              label: "Base compile",
              status: "succeeded",
            },
          ],
          compile_summary: {
            compile_engine: "mineru",
            chunks_count: 42,
            retry_count: 0,
          },
          enrichment_summary: {
            should_enrich: true,
            enrichment_status: "succeeded",
            policy: "auto",
            require_enrichment_success: false,
          },
          artifact_refs: {
            initial_execution_plan: "art-init-1",
          },
          warnings: [],
          errors: [],
          retry_counts: { compile: 0, enrichment: 0 },
        },
      }),
    );
    const resp = await makeClient().getRunFinalIngestionReport("run-1");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toBe(
      "https://api.test.j1.local/ingestion-runs/run-1/final-ingestion-report",
    );
    expect(resp.status).toBe("completed");
    expect(resp.report?.final_status).toBe("completed_with_enrichment");
    expect(resp.report?.compile_summary?.chunks_count).toBe(42);
  });

  it("passes through the unavailable sentinel for legacy runs", async () => {
    withFetch(() =>
      envelope({
        runId: "run-1",
        documentId: null,
        documentName: null,
        status: "unavailable",
        unavailableReason:
          "final_ingestion_report_not_available — this run predates the new aggregate",
        report: null,
      }),
    );
    const resp = await makeClient().getRunFinalIngestionReport("run-1");
    expect(resp.status).toBe("unavailable");
    expect(resp.report).toBeNull();
    expect(resp.unavailableReason).toContain(
      "final_ingestion_report_not_available",
    );
  });

  it("passes through the unavailable sentinel for malformed artifacts", async () => {
    withFetch(() =>
      envelope({
        runId: "run-1",
        status: "unavailable",
        unavailableReason:
          "final_ingestion_report artifact has an unexpected shape",
        report: null,
      }),
    );
    const resp = await makeClient().getRunFinalIngestionReport("run-1");
    expect(resp.status).toBe("unavailable");
    expect(resp.report).toBeNull();
  });
});
