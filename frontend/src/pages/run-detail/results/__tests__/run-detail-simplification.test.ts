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


describe("Run Detail Results tabs — execution-focused four-tab set", () => {
  const src = _readSrc("pages/run-detail/results/index.tsx");

  it("declares only the four execution-focused tab keys", () => {
    // The union type at the top of the file pins the tab keys —
    // any future addition has to extend it, which makes this a
    // useful invariant target. Post-audit: there is NO `graph`
    // tab in Run Detail. The base graph/index lives in
    // RAGAnything/LightRAG's workspace, not in J1's artifact
    // registry, and the Overview now carries a neutral note
    // explaining that. Enrichment/quality/validation moved to
    // Document Detail's `ActiveKnowledgeResultPanel`.
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

  it("renders Chunks / Artifacts / Query Trace labels", () => {
    expect(src).toMatch(/label:\s*"Chunks"/);
    expect(src).toMatch(/label:\s*"Artifacts"/);
    expect(src).toMatch(/label:\s*"Query Trace"/);
  });

  it("does NOT render Compile Graph / Knowledge Graph / Enrichment / Quality / Validation labels", () => {
    // Per audit recommendation: Run Detail surfaces the no-graph-
    // artifact situation as a neutral note in Overview rather
    // than a permanently-empty tab. "Validation" stays as a tab
    // in the SISTER component `ActiveKnowledgeResultPanel` only.
    expect(src).not.toMatch(/label:\s*"Compile Graph"/);
    expect(src).not.toMatch(/label:\s*"Knowledge Graph"/);
    expect(src).not.toMatch(/label:\s*"Enrichment"/);
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


describe("Run Detail PrimaryStatusPanel — execution-focused banner", () => {
  const src = readFileSync(
    resolve(__dirname, "../../PrimaryStatusPanel.tsx"),
    "utf-8",
  );

  it("uses 'Compile completed' eyebrow for both completed branches", () => {
    expect(src).toMatch(/return\s+["'`]Compile completed["'`]/);
  });

  it("does not present skipped enrichment as a failure title", () => {
    expect(src).not.toContain(
      "Base compile succeeded; post-compile enrichment was skipped",
    );
    expect(src).not.toMatch(/return\s+["'`]Completed without enrichment["'`]/);
  });

  it("declares the success lede that points to active knowledge view", () => {
    expect(src).toContain(
      "The base Knowledge Index and graph were created",
    );
    expect(src).toContain("active knowledge view");
  });
});


describe("Run Detail PipelineOutputPanel — Assessment Plan + Compile only", () => {
  const src = readFileSync(
    resolve(__dirname, "../../PipelineOutputPanel.tsx"),
    "utf-8",
  );

  it("renders only AssessmentPlan + CompileStrategy + CompileResult panels", () => {
    expect(src).toMatch(/<AssessmentPlanPanel\b/);
    expect(src).toMatch(/<CompileStrategyPanel\b/);
    expect(src).toMatch(/<CompileResultPanel\b/);
  });

  it("does NOT render EnrichPlan or EnrichmentResult panels", () => {
    // Mentions in source docstrings are fine — we only care that
    // the panels are no longer rendered as children. Match the
    // JSX element form so a comment that names the dropped panel
    // doesn't trip the assertion.
    expect(src).not.toMatch(/<EnrichPlanPanel\b/);
    expect(src).not.toMatch(/<EnrichmentResultPanel\b/);
    // Also assert the imports are gone — keeps stale leaf modules
    // from being pulled into the Run Detail bundle.
    expect(src).not.toMatch(/from\s+"\.\/EnrichPlanPanel"/);
    expect(src).not.toMatch(/from\s+"\.\/EnrichmentResultPanel"/);
  });

  it("declares the active-knowledge-view footer note", () => {
    expect(src).toContain(
      "data-testid=\"pipeline-stream-active-snapshot-note\"",
    );
    expect(src).toContain("active knowledge view");
  });
});


describe("Run Detail Overview — neutral graph/index note", () => {
  const src = readFileSync(
    resolve(__dirname, "../OverviewTab.tsx"),
    "utf-8",
  );

  it("renders the graph note only when graph_json count is zero", () => {
    // Note gated on `artifactCounts.graph_json === 0`. If a
    // future advanced run registers a `graph_json` artifact, the
    // count is non-zero and the note hides itself.
    expect(src).toContain(`summary.artifactCounts["graph_json"]`);
    expect(src).toContain("showGraphNote");
  });

  it("explains the LightRAG workspace and the lack of a J1 graph artifact", () => {
    expect(src).toContain("RAGAnything/LightRAG workspace");
    expect(src).toContain("does not currently persist a separate");
    expect(src).toContain("graph artifact");
  });

  it("declares the note's stable testid", () => {
    expect(src).toContain(
      'data-testid="results-overview-graph-note"',
    );
  });

  it("does NOT describe the missing graph as a failure or skipped step", () => {
    // Neutral framing only — no "skipped" / "failed" / "missing"
    // headlines for the graph note specifically. We assert the
    // forbidden phrases are absent from the note's source span.
    const noteMatch = src.match(
      /results-overview__graph-note[\s\S]*?<\/section>/,
    );
    expect(noteMatch).not.toBeNull();
    const noteBlock = String(noteMatch?.[0] ?? "");
    expect(noteBlock.toLowerCase()).not.toContain("failed");
    expect(noteBlock.toLowerCase()).not.toContain("skipped");
    expect(noteBlock.toLowerCase()).not.toContain("missing");
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
