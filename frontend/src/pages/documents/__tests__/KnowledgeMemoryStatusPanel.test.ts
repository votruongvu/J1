/**
 * Contract — KnowledgeMemoryStatusPanel (Phase 3B).
 *
 * Pins the small status section on Document Detail:
 *
 *   - Renders "Not built" copy when there's no active snapshot
 *     (pure synchronous render — no fetch).
 *   - Renders the loading state on initial mount with an active
 *     snapshot (pre-fetch SSR pin).
 *   - The exported pure `KnowledgeMemoryStatusBody(status)` helper
 *     renders the right copy for each of the five backend status
 *     values: `not_built`, `base_compile_only`,
 *     `updated_with_domain_insights`, `failed`, `unknown`.
 *   - Surfaces entry count + last trigger + last-built timestamp
 *     when present on the loaded status.
 *   - Never claims the persistent memory artifact drives query
 *     (Phase 4 work).
 *   - DocumentDetail imports the panel.
 *
 * Uses `renderToStaticMarkup` only — no DOM dependency. The
 * panel's effect-driven fetch path is exercised at runtime; the
 * effect logic is tiny (call client method, set state). Tests
 * pin the deterministic render shapes via the exported pure
 * helper.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import {
  KnowledgeMemoryStatusBody,
  KnowledgeMemoryStatusPanel,
} from "../KnowledgeMemoryStatusPanel";
import type {
  KnowledgeMemoryStatusResponse,
  KnowledgeMemoryStatusValue,
} from "@/types/execution-profile";


function _status(
  over: Partial<KnowledgeMemoryStatusResponse> = {},
): KnowledgeMemoryStatusResponse {
  return {
    status: "not_built",
    documentId: "doc-1",
    snapshotId: null,
    artifactId: null,
    entryCount: 0,
    includesDomainInsights: false,
    lastTrigger: null,
    lastBuiltAt: null,
    warnings: [],
    ...over,
  };
}


function _renderBody(status: KnowledgeMemoryStatusResponse): string {
  return renderToStaticMarkup(
    createElement(KnowledgeMemoryStatusBody, { status }),
  );
}


function _stubClient() {
  return {
    getDocumentKnowledgeMemoryStatus: () =>
      Promise.resolve(_status()),
  } as unknown as Parameters<typeof KnowledgeMemoryStatusPanel>[0]["client"];
}


// ---- Synchronous render paths (no DOM, no fetch) --------------


describe("KnowledgeMemoryStatusPanel — pre-snapshot + loading", () => {
  it("renders 'Not built' with hint when there is no active snapshot", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgeMemoryStatusPanel, {
        client: _stubClient(),
        documentId: "doc-1",
        activeSnapshotId: null,
      }),
    );
    expect(html).toContain("Knowledge Memory");
    expect(html).toContain('data-testid="kmem-status-not-built"');
    expect(html).toContain("Not built");
    expect(html).toContain("Knowledge Index is built after compile");
  });

  it("renders the loading state on initial mount with an active snapshot", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgeMemoryStatusPanel, {
        client: _stubClient(),
        documentId: "doc-1",
        activeSnapshotId: "snap-1",
      }),
    );
    expect(html).toContain('data-testid="kmem-status-loading"');
  });
});


// ---- Pure status-body renders (every backend status) ----------


describe("KnowledgeMemoryStatusBody — per-status copy", () => {
  it("renders 'Not built' for not_built status", () => {
    const html = _renderBody(_status({
      status: "not_built", snapshotId: "snap-1",
    }));
    expect(html).toContain('data-testid="kmem-status-not-built"');
    expect(html).toContain("Knowledge Index is ready and can be queried");
  });

  it("renders 'Built from base compile' for base_compile_only", () => {
    const html = _renderBody(_status({
      status: "base_compile_only",
      snapshotId: "snap-1",
      artifactId: "mem-1",
      entryCount: 12,
      lastTrigger: "after_compile",
      lastBuiltAt: "2026-05-16T12:00:00+00:00",
    }));
    expect(html).toContain('data-testid="kmem-status-base"');
    expect(html).toContain("Built from base compile");
    expect(html).toContain(
      "Domain-specific answers may improve after post-compile domain enrichment",
    );
    // Details block surfaces entry count + trigger + timestamp.
    expect(html).toContain('data-testid="kmem-status-details"');
    expect(html).toContain("12 entries");
    expect(html).toContain("automatic build after compile");
    expect(html).toContain("2026-05-16T12:00:00+00:00");
  });

  it("renders 'Updated with domain insights' for updated_with_domain_insights", () => {
    const html = _renderBody(_status({
      status: "updated_with_domain_insights",
      snapshotId: "snap-1",
      artifactId: "mem-2",
      entryCount: 42,
      includesDomainInsights: true,
      lastTrigger: "after_domain_enrichment",
      lastBuiltAt: "2026-05-16T12:30:00+00:00",
    }));
    expect(html).toContain('data-testid="kmem-status-updated"');
    expect(html).toContain("Updated with domain insights");
    expect(html).toContain("risks, requirements, validation checks");
    expect(html).toContain("42 entries");
    expect(html).toContain("automatic rebuild after domain enrichment");
  });

  it("renders manual trigger label when trigger=manual", () => {
    const html = _renderBody(_status({
      status: "base_compile_only",
      snapshotId: "snap-1",
      entryCount: 3,
      lastTrigger: "manual",
    }));
    expect(html).toContain("manual action");
  });

  it("renders 'Build failed' for failed status", () => {
    const html = _renderBody(_status({
      status: "failed",
      snapshotId: "snap-1",
    }));
    expect(html).toContain('data-testid="kmem-status-failed"');
    expect(html).toContain("Build failed");
    expect(html).toContain("retry building Knowledge Memory");
  });

  it("renders 'Status unknown' with warnings for unknown status", () => {
    const html = _renderBody(_status({
      status: "unknown",
      snapshotId: "snap-1",
      warnings: ["multiple_active_memory_artifacts:2"],
    }));
    expect(html).toContain('data-testid="kmem-status-unknown"');
    expect(html).toContain("currently unknown");
    expect(html).toContain("multiple_active_memory_artifacts:2");
  });

  it("singular 'entry' when entryCount is 1", () => {
    const html = _renderBody(_status({
      status: "base_compile_only",
      snapshotId: "snap-1",
      entryCount: 1,
    }));
    expect(html).toContain("1 entry");
    // And not the plural form.
    expect(html).not.toContain("1 entries");
  });

  it("omits details block when entry count + trigger + timestamp absent", () => {
    const html = _renderBody(_status({
      status: "base_compile_only",
      snapshotId: "snap-1",
      entryCount: 0,
      lastTrigger: null,
      lastBuiltAt: null,
    }));
    expect(html).not.toContain('data-testid="kmem-status-details"');
  });
});


// ---- Phase-4 query-claim hygiene -------------------------------


describe("KnowledgeMemoryStatusBody — never claims Phase-4 query behaviour", () => {
  const cases: KnowledgeMemoryStatusValue[] = [
    "not_built", "base_compile_only", "updated_with_domain_insights",
    "failed", "unknown",
  ];

  for (const status of cases) {
    it(`does not claim query already uses persistent memory: ${status}`, () => {
      const html = _renderBody(_status({
        status, snapshotId: "snap-1",
        warnings: status === "unknown" ? ["x"] : [],
      }));
      expect(html).not.toContain("query prefers Knowledge Memory");
      expect(html).not.toContain("query uses Knowledge Memory");
      expect(html).not.toContain("answers come from Knowledge Memory");
    });
  }
});


// ---- DocumentDetail integration -------------------------------


describe("DocumentDetailPage integration", () => {
  it("imports KnowledgeMemoryStatusPanel", async () => {
    const detailMod = await import("../../DocumentDetailPage");
    expect(detailMod.DocumentDetailPage).toBeDefined();
    const panelMod = await import("../KnowledgeMemoryStatusPanel");
    expect(panelMod.KnowledgeMemoryStatusPanel).toBeDefined();
    expect(panelMod.KnowledgeMemoryStatusBody).toBeDefined();
  });
});


// ---- EnrichmentResultPanel: memory-link note -----------------


describe("EnrichmentResultPanel — cross-links to Knowledge Memory", () => {
  it("renders the memory note that points users at Document Detail", async () => {
    // Source-level assertion: the panel includes the
    // memory-note copy. Avoids a heavy panel render (the panel
    // mounts the EnrichmentResultPanel, which fetches the run
    // payload — extra moving parts unrelated to this Phase 3B
    // change). Mirrors the source-grep style used elsewhere in
    // the FE test suite for cross-component facts.
    const fs = await import("node:fs");
    const path = await import("node:path");
    const source = fs.readFileSync(
      path.resolve(
        __dirname,
        "../../run-detail/EnrichmentResultPanel.tsx",
      ),
      "utf-8",
    );
    expect(source).toContain('data-testid="enrichment-result-memory-note"');
    // JSX wraps text across lines; grep for the two halves
    // separately so the assertion isn't brittle to formatting.
    expect(source).toContain("Knowledge Memory section on the");
    expect(source).toContain("Document Detail page shows whether memory");
  });
});
