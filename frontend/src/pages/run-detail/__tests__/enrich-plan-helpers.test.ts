/**
 * Pure-logic tests for the enrich-plan card helpers.
 *
 * Mirrors `compile-strategy-helpers.test.ts` — node env, no jsdom,
 * just the banner / availability / label decision tables.
 */

import { describe, expect, it } from "vitest";
import type {
  PostCompileEnrichPlanPayload,
  RunEnrichPlanResponse,
} from "@/types/review";
import {
  bannersForEnrichPlan,
  decisionSourceLabel,
  isEnrichPlanAvailable,
  recommendationLabel,
  taskLabel,
} from "../enrich-plan-helpers";


function _payload(
  overrides: Partial<PostCompileEnrichPlanPayload> = {},
): PostCompileEnrichPlanPayload {
  return {
    schema_version: "1",
    overall_recommendation: "recommended",
    reasons: ["document contains 2 image(s)"],
    recommended_tasks: ["image_captioning", "vision_enrichment"],
    skipped_tasks: ["table_enrichment", "quality_assessment"],
    blocking_issues: [],
    source_signals: { compile_status: "succeeded" },
    decision_source: "rule_based",
    ...overrides,
  };
}


describe("recommendationLabel", () => {
  it("maps each recommendation level to a human-readable label", () => {
    expect(recommendationLabel("skip")).toBe("Skip");
    expect(recommendationLabel("optional")).toBe("Optional");
    expect(recommendationLabel("recommended")).toBe("Recommended");
    expect(recommendationLabel("required")).toBe("Required");
  });
});


describe("bannersForEnrichPlan", () => {
  it("emits NO banner on a clean recommended/optional plan", () => {
    expect(bannersForEnrichPlan(_payload())).toEqual([]);
    expect(bannersForEnrichPlan(
      _payload({ overall_recommendation: "optional" }),
    )).toEqual([]);
  });

  it("emits an error banner with blocker reason for SKIP+blocking", () => {
    const banners = bannersForEnrichPlan(_payload({
      overall_recommendation: "skip",
      blocking_issues: ["compile failed; nothing to enrich"],
    }));
    expect(banners).toHaveLength(1);
    expect(banners[0].kind).toBe("err");
    expect(banners[0].testid).toBe("enrich-banner-skip");
    expect(banners[0].message).toContain("compile failed");
  });

  it("emits a warn banner for SKIP without blocking issues", () => {
    const banners = bannersForEnrichPlan(_payload({
      overall_recommendation: "skip",
      blocking_issues: [],
    }));
    expect(banners).toHaveLength(1);
    expect(banners[0].kind).toBe("warn");
    expect(banners[0].testid).toBe("enrich-banner-skip");
  });

  it("emits a warn banner for REQUIRED so operators see the must-do", () => {
    const banners = bannersForEnrichPlan(_payload({
      overall_recommendation: "required",
    }));
    expect(banners).toHaveLength(1);
    expect(banners[0].testid).toBe("enrich-banner-required");
  });
});


describe("isEnrichPlanAvailable", () => {
  function _resp(
    overrides: Partial<RunEnrichPlanResponse> = {},
  ): RunEnrichPlanResponse {
    return {
      runId: "run-1",
      documentId: "doc-1",
      status: "completed",
      unavailableReason: null,
      artifactId: "a-1",
      plan: _payload(),
      ...overrides,
    };
  }

  it("returns true for a real, populated plan", () => {
    expect(isEnrichPlanAvailable(_resp())).toBe(true);
  });

  it("returns false when status is unavailable", () => {
    expect(isEnrichPlanAvailable(_resp({
      status: "unavailable",
      plan: null,
      unavailableReason: "no artifact yet",
    }))).toBe(false);
  });

  it("returns false when plan is null even if status is completed", () => {
    // Defensive: backend should never emit completed+null, but the
    // helper guards against it.
    expect(isEnrichPlanAvailable(_resp({ plan: null }))).toBe(false);
  });
});


describe("decisionSourceLabel", () => {
  it("maps known sources to human-readable strings", () => {
    expect(decisionSourceLabel("rule_based")).toBe("Rule-based");
    expect(decisionSourceLabel("rule_based_with_fast_llm")).toBe(
      "Rule-based + fast LLM",
    );
  });

  it("falls back to the raw value for unknown sources", () => {
    // Keeps additions on the backend renderable instead of swallowing.
    expect(decisionSourceLabel("future_method")).toBe("future_method");
  });
});


describe("taskLabel", () => {
  it("maps known task ids to operator-friendly labels", () => {
    expect(taskLabel("table_enrichment")).toBe("Table enrichment");
    expect(taskLabel("image_captioning")).toBe("Image captioning");
    expect(taskLabel("vision_enrichment")).toBe("Vision enrichment");
    expect(taskLabel("quality_assessment")).toBe("Quality assessment");
  });

  it("falls back to the raw id for unknown tasks", () => {
    expect(taskLabel("unknown_task")).toBe("unknown_task");
  });
});
