/**
 * Static-markup tests for RunControls — focused on the canonical
 * post-Phase-4 contract.
 *
 *   - The legacy "Refresh Enrichment" / "Run Enrichment" buttons
 *     are permanently retired. The canonical surface is the
 *     Manual Actions panel on Document Detail.
 *   - Capability flags the server still emits
 *     (``canRefreshEnrichment``, ``canRunEnrichment``) are ignored
 *     by the FE; this test pins that they DO NOT render anywhere
 *     regardless of value.
 */

import { describe, expect, it, vi, afterEach } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

vi.mock("@/lib/hooks/useClient", () => ({
  useClient: () => ({
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
    cancelRun: vi.fn(),
    cleanUpRun: vi.fn(),
    refreshRunEnrichment: vi.fn(),
    runDocumentEnrichment: vi.fn(),
  }),
}));


function _baseRun() {
  return {
    runId: "run-1",
    documentId: "doc-1",
    status: "succeeded",
    startedAt: new Date().toISOString(),
    completedAt: new Date().toISOString(),
    metadata: {},
  } as unknown as Parameters<
    typeof import("../RunControls")["RunControls"]
  >[0]["run"];
}


afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});


describe("RunControls — retired legacy enrichment buttons", () => {
  it("never renders Refresh Enrichment, even when the server says canRefreshEnrichment=true", async () => {
    const { RunControls } = await import("../RunControls");
    const html = renderToStaticMarkup(
      createElement(RunControls, {
        run: _baseRun(),
        capability: {
          isActive: true,
          isOnlyRun: false,
          canDeleteRun: true,
          // The server still emits these — they're ignored by the
          // FE. The Manual Actions panel on Document Detail is the
          // canonical surface.
          canRefreshEnrichment: true,
          canRunEnrichment: true,
        },
        onRefresh: () => {},
        pushToast: () => {},
      } as Parameters<typeof RunControls>[0]),
    );
    expect(html).not.toContain("run-controls-refresh-enrichment");
    expect(html).not.toContain("Refresh enrichment for active snapshot");
    expect(html).not.toContain("run-controls-run-enrichment");
    expect(html).not.toContain("Run enrichment");
  });
});
