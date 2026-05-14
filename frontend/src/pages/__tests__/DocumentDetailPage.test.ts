/**
 * Snapshot-centric Document Detail markup tests.
 *
 * Renders through `react-dom/server` to match the existing
 * `IngestionStepIcon` test style (node environment, no jsdom).
 * The page-level component depends on the API client + routing
 * hooks, so we test the *snapshot-aware helper components* in
 * isolation — they carry the UX contract:
 *
 *   ActiveKnowledgePanel      — Snapshot ID / State / Producing run
 *   CandidateKnowledgeList    — "Not used by global query until promoted"
 *   SnapshotStateBadge        — building / ready / superseded / failed
 *
 * These are exported from `DocumentDetailPage` precisely to make
 * them testable without spinning up React Router + Toast context.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import {
  ActiveKnowledgePanel,
  CandidateKnowledgeList,
  SnapshotStateBadge,
} from "@/pages/DocumentDetailPage";
import type {
  DocumentDetail,
  DocumentRunSummary,
  DocumentSnapshotSummary,
} from "@/types/documents";


const _NOW = "2026-05-14T12:00:00Z";


function _run(over: Partial<DocumentRunSummary> = {}): DocumentRunSummary {
  return {
    runId: "ab12cd34ef56",
    runType: "reindex",
    status: "succeeded",
    startedAt: _NOW,
    completedAt: _NOW,
    failureCode: null,
    isActive: false,
    targetSnapshotId: null,
    displayVersion: null,
    isOnlyRun: false,
    canDeleteRun: false,
    canRefreshEnrichment: false,
    canRunEnrichment: false,
    ...over,
  };
}

function _snap(over: Partial<DocumentSnapshotSummary> = {}): DocumentSnapshotSummary {
  return {
    snapshotId: "snap_a1b2c3d4e5f6",
    documentId: "doc-1",
    createdByRunId: "ab12cd34ef56",
    state: "ready",
    createdAt: _NOW,
    promotedAt: _NOW,
    supersededAt: null,
    isActive: true,
    indexKinds: [],
    ...over,
  };
}

function _detail(over: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    documentId: "doc-1",
    displayName: "Bridge Report.pdf",
    knowledgeState: "attached",
    activeSnapshotId: "snap_a1b2c3d4e5f6",
    latestVersionId: null,
    createdAt: _NOW,
    updatedAt: _NOW,
    removedAt: null,
    currentResultSummary: {
      status: "succeeded",
      compileStatus: "completed",
      enrichmentStatus: "completed",
      validationStatus: "completed",
      failureCode: null,
    },
    availableActions: ["view", "reindex", "detach", "remove"],
    runHistory: [],
    ...over,
  };
}


describe("SnapshotStateBadge", () => {
  it("renders 'Ready' with the ok tone for ready snapshots", () => {
    const html = renderToStaticMarkup(
      createElement(SnapshotStateBadge, { state: "ready" }),
    );
    expect(html).toContain("Ready");
    expect(html).toContain("badge--ok");
  });

  it("renders 'Building' with the running tone", () => {
    const html = renderToStaticMarkup(
      createElement(SnapshotStateBadge, { state: "building" }),
    );
    expect(html).toContain("Building");
    expect(html).toContain("badge--running");
  });

  it("renders 'Superseded' with neutral tone (kept for audit, not live)", () => {
    const html = renderToStaticMarkup(
      createElement(SnapshotStateBadge, { state: "superseded" }),
    );
    expect(html).toContain("Superseded");
    expect(html).toContain("badge--neutral");
  });

  it("renders 'Failed' with the err tone", () => {
    const html = renderToStaticMarkup(
      createElement(SnapshotStateBadge, { state: "failed" }),
    );
    expect(html).toContain("Failed");
    expect(html).toContain("badge--err");
  });
});


describe("ActiveKnowledgePanel", () => {
  it("renders snapshot id + producing run + state when active snapshot exists", () => {
    const detail = _detail();
    const producingRun = _run({
      targetSnapshotId: "snap_a1b2c3d4e5f6",
      displayVersion: "14052026-03",
    });
    const activeSnapshot = _snap();
    const html = renderToStaticMarkup(
      createElement(ActiveKnowledgePanel, {
        detail, producingRun, activeSnapshot,
      }),
    );
    // Snapshot id prefix renders.
    expect(html).toContain("snap_a1b2c3d4e5f6");
    // Producing-run label + id prefix.
    expect(html).toContain("Produced by run");
    expect(html).toContain("ab12cd34ef56");
    // Display version chip.
    expect(html).toContain("v14052026-03");
    // State badge for ready.
    expect(html).toContain("Ready");
    // Queryable affordance.
    expect(html).toContain("Queryable in global scope");
    expect(html).toContain("Yes");
  });

  it("renders the not-yet-processed empty state when active snapshot is null", () => {
    const detail = _detail({ activeSnapshotId: null });
    const html = renderToStaticMarkup(
      createElement(ActiveKnowledgePanel, {
        detail, producingRun: null, activeSnapshot: null,
      }),
    );
    expect(html).toContain("No active snapshot yet");
    expect(html).toContain("re-index this document");
    expect(html).not.toContain("Produced by run");
  });

  it("shows queryable=No when document is detached", () => {
    const detail = _detail({ knowledgeState: "detached" });
    const html = renderToStaticMarkup(
      createElement(ActiveKnowledgePanel, {
        detail, producingRun: _run({
          targetSnapshotId: "snap_a1b2c3d4e5f6",
        }), activeSnapshot: _snap(),
      }),
    );
    expect(html).toContain("Queryable in global scope");
    expect(html).toContain("No");
  });
});


describe("CandidateKnowledgeList", () => {
  it("renders 'not used by global query' helper text per candidate row", () => {
    const candidates = [_run({
      runId: "76f9688a3b30",
      status: "running",
      targetSnapshotId: "snap_candidate1234",
    })];
    const html = renderToStaticMarkup(
      createElement(CandidateKnowledgeList, {
        runs: candidates,
        snapshotsById: {},
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("Candidate snapshot");
    // The UI truncates the snapshot id to 16 chars. Pin the prefix
    // rather than the full id so the assertion survives a future
    // change to the truncation length.
    expect(html).toContain("snap_candidate12");
    expect(html).toContain("Not used by global query until promoted.");
    expect(html).toContain("View processing run");
  });

  it("renders the snapshot state badge when the candidate's snapshot is known", () => {
    const candidates = [_run({
      runId: "76f9688a3b30",
      status: "running",
      targetSnapshotId: "snap_candidate1234",
    })];
    const snapshotsById = {
      snap_candidate1234: _snap({
        snapshotId: "snap_candidate1234",
        state: "building",
        isActive: false,
        promotedAt: null,
      }),
    };
    const html = renderToStaticMarkup(
      createElement(CandidateKnowledgeList, {
        runs: candidates,
        snapshotsById,
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("Building");
  });

  it("renders the empty state when there are no candidate runs", () => {
    const html = renderToStaticMarkup(
      createElement(CandidateKnowledgeList, {
        runs: [],
        snapshotsById: {},
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("No candidate snapshots");
  });
});
