/**
 * User-selectable execution profiles for ingestion.
 *
 * Mirrors the backend's `j1.processing.execution_profile`. Wire
 * strings are stable across the API boundary; renaming requires
 * a coordinated backend + FE change.
 *
 * The values are intentionally a string literal union (not a TS
 * enum) so they round-trip through JSON.parse / JSON.stringify
 * unchanged and so feature-flag config files can reference them
 * as plain strings.
 */

export type ExecutionProfileId =
  | "minimum_queryable"
  | "standard"
  | "advanced";

/** Stable ordering for the profile picker UI. Matches backend
 * declaration order; tests pin this so reorders are intentional. */
export const PROFILE_ORDER: readonly ExecutionProfileId[] = [
  "minimum_queryable",
  "standard",
  "advanced",
];

/**
 * Per-profile details returned by `POST /documents/{id}/assessment-plan`.
 * Fields match `profile_details()` in
 * `src/j1/processing/execution_profile.py` — keep the key names
 * verbatim so adding a new flag on the backend lights up here
 * automatically.
 */
export interface ExecutionProfileDetails {
  id: ExecutionProfileId;
  label: string;
  /** True iff the profile produces a queryable index. All current
   * profiles are queryable; the field exists so a future
   * `noop_parse_only` profile can advertise the opposite. */
  queryable: boolean;
  /** "fast" | "medium" | "slow" */
  expected_speed: string;
  /** "none_or_minimal" | "limited" | "high" */
  expected_llm_usage: string;
  graph_enabled: boolean;
  multimodal_processing: boolean;
  enrichment_enabled: boolean;
  domain_enrichment_enabled: boolean;
  validation_enabled: boolean;
  /** Honest disclosure: true iff LightRAG's library-internal
   * entity/relationship extraction still fires inside compile
   * even though the workflow's downstream graph stage is gated.
   * `standard` exposes this as true so users see the trade-off
   * on the picker card; `minimum_queryable` is the only profile
   * that disables it (via the no-op `llm_model_func` hook). */
  compile_lightrag_extraction: boolean;
}

/**
 * Response of `POST /documents/{id}/assessment-plan`.
 *
 * Synchronous endpoint — runs the deterministic profiler + rule-
 * based assessment planner inline and returns the recommendation
 * without dispatching a workflow.
 */
export interface AssessmentPlanResponse {
  documentId: string;
  /** The planner's suggestion based on document signals. The user
   * may accept or override via the picker. */
  recommendedProfile: ExecutionProfileId;
  /** Full profile catalogue with cost / capability disclosures.
   * The FE picker renders these as radio cards without rebuilding
   * the copy on the client side. */
  availableProfiles: ExecutionProfileDetails[];
  /** Operator-readable strings explaining WHY the recommendation
   * was made (e.g. "Document contains scanned pages"). Surfaced
   * next to the picker so the user can audit the suggestion. */
  reasons: string[];
  /** Pre-compile assessment-plan payload (mode + capabilities)
   * — opaque dict the FE may surface in a debug drawer. Null when
   * the profiler couldn't analyze the document. */
  assessment: Record<string, unknown> | null;
  /** Profiler warnings (e.g. "file size exceeds threshold"). */
  warnings: string[];
}
