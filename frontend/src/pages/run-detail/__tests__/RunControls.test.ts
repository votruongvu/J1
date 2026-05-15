/**
 * Static-markup tests for RunControls — focused on the
 * Manual-Actions-mode contract.
 *
 *   - When ``hideLegacyRefreshEnrich`` is true, the Refresh
 *     Enrichment button MUST NOT render even when the server
 *     capability says it could.
 *   - When the feature flag is off (or the override is false),
 *     the legacy button renders as before.
 *
 * We mock the feature-flag module so the tests don't depend on
 * Vite env state.
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


// The flag default + override switches at module-load time. We mock
// the export here and re-import RunControls in each test so the
// closure captures the right value.
async function _loadRunControlsWithFlag(hideLegacy: boolean) {
  vi.resetModules();
  vi.doMock("@/lib/constants/feature-flags", () => ({
    manualActionsEnabled: hideLegacy,
    hideLegacyRefreshEnrich: hideLegacy,
  }));
  return await import("../RunControls");
}


function _baseRun() {
  return {
    runId: "run-1",
    documentId: "doc-1",
    status: "succeeded",
    startedAt: new Date().toISOString(),
    completedAt: new Date().toISOString(),
    metadata: {},
  } as unknown as Parameters<
    Awaited<ReturnType<typeof _loadRunControlsWithFlag>>["RunControls"]
  >[0]["run"];
}


afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});


describe("RunControls — Manual Actions mode", () => {
  it("hides Refresh Enrichment when hideLegacyRefreshEnrich is true", async () => {
    const { RunControls } = await _loadRunControlsWithFlag(true);
    const html = renderToStaticMarkup(
      createElement(RunControls, {
        run: _baseRun(),
        capability: {
          isActive: true,
          isOnlyRun: false,
          canDeleteRun: true,
          // The server still says the capability is available …
          canRefreshEnrichment: true,
          canRunEnrichment: false,
        },
        onRefresh: () => {},
        pushToast: () => {},
      } as Parameters<typeof RunControls>[0]),
    );
    // … but the button MUST be suppressed at the FE because the
    // deployment runs in Manual Actions mode.
    expect(html).not.toContain("run-controls-refresh-enrichment");
    expect(html).not.toContain("Refresh enrichment for active snapshot");
  });

  it("renders Refresh Enrichment when the flag is off", async () => {
    const { RunControls } = await _loadRunControlsWithFlag(false);
    const html = renderToStaticMarkup(
      createElement(RunControls, {
        run: _baseRun(),
        capability: {
          isActive: true,
          isOnlyRun: false,
          canDeleteRun: true,
          canRefreshEnrichment: true,
          canRunEnrichment: false,
        },
        onRefresh: () => {},
        pushToast: () => {},
      } as Parameters<typeof RunControls>[0]),
    );
    // Legacy deployments still see the button — pinned so a
    // future cleanup doesn't accidentally drop the rollback path.
    expect(html).toContain("run-controls-refresh-enrichment");
  });
});
