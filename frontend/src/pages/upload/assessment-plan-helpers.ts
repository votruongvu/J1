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
  type RecommendationSource,
} from "@/types/execution-profile";


/** Human-readable label rendered on the profile card.
 *
 * Post-collapse there is ONE user-facing profile: Knowledge Index.
 * Every legacy wire value also renders the same label so historical
 * run records render consistently. The legacy distinctions (Quick /
 * Standard / Deep) are retired — operators now control
 * image/table/equation processing via the per-request capability
 * checkboxes, not by picking a different profile.
 */
export function profileLabel(_id: ExecutionProfileId): string {
  return "Knowledge Index";
}


/** Short one-line description rendered under the profile label.
 *
 * Hedged: explains WHAT the operator gets, not what gets skipped.
 * The misleading "Graph extraction: no" framing moved into the
 * capability bullets where it can disclaim base RAGAnything
 * behaviour — see ``capabilityBullets`` below.
 */
export function profileTagline(_id: ExecutionProfileId): string {
  return (
    "Builds the base searchable knowledge graph/index. "
    + "This is the minimum valid J1 ingest output."
  );
}


/** Source-aware copy for the "Why this recommendation?" line.
 * Mirrors the backend ``recommendation_resolver`` vocabulary.
 *
 * Naming hygiene: every label here describes COMPILE-TIME
 * recommendations (image / table / equation processing + base
 * profile). It is the "Domain guidance" surface, NOT the
 * post-compile domain enrichment stage — see
 * [[DOMAIN_GUIDANCE_TOOLTIP]] below for the operator-facing
 * distinction. */
export function recommendationSourceLabel(
  source: RecommendationSource,
): string {
  switch (source) {
    case "user_override":
      return "Domain guidance — operator override";
    case "llm_advanced_assessment":
      return "Domain guidance — LLM assessment";
    case "active_domain_rule":
      return "Domain guidance — domain rule";
    case "general_domain_rule":
      return "Domain guidance — general rule";
    case "lightweight_assessment":
      return "Domain guidance — lightweight assessment";
    case "lightweight_assessment_fallback":
      return "Domain guidance — lightweight assessment fallback";
    case "system_default":
      return "Domain guidance — system default";
  }
}


/** Product-explainer tooltip body. Two-sentence version surfaced
 * under the recommendation panel + on a help icon. Pinned in
 * tests so the user-facing distinction stays explicit:
 *
 *   * Domain-guided compile — domain hints used to improve the
 *     BASE Knowledge Index DURING compile.
 *   * Post-compile domain enrichment — separate optional stage
 *     AFTER compile that creates extra domain artifacts /
 *     insights.
 *
 * Kept as a module constant (not inlined into the dialog) so
 * other surfaces (Run Detail compile-status panel, Reports tab)
 * can reuse the same copy without drift.
 */
export const DOMAIN_GUIDANCE_TOOLTIP =
  "Domain guidance improves the base compile. Post-compile "
  + "enrichment creates extra domain insights after the "
  + "Knowledge Index is ready.";


/** Longer-form tooltip used where the UI has the room (e.g. a
 * help drawer). Mirrors the prompt's "Product Explanation
 * Tooltip" copy verbatim. */
export const DOMAIN_GUIDANCE_TOOLTIP_LONG =
  "Domain-guided compile uses domain hints to improve the base "
  + "Knowledge Index during compile. It can recommend image, "
  + "table, or equation processing and may add a short domain "
  + "context to the compiler.\n\n"
  + "Post-compile domain enrichment is a separate stage after "
  + "compile. It can generate additional domain-specific "
  + "artifacts, checks, or insights when enabled or run "
  + "manually.";


/** Standard fallback warning. Pinned in tests; backend emits the
 * same string under ``warnings`` but the helper exists so the FE
 * test can locate the banner without a string-match against the
 * backend warning array. */
export const FALLBACK_WARNING_BODY =
  "No domain-specific document rule matched this filename/title. "
  + "This recommendation is based on lightweight assessment only. "
  + "Please choose based on the visible complexity of the document.";


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
 *
 * Every bullet is DERIVED FROM the backend ``ExecutionProfileDetails``
 * — never hard-coded prose claims like "Skips graph, enrichment,
 * and validation". That older copy went stale every time the
 * backend's capability matrix changed; this version follows the
 * data so a backend-side flip immediately reaches the UI.
 *
 * Honesty disclosure: surfaces ``compile_lightrag_extraction`` as
 * a separate bullet for ``standard`` / ``advanced`` so the user
 * sees the unavoidable library-internal LLM tax. Pinned in tests.
 */
export function capabilityBullets(
  details: ExecutionProfileDetails,
): readonly string[] {
  const bullets: string[] = [];
  bullets.push(details.queryable ? "Document is queryable" : "Not queryable");
  bullets.push(speedLabel(details));
  bullets.push(llmUsageLabel(details));
  // "Extra graph processing" framing instead of "graph extraction: no".
  // The base RAGAnything compile stage MAY still produce graph
  // artifacts as a side-effect of its built-in entity extraction —
  // we disclose that explicitly so the FE can't drift into asserting
  // "zero graph activity" when that's untrue.
  bullets.push(
    details.graph_enabled
      ? "Extra graph processing: yes"
      : "Extra graph processing: no — base RAGAnything compile may "
        + "still produce graph artifacts",
  );
  bullets.push(
    details.multimodal_processing
      ? "Multimodal processing: yes"
      : "Multimodal processing: no",
  );
  // Naming hygiene: "Domain enrichment" here always means the
  // SEPARATE post-compile stage that creates extra domain artifacts
  // / insights. It is NOT the same as the domain-guided compile
  // (capability recommendations + compile prompt context) that
  // happens DURING the base compile and is surfaced by the
  // recommendation panel below.
  bullets.push(
    details.enrichment_enabled
      ? "Post-compile domain enrichment: yes"
      : "Post-compile domain enrichment: no — available as a "
        + "manual action after indexing",
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
