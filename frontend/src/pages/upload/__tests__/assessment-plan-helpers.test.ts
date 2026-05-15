/**
 * Pure-helper tests for the AssessmentPlanDialog.
 *
 * No DOM, no React Testing Library — these helpers are pure
 * functions, so we test them like any other module.
 *
 * Why exhaustive coverage:
 * The dialog renders cost/quality copy directly from these
 * helpers. A regression here means the user sees the wrong
 * trade-off on a profile card — which silently misleads them
 * about LLM cost. Pin every label and the honesty bullet.
 */

import { describe, expect, it } from "vitest";

import type {
  AssessmentPlanResponse,
  ExecutionProfileDetails,
} from "@/types/execution-profile";

import {
  capabilityBullets,
  defaultInitialSelection,
  llmUsageLabel,
  orderedProfiles,
  profileLabel,
  profileTagline,
  speedLabel,
} from "../assessment-plan-helpers";


function _details(
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
    recommendedProfile: "standard",
    recommendationSource: "lightweight_assessment",
    fallbackUsed: false,
    matchedRules: [],
    availableProfiles: [
      _details({ id: "minimum_queryable" }),
      _details({ id: "standard" }),
      _details({ id: "advanced" }),
    ],
    reasons: [],
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


// ---- profileLabel / profileTagline -----------------------------


describe("profileLabel", () => {
  it("renders Minimum Queryable / Standard / Advanced", () => {
    expect(profileLabel("minimum_queryable")).toBe("Minimum Queryable");
    expect(profileLabel("standard")).toBe("Standard");
    expect(profileLabel("advanced")).toBe("Advanced");
  });
});


describe("profileTagline", () => {
  it("returns a non-empty tagline for every profile", () => {
    for (const id of ["minimum_queryable", "standard", "advanced"] as const) {
      const tag = profileTagline(id);
      expect(tag).toBeTruthy();
      expect(tag.length).toBeGreaterThan(10);
    }
  });

  it("avoids hard-coded misleading capability claims", () => {
    // Regression guard: the old tagline asserted "Skips graph,
    // enrichment, and validation" which drifted out of sync every
    // time the backend's capability matrix changed. The refactor
    // moved per-profile specifics to the data-driven bullets and
    // keeps the tagline generic + hedged.
    for (const id of ["minimum_queryable", "standard", "advanced"] as const) {
      const tag = profileTagline(id).toLowerCase();
      expect(tag).not.toContain("skips graph");
      expect(tag).not.toContain("enrichment, and validation");
      expect(tag).not.toContain("highest quality");
    }
  });

  it("points the operator at the data-driven bullets", () => {
    expect(profileTagline("minimum_queryable").toLowerCase()).toContain(
      "current behaviour",
    );
  });
});


// ---- speedLabel / llmUsageLabel --------------------------------


describe("speedLabel", () => {
  it("maps fast/medium/slow to title-case", () => {
    expect(speedLabel(_details({ expected_speed: "fast" }))).toBe("Fast");
    expect(speedLabel(_details({ expected_speed: "medium" }))).toBe("Medium");
    expect(speedLabel(_details({ expected_speed: "slow" }))).toBe("Slow");
  });

  it("falls back to the raw value for unknown speeds", () => {
    expect(speedLabel(_details({ expected_speed: "blazing" }))).toBe("blazing");
  });
});


describe("llmUsageLabel", () => {
  it("maps the three canonical values to human copy", () => {
    expect(llmUsageLabel(_details({ expected_llm_usage: "none_or_minimal" })))
      .toBe("Minimal LLM usage");
    expect(llmUsageLabel(_details({ expected_llm_usage: "limited" })))
      .toBe("Some LLM usage");
    expect(llmUsageLabel(_details({ expected_llm_usage: "high" })))
      .toBe("High LLM usage");
  });
});


// ---- capabilityBullets ----------------------------------------


describe("capabilityBullets", () => {
  it("renders 'Document is queryable' for queryable profiles", () => {
    const bullets = capabilityBullets(_details({ queryable: true }));
    expect(bullets[0]).toBe("Document is queryable");
  });

  it("renders 'Graph extraction: no' when graph_enabled=false", () => {
    const bullets = capabilityBullets(_details({ graph_enabled: false }));
    expect(bullets.some((b) => b.includes("Graph extraction: no"))).toBe(true);
  });

  it("emits the honesty bullet for standard (LightRAG tax still fires)", () => {
    const bullets = capabilityBullets(
      _details({ id: "standard", compile_lightrag_extraction: true }),
    );
    expect(
      bullets.some((b) =>
        b.toLowerCase().includes("lightrag entity/relationship extraction"),
      ),
    ).toBe(true);
  });

  it("does NOT emit the honesty bullet for minimum_queryable", () => {
    const bullets = capabilityBullets(
      _details({
        id: "minimum_queryable",
        compile_lightrag_extraction: false,
      }),
    );
    expect(
      bullets.some((b) =>
        b.toLowerCase().includes("lightrag entity/relationship extraction"),
      ),
    ).toBe(false);
  });
});


// ---- orderedProfiles ------------------------------------------


describe("orderedProfiles", () => {
  it("orders the three known profiles minimum → standard → advanced", () => {
    const ordered = orderedProfiles(_response());
    expect(ordered.map((p) => p.id)).toEqual([
      "minimum_queryable",
      "standard",
      "advanced",
    ]);
  });

  it("preserves the canonical order even when backend reverses it", () => {
    const resp = _response({
      availableProfiles: [
        _details({ id: "advanced" }),
        _details({ id: "standard" }),
        _details({ id: "minimum_queryable" }),
      ],
    });
    expect(orderedProfiles(resp).map((p) => p.id)).toEqual([
      "minimum_queryable",
      "standard",
      "advanced",
    ]);
  });

  it("drops missing profiles silently rather than blowing up", () => {
    const resp = _response({
      availableProfiles: [_details({ id: "standard" })],
    });
    expect(orderedProfiles(resp).map((p) => p.id)).toEqual(["standard"]);
  });
});


// ---- defaultInitialSelection ----------------------------------


describe("defaultInitialSelection", () => {
  it("pre-selects the backend-recommended profile", () => {
    expect(
      defaultInitialSelection(_response({ recommendedProfile: "advanced" })),
    ).toBe("advanced");
  });

  it("falls back to standard when the recommendation is absent", () => {
    const resp = _response({
      recommendedProfile: "advanced" as const,
      availableProfiles: [_details({ id: "minimum_queryable" })],
    });
    expect(defaultInitialSelection(resp)).toBe("standard");
  });
});
