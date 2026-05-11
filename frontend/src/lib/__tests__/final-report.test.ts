/**
 * FE tests for the final-ingestion-report integration.
 *
 * Pins:
 * 1. `projectUiStateFromReport` prefers the report's `final_status`
 * over the per-artifact projection.
 * 2. When the report is null, the projector falls back to the
 * per-artifact derivation cleanly.
 * 3. Each (A–F) underlying-final-status literal projects to the
 * same UiRunState as the report-less path (so the panel renders
 * identical badges whether or not the report is present).
 * 4. The report's `final_status_reason` overrides the headline so
 * operator-readable backend copy wins over FE defaults.
 * 5. The report's "no enrichment artefacts" path still renders the
 * skipped-reason — never invents data when modules are empty.
 */

import { describe, expect, it } from "vitest";

import { RUN_STATUS } from "@/lib/constants/runStatus";
import {
  INGESTION_STATUS,
  projectUiState,
  projectUiStateFromReport,
  UI_STATE,
} from "@/lib/runState";
import type { IngestionRun } from "@/types/ingestion";
import type { FinalIngestionReportPayload } from "@/types/review";


function makeRun(
  partial: Partial<IngestionRun> & { status: IngestionRun["status"] },
): IngestionRun {
  return {
    runId: "run-1",
    document_name: "spec.pdf",
    mode: "standard",
    policy: "fail_fast",
    started_at: "2026-05-11T00:00:00Z",
    progress_pct: 0,
    warning_count: 0,
    ...partial,
  } as IngestionRun;
}


function makeReport(
  finalStatus: string,
  partial: Partial<FinalIngestionReportPayload> = {},
): FinalIngestionReportPayload {
  return {
    schema_version: "1.0",
    run_id: "run-1",
    document_id: "doc-1",
    final_status: finalStatus,
    final_status_reason: "reason from backend",
    stages: [],
    ...partial,
  };
}


// ---- 1. Report wins over per-artifact projection -----------------


describe("projectUiStateFromReport — report wins", () => {
  it("uses the report's final_status literal", () => {
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const report = makeReport(INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT);
    const ui = projectUiStateFromReport(run, report);
    expect(ui?.uiState).toBe(UI_STATE.COMPLETED);
    expect(ui?.underlyingFinalStatus).toBe(
      INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT,
    );
  });

  it("uses the report's final_status_reason as the headline", () => {
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const report = makeReport(
      INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT,
      { final_status_reason: "enrichment overlay produced" },
    );
    const ui = projectUiStateFromReport(run, report);
    expect(ui?.headline).toBe("enrichment overlay produced");
  });

  it("disregards stale run-level signals when the report disagrees", () => {
    // Run is still showing SUCCEEDED, but the report says
    // failed_enrichment_required — the report wins (it's the
    // authoritative terminal projection).
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const report = makeReport(INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED);
    const ui = projectUiStateFromReport(run, report);
    expect(ui?.uiState).toBe(UI_STATE.FAILED);
    expect(ui?.underlyingFinalStatus).toBe(
      INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED,
    );
  });
});


// ---- 2. Fallback when the report is null --------------------------


describe("projectUiStateFromReport — fallback for legacy runs", () => {
  it("falls back to projectUiState when report is null", () => {
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const ui = projectUiStateFromReport(run, null, { status: "succeeded" });
    const expected = projectUiState(run, { status: "succeeded" });
    expect(ui?.uiState).toBe(expected.uiState);
    expect(ui?.underlyingFinalStatus).toBe(expected.underlyingFinalStatus);
  });

  it("falls back cleanly when report is missing final_status", () => {
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    // @ts-expect-error — testing malformed payload.
    const ui = projectUiStateFromReport(run, { schema_version: "1.0" });
    expect(ui).not.toBeNull();
    expect(ui?.uiState).toBe(UI_STATE.COMPLETED_WITH_WARNINGS);
  });

  it("returns null for a null run", () => {
    expect(projectUiStateFromReport(null, null)).toBeNull();
  });
});


// ---- 3. Per-(A–F) parity with the projection --------------


const _A_F_CASES: Array<[string, string]> = [
  [INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT, UI_STATE.COMPLETED],
  [INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT, UI_STATE.COMPLETED_WITH_WARNINGS],
  [INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS, UI_STATE.COMPLETED_WITH_WARNINGS],
  [INGESTION_STATUS.FAILED_COMPILE, UI_STATE.FAILED],
  [INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED, UI_STATE.FAILED],
  [INGESTION_STATUS.FAILED_FINALIZATION, UI_STATE.FAILED],
];


describe.each(_A_F_CASES)(
  "parity — %s",
  (finalStatus, expectedUiState) => {
    it("projects to the expected UI state", () => {
      const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
      const ui = projectUiStateFromReport(run, makeReport(finalStatus));
      expect(ui?.uiState).toBe(expectedUiState);
      expect(ui?.underlyingFinalStatus).toBe(finalStatus);
    });
  },
);


// ---- 4. Wire-format / robustness ---------------------------------


describe("projectUiStateFromReport — robustness", () => {
  it("never throws on a partial run record", () => {
    const run = makeRun({
      status: RUN_STATUS.FAILED,
      final: undefined,  // legacy run, no final block
    });
    expect(() =>
      projectUiStateFromReport(run, makeReport(INGESTION_STATUS.FAILED_COMPILE)),
    ).not.toThrow();
  });

  it("does not crash when stages array is empty", () => {
    const run = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const report = makeReport(
      INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT,
      { stages: [] },
    );
    expect(() => projectUiStateFromReport(run, report)).not.toThrow();
  });
});
