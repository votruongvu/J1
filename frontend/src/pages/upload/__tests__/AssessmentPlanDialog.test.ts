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
    assessmentDecisionId: null,
    selectedDomainId: "general",
    recommendedProfile: "advanced",
    recommendationSource: "active_domain_rule",
    fallbackUsed: false,
    matchedRules: [],
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
    reasons: ["Document likely contains tables and images."],
    assessment: null,
    compileOptionPreview: {
      suspectedTables: false,
      suspectedImages: false,
      suspectedScanned: false,
      suspectedRequirements: false,
      suspectedLongDocument: false,
      note: "These are rule-based hints, not exact detection.",
    },
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
    // The label is now "Deep Knowledge Index" — framing what the
    // operator GETS, not the wire enum name.
    expect(html).toContain("Deep Knowledge Index");
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
    // Per the showcase spec: each card shows the "what you GET"
    // label — Quick Index / Standard Index / Deep Knowledge Index.
    const html = _render();
    expect(html).toContain("Quick Index");
    expect(html).toContain("Standard Index");
    expect(html).toContain("Deep Knowledge Index");
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


describe("AssessmentPlanDialog — recommendation source + fallback", () => {
  it("renders a source label for active-domain rule recommendations", () => {
    const html = _render({
      plan: _response({
        recommendationSource: "active_domain_rule",
        fallbackUsed: false,
      }),
    });
    expect(html).toContain("Recommended by domain rule");
    // And NOT the fallback banner.
    expect(html).not.toContain("assessment-plan-fallback-warning");
  });

  it("renders a different source label for general-rule recommendations", () => {
    const html = _render({
      plan: _response({
        recommendationSource: "general_domain_rule",
        fallbackUsed: false,
      }),
    });
    expect(html).toContain("Recommended by general rule");
    expect(html).not.toContain("assessment-plan-fallback-warning");
  });

  it("renders the LLM source label after Advanced Assessment", () => {
    // Pins the precedence-chain → UI mapping: when the operator
    // ran Advanced Assessment and got a result, the picker
    // attributes the recommendation to the LLM.
    const html = _render({
      plan: _response({
        recommendationSource: "llm_advanced_assessment",
        fallbackUsed: false,
      }),
    });
    expect(html).toContain("Recommended by LLM assessment");
  });

  it("renders the fallback warning when fallbackUsed=true", () => {
    const html = _render({
      plan: _response({
        recommendationSource: "lightweight_assessment_fallback",
        fallbackUsed: true,
      }),
    });
    expect(html).toContain("assessment-plan-fallback-warning");
    // Standard wording — pinned so a drift on either side is caught.
    expect(html).toContain("No domain-specific document rule matched");
    expect(html).toContain("lightweight assessment only");
    expect(html).toContain("visible complexity");
    expect(html).toContain("Recommended by lightweight assessment fallback");
  });

  it("renders the compile-option preview with hedged language", () => {
    const html = _render({
      plan: _response({
        compileOptionPreview: {
          suspectedTables: true,
          suspectedImages: false,
          suspectedScanned: true,
          suspectedRequirements: true,
          suspectedLongDocument: false,
          note: "These are rule-based hints, not exact detection.",
        },
      }),
    });
    expect(html).toContain("assessment-plan-compile-preview");
    // Hedged words, no exact-detection claims.
    expect(html).toContain("Tables likely");
    expect(html).toContain("Scanned content suspected");
    expect(html).toContain("Requirements likely (rule-based hint)");
    // The "not exact detection" disclaimer is surfaced verbatim.
    expect(html).toContain("not exact detection");
  });

  it("hides the compile-option preview when no hint is active", () => {
    const html = _render({
      plan: _response({
        compileOptionPreview: {
          suspectedTables: false,
          suspectedImages: false,
          suspectedScanned: false,
          suspectedRequirements: false,
          suspectedLongDocument: false,
          note: "n/a",
        },
      }),
    });
    expect(html).not.toContain("assessment-plan-compile-preview");
  });

  it("does NOT hard-code misleading 'Graph extraction: no' copy", () => {
    // Regression guard: the old bullet asserted "Graph extraction:
    // no" which lied about the base RAGAnything compile stage —
    // it may still produce graph artifacts. The current bullet
    // hedges with "Extra graph processing: no — base RAGAnything
    // compile may still produce graph artifacts".
    const html = _render();
    expect(html).not.toContain("Graph extraction: no");
    expect(html).not.toContain("Skips graph, enrichment, and validation");
    // The hedged disclaimer is rendered for the minimum_queryable
    // and standard cards.
    expect(html).toContain("base RAGAnything compile may");
  });

  it("filters the duplicate fallback warning out of the generic notes list", () => {
    // The fallback warning gets its own dedicated banner. The
    // "Notes on this document" generic list MUST NOT also surface
    // the same string — that would render the same banner twice.
    const html = _render({
      plan: _response({
        recommendationSource: "lightweight_assessment_fallback",
        fallbackUsed: true,
        warnings: [
          // The backend emits FALLBACK_WARNING in `warnings` too,
          // plus a profiler note. Only the profiler note should
          // surface in the generic "Notes" list.
          "No domain-specific document rule matched this filename/title. "
          + "This recommendation is based on lightweight assessment only. "
          + "Please choose based on the visible complexity of the document.",
          "file size exceeds 100MB threshold",
        ],
      }),
    });
    // Fallback banner gets the fallback copy.
    expect(html).toContain("assessment-plan-fallback-warning");
    // Generic notes list gets the OTHER warning.
    expect(html).toContain("file size exceeds 100MB threshold");
    // The fallback copy must not appear inside the generic notes
    // panel — check that the fallback string appears EXACTLY ONCE.
    const matches = html.match(/No domain-specific document rule matched/g);
    expect(matches?.length ?? 0).toBe(1);
  });
});


describe("AssessmentPlanDialog — Advanced Assessment trigger", () => {
  it("renders the Run Advanced Assessment button when handler is supplied", () => {
    // Operator-triggered ONLY — the dialog never auto-runs the LLM
    // assessment. The button exists so the user can opt in.
    const html = _render({ onRunAdvancedAssessment: () => {} });
    expect(html).toContain("assessment-plan-advanced-assessment-button");
    expect(html).toContain("Run Advanced Assessment");
  });

  it("hides the button when no handler is supplied", () => {
    // Deployments without the LLM service wired pass no handler.
    // The button must be hidden — clicking a 'Run Advanced'
    // button that resolves to "not configured" each time is a
    // worse UX than not showing it at all.
    const html = _render({ onRunAdvancedAssessment: undefined });
    expect(html).not.toContain("assessment-plan-advanced-assessment-button");
  });

  it("renders a busy state while the request is in flight", () => {
    const html = _render({
      onRunAdvancedAssessment: () => {},
      advancedAssessmentRunning: true,
    });
    expect(html).toContain("Running Advanced Assessment…");
    // Button is disabled so the operator can't double-click.
    const match = html.match(
      /<button[^>]*assessment-plan-advanced-assessment-button[^>]*>/,
    )?.[0];
    expect(match).toContain("disabled");
  });
});


describe("AssessmentPlanDialog — Advanced Assessment trigger", () => {
  it("does NOT render the trigger when no handler is supplied", () => {
    // Deployments that don't wire the LLM service must NOT advertise
    // a button the operator can't actually use. The button is hidden
    // by omitting ``onRunAdvancedAssessment`` from props.
    const html = _render();
    expect(html).not.toContain(
      "assessment-plan-advanced-assessment-button",
    );
  });

  it("renders the Run Advanced Assessment button when a handler is supplied", () => {
    const html = _render({
      onRunAdvancedAssessment: () => {},
    });
    expect(html).toContain(
      "assessment-plan-advanced-assessment-button",
    );
    expect(html).toContain("Run Advanced Assessment");
    // The hedge copy explaining cost + that it's an LLM estimate
    // must surface beneath the button.
    expect(html).toContain("Uses an LLM");
    expect(html).toContain("May cost more");
  });

  it("renders a busy state when the trigger is in flight", () => {
    const html = _render({
      onRunAdvancedAssessment: () => {},
      advancedAssessmentRunning: true,
    });
    expect(html).toContain("Running Advanced Assessment…");
    // And disables the button so the operator can't double-trigger.
    const btn = html.match(
      /<button[^>]*assessment-plan-advanced-assessment-button[^>]*>/,
    )?.[0];
    expect(btn).toBeDefined();
    expect(btn).toContain("disabled");
  });

  it("is NOT presented as part of the default Index path", () => {
    // The default render (no handler) doesn't expose the button at
    // all — the FE-side contract that Advanced Assessment is
    // operator-only.
    const html = _render();
    expect(html).not.toContain("Run Advanced Assessment");
    // And there's no copy implying the picker will run an LLM
    // automatically.
    expect(html.toLowerCase()).not.toContain("automatically uses");
    expect(html.toLowerCase()).not.toContain("auto llm");
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
