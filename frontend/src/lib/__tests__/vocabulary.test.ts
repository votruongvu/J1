/**
 * FE vocabulary guard.
 *
 * Pins that no legacy gating / split-mode language appears in
 * user-facing strings. The check inspects the runtime maps in
 * `display.ts` + the new panels' source code so a future copy
 * change is caught at CI rather than discovered in screenshots.
 *
 * Intentional deprecation comments mentioning the legacy planner
 * ("IngestPlanner is gone") in non-rendered comments are allowed
 * — the regex matches strings in source positions where the FE
 * would render the text (data values, panel copy).
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { EventTypeDisplay, StatusDisplay } from "@/lib/display";


// ---- 1. Runtime display maps must not carry stale wording --------


describe("StatusDisplay + EventTypeDisplay", () => {
  it("no display label contains 'split mode'", () => {
    for (const [k, meta] of Object.entries(StatusDisplay)) {
      expect(
        meta.label.toLowerCase(),
        `StatusDisplay[${k}].label contains split-mode wording`,
      ).not.toContain("split mode");
      expect(meta.label.toLowerCase()).not.toContain("split_mode");
    }
    for (const [k, label] of Object.entries(EventTypeDisplay)) {
      expect(
        label.toLowerCase(),
        `EventTypeDisplay[${k}] contains split-mode wording`,
      ).not.toContain("split mode");
    }
  });

  it("no display label calls out 'graph gating' or 'index gating'", () => {
    for (const meta of Object.values(StatusDisplay)) {
      expect(meta.label.toLowerCase()).not.toContain("graph gating");
      expect(meta.label.toLowerCase()).not.toContain("index gating");
    }
    for (const label of Object.values(EventTypeDisplay)) {
      expect(label.toLowerCase()).not.toContain("graph gating");
      expect(label.toLowerCase()).not.toContain("index gating");
    }
  });
});


// ---- 2. New macro-event labels are present + business-friendly ---


describe("New macro-event labels (macro-event labels)", () => {
  it("renders 'Base compile' wording for compile macro events", () => {
    expect(EventTypeDisplay["compile.started"]).toContain("Base compile");
    expect(EventTypeDisplay["compile.completed"]).toContain("Base compile");
    expect(EventTypeDisplay["compile.failed"]).toContain("Base compile");
  });

  it("renders 'Compile quality analysis' for assess_enrichment events", () => {
    expect(EventTypeDisplay["assess_enrichment.started"]).toContain(
      "Compile quality analysis",
    );
    expect(EventTypeDisplay["assess_enrichment.completed"]).toContain(
      "Compile quality analysis",
    );
    expect(EventTypeDisplay["assess_enrichment.skipped"]).toContain(
      "Compile quality analysis",
    );
  });

  it("renders 'Domain enrichment' for enrich macro events", () => {
    expect(EventTypeDisplay["enrich.started"]).toContain("Domain enrichment");
    expect(EventTypeDisplay["enrich.completed"]).toContain("Domain enrichment");
    expect(EventTypeDisplay["enrich.failed"]).toContain("Domain enrichment");
    expect(EventTypeDisplay["enrich.skipped"]).toContain("Domain enrichment");
  });
});


// ---- 3. Panel source code is free of legacy gating jargon --------
//
// Reading the source from disk + asserting on the text catches
// rendered strings even where the static type system can't (e.g.
// JSX text nodes).

const _NEW_PANEL_PATHS = [
  "src/pages/run-detail/AssessmentPlanPanel.tsx",
  "src/pages/run-detail/CompileResultPanel.tsx",
  "src/pages/run-detail/EnrichmentResultPanel.tsx",
  "src/pages/run-detail/PrimaryStatusPanel.tsx",
];


function _readPanel(rel: string): string {
  // process.cwd during vitest run is the frontend directory.
  return readFileSync(join(process.cwd(), rel), "utf-8");
}


describe("New panel sources are free of legacy gating language", () => {
  it.each(_NEW_PANEL_PATHS)(
    "%s — no split_mode / split mode / SplitMode wording",
    (path) => {
      const src = _readPanel(path);
      const lower = src.toLowerCase();
      expect(lower).not.toContain("split mode");
      expect(lower).not.toContain("split_mode");
      expect(src).not.toContain("SplitMode");
    },
  );

  it.each(_NEW_PANEL_PATHS)(
    "%s — no 'graph gating' / 'index gating' / 'pre-compile gating' phrasing",
    (path) => {
      const src = _readPanel(path).toLowerCase();
      expect(src).not.toContain("graph gating");
      expect(src).not.toContain("index gating");
      expect(src).not.toContain("pre-compile gating");
      expect(src).not.toContain("pre_compile_gating");
    },
  );

  it.each(_NEW_PANEL_PATHS)(
    "%s — no 'enrich decision before compile' wording",
    (path) => {
      const src = _readPanel(path).toLowerCase();
      expect(src).not.toContain("enrich decision before compile");
      expect(src).not.toContain("pre-compile final decision");
    },
  );
});
