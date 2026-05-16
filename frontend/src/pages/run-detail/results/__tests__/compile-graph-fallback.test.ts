/**
 * Contract — `GraphTab` Compile Graph Summary fallback.
 *
 * Under the simplified compile flow, RAGAnything/LightRAG writes
 * its graph state into the per-snapshot workspace and does NOT
 * register a separate `graph_json` artifact in J1's artifact
 * registry. The strict `/ingestion-runs/{id}/graph` snapshot view
 * therefore reports `unavailable.reason` for every standard run.
 *
 * Run Detail handles this by NOT exposing a graph tab at all —
 * the Overview surfaces a neutral note instead (see
 * `run-detail-simplification.test.ts` for that pin).
 *
 * Document Detail's `ActiveKnowledgeResultPanel` still mounts
 * `GraphTab`, where the fallback below renders if the snapshot
 * view is unavailable. These tests pin the projection helper +
 * the fallback's source contract.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { projectCompileGraphSummary } from "../GraphTab";


function _readSrc(rel: string): string {
  return readFileSync(
    resolve(__dirname, "../../../..", rel),
    "utf-8",
  );
}


describe("projectCompileGraphSummary", () => {
  it("extracts the fields the fallback view renders", () => {
    const out = projectCompileGraphSummary({
      compile_engine: "raganything",
      engine_version: "0.3.1",
      status: "succeeded",
      chunks_count: 12,
      page_count: 4,
      detected_content_types: ["text", "tables"],
      graph_artifact_refs: ["graph-art-1"],
      index_artifact_refs: ["index-art-1"],
    });
    expect(out.compileEngine).toBe("raganything");
    expect(out.engineVersion).toBe("0.3.1");
    expect(out.status).toBe("succeeded");
    expect(out.chunksCount).toBe(12);
    expect(out.pageCount).toBe(4);
    expect(out.detectedContentTypes).toEqual(["text", "tables"]);
    expect(out.graphArtifactRefs).toEqual(["graph-art-1"]);
    expect(out.indexArtifactRefs).toEqual(["index-art-1"]);
  });

  it("returns sane defaults for an empty payload", () => {
    const out = projectCompileGraphSummary({});
    // `raganything` is the default compile engine — the FE shows
    // it rather than a blank field so operators always see what
    // ran.
    expect(out.compileEngine).toBe("raganything");
    expect(out.engineVersion).toBeNull();
    expect(out.status).toBeNull();
    expect(out.chunksCount).toBeNull();
    expect(out.pageCount).toBeNull();
    expect(out.detectedContentTypes).toEqual([]);
    expect(out.graphArtifactRefs).toEqual([]);
    expect(out.indexArtifactRefs).toEqual([]);
  });

  it("tolerates malformed shapes (non-object, null, array)", () => {
    expect(
      projectCompileGraphSummary(null).compileEngine,
    ).toBe("raganything");
    expect(
      projectCompileGraphSummary(["nope"]).compileEngine,
    ).toBe("raganything");
    expect(
      projectCompileGraphSummary("hello").compileEngine,
    ).toBe("raganything");
  });

  it("rejects non-string entries inside content-type / ref arrays", () => {
    const out = projectCompileGraphSummary({
      detected_content_types: ["text", 42, null, "tables"],
      graph_artifact_refs: ["good", { ref: "bad" }],
    });
    expect(out.detectedContentTypes).toEqual(["text", "tables"]);
    expect(out.graphArtifactRefs).toEqual(["good"]);
  });
});


describe("GraphTab fallback — source contract", () => {
  const indexSrc = _readSrc("pages/run-detail/results/index.tsx");
  const graphSrc = _readSrc("pages/run-detail/results/GraphTab.tsx");

  it("Run Detail no longer exposes a graph tab", () => {
    // Post-audit: Run Detail's Results section ships exactly four
    // tabs (overview / chunks / raw / manual-trace). The base
    // graph/index lives in LightRAG's workspace and J1 doesn't
    // persist a graph artifact for it, so the tab was removed.
    // The graph-availability framing now lives as a neutral note
    // in the Overview tab.
    expect(indexSrc).not.toMatch(/key:\s*"graph"/);
    expect(indexSrc).not.toMatch(/label:\s*"Compile Graph"/);
    expect(indexSrc).not.toMatch(/label:\s*"Knowledge Graph"/);
    expect(indexSrc).not.toMatch(/from\s+"\.\/GraphTab"/);
  });

  it("GraphTab still renders the fallback component for ActiveKnowledgeResultPanel callers", () => {
    // Document Detail's `ActiveKnowledgeResultPanel` still mounts
    // GraphTab. When the snapshot view is unavailable there, the
    // fallback below is what the operator sees.
    expect(graphSrc).toContain("CompileGraphSummaryFallback");
    expect(graphSrc).toContain(
      'data-testid="results-graph-summary-fallback"',
    );
  });

  it("fallback explains the LightRAG workspace + missing graph_json", () => {
    expect(graphSrc).toContain("LightRAG");
    expect(graphSrc).toContain("graph_json");
    expect(graphSrc).toContain("Compile Graph Summary");
  });

  it("fallback reads compile_result_summary via listRunArtifacts", () => {
    expect(graphSrc).toContain('kind: "compile_result_summary"');
    expect(graphSrc).toContain("listRunArtifacts");
    expect(graphSrc).toContain("getRunArtifactContent");
  });
});
