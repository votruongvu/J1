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
 * The labels are intentionally framed as "what you GET" (Quick /
 * Standard / Deep Knowledge Index) rather than the wire enum names
 * (minimum_queryable / standard / advanced) so the operator sees
 * an honest tradeoff axis. The wire strings stay stable — this
 * function is the single source of truth for the visible label.
 */
export function profileLabel(id: ExecutionProfileId): string {
  switch (id) {
    case "minimum_queryable":
      return "Quick Index";
    case "standard":
      return "Standard Index";
    case "advanced":
      return "Deep Knowledge Index";
  }
}


/** Short one-line description rendered under the profile label.
 *
 * Hedged: explains WHAT the operator gets, not what gets skipped.
 * The misleading "Graph extraction: no" framing moved into the
 * capability bullets where it can disclaim base RAGAnything
 * behaviour — see ``capabilityBullets`` below.
 */
export function profileTagline(id: ExecutionProfileId): string {
  switch (id) {
    case "minimum_queryable":
      return (
        "Quick Index: fastest, basic searchable document. "
        + "Best when you just need text retrieval."
      );
    case "standard":
      return (
        "Standard Index: balanced default for normal documents. "
        + "Suitable for most uploads."
      );
    case "advanced":
      return (
        "Deep Knowledge Index: slower, richer knowledge build. "
        + "Best for complex documents with tables, diagrams, or "
        + "requirements."
      );
  }
}


/** Source-aware copy for the "Why this recommendation?" line.
 * Mirrors the backend ``recommendation_resolver`` vocabulary. */
export function recommendationSourceLabel(
  source: RecommendationSource,
): string {
  switch (source) {
    case "user_override":
      return "Recommended by operator override";
    case "llm_advanced_assessment":
      return "Recommended by LLM assessment";
    case "active_domain_rule":
      return "Recommended by domain rule";
    case "general_domain_rule":
      return "Recommended by general rule";
    case "lightweight_assessment":
      return "Recommended by lightweight assessment";
    case "lightweight_assessment_fallback":
      return "Recommended by lightweight assessment fallback";
    case "system_default":
      return "Recommended by system default";
  }
}


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
  bullets.push(
    details.enrichment_enabled
      ? "Domain enrichment: yes"
      : "Domain enrichment: no — available as a manual action "
        + "after indexing",
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
