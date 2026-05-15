/**
 * Static-markup tests for the AssessmentPlanDialog.
 *
 * Renders through `react-dom/server` to match the existing
 * project convention (no jsdom dependency). Interactive behavior
 * (click → onConfirm with selectedProfile) lives in the
 * api-client tests + the e2e suite; here we pin:
 *
 *   - the recommendation banner shows the right profile name
 *   - all three profile cards render (in canonical order)
 *   - the recommended pill lights up on the recommended card
 *   - the loading state renders when `plan` is null
 *   - the error banner renders when `loadError` is set
 *   - warnings from the profiler surface in the UI
 *
 * These are the user-visible promises the dialog makes; a
 * regression here is something the user would see.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { AssessmentPlanDialog } from "../AssessmentPlanDialog";
import type {
  AssessmentPlanResponse,
  ExecutionProfileDetails,
} from "@/types/execution-profile";


function _profile(
  overrides: Partial<ExecutionProfileDetails>,
): ExecutionProfileDetails {
  return {
    id: "standard",
    label: "Standard",
    queryable: true,
    expected_speed: "medium",
    expected_llm_usage: "limited",
    graph_enabled: false,
    multimodal_processing: true,
    enrichment_enabled: false,
    domain_enrichment_enabled: false,
    validation_enabled: false,
    compile_lightrag_extraction: true,
    ...overrides,
  };
}


function _response(
  overrides: Partial<AssessmentPlanResponse> = {},
): AssessmentPlanResponse {
  return {
    documentId: "doc-1",
    recommendedProfile: "advanced",
    availableProfiles: [
      _profile({
        id: "minimum_queryable",
        expected_speed: "fast",
        expected_llm_usage: "none_or_minimal",
        graph_enabled: false,
        multimodal_processing: false,
        compile_lightrag_extraction: false,
      }),
      _profile({ id: "standard" }),
      _profile({
        id: "advanced",
        expected_speed: "slow",
        expected_llm_usage: "high",
        graph_enabled: true,
        enrichment_enabled: true,
      }),
    ],
    reasons: ["Document contains tables and images."],
    assessment: null,
    warnings: [],
    ...overrides,
  };
}


function _render(props: Partial<Parameters<typeof AssessmentPlanDialog>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(AssessmentPlanDialog, {
      filename: "report.pdf",
      plan: _response(),
      loadError: null,
      onConfirm: () => {},
      onCancel: () => {},
      ...props,
    }),
  );
}


describe("AssessmentPlanDialog — recommendation banner", () => {
  it("renders the recommended profile label", () => {
    const html = _render({
      plan: _response({ recommendedProfile: "advanced" }),
    });
    expect(html).toContain("Recommended:");
    expect(html).toContain("Advanced");
  });

  it("renders every reason from the backend", () => {
    const html = _render({
      plan: _response({
        reasons: [
          "Document contains scanned pages.",
          "Layout-heavy parsing recommended.",
        ],
      }),
    });
    expect(html).toContain("Document contains scanned pages");
    expect(html).toContain("Layout-heavy parsing recommended");
  });

  it("renders the filename in the header", () => {
    const html = _render({ filename: "bridge-report-2026.pdf" });
    expect(html).toContain("bridge-report-2026.pdf");
  });
});


describe("AssessmentPlanDialog — profile picker", () => {
  it("renders all three profile cards", () => {
    const html = _render();
    expect(html).toContain("assessment-plan-card-minimum_queryable");
    expect(html).toContain("assessment-plan-card-standard");
    expect(html).toContain("assessment-plan-card-advanced");
  });

  it("shows the Recommended pill on the recommended card only", () => {
    const html = _render({
      plan: _response({ recommendedProfile: "minimum_queryable" }),
    });
    expect(html).toContain("assessment-plan-recommended-pill-minimum_queryable");
    expect(html).not.toContain("assessment-plan-recommended-pill-standard");
    expect(html).not.toContain("assessment-plan-recommended-pill-advanced");
  });

  it("renders profile labels via the FE-side label helper", () => {
    const html = _render();
    expect(html).toContain("Minimum Queryable");
    expect(html).toContain("Standard");
    expect(html).toContain("Advanced");
  });

  it("renders the LightRAG-extraction honesty bullet on standard", () => {
    const html = _render();
    expect(html.toLowerCase()).toContain(
      "lightrag entity/relationship extraction still runs",
    );
  });

  it("pre-checks the recommended profile via the radio input", () => {
    const html = _render({
      plan: _response({ recommendedProfile: "minimum_queryable" }),
    });
    // The pre-checked card has the `--checked` modifier class.
    expect(html).toContain(
      "assessment-plan-dialog__profile-card--checked",
    );
  });
});


describe("AssessmentPlanDialog — state machine", () => {
  it("renders the loading state when plan is null and no error", () => {
    const html = _render({ plan: null, loadError: null });
    expect(html).toContain("Analysing document");
    // No picker rendered while loading — keep the user from
    // clicking on stale capability claims.
    expect(html).not.toContain("assessment-plan-card-standard");
  });

  it("disables the Start Indexing button while loading", () => {
    const html = _render({ plan: null });
    // React renders `disabled` as the bare attribute in static markup.
    const startBtn = html.match(
      /<button[^>]*assessment-plan-confirm[^>]*>/,
    )?.[0];
    expect(startBtn).toBeDefined();
    expect(startBtn).toContain("disabled");
  });

  it("renders the error banner with the load error message", () => {
    const html = _render({
      plan: null,
      loadError: "document is not registered",
    });
    expect(html).toContain("Could not analyse document");
    expect(html).toContain("document is not registered");
  });

  it("renders profiler warnings as a list", () => {
    const html = _render({
      plan: _response({
        warnings: ["file size exceeds 100MB threshold"],
      }),
    });
    expect(html).toContain("file size exceeds 100MB threshold");
  });
});


describe("AssessmentPlanDialog — action buttons", () => {
  it("renders Cancel and Start Indexing buttons", () => {
    const html = _render();
    expect(html).toContain("assessment-plan-cancel");
    expect(html).toContain("assessment-plan-confirm");
    expect(html).toContain("Cancel");
    expect(html).toContain("Start Indexing");
  });

  it("disables Start Indexing when a load error is present", () => {
    const html = _render({ loadError: "boom" });
    const startBtn = html.match(
      /<button[^>]*assessment-plan-confirm[^>]*>/,
    )?.[0];
    expect(startBtn).toContain("disabled");
  });

  it("enables Start Indexing once the plan loads successfully", () => {
    const html = _render();
    const startBtn = html.match(
      /<button[^>]*assessment-plan-confirm[^>]*>/,
    )?.[0];
    expect(startBtn).toBeDefined();
    expect(startBtn).not.toContain("disabled");
  });
});
