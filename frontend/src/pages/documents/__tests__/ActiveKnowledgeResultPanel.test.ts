/**
 * Contract — `ActiveKnowledgeResultPanel`.
 *
 * Pins the Document-Detail-side counterpart to Run Detail's
 * simplified Results panel:
 *
 *   * Tab keys are Overview / Enrichment / Knowledge Graph /
 *     Quality / Validation — the rich active-snapshot set.
 *   * Header copy makes the active-snapshot scope explicit so the
 *     operator doesn't confuse this with a one-off run inspection.
 *   * The DocumentDetail page renders the panel and the new
 *     "Active Knowledge Actions" section + Re-index hint.
 *
 * Tests here use source inspection + minimal SSR — none of the
 * leaf tabs need to render. The panel's runtime behavior reuses
 * the existing leaf-tab components which already have their own
 * tests.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";


function _readSrc(rel: string): string {
  return readFileSync(
    resolve(__dirname, "../../..", rel),
    "utf-8",
  );
}


describe("ActiveKnowledgeResultPanel — tab vocabulary", () => {
  const src = _readSrc(
    "pages/documents/ActiveKnowledgeResultPanel.tsx",
  );

  it("declares the five active-snapshot tab keys", () => {
    const match = src.match(/type ActiveKnowledgeTab =\s*([^;]+);/);
    const captured = match?.[1];
    expect(captured).toBeTruthy();
    const body = String(captured).replace(/\s+/g, " ");
    expect(body).toContain('"overview"');
    expect(body).toContain('"enrichment"');
    expect(body).toContain('"graph"');
    expect(body).toContain('"quality"');
    expect(body).toContain('"validation"');
    // The Chunks / Artifacts / Query Trace surfaces live on Run
    // Detail — Document Detail's panel must not duplicate them.
    expect(body).not.toContain('"chunks"');
    expect(body).not.toContain('"raw"');
    expect(body).not.toContain('"manual-trace"');
  });

  it("renders the active-snapshot helper banner", () => {
    expect(src).toContain(
      "This view reflects the document",
    );
    expect(src).toContain("active snapshot");
  });

  it("reuses the existing leaf tab components", () => {
    expect(src).toContain("AssetsTab");
    expect(src).toContain("GraphTab");
    expect(src).toContain("QualityTab");
    expect(src).toContain("ValidationTab");
    expect(src).toContain("OverviewTab");
  });

  it("declares the panel's test id", () => {
    expect(src).toContain(
      'data-testid="active-knowledge-result-panel"',
    );
  });
});


describe("DocumentDetail — section composition", () => {
  const src = _readSrc("pages/DocumentDetailPage.tsx");

  it("renders ActiveKnowledgeResultPanel (not the old ResultsSection)", () => {
    expect(src).toContain("ActiveKnowledgeResultPanel");
    // The page previously rendered Run Detail's ResultsSection
    // directly — that import + render must be gone.
    expect(src).not.toMatch(
      /import\s*{[^}]*ResultsSection[^}]*}\s*from\s*"\.\/run-detail\/results"/,
    );
  });

  it("renders the Active Knowledge Actions section with helper text", () => {
    expect(src).toContain("Active Knowledge Actions");
    expect(src).toContain(
      "These actions apply to the document",
    );
  });

  it("renders the Re-index clarifying hint", () => {
    expect(src).toContain(
      "Re-index creates a new knowledge snapshot",
    );
    expect(src).toContain(
      'data-testid="document-detail-reindex-hint"',
    );
  });

  it("keeps Knowledge Memory as a separate panel", () => {
    expect(src).toContain("KnowledgeMemoryStatusPanel");
  });
});
