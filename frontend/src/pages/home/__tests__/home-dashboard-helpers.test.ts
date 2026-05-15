/**
 * Pure-logic tests for the Home dashboard helpers.
 *
 * The dashboard renders directly off these functions — a
 * regression here is something the operator sees. Pin every
 * aggregation rule explicitly.
 */

import { describe, expect, it } from "vitest";

import type {
  DocumentListItem,
  DocumentRunSummary,
} from "@/types/documents";

import {
  aggregateDocumentStatus,
  collectRecentRuns,
  computeNeedsAttention,
  formatDuration,
  runTypeLabel,
} from "../home-dashboard-helpers";


const _NOW = "2026-05-15T12:00:00Z";


function _run(
  overrides: Partial<DocumentRunSummary> = {},
): DocumentRunSummary {
  return {
    runId: "run-1",
    runType: "initial",
    status: "succeeded",
    startedAt: _NOW,
    completedAt: _NOW,
    failureCode: null,
    isActive: false,
    targetSnapshotId: null,
    displayVersion: null,
    isOnlyRun: true,
    canDeleteRun: false,
    canRefreshEnrichment: false,
    canRunEnrichment: false,
    ...overrides,
  };
}


function _doc(
  overrides: Partial<DocumentListItem> = {},
): DocumentListItem {
  return {
    documentId: "doc-1",
    displayName: "report.pdf",
    knowledgeState: "attached",
    activeSnapshotId: "snap-1",
    latestVersionId: null,
    createdAt: _NOW,
    updatedAt: _NOW,
    removedAt: null,
    currentResultSummary: {
      status: "succeeded",
      compileStatus: "completed",
      enrichmentStatus: null,
      validationStatus: null,
      failureCode: null,
    },
    availableActions: ["view", "reindex", "detach"],
    runHistorySummary: [_run()],
    ...overrides,
  };
}


// ---- aggregateDocumentStatus ------------------------------------


describe("aggregateDocumentStatus", () => {
  it("returns zeros on empty input", () => {
    const s = aggregateDocumentStatus([]);
    expect(s).toEqual({
      total: 0,
      indexed: 0,
      failed: 0,
      running: 0,
      detached: 0,
      lastSuccessfulAt: null,
    });
  });

  it("counts succeeded attached documents as indexed", () => {
    const s = aggregateDocumentStatus([
      _doc({ documentId: "a" }),
      _doc({ documentId: "b" }),
    ]);
    expect(s.indexed).toBe(2);
    expect(s.failed).toBe(0);
    expect(s.running).toBe(0);
    expect(s.total).toBe(2);
  });

  it("counts failed documents and excludes them from indexed", () => {
    const s = aggregateDocumentStatus([
      _doc({
        documentId: "ok",
      }),
      _doc({
        documentId: "bad",
        currentResultSummary: {
          status: "failed",
          compileStatus: "failed",
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: "PARSE_FAILED",
        },
      }),
    ]);
    expect(s.indexed).toBe(1);
    expect(s.failed).toBe(1);
  });

  it("counts running statuses (running/compiling/assessing) as running", () => {
    for (const status of ["running", "compiling", "assessing"]) {
      const s = aggregateDocumentStatus([
        _doc({
          documentId: "x",
          currentResultSummary: {
            status,
            compileStatus: null,
            enrichmentStatus: null,
            validationStatus: null,
            failureCode: null,
          },
        }),
      ]);
      expect(s.running).toBe(1);
      expect(s.indexed).toBe(0);
    }
  });

  it("counts detached separately and excludes from indexed", () => {
    const s = aggregateDocumentStatus([
      _doc({
        documentId: "d",
        knowledgeState: "detached",
      }),
    ]);
    expect(s.detached).toBe(1);
    expect(s.indexed).toBe(0);
    // Detached docs are still in `total` — they're visible to
    // the user, just excluded from search.
    expect(s.total).toBe(1);
  });

  it("excludes removed documents from total", () => {
    const s = aggregateDocumentStatus([
      _doc({ documentId: "live" }),
      _doc({ documentId: "gone", knowledgeState: "removed" }),
    ]);
    expect(s.total).toBe(1);
  });

  it("picks the max updatedAt across indexed documents", () => {
    const s = aggregateDocumentStatus([
      _doc({ documentId: "old", updatedAt: "2026-05-01T00:00:00Z" }),
      _doc({ documentId: "new", updatedAt: "2026-05-14T00:00:00Z" }),
      _doc({ documentId: "mid", updatedAt: "2026-05-10T00:00:00Z" }),
    ]);
    expect(s.lastSuccessfulAt).toBe("2026-05-14T00:00:00Z");
  });

  it("returns null lastSuccessfulAt when no documents are indexed", () => {
    const s = aggregateDocumentStatus([
      _doc({
        documentId: "bad",
        currentResultSummary: {
          status: "failed",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ]);
    expect(s.lastSuccessfulAt).toBeNull();
  });
});


// ---- collectRecentRuns ------------------------------------------


describe("collectRecentRuns", () => {
  it("flattens runs across documents", () => {
    const rows = collectRecentRuns([
      _doc({
        documentId: "a",
        runHistorySummary: [_run({ runId: "r1" }), _run({ runId: "r2" })],
      }),
      _doc({
        documentId: "b",
        runHistorySummary: [_run({ runId: "r3" })],
      }),
    ]);
    expect(rows).toHaveLength(3);
    expect(new Set(rows.map((r) => r.runId))).toEqual(
      new Set(["r1", "r2", "r3"]),
    );
  });

  it("sorts by startedAt descending", () => {
    const rows = collectRecentRuns([
      _doc({
        documentId: "a",
        runHistorySummary: [
          _run({ runId: "old", startedAt: "2026-05-01T00:00:00Z" }),
          _run({ runId: "new", startedAt: "2026-05-14T00:00:00Z" }),
          _run({ runId: "mid", startedAt: "2026-05-10T00:00:00Z" }),
        ],
      }),
    ]);
    expect(rows.map((r) => r.runId)).toEqual(["new", "mid", "old"]);
  });

  it("places runs without startedAt at the end", () => {
    const rows = collectRecentRuns([
      _doc({
        documentId: "a",
        runHistorySummary: [
          _run({ runId: "no-start", startedAt: null }),
          _run({ runId: "old", startedAt: "2026-05-01T00:00:00Z" }),
        ],
      }),
    ]);
    expect(rows.map((r) => r.runId)).toEqual(["old", "no-start"]);
  });

  it("honours the limit", () => {
    const runs = Array.from({ length: 10 }, (_, i) =>
      _run({
        runId: `r${i}`,
        startedAt: `2026-05-${(15 - i).toString().padStart(2, "0")}T00:00:00Z`,
      }),
    );
    const rows = collectRecentRuns(
      [_doc({ runHistorySummary: runs })],
      3,
    );
    expect(rows).toHaveLength(3);
    // Top 3 = the three with the LATEST startedAt (r0 is 2026-05-15).
    expect(rows.map((r) => r.runId)).toEqual(["r0", "r1", "r2"]);
  });

  it("computes durationMs from startedAt + completedAt", () => {
    const rows = collectRecentRuns([
      _doc({
        runHistorySummary: [_run({
          startedAt: "2026-05-15T12:00:00Z",
          completedAt: "2026-05-15T12:00:42Z",
        })],
      }),
    ]);
    expect(rows[0]!.durationMs).toBe(42000);
  });

  it("returns null durationMs when timestamps are missing", () => {
    const rows = collectRecentRuns([
      _doc({
        runHistorySummary: [_run({
          startedAt: "2026-05-15T12:00:00Z",
          completedAt: null,
        })],
      }),
    ]);
    expect(rows[0]!.durationMs).toBeNull();
  });
});


// ---- computeNeedsAttention --------------------------------------


describe("computeNeedsAttention", () => {
  it("returns empty when everything is healthy", () => {
    const docs = [_doc()];
    const summary = aggregateDocumentStatus(docs);
    expect(computeNeedsAttention(docs, summary)).toEqual([]);
  });

  it("warns when there are documents but none are indexed", () => {
    const docs = [
      _doc({
        currentResultSummary: {
          status: "failed",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ];
    const summary = aggregateDocumentStatus(docs);
    const attention = computeNeedsAttention(docs, summary);
    expect(attention[0]!.id).toBe("no-indexed");
    expect(attention[0]!.kind).toBe("err");
  });

  it("warns when documents are still running", () => {
    const docs = [
      _doc({
        currentResultSummary: {
          status: "running",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ];
    const summary = aggregateDocumentStatus(docs);
    const attention = computeNeedsAttention(docs, summary);
    expect(attention.find((a) => a.id === "running")).toBeDefined();
  });

  it("warns when documents have failed", () => {
    const docs = [
      _doc({ documentId: "ok" }),
      _doc({
        documentId: "bad",
        currentResultSummary: {
          status: "failed",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ];
    const summary = aggregateDocumentStatus(docs);
    const attention = computeNeedsAttention(docs, summary);
    expect(attention.find((a) => a.id === "failed")).toBeDefined();
  });

  it("warns when attached documents have no active snapshot", () => {
    const docs = [
      _doc({
        documentId: "no-snap",
        activeSnapshotId: null,
        currentResultSummary: {
          status: "succeeded",
          compileStatus: "completed",
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ];
    const summary = aggregateDocumentStatus(docs);
    const attention = computeNeedsAttention(docs, summary);
    expect(
      attention.find((a) => a.id === "attached-not-indexed"),
    ).toBeDefined();
  });

  it("orders entries no-indexed → running → failed → attached-not-indexed", () => {
    const docs = [
      _doc({
        documentId: "a",
        activeSnapshotId: null,
        currentResultSummary: {
          status: "running",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
      _doc({
        documentId: "b",
        currentResultSummary: {
          status: "failed",
          compileStatus: null,
          enrichmentStatus: null,
          validationStatus: null,
          failureCode: null,
        },
      }),
    ];
    const summary = aggregateDocumentStatus(docs);
    const attention = computeNeedsAttention(docs, summary);
    const ids = attention.map((a) => a.id);
    // `no-indexed` first (nothing is succeeded). Then running.
    // Then failed. Then attached-not-indexed.
    expect(ids).toEqual([
      "no-indexed",
      "running",
      "failed",
      "attached-not-indexed",
    ]);
  });

  it("returns empty when there are no documents at all", () => {
    const summary = aggregateDocumentStatus([]);
    // With zero documents, "no-indexed" is NOT raised — the empty
    // state is handled by the dashboard's empty hint instead, so
    // we don't double-message.
    expect(computeNeedsAttention([], summary)).toEqual([]);
  });
});


// ---- runTypeLabel + formatDuration ------------------------------


describe("runTypeLabel", () => {
  it("maps every run type to a business-friendly label", () => {
    expect(runTypeLabel("initial")).toBe("Initial ingest");
    expect(runTypeLabel("reindex")).toBe("Reindex");
    expect(runTypeLabel("resume")).toBe("Resume");
    expect(runTypeLabel("retry")).toBe("Retry");
    expect(runTypeLabel("validation")).toBe("Validation");
    expect(runTypeLabel("refresh_enrich")).toBe("Refresh enrichment");
    expect(runTypeLabel("run_domain_enrichment")).toBe("Domain Enrichment");
  });

  it("falls back to the raw wire string for unknown future types", () => {
    // Forward-compat: a brand-new BE ``RunType`` value must not
    // crash the run-history rows on an older FE bundle. The label
    // just degrades to the wire string until the FE rebuild ships.
    expect(runTypeLabel("brand_new_future_type")).toBe("brand_new_future_type");
  });
});


describe("formatDuration", () => {
  it("renders ms / s / m / h thresholds", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(42)).toBe("42 ms");
    expect(formatDuration(1200)).toBe("1.2 s");
    expect(formatDuration(15_000)).toBe("15 s");
    expect(formatDuration(150_000)).toBe("2m 30s");
    expect(formatDuration(60_000)).toBe("1m");
    expect(formatDuration(3_900_000)).toBe("1h 5m");
    expect(formatDuration(3_600_000)).toBe("1h");
  });
});
