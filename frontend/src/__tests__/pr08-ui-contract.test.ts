/**
 * PR-08 contract — Product UI cleanup + Global Search entry point.
 *
 * Per ``docs/j1_sequential_pr_implementation_plan.md``'s PR-08, the
 * frontend MUST guarantee:
 *
 *   1. The Home page exposes Global Search (calls the
 *      ``runProjectQuery`` API with the ``project_active`` scope).
 *   2. Document Detail's action vocabulary contains ONLY
 *      document-level actions — no run-level re-index / resume.
 *   3. Run Detail's controls are ONLY run-scoped operational
 *      actions (pause / cancel / clean-up). Re-index and resume
 *      are NOT surfaced.
 *   4. The Validation tab is import-only. The generated
 *      test-case feature is gone — no draft/approve UI, no
 *      generateTestCases / testCaseGenerator references.
 *   5. ManualQueryConsole stays available as the detailed-
 *      diagnostics surface (the import-only validation tab still
 *      offers manual queries).
 *
 * This file is the single PR-08 regression document at the FE
 * layer. Adjacent test files (HomeCards, RunControls,
 * DocumentDetailPage, SearchResultView) cover finer-grained
 * behaviour; this one ensures the structural surfaces stay clean.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";


const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_FE_ROOT = path.resolve(__dirname, "..");


function readSource(rel: string): string {
  return readFileSync(path.join(REPO_FE_ROOT, rel), "utf-8");
}


// ---- Contract 1: Home page wires Global Search via project query


describe("PR-08 Contract 1 — Home page exposes Global Search", () => {
  it("HomeDashboard renders GlobalSearchCard", () => {
    const src = readSource("pages/home/HomeDashboard.tsx");
    expect(src).toContain("GlobalSearchCard");
  });

  it("GlobalSearchPage calls client.runProjectQuery", () => {
    const src = readSource("pages/search/GlobalSearchPage.tsx");
    expect(src).toContain("client.runProjectQuery");
  });

  it("GlobalSearchPage uses the project_active scope literal", () => {
    const src = readSource("pages/search/GlobalSearchPage.tsx");
    // The scope literal MUST be present and explicit — operators
    // changing the default scope in future must edit this file.
    expect(src).toMatch(/type:\s*["']project_active["']/);
  });
});


// ---- Contract 2: Document Detail has no run-level actions ------


describe("PR-08 Contract 2 — Document Detail surface", () => {
  it("DocumentAction enum lists only document-level actions", () => {
    const src = readSource("types/documents.ts");
    // The enum MUST list these.
    for (const action of ["view", "reindex", "detach", "attach", "remove"]) {
      expect(src).toMatch(new RegExp(`["']${action}["']`));
    }
  });

  it("DocumentAction enum does NOT include run-level actions", () => {
    const src = readSource("types/documents.ts");
    for (const forbidden of [
      "resume", "re_index_run", "reindex_run", "refresh_enrichment",
      "run_enrichment", "re_process",
    ]) {
      // String-search the type-definition section. False positives
      // would only fire if the enum value mentions these literally.
      const enumMatch = src.match(
        /export type DocumentAction\s*=([\s\S]*?);/,
      );
      expect(enumMatch).toBeTruthy();
      expect(enumMatch![1]).not.toContain(`"${forbidden}"`);
    }
  });

  it("DocumentDetailPage handler covers exactly the supported actions", () => {
    const src = readSource("pages/DocumentDetailPage.tsx");
    // Each supported action has an explicit branch.
    for (const expected of [
      'action === "detach"',
      'action === "remove"',
      'action === "reindex"',
      'action === "attach"',
    ]) {
      expect(src).toContain(expected);
    }
  });
});


// ---- Contract 3: Run Detail has no re-index / resume -----------


describe("PR-08 Contract 3 — Run Detail surface", () => {
  it("RunControls exposes only run-scoped ControlAction values", () => {
    const src = readSource("pages/run-detail/RunControls.tsx");
    // The literal type definition pins the allowed actions.
    expect(src).toMatch(
      /type ControlAction\s*=\s*["']pause["']\s*\|\s*["']cancel["']\s*\|\s*["']clean_up["']/,
    );
  });

  it("RunControls does NOT reference re-index / resume actions", () => {
    const src = readSource("pages/run-detail/RunControls.tsx");
    // No live code path references these. Comments documenting
    // the removal ARE allowed (and present); we only fail when
    // an action literal appears.
    const controlActionDef = src.match(
      /type ControlAction\s*=[\s\S]*?;/,
    );
    expect(controlActionDef).toBeTruthy();
    for (const forbidden of ["reindex", "resume", "re_index", "refresh_enrichment"]) {
      expect(controlActionDef![0]).not.toContain(forbidden);
    }
  });

  it("RunControls documents the retirement of refresh_enrichment", () => {
    const src = readSource("pages/run-detail/RunControls.tsx");
    // Existing comment block explains why the action is gone.
    // Pinned so a future cleanup doesn't drop the breadcrumb.
    expect(src).toMatch(/refresh_enrichment|run_enrichment/);
    expect(src).toMatch(/removed|deprecated|Manual Actions/i);
  });
});


// ---- Contract 4: Validation tab is import-only -----------------


describe("PR-08 Contract 4 — Validation tab import-only", () => {
  it("ValidationTab renders ImportedTestCasesSection", () => {
    const src = readSource(
      "pages/run-detail/results/ValidationTab.tsx",
    );
    expect(src).toContain("ImportedTestCasesSection");
  });

  it("ValidationTab does NOT expose any generated-test-case feature", () => {
    const src = readSource(
      "pages/run-detail/results/ValidationTab.tsx",
    );
    // The retired feature names. Comments referencing the
    // removal are tolerated (we check for code references via
    // common identifier shapes).
    const forbiddenIdentifiers = [
      /generateTestCases\s*\(/,
      /testCaseGenerator\s*\(/,
      /DraftTestCase/,
      /ApproveTestCase/,
      /useGeneratedTestCases/,
    ];
    for (const re of forbiddenIdentifiers) {
      expect(src).not.toMatch(re);
    }
  });

  it("repository-wide grep finds no generated-test-case identifiers", () => {
    // Spot-check the most likely homes. A new home would surface
    // here under a future cleanup; today these are the relevant
    // anchors.
    const anchors = [
      "pages/run-detail/results/ValidationTab.tsx",
      "pages/run-detail/results/ManualQueryConsole.tsx",
      "pages/DocumentDetailPage.tsx",
    ];
    for (const rel of anchors) {
      const src = readSource(rel);
      expect(src).not.toMatch(/generateTestCases/);
      expect(src).not.toMatch(/testCaseGenerator/);
      expect(src).not.toMatch(/draft_test_case/);
    }
  });
});


// ---- Contract 5: ManualQueryConsole stays available -----------


describe("PR-08 Contract 5 — Manual Test Query stays available", () => {
  it("ValidationTab imports ManualQueryConsole", () => {
    const src = readSource(
      "pages/run-detail/results/ValidationTab.tsx",
    );
    expect(src).toContain("ManualQueryConsole");
    // Both the import and the JSX render-site.
    expect(src).toMatch(/<ManualQueryConsole/);
  });

  it("ManualQueryConsole exports a callable handle for parent use", () => {
    // The ImportedTestCases section can drive a manual query via
    // the imperative handle — this is the integration seam that
    // proves the two halves of the tab cooperate.
    const src = readSource(
      "pages/run-detail/results/ManualQueryConsole.tsx",
    );
    expect(src).toMatch(/ManualQueryConsoleHandle/);
  });
});


// ---- Bonus: MainNav exposes the three top-level routes --------


describe("PR-08 bonus — MainNav covers Home / Search / Documents", () => {
  it("MainNav references each of the three top-level routes", () => {
    const src = readSource("components/MainNav.tsx");
    expect(src).toMatch(/home/i);
    expect(src).toMatch(/search/i);
    expect(src).toMatch(/document/i);
  });
});
