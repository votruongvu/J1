/**
 * Contract — Run Detail simplification.
 *
 * After moving active-snapshot concerns to `ActiveKnowledgeResultPanel`
 * on Document Detail, Run Detail's Results section is reduced to the
 * four execution-focused tabs the spec calls out:
 *
 *   Overview · Chunks · Artifacts · Query Trace
 *
 * The OverviewTab's Pipeline Steps table also strips legacy
 * "disabled by selected execution profile" rows that mislead the
 * operator into thinking the run was incomplete.
 *
 * These tests pin both contracts so future changes can't silently
 * re-introduce the dropped surfaces.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { summariseDisabledReason } from "../index";
import { filterPrimaryRunSteps } from "../OverviewTab";
import type { ReviewStepResult } from "@/types/review";


function _readSrc(rel: string): string {
  return readFileSync(
    resolve(__dirname, "../../../..", rel),
    "utf-8",
  );
}


describe("Run Detail Results tabs — simplified four-tab set", () => {
  const src = _readSrc("pages/run-detail/results/index.tsx");

  it("declares only the four execution-focused tab keys", () => {
    // The union type at the top of the file pins the tab keys —
    // any future addition has to extend it, which makes this a
    // useful invariant target.
    const match = src.match(/type ResultsTab =\s*([^;]+);/);
    const captured = match?.[1];
    expect(captured).toBeTruthy();
    const body = String(captured).replace(/\s+/g, " ");
    expect(body).toContain('"overview"');
    expect(body).toContain('"chunks"');
    expect(body).toContain('"raw"');
    expect(body).toContain('"manual-trace"');
    // Dropped tab keys must not reappear.
    expect(body).not.toContain('"assets"');
    expect(body).not.toContain('"graph"');
    expect(body).not.toContain('"quality"');
    expect(body).not.toContain('"validation"');
  });

  it("renders Chunks / Artifacts / Query Trace tab labels", () => {
    expect(src).toMatch(/label:\s*"Chunks"/);
    expect(src).toMatch(/label:\s*"Artifacts"/);
    expect(src).toMatch(/label:\s*"Query Trace"/);
  });

  it("does NOT render Enrichment / Knowledge Graph / Quality / Validation labels", () => {
    // Note: "Validation" appears as a tab in the SISTER component
    // `ActiveKnowledgeResultPanel`, but inside Run Detail's
    // results/index.tsx the four dropped labels must be gone.
    expect(src).not.toMatch(/label:\s*"Enrichment"/);
    expect(src).not.toMatch(/label:\s*"Knowledge Graph"/);
    expect(src).not.toMatch(/label:\s*"Quality"/);
    expect(src).not.toMatch(/label:\s*"Validation"/);
  });

  it("does NOT import the dropped per-tab leaf components", () => {
    expect(src).not.toMatch(/from\s+"\.\/AssetsTab"/);
    expect(src).not.toMatch(/from\s+"\.\/GraphTab"/);
    expect(src).not.toMatch(/from\s+"\.\/QualityTab"/);
    expect(src).not.toMatch(/from\s+"\.\/ValidationTab"/);
  });

  it("declares the Query Trace helper that points to Document Detail", () => {
    expect(src).toContain("Tests retrieval against the compiled output");
    expect(src).toContain("active knowledge view");
  });
});


describe("Run Detail Overview — pipeline steps filter", () => {
  const stepBase = {
    durationMs: null,
    artifactCount: 0,
    source: "caller",
    required: true,
    reason: null,
    error: null,
  } as const;

  function _step(over: Partial<ReviewStepResult>): ReviewStepResult {
    return { ...stepBase, ...over } as ReviewStepResult;
  }

  it("keeps the compile step in the Pipeline Steps table", () => {
    const out = filterPrimaryRunSteps([
      _step({ step: "compile", status: "completed" }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0]!.step).toBe("compile");
  });

  it("keeps the assess_compile_strategy step", () => {
    const out = filterPrimaryRunSteps([
      _step({ step: "assess_compile_strategy", status: "completed" }),
    ]);
    expect(out).toHaveLength(1);
  });

  it("strips legacy 'disabled by selected execution profile' rows", () => {
    const out = filterPrimaryRunSteps([
      _step({ step: "compile", status: "completed" }),
      _step({
        step: "enrich",
        status: "skipped",
        reason: "disabled by selected execution profile 'standard'",
      }),
      _step({
        step: "graph",
        status: "skipped",
        reason: "disabled by selected execution profile 'standard'",
      }),
      _step({
        step: "index",
        status: "skipped",
        reason: "indexer_kind not provided in request",
      }),
    ]);
    expect(out.map((s) => s.step)).toEqual(["compile"]);
  });

  it("keeps a non-legacy skipped step (defensive — future skip reasons)", () => {
    // A step skipped for a real diagnostic reason (e.g. compile
    // didn't produce required inputs) should still surface so the
    // operator can investigate. Only the two legacy reasons are
    // filtered.
    const out = filterPrimaryRunSteps([
      _step({
        step: "compile",
        status: "skipped",
        reason: "compile failed; downstream skipped",
      }),
    ]);
    expect(out).toHaveLength(1);
  });

  it("preserves failed and running rows regardless of step id", () => {
    const out = filterPrimaryRunSteps([
      _step({ step: "compile", status: "failed" }),
      _step({ step: "weird_step", status: "running" }),
    ]);
    expect(out).toHaveLength(2);
  });

  it("renders a note pointing Document Detail for snapshot concerns", () => {
    const src = _readSrc("pages/run-detail/results/OverviewTab.tsx");
    expect(src).toContain("Document Detail");
    expect(src).toContain("active snapshot");
  });
});


describe("summariseDisabledReason — vocabulary stays stable", () => {
  it("returns 'skipped' for execution-profile reasons", () => {
    expect(
      summariseDisabledReason(
        "disabled by selected execution profile 'standard'",
      ),
    ).toBe("skipped");
  });

  it("returns 'soon' for 'available once' reasons", () => {
    expect(
      summariseDisabledReason("Available once the document is processed."),
    ).toBe("soon");
  });

  it("returns empty string for null reason", () => {
    expect(summariseDisabledReason(null)).toBe("");
    expect(summariseDisabledReason(undefined)).toBe("");
  });
});
