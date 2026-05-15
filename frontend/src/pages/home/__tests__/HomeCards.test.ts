/**
 * Static-markup tests for the Home dashboard's leaf cards.
 *
 * Match the project convention: `react-dom/server` (no jsdom).
 * Owner-driven pages aren't rendered here — the leaves are the
 * pieces with branching presentation, and they're what would
 * regress visually if a refactor missed a case.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { GlobalSearchCard } from "../GlobalSearchCard";
import { NeedsAttentionPanel } from "../NeedsAttentionPanel";
import { RecentRunsPanel } from "../RecentRunsPanel";
import { SystemStatusSummary } from "../SystemStatusSummary";


// ---- GlobalSearchCard ------------------------------------------


describe("GlobalSearchCard", () => {
  it("renders the helper text + search input + submit button", () => {
    const html = renderToStaticMarkup(
      createElement(GlobalSearchCard, { onSubmit: () => {} }),
    );
    expect(html).toContain("Search the knowledge base");
    expect(html).toContain("Ask across all active indexed knowledge");
    expect(html).toContain("home-global-search-input");
    expect(html).toContain("home-global-search-submit");
  });

  it("disables the submit button when disabled=true", () => {
    const html = renderToStaticMarkup(
      createElement(GlobalSearchCard, {
        onSubmit: () => {},
        disabled: true,
      }),
    );
    const submitBtn = html.match(
      /<button[^>]*home-global-search-submit[^>]*>/,
    )?.[0];
    expect(submitBtn).toContain("disabled");
  });

  it("renders the disabled hint when disabled", () => {
    const html = renderToStaticMarkup(
      createElement(GlobalSearchCard, {
        onSubmit: () => {},
        disabled: true,
        disabledHint: "Configure tenant + project first.",
      }),
    );
    expect(html).toContain("home-global-search-disabled-hint");
    expect(html).toContain("Configure tenant + project first.");
  });
});


// ---- NeedsAttentionPanel ---------------------------------------


describe("NeedsAttentionPanel", () => {
  it("renders nothing when there are no items", () => {
    const html = renderToStaticMarkup(
      createElement(NeedsAttentionPanel, { items: [] }),
    );
    expect(html).toBe("");
  });

  it("renders one row per item with a stable testid", () => {
    const html = renderToStaticMarkup(
      createElement(NeedsAttentionPanel, {
        items: [
          { id: "running", kind: "warn", message: "2 documents are running." },
          { id: "failed", kind: "err", message: "1 document failed." },
        ],
      }),
    );
    expect(html).toContain("needs-attention-running");
    expect(html).toContain("needs-attention-failed");
    expect(html).toContain("2 documents are running");
    expect(html).toContain("1 document failed");
  });
});


// ---- SystemStatusSummary ---------------------------------------


describe("SystemStatusSummary", () => {
  it("renders the five counters with the documented testids", () => {
    const html = renderToStaticMarkup(
      createElement(SystemStatusSummary, {
        summary: {
          total: 5,
          indexed: 3,
          failed: 1,
          running: 1,
          detached: 0,
          lastSuccessfulAt: "2026-05-15T11:00:00Z",
        },
      }),
    );
    expect(html).toContain("status-total");
    expect(html).toContain("status-indexed");
    expect(html).toContain("status-running");
    expect(html).toContain("status-failed");
    expect(html).toContain("status-last-success");
    // Pin a couple of values so the formatting isn't lost in a
    // refactor.
    expect(html).toContain(">5<");  // total
    expect(html).toContain(">3<");  // indexed
  });

  it("renders em-dash for last successful when null", () => {
    const html = renderToStaticMarkup(
      createElement(SystemStatusSummary, {
        summary: {
          total: 0,
          indexed: 0,
          failed: 0,
          running: 0,
          detached: 0,
          lastSuccessfulAt: null,
        },
      }),
    );
    expect(html).toContain("—");
  });
});


// ---- RecentRunsPanel -------------------------------------------


describe("RecentRunsPanel", () => {
  it("renders empty state when no rows", () => {
    const html = renderToStaticMarkup(
      createElement(RecentRunsPanel, {
        rows: [],
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("home-recent-runs-empty");
    expect(html).toContain("No runs yet");
  });

  it("renders one row per run with View button", () => {
    const html = renderToStaticMarkup(
      createElement(RecentRunsPanel, {
        rows: [
          {
            runId: "run-1",
            documentId: "doc-1",
            documentName: "report.pdf",
            runType: "initial",
            status: "succeeded",
            startedAt: "2026-05-15T10:00:00Z",
            completedAt: "2026-05-15T10:00:30Z",
            durationMs: 30000,
          },
          {
            runId: "run-2",
            documentId: "doc-2",
            documentName: "analysis.docx",
            runType: "reindex",
            status: "failed",
            startedAt: "2026-05-15T09:00:00Z",
            completedAt: "2026-05-15T09:00:05Z",
            durationMs: 5000,
          },
        ],
        onOpenRun: () => {},
      }),
    );
    expect(html).toContain("recent-run-run-1");
    expect(html).toContain("recent-run-run-2");
    expect(html).toContain("recent-run-open-run-1");
    expect(html).toContain("report.pdf");
    expect(html).toContain("analysis.docx");
    // Business-friendly run-type labels.
    expect(html).toContain("Initial ingest");
    expect(html).toContain("Reindex");
    // Duration formatting.
    expect(html).toContain("30 s");
    expect(html).toContain("5.0 s");
  });
});
