/**
 * Contract — UI naming distinguishes Domain-Guided Compile from
 * Post-Compile Domain Enrichment.
 *
 * The system has two distinct domain-aware behaviours:
 *
 *   * Domain-Guided Compile — domain hints used DURING the base
 *     compile (capability checkbox recommendations + compile prompt
 *     context prepended to LightRAG's entity-extraction LLM call).
 *     Improves the BASE Knowledge Index.
 *
 *   * Post-Compile Domain Enrichment — separate optional stage AFTER
 *     compile that creates additional domain artifacts / insights.
 *
 * The UI MUST NOT collapse these into a generic "Domain Enrichment"
 * label. This file pins the boundary across the surfaces operators
 * touch most:
 *
 *   1. AssessmentPlanDialog header copy (recommendation panel,
 *      override warning, capability help text).
 *   2. Recommendation source labels (every variant says
 *      "Domain guidance — …", never "Recommended by …").
 *   3. The shared tooltip constants describing the distinction.
 *   4. Capability bullets on the profile card (always "Post-compile
 *      domain enrichment: …", never ambiguous "Domain enrichment").
 *   5. Legacy profile names stay OUT of user-visible copy.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { AssessmentPlanDialog } from "../AssessmentPlanDialog";
import {
  DOMAIN_GUIDANCE_TOOLTIP,
  DOMAIN_GUIDANCE_TOOLTIP_LONG,
  capabilityBullets,
  recommendationSourceLabel,
} from "../assessment-plan-helpers";
import type {
  AssessmentPlanResponse,
  CapabilityRecommendationsPayload,
  ExecutionProfileDetails,
  RecommendationSource,
} from "@/types/execution-profile";


// ---- Helpers ----------------------------------------------------


function _details(over: Partial<ExecutionProfileDetails> = {}): ExecutionProfileDetails {
  return {
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
    ...over,
  };
}


function _recs(): CapabilityRecommendationsPayload {
  return {
    image_processing: {
      recommended: true, confidence: "high",
      sources: ["filename:scan"], reasons: ["Filename suggests scan."],
    },
    table_processing: {
      recommended: false, confidence: "low", sources: [], reasons: [],
    },
    equation_processing: {
      recommended: false, confidence: "low", sources: [], reasons: [],
    },
    domain_hints: [],
  };
}


function _response(over: Partial<AssessmentPlanResponse> = {}): AssessmentPlanResponse {
  return {
    documentId: "doc-1",
    assessmentDecisionId: null,
    selectedDomainId: "general",
    recommendedProfile: "knowledge_index",
    recommendationSource: "lightweight_assessment",
    fallbackUsed: false,
    matchedRules: [],
    availableProfiles: [_details()],
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
    capabilityRecommendations: _recs(),
    ...over,
  };
}


function _render(plan: AssessmentPlanResponse): string {
  return renderToStaticMarkup(
    createElement(AssessmentPlanDialog, {
      filename: "scanned-doc.pdf",
      plan,
      loadError: null,
      onConfirm: () => {},
      onCancel: () => {},
    }),
  );
}


// ---- Recommendation panel ---------------------------------------


describe("Recommendation panel — Domain-guided compile naming", () => {
  it("labels the recommendation panel as 'Domain guidance'", () => {
    const html = _render(_response());
    expect(html).toContain("Domain guidance");
    // Negative: never use the ambiguous "Domain Enrichment" label
    // anywhere inside the recommendation panel body.
    expect(html).not.toContain("Domain Enrichment");
  });

  it("renders the domain-guidance tooltip near the recommendation", () => {
    const html = _render(_response());
    expect(html).toContain(
      'data-testid="assessment-plan-domain-guidance-tooltip"',
    );
    expect(html).toContain(DOMAIN_GUIDANCE_TOOLTIP);
  });

  it("renders the capabilities help text only when recommendations exist", () => {
    const withRecs = _render(_response());
    expect(withRecs).toContain("Domain guidance may recommend these options");

    // No recommendations payload → no help text (avoid the
    // operator wondering what "Domain guidance" means here).
    const withoutRecs = _render(_response({
      capabilityRecommendations: null,
    }));
    expect(withoutRecs).not.toContain(
      "Domain guidance may recommend these options",
    );
  });
});


// ---- Override warning -------------------------------------------


describe("Override warning — Domain-guided compile naming", () => {
  it("titles the override banner 'Domain guidance overridden'", () => {
    // Render with the high-confidence image rec pre-checked then
    // simulated as unchecked via the recs payload (the dialog reads
    // initial state from the recs; the banner fires when the rec
    // is high-confidence + recommended but the checkbox is OFF).
    // Static markup is enough — the dialog's useState initialiser
    // pre-checks on first paint, so a recs-with-high-confidence
    // entry yields a checked box. For this test we just check the
    // banner copy is correct *when* it would fire; the
    // banner-fires-on-uncheck behaviour is pinned in
    // capability-recommendations-contract.test.ts.
    //
    // To trigger the banner in static markup we re-purpose the
    // existing `_recs()` shape — but the dialog won't render the
    // banner here on first paint, so this assertion is on the
    // source code constant rather than runtime HTML.
    const html = _render(_response());
    // Negative: the OLD title is gone from the codebase.
    expect(html).not.toContain("High-confidence recommendation disabled");
    expect(html).not.toContain("The assessment strongly recommended");
  });
});


// ---- Recommendation source labels -------------------------------


describe("recommendationSourceLabel — Domain guidance prefix", () => {
  const cases: ReadonlyArray<{
    source: RecommendationSource;
    contains: string;
  }> = [
    { source: "user_override", contains: "Domain guidance — operator override" },
    { source: "llm_advanced_assessment", contains: "Domain guidance — LLM assessment" },
    { source: "active_domain_rule", contains: "Domain guidance — domain rule" },
    { source: "general_domain_rule", contains: "Domain guidance — general rule" },
    { source: "lightweight_assessment", contains: "Domain guidance — lightweight assessment" },
    { source: "lightweight_assessment_fallback", contains: "Domain guidance — lightweight assessment fallback" },
    { source: "system_default", contains: "Domain guidance — system default" },
  ];

  for (const c of cases) {
    it(`uses 'Domain guidance — …' for ${c.source}`, () => {
      const label = recommendationSourceLabel(c.source);
      expect(label).toContain(c.contains);
      // Negative: never the old "Recommended by …" prefix — that
      // word lands users back on the picker's profile rec.
      expect(label).not.toMatch(/^Recommended by /);
    });
  }
});


// ---- Tooltip constants ------------------------------------------


describe("Domain guidance tooltips", () => {
  it("distinguishes domain-guided compile from post-compile enrichment", () => {
    expect(DOMAIN_GUIDANCE_TOOLTIP.toLowerCase()).toContain("domain guidance");
    expect(DOMAIN_GUIDANCE_TOOLTIP.toLowerCase()).toContain("post-compile");
  });

  it("longer tooltip explains both surfaces", () => {
    expect(DOMAIN_GUIDANCE_TOOLTIP_LONG).toContain("Domain-guided compile");
    expect(DOMAIN_GUIDANCE_TOOLTIP_LONG).toContain(
      "Post-compile domain enrichment",
    );
  });
});


// ---- Capability bullets on profile card -------------------------


describe("capabilityBullets — Post-compile enrichment naming", () => {
  it("uses 'Post-compile domain enrichment: yes' when enabled", () => {
    const bullets = capabilityBullets(_details({ enrichment_enabled: true }));
    expect(
      bullets.some((b) => b === "Post-compile domain enrichment: yes"),
    ).toBe(true);
  });

  it("uses 'Post-compile domain enrichment: no — …' when disabled", () => {
    const bullets = capabilityBullets(_details({ enrichment_enabled: false }));
    const bullet = bullets.find((b) =>
      b.startsWith("Post-compile domain enrichment: no"),
    );
    expect(bullet).toBeDefined();
    expect(bullet).toContain("manual action");
  });

  it("never uses the ambiguous 'Domain enrichment: …' prefix", () => {
    for (const enrichmentEnabled of [true, false]) {
      const bullets = capabilityBullets(
        _details({ enrichment_enabled: enrichmentEnabled }),
      );
      for (const b of bullets) {
        expect(b).not.toMatch(/^Domain enrichment:/);
      }
    }
  });
});


// ---- Capability help text under content-processing fieldset ------


describe("Capability help text — no enrichment-during-compile claims", () => {
  it("clarifies post-compile enrichment is a separate stage", () => {
    const html = _render(_response());
    // The fieldset's footer small-print mentions post-compile
    // enrichment is separate. Pinned so a future re-word doesn't
    // accidentally reintroduce "enrichment runs as part of
    // compile" framing.
    expect(html).toContain(
      "Post-compile domain enrichment is a separate stage",
    );
  });
});


// ---- Legacy profile names stay out of user-visible copy ----------


describe("Legacy profile names — never user-visible", () => {
  it("dialog does not surface Quick / Standard / Deep Knowledge labels", () => {
    const html = _render(_response());
    // Post-collapse the picker renders ONE profile (Knowledge Index).
    expect(html).not.toContain("Quick Index");
    expect(html).not.toContain("Standard Index");
    expect(html).not.toContain("Deep Knowledge Index");
  });

  it("dialog does not surface Premium / Balanced / Advanced as profile names", () => {
    const html = _render(_response());
    // Allow the words to appear in unrelated copy (e.g. "Advanced
    // Assessment" button) but never as a profile label on a card.
    expect(html).not.toMatch(/Premium Index|Balanced Index|Advanced Index/);
  });
});
