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

/** Stable vocabulary for the ``recommendationSource`` field. Mirrors
 * ``j1.processing.recommendation_resolver`` so renames require a
 * coordinated BE + FE change. */
export type RecommendationSource =
  | "user_override"
  | "llm_advanced_assessment"
  | "active_domain_rule"
  | "general_domain_rule"
  | "lightweight_assessment"
  | "lightweight_assessment_fallback"
  | "system_default";

/** Strict-JSON output of ``POST /documents/{id}/advanced-assessment``.
 * Mirrors ``LLMAdvancedAssessmentResult.to_payload()`` on the BE.
 * ``status='refused'`` carries ``refusalReason`` + ``message`` and
 * NO complexity / profile fields. */
export interface LLMAdvancedAssessmentResult {
  status: "ok" | "refused";
  refusalReason: string | null;
  message: string | null;
  documentComplexity: "simple" | "moderate" | "complex" | "very_complex" | null;
  recommendedProfile: "quick_index" | "standard_index" | "deep_knowledge_index" | null;
  confidence: "low" | "medium" | "high" | null;
  detectedSignals: Record<string, string>;
  recommendedNextSteps: string[];
  reasoningSummary: string[];
  warnings: string[];
}

/** Response shape for ``POST /documents/{id}/advanced-assessment``. */
export interface AdvancedAssessmentResponse {
  documentId: string;
  /** Server-minted id for the NEW ``AssessmentDecision`` that
   * carries the LLM result. ``null`` when the service refused or
   * the deployment didn't wire the decision store. */
  assessmentDecisionId: string | null;
  result: LLMAdvancedAssessmentResult;
}

/** One descriptor returned by ``GET /documents/{id}/manual-actions``.
 * Mirrors ``ManualActionDescriptor`` on the BE. */
export interface ManualActionDescriptor {
  id: string;
  label: string;
  description: string;
  costNote: string;
  status: "available" | "not_implemented" | "disabled";
}

/** Rule-based hints attached to a winning ``document_profile_rule``.
 * Advisory only — the FE renders these as "likely / suspected"
 * copy, not as facts. */
export interface AssessmentPlanRuleHints {
  likelyTables: boolean;
  likelyImages: boolean;
  likelyRequirements: boolean;
  likelyScanned: boolean;
  likelyLongDocument: boolean;
}

/** One ``document_profile_rule`` that fired during resolution.
 * The resolver surfaces every match (not just the winner) so the
 * audit panel can show why a candidate was demoted by priority. */
export interface MatchedProfileRule {
  ruleId: string;
  domainId: string;
  priority: number;
  recommendedProfile: ExecutionProfileId;
  confidence: number;
  reason: string;
  winner: boolean;
  hints: AssessmentPlanRuleHints;
}

/** Compile-option preview surfaced under the picker. Every field
 * is a SUSPICION, never a fact — the compile stage decides actual
 * behaviour. The dialog copy uses "likely / suspected" verbatim. */
export interface CompileOptionPreview {
  suspectedTables: boolean;
  suspectedImages: boolean;
  suspectedScanned: boolean;
  suspectedRequirements: boolean;
  suspectedLongDocument: boolean;
  note: string;
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
  /** Server-minted id for the persisted ``AssessmentDecision``.
   * Threaded back to ``POST /ingestion-runs`` as
   * ``assessmentDecisionId`` so the workflow consumes the same
   * recommendation the picker showed instead of re-running the
   * resolver downstream. ``null`` when the deployment didn't wire
   * the decision store (legacy path). */
  assessmentDecisionId: string | null;
  /** Active domain pack id that owned the resolution. Mirrors the
   * resolver's precedence chain (user > workspace default >
   * general). Surfaced for audit + future per-document overrides. */
  selectedDomainId: string;
  /** The planner's suggestion based on document signals. The user
   * may accept or override via the picker. */
  recommendedProfile: ExecutionProfileId;
  /** Which layer of the precedence chain produced the
   * recommendation. The dialog renders source-specific copy
   * (e.g. "Recommended by domain rule") so the operator
   * understands the authority. */
  recommendationSource: RecommendationSource;
  /** True when no domain or general rule matched the document's
   * filename / title and the recommendation fell back to the
   * lightweight profiler signals. The FE MUST show the standard
   * fallback warning when this is true. */
  fallbackUsed: boolean;
  /** Every rule that matched. The ``winner`` flag tells the FE
   * which one drove the recommendation. */
  matchedRules: MatchedProfileRule[];
  /** Full profile catalogue with cost / capability disclosures.
   * The FE picker renders these as radio cards without rebuilding
   * the copy on the client side. */
  availableProfiles: ExecutionProfileDetails[];
  /** Operator-readable strings explaining WHY the recommendation
   * was made. Hedged language ("likely / suspected") preferred. */
  reasons: string[];
  /** Pre-compile assessment-plan payload (mode + capabilities)
   * — opaque dict the FE may surface in a debug drawer. Null when
   * the profiler couldn't analyze the document. */
  assessment: Record<string, unknown> | null;
  /** Rule-based hints about compile-time behaviour. Advisory; the
   * compile / RAGAnything layers decide actual behaviour. */
  compileOptionPreview: CompileOptionPreview;
  /** Resolver warnings (env-disable downgrade messages, fallback
   * notice, profiler warnings). */
  warnings: string[];
  /** Most-recent LLM Advanced Assessment payload, if the operator
   * has run it for this document. ``null`` otherwise. Mirrors the
   * ``LLMAdvancedAssessmentResult`` shape. */
  llmAssessment?: LLMAdvancedAssessmentResult | null;
  /** Operator-triggered manual actions the LLM suggested running
   * AFTER indexing (wire ids from
   * ``j1.processing.manual_actions``). The picker renders each as
   * a disabled "Coming soon" button until the corresponding
   * endpoint is wired. */
  recommendedNextSteps?: string[];
}
