/**
 * Contract — Knowledge Index picker (single profile + 3 checkboxes).
 *
 * Post-collapse the ingest dialog renders ONE profile card
 * ("Knowledge Index") plus three independent capability
 * checkboxes (Process images / tables / equations). The legacy
 * "Quick / Standard / Deep" multi-card picker is retired. This
 * file pins the new shape as a single regression document.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

import { AssessmentPlanDialog } from "../AssessmentPlanDialog";
import {
  profileLabel,
  profileTagline,
} from "../assessment-plan-helpers";
import type { AssessmentPlanResponse } from "@/types/execution-profile";


function _knowledgeIndexCatalogueResponse(): AssessmentPlanResponse {
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
  };
}


// ---- Helpers collapse all legacy ids to one label --------------


describe("profileLabel — every id renders Knowledge Index", () => {
  it("returns the same label for every profile id", () => {
    expect(profileLabel("knowledge_index")).toBe("Knowledge Index");
    expect(profileLabel("minimum_queryable")).toBe("Knowledge Index");
    expect(profileLabel("standard")).toBe("Knowledge Index");
    expect(profileLabel("advanced")).toBe("Knowledge Index");
  });

  it("profileTagline returns the same promise for every id", () => {
    for (const id of [
      "knowledge_index", "minimum_queryable", "standard", "advanced",
    ] as const) {
      const tag = profileTagline(id).toLowerCase();
      expect(tag).toContain("searchable knowledge");
    }
  });
});


// ---- AssessmentPlanDialog wires the three capability checkboxes


describe("AssessmentPlanDialog — capability checkboxes", () => {
  it("renders the three capability checkbox surfaces", () => {
    const html = renderToStaticMarkup(
      createElement(AssessmentPlanDialog, {
        filename: "spec.pdf",
        plan: _knowledgeIndexCatalogueResponse(),
        loadError: null,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );
    expect(html).toContain("assessment-plan-capabilities");
    expect(html).toContain("assessment-plan-capability-image");
    expect(html).toContain("assessment-plan-capability-table");
    expect(html).toContain("assessment-plan-capability-equation");
  });

  it("renders the operator-facing checkbox copy", () => {
    const html = renderToStaticMarkup(
      createElement(AssessmentPlanDialog, {
        filename: "spec.pdf",
        plan: _knowledgeIndexCatalogueResponse(),
        loadError: null,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );
    expect(html).toContain("Process images");
    expect(html).toContain("Process tables");
    expect(html).toContain("Process equations");
    expect(html).toContain("Additional content processing");
  });

  it("renders the warning copy about adapter limitations", () => {
    const html = renderToStaticMarkup(
      createElement(AssessmentPlanDialog, {
        filename: "spec.pdf",
        plan: _knowledgeIndexCatalogueResponse(),
        loadError: null,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );
    expect(html).toContain("RAGAnything");
    expect(html).toContain("records a warning");
  });
});


// ---- The title is the new operator-facing prompt --------------


describe("AssessmentPlanDialog — header copy", () => {
  it("uses the new 'Configure knowledge indexing' title", () => {
    const html = renderToStaticMarkup(
      createElement(AssessmentPlanDialog, {
        filename: "spec.pdf",
        plan: _knowledgeIndexCatalogueResponse(),
        loadError: null,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );
    expect(html).toContain("Configure knowledge indexing");
    expect(html).not.toContain("How thorough should this ingest");
  });
});


// ---- Catalogue with one entry renders one card ----------------


describe("AssessmentPlanDialog — single-card catalogue", () => {
  it("renders exactly one profile card for the post-collapse catalogue", () => {
    const html = renderToStaticMarkup(
      createElement(AssessmentPlanDialog, {
        filename: "spec.pdf",
        plan: _knowledgeIndexCatalogueResponse(),
        loadError: null,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );
    // The card is rendered.
    expect(html).toContain("assessment-plan-card-knowledge_index");
    // Legacy profile ids do NOT appear in the rendered catalogue
    // (the backend post-collapse only ships knowledge_index).
    expect(html).not.toContain("assessment-plan-card-minimum_queryable");
    expect(html).not.toContain("assessment-plan-card-standard");
    expect(html).not.toContain("assessment-plan-card-advanced");
  });
});
