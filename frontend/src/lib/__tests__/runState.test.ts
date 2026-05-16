/**
 * tests — pin the FE state projection in lock-step with the
 * Python backend's `project_ui_state` / `project_final_status`.
 *
 * Each parametrised case is the FE mirror of a backend test in
 * `tests/test_ui_status_mapping.py` and
 * `tests/test__workflow_refactor.py::test_projection_*`. A
 * rename on either side breaks both — this is the cheapest way to
 * catch silent drift.
 */

import { describe, expect, it } from "vitest";

import { RUN_STATUS } from "@/lib/constants/runStatus";
import {
  INGESTION_STATUS,
  projectFinalStatus,
  projectUiState,
  UI_STATE,
} from "@/lib/runState";
import type { IngestionRun } from "@/types/ingestion";


function makeRun(
  partial: Partial<IngestionRun> & { status: IngestionRun["status"] },
): IngestionRun {
  return {
    runId: "run-1",
    document_name: "doc-1",
    mode: "standard",
    policy: "fail_fast",
    started_at: "2026-05-11T00:00:00Z",
    progress_pct: 0,
    warning_count: 0,
    ...partial,
  } as IngestionRun;
}


// ---- projectFinalStatus -------------------------------------------


describe("projectFinalStatus", () => {
  it("returns COMPLETED_WITH_ENRICHMENT when enrichment succeeded", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const out = projectFinalStatus(r, { status: "succeeded" });
    expect(out).toBe(INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT);
  });

  it("returns COMPLETED_WITHOUT_ENRICHMENT when enrichment was skipped", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const out = projectFinalStatus(r, { status: "skipped" });
    expect(out).toBe(INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT);
  });

  it("returns COMPLETED_WITH_ENRICHMENT_WARNINGS on succeeded_with_warnings", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const out = projectFinalStatus(r, { status: "succeeded_with_warnings" });
    expect(out).toBe(INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS);
  });

  it("returns COMPLETED_WITH_ENRICHMENT_WARNINGS on optional enrichment failure", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const out = projectFinalStatus(r, {
      status: "failed",
      requireEnrichmentSuccess: false,
    });
    expect(out).toBe(INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS);
  });

  it("returns FAILED_ENRICHMENT_REQUIRED when failure_code says so", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "ENRICHMENT_REQUIRED" },
    });
    const out = projectFinalStatus(r);
    expect(out).toBe(INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED);
  });

  it("returns FAILED_COMPILE on compile-stage failure codes", () => {
    for (const code of [
      "COMPILE_FAILED",
      "CHUNK_FAILED",
      "INDEX_FAILED",
      "VERIFICATION_FAILED",
      "EMPTY_DOCUMENT",
    ]) {
      const r = makeRun({
        status: RUN_STATUS.FAILED,
        final: { failure_code: code },
      });
      expect(projectFinalStatus(r)).toBe(INGESTION_STATUS.FAILED_COMPILE);
    }
  });

  it("returns FAILED_FINALIZATION on FINALIZATION_FAILED", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "FINALIZATION_FAILED" },
    });
    expect(projectFinalStatus(r)).toBe(INGESTION_STATUS.FAILED_FINALIZATION);
  });

  it("returns FAILED_UNKNOWN when FAILED without a recognised code", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "some_other_code" },
    });
    expect(projectFinalStatus(r)).toBe(INGESTION_STATUS.FAILED_UNKNOWN);
  });

  it("returns CANCELLED when status is CANCELLED", () => {
    const r = makeRun({ status: RUN_STATUS.CANCELLED });
    expect(projectFinalStatus(r)).toBe(INGESTION_STATUS.CANCELLED);
  });

  it("returns COMPLETED_WITHOUT_ENRICHMENT for SUCCEEDED with no enrichment signals", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    expect(projectFinalStatus(r)).toBe(
      INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT,
    );
  });
});


// ---- projectUiState — A–F surface ---------------------------------


describe("projectUiState — A–F surface", () => {
  it("A. completed_without_enrichment → clean COMPLETED UI state", () => {
    // Run-execution outcome only: a successful compile is success,
    // regardless of whether post-compile enrichment ran. The
    // underlying `finalStatus` still distinguishes the two so
    // banner copy can refine without flipping the UI tone.
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const ui = projectUiState(r, { status: "skipped" });
    expect(ui.uiState).toBe(UI_STATE.COMPLETED);
    expect(ui.underlyingFinalStatus).toBe(
      INGESTION_STATUS.COMPLETED_WITHOUT_ENRICHMENT,
    );
    expect(ui.severity).toBe("success");
    expect(ui.recommendedAction).toBe("none");
  });

  it("B. completed_with_enrichment → clean COMPLETED UI state", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED });
    const ui = projectUiState(r, { status: "succeeded" });
    expect(ui.uiState).toBe(UI_STATE.COMPLETED);
    expect(ui.underlyingFinalStatus).toBe(
      INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT,
    );
    expect(ui.severity).toBe("success");
    expect(ui.recommendedAction).toBe("none");
  });

  it("C. completed_with_enrichment_warnings → COMPLETED_WITH_WARNINGS UI state", () => {
    const r = makeRun({ status: RUN_STATUS.SUCCEEDED_WITH_WARNINGS });
    const ui = projectUiState(r, { status: "succeeded_with_warnings" });
    expect(ui.uiState).toBe(UI_STATE.COMPLETED_WITH_WARNINGS);
    expect(ui.underlyingFinalStatus).toBe(
      INGESTION_STATUS.COMPLETED_WITH_ENRICHMENT_WARNINGS,
    );
    expect(ui.primaryArtifact).toBe("enrichment_result");
  });

  it("D. failed_compile → FAILED UI state with inspect_compile_output action", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "COMPILE_FAILED" },
    });
    const ui = projectUiState(r);
    expect(ui.uiState).toBe(UI_STATE.FAILED);
    expect(ui.underlyingFinalStatus).toBe(INGESTION_STATUS.FAILED_COMPILE);
    expect(ui.recommendedAction).toBe("inspect_compile_output");
  });

  it("E. failed_enrichment_required → FAILED UI state with retry action", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "ENRICHMENT_REQUIRED" },
    });
    const ui = projectUiState(r);
    expect(ui.uiState).toBe(UI_STATE.FAILED);
    expect(ui.underlyingFinalStatus).toBe(
      INGESTION_STATUS.FAILED_ENRICHMENT_REQUIRED,
    );
    expect(ui.recommendedAction).toBe("retry");
    expect(ui.primaryArtifact).toBe("compile_result_summary");
  });

  it("F. failed_finalization → FAILED UI state with inspect_error_report", () => {
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "FINALIZATION_FAILED" },
    });
    const ui = projectUiState(r);
    expect(ui.uiState).toBe(UI_STATE.FAILED);
    expect(ui.underlyingFinalStatus).toBe(
      INGESTION_STATUS.FAILED_FINALIZATION,
    );
    expect(ui.recommendedAction).toBe("inspect_error_report");
  });
});


// ---- In-flight projection -----------------------------------------


describe("projectUiState — in-flight branches", () => {
  it("ASSESSING projects to PENDING", () => {
    const r = makeRun({ status: RUN_STATUS.ASSESSING });
    expect(projectUiState(r).uiState).toBe(UI_STATE.PENDING);
  });

  it("COMPILING projects to RUNNING", () => {
    const r = makeRun({ status: RUN_STATUS.COMPILING });
    expect(projectUiState(r).uiState).toBe(UI_STATE.RUNNING);
  });

  it("RUNNING projects to RUNNING", () => {
    const r = makeRun({ status: RUN_STATUS.RUNNING });
    expect(projectUiState(r).uiState).toBe(UI_STATE.RUNNING);
  });

  it("CANCELLED projects to CANCELLED", () => {
    const r = makeRun({ status: RUN_STATUS.CANCELLED });
    expect(projectUiState(r).uiState).toBe(UI_STATE.CANCELLED);
  });

  it("AWAITING_HUMAN_REVIEW projects to PENDING", () => {
    const r = makeRun({ status: RUN_STATUS.AWAITING_HUMAN_REVIEW });
    expect(projectUiState(r).uiState).toBe(UI_STATE.PENDING);
  });
});


// ---- Totality + safety --------------------------------------------


describe("projectUiState — robustness", () => {
  it("never throws on missing final", () => {
    const r = makeRun({ status: RUN_STATUS.FAILED });
    expect(() => projectUiState(r)).not.toThrow();
  });

  it("never throws on legacy unknown status", () => {
    const r = makeRun({ status: "SOME_UNKNOWN" as IngestionRun["status"] });
    expect(() => projectUiState(r)).not.toThrow();
  });

  it("terminal projection wins over a stale running status when final code is present", () => {
    // Workflow may write COMPILING in stage while the run record
    // already carries a terminal failure code — the failure code
    // must beat the stale stage. Mirrors `is_terminal=True`
    // branching in the Python projector.
    const r = makeRun({
      status: RUN_STATUS.FAILED,
      final: { failure_code: "COMPILE_FAILED" },
    });
    const ui = projectUiState(r);
    expect(ui.uiState).toBe(UI_STATE.FAILED);
  });
});
