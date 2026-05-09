/**
 * Tests for `IngestionStepIcon` and the step → icon mapping.
 *
 * Render through `react-dom/server` so we can assert markup
 * without spinning up jsdom — vitest defaults to the node
 * environment in this suite. Snapshot-style class-string
 * assertions are enough to pin the contract; pixel-level CSS
 * verification is the design's job.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import {
  IngestionStepIcon,
  INGESTION_STEP_ICONS,
} from "@/components/ingestion-icons";
import { PROCESSING_STEP_IDS } from "@/lib/processing-steps";

function render(props: Parameters<typeof IngestionStepIcon>[0]): string {
  return renderToStaticMarkup(createElement(IngestionStepIcon, props));
}

describe("INGESTION_STEP_ICONS mapping", () => {
  it("has an icon for every canonical step id", () => {
    for (const id of PROCESSING_STEP_IDS) {
      expect(INGESTION_STEP_ICONS[id]).toBeTruthy();
    }
  });

  it("has exactly seven entries (matching the user-facing flow)", () => {
    expect(Object.keys(INGESTION_STEP_ICONS)).toHaveLength(7);
  });
});

describe("<IngestionStepIcon /> rendering", () => {
  it("renders the parse icon for parse_source_content / running", () => {
    const html = render({
      step: "parse_source_content",
      status: "running",
    });
    // Parse icon's SVG carries the `pix-ic-parse` class — pinned
    // so a refactor that swaps class names breaks loudly.
    expect(html).toContain("pix-ic-parse");
    expect(html).toContain("state-running");
    // Running state has no badge.
    expect(html).not.toContain("pix-state-badge");
  });

  it("renders completed badge + paused state for completed", () => {
    const html = render({
      step: "build_content_inventory",
      status: "completed",
    });
    expect(html).toContain("pix-ic-inv");
    expect(html).toContain("state-completed");
    expect(html).toContain("pix-state-badge");
    // Completed badge does NOT carry the .warn class.
    expect(html).not.toContain("pix-state-badge warn");
  });

  it("renders failed state with warn badge", () => {
    const html = render({
      step: "build_knowledge_graph",
      status: "failed",
    });
    expect(html).toContain("pix-ic-graph");
    expect(html).toContain("state-failed");
    expect(html).toContain("pix-state-badge warn");
  });

  it("renders skipped state with no badge but reduced opacity class", () => {
    const html = render({
      step: "enrich_extracted_content",
      status: "skipped",
    });
    expect(html).toContain("pix-ic-enrich");
    expect(html).toContain("state-skipped");
    expect(html).not.toContain("pix-state-badge");
  });

  it("normalises internal step names to user-facing ids", () => {
    // `compile` → parse_source_content → ParseIcon
    expect(render({ step: "compile", status: "running" })).toContain(
      "pix-ic-parse",
    );
    // `plan.revised` → create_execution_plan → PlanIcon
    expect(render({ step: "plan.revised", status: "completed" })).toContain(
      "pix-ic-plan",
    );
    // `chunks` → generate_knowledge_chunks → ChunkIcon
    expect(render({ step: "chunks", status: "running" })).toContain(
      "pix-ic-chunk",
    );
    // `graph_build` → build_knowledge_graph → GraphIcon
    expect(render({ step: "graph_build", status: "running" })).toContain(
      "pix-ic-graph",
    );
  });

  it("falls back to a neutral frame for unknown step names", () => {
    const html = render({ step: "totally_made_up", status: "running" });
    // Frame still renders + carries the running state, but no
    // step-specific class is present.
    expect(html).toContain("pix-icon-frame");
    expect(html).toContain("state-running");
    expect(html).not.toMatch(/pix-ic-(parse|inv|plan|chunk|enrich|graph|final)/);
  });

  it("respects the `flat` prop", () => {
    const html = render({
      step: "parse_source_content",
      status: "running",
      flat: true,
    });
    expect(html).toContain("flat");
  });

  it("size prop controls the size class (sm/md/lg/xs)", () => {
    expect(
      render({ step: "parse_source_content", status: "running", size: "xs" }),
    ).toContain("size-xs");
    expect(
      render({ step: "parse_source_content", status: "running", size: "sm" }),
    ).toContain("size-sm");
    expect(
      render({ step: "parse_source_content", status: "running", size: "lg" }),
    ).toContain("size-lg");
  });

  it("numeric size sets a CSS variable instead of a size class", () => {
    const html = render({
      step: "parse_source_content",
      status: "running",
      size: 64,
    });
    expect(html).toContain("--pix-size:64px");
    expect(html).not.toContain("size-md");
  });

  it("status defaults to pending and renders the muted frame", () => {
    const html = render({ step: "parse_source_content" });
    expect(html).toContain("state-pending");
    expect(html).not.toContain("pix-state-badge");
  });

  it("provides an aria-label for accessibility", () => {
    const html = render({
      step: "parse_source_content",
      status: "running",
      ariaLabel: "Parsing source",
    });
    expect(html).toContain('aria-label="Parsing source"');
  });

  it("derives a default aria-label when none is provided", () => {
    const html = render({
      step: "parse_source_content",
      status: "running",
    });
    expect(html).toContain('aria-label="Parse Source Content"');
  });
});
