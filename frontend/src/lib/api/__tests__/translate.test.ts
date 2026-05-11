/**
 * Guards for `translate.ts` — the only place the FE depends on
 * backend field names. These tests pin the contract for the bugs
 * fixed during the integration audit:
 *
 * - Backend `RunStatus` is lowercase on the wire (StrEnum). The
 * translator must upper-case it. It does NOT collapse
 * `SUCCEEDED → COMPLETED`: predicate sets in
 * `lib/constants/runStatus.ts` carry both spellings, and a
 * collapse silently disables status checks against the
 * `SUCCEEDED` constants.
 * - The backend wraps unknown payload keys into `metadata` as a
 * plain dict, which preserves snake_case (`failure_code`,
 * `failure_message`, `error_type`, `error_message`, `reason`,
 * `gate`). The translator must read those snake_case keys.
 * - `step.warning` puts the warning text on the top-level
 * `message` field (no `metadata.warning` exists). The
 * translator mirrors it onto `data.warning` so the timeline's
 * warning-emphasis panel renders.
 * - `step.failed` carries `error_type` / `error_message` whereas
 * `run.failed` uses `failure_code` / `failure_message`. The
 * translator collapses both shapes onto the FE's single
 * `failure_code` / `failure_message` pair.
 */

import { describe, expect, it } from "vitest";

import { eventFromApi, runFromApi, runListItemFromApi } from "../translate";

describe("runFromApi", () => {
  it("upper-cases lowercase backend statuses", () => {
    const run = runFromApi({
      runId: "r1",
      documentId: "d1",
      workflowId: "r1",
      status: "running",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      progressPercent: 50,
      warningCount: 0,
    });
    expect(run.status).toBe("RUNNING");
  });

  it("preserves both SUCCEEDED and COMPLETED spellings (no collapse)", () => {
    // Regression for C13: the translator used to collapse
    // SUCCEEDED → COMPLETED, REQUIRES_HUMAN_REVIEW → AWAITING_HUMAN_REVIEW,
    // which silently broke any FE check against `RUN_STATUS.SUCCEEDED`
    // / `RUN_STATUS.REQUIRES_HUMAN_REVIEW`. The display map and the
    // predicate sets handle both spellings, so the translator only
    // needs to upper-case.
    const succeeded = runFromApi({
      runId: "r1",
      documentId: "d1",
      workflowId: "r1",
      status: "succeeded",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      completedAt: "2026-05-01T00:00:30Z",
      progressPercent: 100,
      warningCount: 0,
    });
    expect(succeeded.status).toBe("SUCCEEDED");

    const warned = runFromApi({
      runId: "r1",
      documentId: "d1",
      workflowId: "r1",
      status: "succeeded_with_warnings",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      progressPercent: 100,
      warningCount: 2,
    });
    expect(warned.status).toBe("SUCCEEDED_WITH_WARNINGS");

    const review = runFromApi({
      runId: "r1",
      documentId: "d1",
      workflowId: "r1",
      status: "requires_human_review",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      progressPercent: 80,
      warningCount: 0,
    });
    expect(review.status).toBe("REQUIRES_HUMAN_REVIEW");
  });

  it("populates final.failure_* from camelCase top-level fields", () => {
    const run = runFromApi({
      runId: "r1",
      documentId: "d1",
      workflowId: "r1",
      status: "failed",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      progressPercent: 10,
      warningCount: 0,
      failureCode: "J1_INGEST_REQUIRED_STEP_FAILED",
      failureMessage: "compile failed: 500 from VLM",
    });
    expect(run.final).toEqual({
      failure_code: "J1_INGEST_REQUIRED_STEP_FAILED",
      failure_message: "compile failed: 500 from VLM",
    });
  });
});

describe("runListItemFromApi", () => {
  it("translates the list item shape with documentName fallback chain", () => {
    const item = runListItemFromApi({
      runId: "r1",
      documentId: "d1",
      documentName: "earnings.pdf",
      status: "running",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
      progressPercent: 30,
      warningCount: 0,
      currentStage: "COMPILE",
      currentStep: "layout",
    });
    expect(item.runId).toBe("r1");
    expect(item.documentName).toBe("earnings.pdf");
    expect(item.status).toBe("RUNNING");
    expect(item.currentStage).toBe("COMPILE");
    expect(item.progressPercent).toBe(30);
  });

  it("falls back to documentId when documentName is missing", () => {
    const item = runListItemFromApi({
      runId: "r1",
      documentId: "doc-1",
      status: "succeeded",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
    });
    expect(item.documentName).toBe("doc-1");
    expect(item.status).toBe("SUCCEEDED");
  });

  it("threads mode/policy through and falls back when missing", () => {
    const withMeta = runListItemFromApi({
      runId: "r1",
      documentId: "doc-1",
      mode: "FAST",
      policy: "redact-pii",
      status: "running",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
    });
    expect(withMeta.mode).toBe("FAST");
    expect(withMeta.policy).toBe("redact-pii");

    const withoutMeta = runListItemFromApi({
      runId: "r2",
      documentId: "doc-2",
      status: "running",
      startedAt: "2026-05-01T00:00:00Z",
      updatedAt: "2026-05-01T00:00:01Z",
    });
    expect(withoutMeta.mode).toBe("STANDARD");
    expect(withoutMeta.policy).toBe("auto");
  });
});

describe("eventFromApi", () => {
  it("normalises ISO timestamps to ms", () => {
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "step.started",
      timestamp: "2026-05-01T00:00:01.000Z",
    });
    expect(evt.ts).toBe(Date.parse("2026-05-01T00:00:01.000Z"));
  });

  it("converts progressPercent (0..100) into a 0..1 fraction", () => {
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "step.progress",
      timestamp: "2026-05-01T00:00:01Z",
      progressPercent: 42,
      current: 21,
      total: 50,
    });
    expect(evt.data.progress).toBeCloseTo(0.42, 5);
    expect(evt.data.current).toBe(21);
    expect(evt.data.total).toBe(50);
  });

  it("reads run.failed metadata using camelCase keys", () => {
    // The audit-to-record translator on the backend camelizes the
    // metadata bag at serialisation time, so the FE only ever sees
    // camelCase here (`failureCode`, `failureMessage`).
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "run.failed",
      timestamp: "2026-05-01T00:00:01Z",
      severity: "ERROR",
      status: "failed",
      metadata: {
        failureCode: "J1_INGEST_RUN_FAILED",
        failureMessage: "graph build failed",
      },
    });
    expect(evt.data.failure_code).toBe("J1_INGEST_RUN_FAILED");
    expect(evt.data.failure_message).toBe("graph build failed");
  });

  it("collapses step.failed errorType / errorMessage onto failure_*", () => {
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "step.failed",
      timestamp: "2026-05-01T00:00:01Z",
      severity: "ERROR",
      stage: "GRAPH",
      step: "graph.build",
      status: "failed",
      metadata: {
        errorType: "GraphBuildError",
        errorMessage: "duplicate node id",
        retryable: false,
      },
    });
    expect(evt.data.failure_code).toBe("GraphBuildError");
    expect(evt.data.failure_message).toBe("duplicate node id");
  });

  it("mirrors step.warning text onto data.warning", () => {
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "step.warning",
      timestamp: "2026-05-01T00:00:01Z",
      severity: "WARNING",
      stage: "ENRICH",
      step: "enrich.images",
      message: "low-confidence detection",
    });
    expect(evt.data.warning).toBe("low-confidence detection");
    expect(evt.data.severity).toBe("WARNING");
  });

  it("reads step.skipped reason from metadata.reason (camelCase passthrough)", () => {
    // `reason` is single-word and round-trips identically through
    // the backend's snake-to-camel translator.
    const evt = eventFromApi({
      eventId: "e1",
      runId: "r1",
      eventType: "step.skipped",
      timestamp: "2026-05-01T00:00:01Z",
      severity: "INFO",
      stage: "ENRICH",
      step: "enrich.tables",
      status: "skipped",
      metadata: { reason: "no tables in document" },
    });
    expect(evt.data.reason).toBe("no tables in document");
  });
});
