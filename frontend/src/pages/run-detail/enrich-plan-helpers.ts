/**
 * Pure helpers for the post-compile enrich-plan card.
 *
 * Mirrors the pattern in `compile-strategy-helpers.ts` — all
 * banner/recommendation logic lives here as side-effect-free
 * functions so the FE can pin them without standing up jsdom.
 *
 * Source of truth: the
 * `GET /ingestion-runs/{run_id}/enrich-plan` envelope, served
 * from the `post_compile_enrich_plan` artifact written by the
 * workflow's `_run_post_compile_enrich_assessment` step. See
 * `j1.processing.enrich_assessment.PostCompileEnrichPlan`.
 */

import type {
  PostCompileEnrichPlanPayload,
  RunEnrichPlanResponse,
} from "@/types/review";

export type EnrichRecommendation =
  | "skip"
  | "optional"
  | "recommended"
  | "required";

export interface EnrichPlanBanner {
  /** "warn" | "err" | "info" — drives the FE's banner colour. */
  kind: "warn" | "err" | "info";
  message: string;
  /** Stable testid the FE can assert on; one per banner variant. */
  testid: string;
}

/**
 * Operator-readable label for each recommendation level. Centralised
 * so future label tweaks only land here.
 */
export function recommendationLabel(rec: EnrichRecommendation): string {
  switch (rec) {
    case "skip":
      return "Skip";
    case "optional":
      return "Optional";
    case "recommended":
      return "Recommended";
    case "required":
      return "Required";
  }
}

/**
 * Banner decision matrix. Returns 0..1 banner depending on the
 * recommendation level + presence of blocking issues. The FE renders
 * them in order; banners further down the list are higher priority.
 */
export function bannersForEnrichPlan(
  plan: PostCompileEnrichPlanPayload,
): EnrichPlanBanner[] {
  const banners: EnrichPlanBanner[] = [];
  const rec = plan.overall_recommendation;
  if (rec === "skip") {
    const blockers = plan.blocking_issues ?? [];
    banners.push({
      kind: blockers.length > 0 ? "err" : "warn",
      message: blockers.length > 0
        ? `Enrichment skipped: ${blockers.join("; ")}`
        : "Enrichment skipped by post-compile assessment.",
      testid: "enrich-banner-skip",
    });
    return banners;
  }
  if (rec === "required") {
    banners.push({
      kind: "warn",
      message: "Enrichment is required for this document.",
      testid: "enrich-banner-required",
    });
  }
  return banners;
}

/**
 * Returns true when the response represents a real plan we can
 * render. False when the run hasn't reached post-compile yet, the
 * artifact wasn't persisted, or the payload is malformed (the
 * backend already collapses these into status="unavailable").
 */
export function isEnrichPlanAvailable(
  resp: RunEnrichPlanResponse,
): resp is RunEnrichPlanResponse & { plan: PostCompileEnrichPlanPayload } {
  return resp.status === "completed" && resp.plan !== null;
}

/**
 * Operator-readable label for `decision_source`. Shown on the card
 * so reviewers know whether the recommendation came from the
 * deterministic rules or whether a fast-LLM consult augmented it.
 */
export function decisionSourceLabel(source: string): string {
  switch (source) {
    case "rule_based":
      return "Rule-based";
    case "rule_based_with_fast_llm":
      return "Rule-based + fast LLM";
    default:
      return source;
  }
}

/**
 * Pretty operator-readable name for an enrich task id. Falls back to
 * the raw id if unknown so additions in the backend don't silently
 * disappear.
 */
export function taskLabel(taskId: string): string {
  switch (taskId) {
    case "table_enrichment":
      return "Table enrichment";
    case "image_captioning":
      return "Image captioning";
    case "vision_enrichment":
      return "Vision enrichment";
    case "requirement_extraction":
      return "Requirement extraction";
    case "risk_extraction":
      return "Risk extraction";
    case "quality_assessment":
      return "Quality assessment";
    default:
      return taskId;
  }
}
