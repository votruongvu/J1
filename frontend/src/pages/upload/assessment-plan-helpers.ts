/**
 * Pure helpers for the `AssessmentPlanDialog`.
 *
 * Side-effect-free so they're trivial to unit-test. The dialog
 * itself stays a thin renderer over these; new copy rules / UI
 * states should land here first and the dialog re-renders.
 *
 * Backend contract reference:
 * [`j1.processing.execution_profile`](../../../../src/j1/processing/execution_profile.py)
 * — keep wire strings + field names in lockstep.
 */

import {
  PROFILE_ORDER,
  type AssessmentPlanResponse,
  type ExecutionProfileDetails,
  type ExecutionProfileId,
} from "@/types/execution-profile";


/** Human-readable label rendered on the profile card. Single
 * source of truth on the FE so the dialog and any future
 * "current profile" badge stay in sync. */
export function profileLabel(id: ExecutionProfileId): string {
  switch (id) {
    case "minimum_queryable":
      return "Minimum Queryable";
    case "standard":
      return "Standard";
    case "advanced":
      return "Advanced";
  }
}


/** Short one-line description rendered under the profile label.
 * Mirrors the cost/quality copy from the task spec — kept here
 * (not in the backend response) so design tweaks don't require
 * a backend deploy. */
export function profileTagline(id: ExecutionProfileId): string {
  switch (id) {
    case "minimum_queryable":
      return "Fastest. Skips graph, enrichment, and validation.";
    case "standard":
      return "Balanced. Limited optional processing.";
    case "advanced":
      return "Highest quality. May run graph, multimodal, enrichment.";
  }
}


/** Speed label rendered as a chip. Maps the backend's
 * `expected_speed` string to a stable wire-style value the FE
 * can colour-code without parsing free-form English. */
export function speedLabel(details: ExecutionProfileDetails): string {
  switch (details.expected_speed) {
    case "fast":
      return "Fast";
    case "medium":
      return "Medium";
    case "slow":
      return "Slow";
    default:
      return details.expected_speed;
  }
}


/** LLM-usage chip copy. */
export function llmUsageLabel(details: ExecutionProfileDetails): string {
  switch (details.expected_llm_usage) {
    case "none_or_minimal":
      return "Minimal LLM usage";
    case "limited":
      return "Some LLM usage";
    case "high":
      return "High LLM usage";
    default:
      return details.expected_llm_usage;
  }
}


/**
 * Render the bullet-list of capability flags for a profile card.
 * Honesty disclosure: surfaces `compile_lightrag_extraction` as
 * a separate bullet for `standard` / `advanced` so the user
 * sees the unavoidable library-internal LLM tax. Pinned in tests.
 */
export function capabilityBullets(
  details: ExecutionProfileDetails,
): readonly string[] {
  const bullets: string[] = [];
  bullets.push(details.queryable ? "Document is queryable" : "Not queryable");
  bullets.push(speedLabel(details));
  bullets.push(llmUsageLabel(details));
  bullets.push(
    details.graph_enabled
      ? "Graph extraction: yes"
      : "Graph extraction: no",
  );
  bullets.push(
    details.multimodal_processing
      ? "Multimodal processing: yes"
      : "Multimodal processing: no",
  );
  bullets.push(
    details.enrichment_enabled
      ? "Enrichment: yes"
      : "Enrichment: no",
  );
  // Honesty bullet — only emit when the profile inherits the
  // library-internal extraction tax. Hidden for `minimum_queryable`
  // (the no-op short-circuits it) so the card stays clean.
  if (details.compile_lightrag_extraction) {
    bullets.push(
      "Note: LightRAG entity/relationship extraction still runs "
      + "inside compile (library limitation).",
    );
  }
  return bullets;
}


/** Order the catalogue by the canonical `PROFILE_ORDER`. Defends
 * against a backend that might return them in a different order
 * — the picker always renders minimum/standard/advanced in that
 * order top-to-bottom. */
export function orderedProfiles(
  resp: AssessmentPlanResponse,
): readonly ExecutionProfileDetails[] {
  const byId = new Map(resp.availableProfiles.map((p) => [p.id, p]));
  const ordered: ExecutionProfileDetails[] = [];
  for (const id of PROFILE_ORDER) {
    const entry = byId.get(id);
    if (entry !== undefined) ordered.push(entry);
  }
  // Append any unknown profiles (forward-compatibility: a future
  // backend adds a 4th profile and we want it visible rather
  // than silently dropped).
  for (const p of resp.availableProfiles) {
    if (!PROFILE_ORDER.includes(p.id)) ordered.push(p);
  }
  return ordered;
}


/**
 * Default initial selection for the radio group. We pre-select
 * the BACKEND-RECOMMENDED profile so the user sees a sensible
 * default — but the picker is still active and one click switches.
 * Falls back to `standard` if the recommendation isn't in the
 * catalogue (shouldn't happen; defensive).
 */
export function defaultInitialSelection(
  resp: AssessmentPlanResponse,
): ExecutionProfileId {
  const recommended = resp.recommendedProfile;
  const present = resp.availableProfiles.some((p) => p.id === recommended);
  return present ? recommended : "standard";
}
