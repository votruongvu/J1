/**
 * Contract — Lightweight capability recommendations drive the
 * Knowledge Index checkboxes.
 *
 * Pins the FE side of the BE ``CapabilityRecommendations`` payload:
 *
 *   - ``confidence === "high"`` + ``recommended === true`` pre-checks
 *     the matching box AND surfaces a "Recommended" pill.
 *   - Medium / low confidence (or recommended=false) leaves the box
 *     unchecked even when reasons are present.
 *   - Each entry's ``reasons`` list renders as a small bulleted list
 *     under its checkbox label so the operator can see WHY.
 *   - Unchecking a high-confidence recommendation surfaces the
 *     informational override banner without blocking submit.
 *
 * The dialog stays presentation-only. Pre-check is driven by the
 * `_isPreChecked` predicate, so we exercise the full rendered
 * markup (static markup is enough — `useState`'s initialiser fires
 * synchronously on the first render).
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { AssessmentPlanDialog } from "../AssessmentPlanDialog";
import type {
  AssessmentPlanResponse,
  CapabilityRecommendationEntry,
  CapabilityRecommendationsPayload,
  RecommendationConfidence,
} from "@/types/execution-profile";


// ---- Fixture helpers -------------------------------------------


function _rec(
  recommended: boolean,
  confidence: RecommendationConfidence,
  sources: string[] = [],
  reasons: string[] = [],
): CapabilityRecommendationEntry {
  return { recommended, confidence, sources, reasons };
}


function _recs(
  image: CapabilityRecommendationEntry,
  table: CapabilityRecommendationEntry,
  equation: CapabilityRecommendationEntry,
  domainHints: string[] = [],
): CapabilityRecommendationsPayload {
  return {
    image_processing: image,
    table_processing: table,
    equation_processing: equation,
    domain_hints: domainHints,
  };
}


function _planWithRecs(
  recs: CapabilityRecommendationsPayload | null,
): AssessmentPlanResponse {
  return {
    documentId: "doc-test",
    assessmentDecisionId: null,
    selectedDomainId: "general",
    recommendedProfile: "knowledge_index",
    recommendationSource: "lightweight_assessment",
    fallbackUsed: false,
    matchedRules: [],
    availableProfiles: [
      {
        id: "knowledge_index",
        label: "Knowledge Index",
        queryable: true,
        expected_speed: "medium",
        expected_llm_usage: "limited",
        graph_enabled: true,
        multimodal_processing: true,
        enrichment_enabled: true,
        domain_enrichment_enabled: true,
        validation_enabled: true,
        compile_lightrag_extraction: true,
      },
    ],
    reasons: ["Document looks typical."],
    warnings: [],
    assessment: null,
    compileOptionPreview: {
      suspectedTables: false,
      suspectedImages: false,
      suspectedScanned: false,
      suspectedRequirements: false,
      suspectedLongDocument: false,
      note: "Hints, not promises.",
    },
    capabilityRecommendations: recs,
  };
}


function _renderDialog(plan: AssessmentPlanResponse): string {
  return renderToStaticMarkup(
    createElement(AssessmentPlanDialog, {
      filename: "spec.pdf",
      plan,
      loadError: null,
      onConfirm: () => {},
      onCancel: () => {},
    }),
  );
}


// ---- Pre-check predicate: high + recommended only --------------


describe("Capability recommendations — pre-check behavior", () => {
  it("pre-checks ONLY high-confidence + recommended boxes", () => {
    const recs = _recs(
      _rec(true, "high", ["filename:scanned"], ["Filename hint: scanned"]),
      _rec(true, "medium", ["filename:schedule"], ["Filename hint: schedule"]),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    // The static markup includes the `checked` attribute on the
    // image checkbox (high + recommended), but not on the others.
    const imageBlock = _extractBlock(
      html, "assessment-plan-capability-image",
    );
    const tableBlock = _extractBlock(
      html, "assessment-plan-capability-table",
    );
    const equationBlock = _extractBlock(
      html, "assessment-plan-capability-equation",
    );
    expect(imageBlock).toContain("checked");
    expect(tableBlock).not.toContain("checked");
    expect(equationBlock).not.toContain("checked");
  });

  it("does not pre-check when recommended=false even at high confidence", () => {
    // The recommender never emits recommended=false + high in
    // practice, but the predicate must defend against it anyway.
    const recs = _recs(
      _rec(false, "high", [], []),
      _rec(false, "high", [], []),
      _rec(false, "high", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(_extractBlock(html, "assessment-plan-capability-image"))
      .not.toContain("checked");
    expect(_extractBlock(html, "assessment-plan-capability-table"))
      .not.toContain("checked");
    expect(_extractBlock(html, "assessment-plan-capability-equation"))
      .not.toContain("checked");
  });

  it("leaves all boxes unchecked when recommendations payload is null", () => {
    const html = _renderDialog(_planWithRecs(null));
    expect(_extractBlock(html, "assessment-plan-capability-image"))
      .not.toContain("checked");
    expect(_extractBlock(html, "assessment-plan-capability-table"))
      .not.toContain("checked");
    expect(_extractBlock(html, "assessment-plan-capability-equation"))
      .not.toContain("checked");
  });

  it("leaves all boxes unchecked when capabilityRecommendations is absent (legacy)", () => {
    const plan = _planWithRecs(null);
    delete plan.capabilityRecommendations;
    const html = _renderDialog(plan);
    expect(_extractBlock(html, "assessment-plan-capability-image"))
      .not.toContain("checked");
  });
});


// ---- "Recommended" pill renders on high-confidence boxes only --


describe("Capability recommendations — Recommended pill", () => {
  it("renders the pill on high-confidence + recommended capabilities", () => {
    const recs = _recs(
      _rec(true, "high", ["filename:blueprint"], ["Filename hint: blueprint"]),
      _rec(true, "medium", ["filename:schedule"], ["Filename hint: schedule"]),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(html).toContain(
      'data-testid="assessment-plan-capability-image-recommended"',
    );
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-table-recommended"',
    );
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-equation-recommended"',
    );
  });

  it("does not render the pill when recommendations payload is null", () => {
    const html = _renderDialog(_planWithRecs(null));
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-image-recommended"',
    );
  });
});


// ---- Reasons render under the checkbox -------------------------


describe("Capability recommendations — reasons list", () => {
  it("renders the BE-supplied reasons under each checkbox", () => {
    const recs = _recs(
      _rec(
        true, "high",
        ["filename:scanned", "filename:figure"],
        ["Filename hint: scanned", "Filename hint: figure"],
      ),
      _rec(false, "low", [], []),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    const imageBlock = _extractBlock(
      html, "assessment-plan-capability-image",
    );
    expect(imageBlock).toContain("Filename hint: scanned");
    expect(imageBlock).toContain("Filename hint: figure");
    expect(html).toContain(
      'data-testid="assessment-plan-capability-image-reasons"',
    );
  });

  it("hides reasons when the entry isn't recommended", () => {
    // Low/not-recommended entries should not surface noisy reasons
    // — they'd suggest the operator should pick them.
    const recs = _recs(
      _rec(false, "low", [], ["No image signals fired"]),
      _rec(false, "low", [], []),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(html).not.toContain("No image signals fired");
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-image-reasons"',
    );
  });

  it("renders reasons for medium-confidence recommended entries", () => {
    // Medium = single signal fired. We don't auto-check, but the
    // reasons help the operator decide.
    const recs = _recs(
      _rec(true, "medium", ["filename:figure"], ["Filename hint: figure"]),
      _rec(false, "low", [], []),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(html).toContain("Filename hint: figure");
    expect(html).toContain(
      'data-testid="assessment-plan-capability-image-reasons"',
    );
    // Pill belongs to HIGH only.
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-image-recommended"',
    );
  });
});


// ---- Override warning fires only on the pre-checked default ----


describe("Capability recommendations — override warning", () => {
  it("does NOT fire on first render (default pre-check accepted)", () => {
    // Pre-check matches the recommendation, so there's no override
    // to warn about on the first paint.
    const recs = _recs(
      _rec(true, "high", ["filename:scanned"], ["Filename hint: scanned"]),
      _rec(false, "low", [], []),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-override-warning"',
    );
  });

  it("does NOT fire when no high-confidence recommendations exist", () => {
    const recs = _recs(
      _rec(true, "medium", ["filename:figure"], ["Filename hint: figure"]),
      _rec(false, "low", [], []),
      _rec(false, "low", [], []),
    );
    const html = _renderDialog(_planWithRecs(recs));
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-override-warning"',
    );
  });

  it("does NOT fire when recommendations payload is null", () => {
    const html = _renderDialog(_planWithRecs(null));
    expect(html).not.toContain(
      'data-testid="assessment-plan-capability-override-warning"',
    );
  });
});


// ---- Helper -----------------------------------------------------


/** Extract the substring containing a single capability row's
 * markup so we can assert on per-row attributes (checked /
 * pill / reasons) without false positives from sibling rows.
 *
 * The dialog renders each row as a `<label data-testid="...">`
 * — we slice from that opening tag to its closing `</label>`.
 */
function _extractBlock(html: string, testid: string): string {
  const start = html.indexOf(`data-testid="${testid}"`);
  if (start === -1) {
    throw new Error(`testid not found in markup: ${testid}`);
  }
  // Walk back to the opening `<label`
  const labelStart = html.lastIndexOf("<label", start);
  if (labelStart === -1) {
    throw new Error(`<label> opener not found before ${testid}`);
  }
  const labelEnd = html.indexOf("</label>", start);
  if (labelEnd === -1) {
    throw new Error(`<label> closer not found after ${testid}`);
  }
  return html.slice(labelStart, labelEnd + "</label>".length);
}
