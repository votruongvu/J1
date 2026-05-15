/**
 * PR-01: tests for the diagnostic-warnings banner helper.
 *
 * Component-level DOM rendering is covered by the Playwright
 * harness; here we pin the pure ``extractDiagnosticWarnings``
 * helper that backs the banner so the wire shape contract stays
 * stable.
 */

import { describe, expect, it } from "vitest";

import { extractDiagnosticWarnings } from "../ManualQueryConsole";


describe("extractDiagnosticWarnings", () => {
  it("returns [] when debug is null", () => {
    expect(extractDiagnosticWarnings(null)).toEqual([]);
  });

  it("returns [] when debug is undefined", () => {
    expect(extractDiagnosticWarnings(undefined)).toEqual([]);
  });

  it("returns [] when debug omits the diagnostic_warnings key (legacy backend)", () => {
    expect(
      extractDiagnosticWarnings({
        retrievedCount: 5,
        evidenceItemsBeforeFilter: 5,
        evidenceItemsAfterFilter: 4,
        artifactTypesBeforeFilter: [],
        artifactTypesAfterFilter: [],
        totalContextChars: 1000,
        topEvidencePreview: "",
        fallbackReason: null,
        deprioritizedKinds: [],
      }),
    ).toEqual([]);
  });

  it("returns [] when diagnostic_warnings is the empty array (happy path)", () => {
    expect(
      extractDiagnosticWarnings({
        retrievedCount: 5,
        evidenceItemsBeforeFilter: 5,
        evidenceItemsAfterFilter: 4,
        artifactTypesBeforeFilter: [],
        artifactTypesAfterFilter: [],
        totalContextChars: 1000,
        topEvidencePreview: "",
        fallbackReason: null,
        deprioritizedKinds: [],
        diagnostic_warnings: [],
      }),
    ).toEqual([]);
  });

  it("returns the string list verbatim when populated", () => {
    const warnings = [
      "snapshot_scope.eligible_snapshot_ids: empty",
      "duration_ms: absent from orchestrator trace",
    ];
    expect(
      extractDiagnosticWarnings({
        retrievedCount: 0,
        evidenceItemsBeforeFilter: 0,
        evidenceItemsAfterFilter: 0,
        artifactTypesBeforeFilter: [],
        artifactTypesAfterFilter: [],
        totalContextChars: 0,
        topEvidencePreview: "",
        fallbackReason: null,
        deprioritizedKinds: [],
        diagnostic_warnings: warnings,
      }),
    ).toEqual(warnings);
  });

  it("filters out non-string entries defensively", () => {
    // A misbehaving backend could send mixed types; the banner
    // renders only strings, never crashes.
    expect(
      extractDiagnosticWarnings({
        retrievedCount: 0,
        evidenceItemsBeforeFilter: 0,
        evidenceItemsAfterFilter: 0,
        artifactTypesBeforeFilter: [],
        artifactTypesAfterFilter: [],
        totalContextChars: 0,
        topEvidencePreview: "",
        fallbackReason: null,
        deprioritizedKinds: [],
        diagnostic_warnings: [
          "valid warning string",
          42,
          null,
          { not: "a string" },
          "another valid string",
        ],
      }),
    ).toEqual(["valid warning string", "another valid string"]);
  });

  it("returns [] when diagnostic_warnings is not an array", () => {
    expect(
      extractDiagnosticWarnings({
        retrievedCount: 0,
        evidenceItemsBeforeFilter: 0,
        evidenceItemsAfterFilter: 0,
        artifactTypesBeforeFilter: [],
        artifactTypesAfterFilter: [],
        totalContextChars: 0,
        topEvidencePreview: "",
        fallbackReason: null,
        deprioritizedKinds: [],
        diagnostic_warnings: "not-an-array",
      }),
    ).toEqual([]);
  });
});
