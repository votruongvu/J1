/**
 * Ingestion-result review types.
 *
 * Mirror of the backend's `j1.ingestion_review.dtos` shapes — kept
 * in their own file (not `ingestion.ts`) so the existing run/plan/
 * event types stay focused. All fields are camelCase, matching the
 * REST envelope wire format.
 *
 * Translator lives at `lib/api/translate.ts` (`*FromApi` helpers).
 * Components import from here.
 */

// ---- Step / warning records ---------------------------------------

export interface ReviewStepError {
  type: string;
  message: string;
  retryable: boolean;
}

export interface ReviewStepResult {
  step: string;
  status: string;
  required: boolean;
  source: string;
  startedAt?: string | null;
  completedAt?: string | null;
  durationMs?: number | null;
  reason?: string | null;
  error?: ReviewStepError | null;
  artifactCount: number;
  metadata: Record<string, unknown>;
}

export interface ReviewWarning {
  code: string;
  message: string;
  /** "info" | "warning" | "error" */
  severity: string;
  step?: string | null;
  documentId?: string | null;
  page?: number | null;
  chunkId?: string | null;
  artifactId?: string | null;
}

// ---- Availability -------------------------------------------------

export interface ReviewAvailability {
  available: boolean;
  reason?: string | null;
}

export interface ReviewAvailableViews {
  chunks: ReviewAvailability;
  assets: ReviewAvailability;
  graph: ReviewAvailability;
  quality: ReviewAvailability;
  rawArtifacts: ReviewAvailability;
  validation: ReviewAvailability;
  // Optional: backend added this in the Content Inventory release.
  // Older API responses omit it; the FE handles `undefined` by
  // showing the tab as disabled with a generic reason.
  parsedContent?: ReviewAvailability;
  // Optional: backend added this in the Planning Report release.
  // Same forward-compat treatment as `parsedContent`.
  planning?: ReviewAvailability;
}

// ---- Content Inventory (parsed-content manifest projection) ---

export interface ContentInventorySource {
  compiler?: string | null;
  parser?: string | null;
  parserVersion?: string | null;
  parseMethod?: string | null;
  profile?: string | null;
}

export interface ContentInventorySummary {
  pageCount?: number | null;
  textBlockCount: number;
  tableCount: number;
  imageCount: number;
  formulaCount: number;
  headingCount?: number | null;
  otherCount: number;
  totalItems: number;
}

export interface ContentInventoryItem {
  itemId: string;
  /** "text" | "table" | "image" | "formula" | "heading" | "other" */
  type: string;
  page?: number | null;
  location?: string | null;
  preview?: string | null;
  confidence?: number | null;
  passedToEnrichment?: boolean | null;
  skipped: boolean;
  skipReason?: string | null;
  metadata: Record<string, unknown>;
}

export interface ContentInventory {
  runId: string;
  documentId?: string | null;
  documentName?: string | null;
  /** "completed" | "empty" | "unavailable" */
  status: string;
  source: ContentInventorySource;
  summary: ContentInventorySummary;
  items: ContentInventoryItem[];
  rawArtifactId?: string | null;
  unavailableReason?: string | null;
}

// ---- Planning Report (legacy planning artifact projection) -----
//
// Surfaces the historical planning report shape for tenants that
// still query the legacy endpoint. The current pipeline derives
// stage decisions from compile evidence + the post-compile enrich
// plan; this type stays for backwards-compatible response parsing.

export interface PlanningStepDecision {
  stepId: string;
  stage: string;
  /** "RUN" | "SKIP" | "CONDITIONAL" */
  decision: string;
  enabled: boolean;
  required: boolean;
  source: string;
  reason?: string | null;
  /** "low" | "medium" | "high" */
  riskLevel: string;
  /** "NONE" | "LOW" | "MEDIUM" | "HIGH" */
  estimatedCostTier: string;
  /** "none" | "fast" | "standard" | "premium" */
  llmClass: string;
  expectedEngine?: string | null;
  expectedProvider?: string | null;
  dependencyStepIds: string[];
  warning?: string | null;
  metadata: Record<string, unknown>;
}

export interface PlanningContentDigest {
  pageCount?: number | null;
  textBlockCount: number;
  tableCount: number;
  imageCount: number;
  formulaCount: number;
  headingCount?: number | null;
  totalItems: number;
  /** Number of text blocks that would be sampled into an LLM digest. */
  sampledBlockCount: number;
  /** Per-block character cap enforced when sampling. */
  maxPreviewChars: number;
}

export interface PlanningAssessment {
  mode: string;
  policy: string;
  confidence: number;
  /** "low" | "medium" | "high" */
  estimatedCostLevel: string;
  fastLlmUsed: boolean;
  requiresVision: boolean;
  requiresPremiumLlm: boolean;
  reasons: string[];
  warnings: string[];
}

export interface PlanningLLMRecommendation {
  /** "disabled" | "applied" | "advisory" | "failed" */
  status: string;
  modelProfile?: string | null;
  summary?: string | null;
  failureReason?: string | null;
}

export interface PlanningDocumentUnderstanding {
  titleSource?: string | null;
  detectedTitle?: string | null;
  /** "clear" | "ambiguous" | "missing" | "generic" */
  titleQuality?: string | null;
  documentType?: string | null;
  documentTypeConfidence?: number | null;
  businessDomain?: string | null;
  primaryTopic?: string | null;
  documentPurpose?: string | null;
  intendedAudience?: string | null;
  /** "low" | "medium" | "high" | "unknown" */
  documentImportance?: string | null;
  expectedInformationTypes?: string[];
  recommendedAnalysisBias?: {
    preferRequirementExtraction?: boolean;
    preferRiskExtraction?: boolean;
    preferTableEnrichment?: boolean;
    preferGraphExtraction?: boolean;
    preferVisualEnrichment?: boolean;
    preferQualityReview?: boolean;
    reason?: string;
  };
  evidence?: Array<{
    source: string;
    page?: number | null;
    textPreview?: string;
    reason?: string;
  }>;
  warnings?: string[];
}

export interface PlanningContentReport {
  language?: string | null;
  pageCount?: number | null;
  structureQuality?: string;
  layoutComplexity?: string;
  contentDensity?: string;
  hasClearSections?: boolean;
  hasTables?: boolean;
  hasImages?: boolean;
  hasFormulas?: boolean;
  hasOcrPages?: boolean;
  importantObservations?: string[];
}

export interface PlanningQualityIssue {
  issue: string;
  severity: string;
  affectedPages?: number[];
  recommendation?: string;
}

export interface PlanningQualityReport {
  parseConfidence?: string;
  riskLevel?: string;
  detectedIssues?: PlanningQualityIssue[];
  manualReviewRequired?: boolean;
  manualReviewCandidates?: Array<{
    page: number;
    reason: string;
    blockTypes?: string[];
  }>;
}

export interface PlanningStepEntry {
  enabled: boolean;
  scope?: string;
  pages?: number[];
  reason?: string;
  /** Only present on chunking entries. */
  strategy?: string;
  settings?: Record<string, unknown>;
  candidateEntityTypes?: string[];
  modelProfile?: string;
}

export interface PlanningExecutionPlan {
  estimatedTime?: string;
  estimatedCost?: string;
  steps?: Record<string, PlanningStepEntry>;
}

export interface PlanningRuleBasedComparison {
  acceptedRuleRecommendations?: string[];
  overriddenRuleRecommendations?: Array<{
    rule: string;
    originalRecommendation: string;
    llmRecommendation: string;
    reason?: string;
  }>;
}

export interface PlanningDomainContext {
  /** "general" | "civil_engineering" | future packs. */
  selectedDomain: string;
  /** "user" | "workspace" | "auto_detected" | "fallback_general" */
  selectionSource: string;
  confidence: number;
  domainPackVersion?: string;
  evidence?: string[];
  appliedDomainRules?: string[];
  warnings?: string[];
  recommendedButUnsupported?: Array<{
    capability: string;
    reason: string;
  }>;
  candidates?: Array<{
    domainId: string;
    confidence: number;
    evidence?: string[];
  }>;
}

export interface PlanningResult {
  runId: string;
  documentId?: string | null;
  documentName?: string | null;
  /** "completed" | "unavailable" */
  status: string;
  generatedAt?: string | null;
  revised: boolean;
  /** "rule_based" | "llm" | "rule_based_fallback" | "audit_log" */
  source?: string | null;
  /** "post_compile" | "initial" */
  planningPhase?: string | null;
  assessment?: PlanningAssessment | null;
  decisions: PlanningStepDecision[];
  digest?: PlanningContentDigest | null;
  llmRecommendation: PlanningLLMRecommendation;
  unavailableReason?: string | null;
  // Post-compile fields. Optional — older runs / audit-log responses
  // omit them.
  documentUnderstanding?: PlanningDocumentUnderstanding | null;
  decisionSummary?: {
    overallAssessment?: string;
    documentComplexity?: string;
    parseQuality?: string;
    recommendedStrategy?: string;
    mainReasoning?: string[];
  } | null;
  contentReport?: PlanningContentReport | null;
  qualityReport?: PlanningQualityReport | null;
  executionPlan?: PlanningExecutionPlan | null;
  ruleBasedAssessment?: Record<string, unknown> | null;
  ruleBasedComparison?: PlanningRuleBasedComparison | null;
  nextActions?: string[];
  warnings?: string[];
  rawArtifactId?: string | null;
  domainContext?: PlanningDomainContext | null;
  /**
 * Operator-facing planner mode.
 * "rule_based" — deterministic only
 * "llm" — LLM-assisted; rule-based runs first
 * "hybrid" — both run; rule-based is the safety net
 * "rule_based_fallback" — LLM ran but failed/invalid; kept rules
 */
  plannerMode?: string | null;
}

/**
 * Post-compile rule-based enrich plan, served by
 * `GET /ingestion-runs/{run_id}/enrich-plan`. Mirrors the backend's
 * `PostCompileEnrichPlan.to_payload` shape under `plan` plus a
 * thin envelope for run-scope metadata + unavailable reasons.
 */
export interface RunEnrichPlanResponse {
  runId: string;
  documentId?: string | null;
  documentName?: string | null;
  /** "completed" | "unavailable" */
  status: "completed" | "unavailable";
  unavailableReason?: string | null;
  artifactId?: string | null;
  plan: PostCompileEnrichPlanPayload | null;
}

export interface PostCompileEnrichPlanPayload {
  schema_version: string;
  /** "skip" | "optional" | "recommended" | "required" */
  overall_recommendation: "skip" | "optional" | "recommended" | "required";
  reasons: string[];
  recommended_tasks: string[];
  skipped_tasks: string[];
  blocking_issues: string[];
  source_signals: Record<string, unknown>;
  /** "rule_based" | "rule_based_with_fast_llm" */
  decision_source: "rule_based" | "rule_based_with_fast_llm";
  /** closure fields (always present on new runs; absent on legacy). */
  should_enrich?: boolean;
  confidence?: number;
  require_enrichment_success?: boolean;
  warnings?: string[];
}


// ---- typed artifact endpoints ----------------------------

/**
 * Shared envelope for the three artifact endpoints. All three
 * (`/initial-execution-plan`, `/compile-result`, `/enrichment-result`)
 * return the same 7-key wire shape — `status="completed"` carries
 * the typed `plan` payload; `status="unavailable"` carries an
 * operator-readable `unavailableReason` and `plan: null`.
 */
export interface RunArtifactEnvelope<TPlan> {
  runId: string;
  documentId?: string | null;
  documentName?: string | null;
  status: "completed" | "unavailable";
  unavailableReason?: string | null;
  artifactId?: string | null;
  plan: TPlan | null;
}


/**
 * Pre-compile initial execution plan payload (mirrors
 * `InitialExecutionPlan.to_payload` in Python). Cheap profile +
 * resolved domain pack + enrichment policy + candidate modules.
 */
export interface InitialExecutionPlanPayload {
  schema_version?: string;
  document_id?: string | null;
  domain_profile_id?: string | null;
  enrichment_policy?: "auto" | "always" | "never" | string | null;
  require_enrichment_success?: boolean | null;
  candidate_modules?: string[];
  cheap_signals?: Record<string, unknown>;
  resource_hints?: Record<string, unknown>;
  reasons?: string[];
  warnings?: string[];
  compile_plan?: Record<string, unknown> | null;
}


/**
 * Typed `NormalizedCompileResult.to_payload` projection. Bridges
 * the vendor-specific compile output into a stable FE-facing shape
 * — chunks, detected tables/images, retry history, quality
 * signals. Raw vendor blob stays in the workspace; the FE only
 * sees `raw_artifact_refs` pointing at it.
 */
export interface NormalizedCompileResultPayload {
  schema_version?: string;
  document_id?: string | null;
  parser?: string | null;
  parse_method?: string | null;
  status?: string;
  chunks_count?: number;
  extracted_text_chars?: number;
  page_count?: number | null;
  detected_tables?: Array<Record<string, unknown>>;
  detected_images?: Array<Record<string, unknown>>;
  quality_signals?: Record<string, unknown>;
  retry_attempts?: Array<Record<string, unknown>>;
  final_quality?: string | null;
  warnings?: string[];
  errors?: string[];
  raw_artifact_refs?: string[];
  metadata_presence?: Record<string, unknown>;
}


/**
 * Typed `EnrichmentResult.to_payload` projection. The post-compile
 * overlay carrying per-module outcomes, document-metadata, terminology,
 * validation findings, and aggregate model usage.
 */
export interface EnrichmentResultPayload {
  schema_version?: string;
  document_id?: string | null;
  /** `succeeded` / `succeeded_with_warnings` / `failed` / `skipped`. */
  status?: "succeeded" | "succeeded_with_warnings" | "failed" | "skipped";
  reason?: string;
  domain_id?: string | null;
  module_outcomes?: EnrichmentModuleOutcomePayload[];
  document_metadata?: Record<string, unknown>;
  terminology?: Array<Record<string, unknown>>;
  classification?: Record<string, unknown> | null;
  validation?: Record<string, unknown> | null;
  warnings?: string[];
  errors?: string[];
  model_usage?: Record<string, unknown>;
}


export interface EnrichmentModuleOutcomePayload {
  module_id: string;
  /** `run` / `partial` / `skipped` / `failed`. */
  status: string;
  reason?: string;
  duration_ms?: number | null;
  output_artifact_refs?: string[];
  source_refs?: Array<Record<string, unknown>>;
  model_usage?: Record<string, unknown>;
  warnings?: string[];
  errors?: string[];
}


export type RunInitialExecutionPlanResponse =
  RunArtifactEnvelope<InitialExecutionPlanPayload>;

export type RunCompileResultResponse =
  RunArtifactEnvelope<NormalizedCompileResultPayload>;

export type RunEnrichmentResultResponse =
  RunArtifactEnvelope<EnrichmentResultPayload>;


// ---- final_ingestion_report --------------------------

/**
 * Aggregated end-to-end report — the single source of truth the
 * run-detail page prefers when present. Mirrors the Python
 * `FinalIngestionReport.to_dict` shape. The envelope uses
 * `report` as the payload key (vs. `plan` on the other artifact
 * endpoints).
 */
export interface FinalIngestionReportResponse {
  runId: string;
  documentId?: string | null;
  documentName?: string | null;
  status: "completed" | "unavailable";
  unavailableReason?: string | null;
  artifactId?: string | null;
  report: FinalIngestionReportPayload | null;
}


export interface StageSummaryPayload {
  stage_id: string;
  label: string;
  /** pending | skipped | running | succeeded | succeeded_with_warnings | failed */
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  reasons?: string[];
  warnings?: string[];
  errors?: string[];
  artifact_refs?: Record<string, string>;
}


export interface CompileSummaryPayload {
  compile_engine?: string | null;
  compile_status?: string | null;
  chunks_count?: number;
  page_count?: number | null;
  extracted_text_chars?: number | null;
  detected_tables_count?: number;
  detected_images_count?: number;
  quality_verdict?: string | null;
  warnings?: string[];
  errors?: string[];
  retry_count?: number;
  artifact_refs?: string[];
}


export interface EnrichmentSummaryPayload {
  should_enrich?: boolean;
  enrichment_status?:
    | "succeeded"
    | "succeeded_with_warnings"
    | "failed"
    | "skipped"
    | null;
  policy?: string | null;
  require_enrichment_success?: boolean;
  selected_modules?: string[];
  skipped_modules?: string[];
  module_outcomes?: Array<Record<string, unknown>>;
  what_enrichment_added?: string[];
  warnings?: string[];
  errors?: string[];
  retry_count?: number;
  skipped_reason?: string | null;
  artifact_refs?: string[];
}


export interface FinalIngestionReportPayload {
  schema_version: string;
  run_id: string;
  document_id?: string | null;
  document_name?: string | null;
  tenant_id?: string | null;
  project_id?: string | null;
  domain_profile_id?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  /** `INGESTION_STATUS_*` literal. */
  final_status: string;
  final_status_reason?: string;
  stages?: StageSummaryPayload[];
  compile_summary?: CompileSummaryPayload;
  enrichment_summary?: EnrichmentSummaryPayload;
  artifact_refs?: Record<string, string>;
  warnings?: string[];
  errors?: string[];
  retry_counts?: Record<string, number>;
  operator_notes?: string[];
}

// ---- Validation (manual test query) --------------------

export interface ValidationCheck {
  name: string;
  severity: "required" | "optional";
  passed: boolean;
  detail?: string | null;
  expected?: unknown;
  actual?: unknown;
  // ``skipped=true`` means the check's precondition wasn't met
  // (e.g. zero retrieved chunks for chunks-belong-to-run). FE
  // renders this as a neutral "N/A" rather than green ✓.
  skipped?: boolean;
  skippedReason?: string | null;
}

export interface ValidationCitation {
  artifactId: string;
  artifactType: string;
  sourceDocumentId?: string | null;
  sourceLocation?: string | null;
  // Server-derived from index/artifact metadata. Trusted by the FE
  // for ownership / grounding affordances; never echoed from LLM
  // output or client input.
  chunkId?: string | null;
  runId?: string | null;
}

export interface ValidationRetrievedChunk {
  artifactId: string;
  chunkId?: string | null;
  runId?: string | null;
  documentId?: string | null;
  sourceLocation?: string | null;
  score: number;
  preview: string;
  // artifact kind verbatim from the FTS index. Lets the
  // FE branch on modality (e.g. table icon for `enriched.tables`).
  // Optional because pre- backends don't surface it.
  artifactKind?: string | null;
}

export interface ValidationEvidenceFlags {
  graphUsed: boolean;
  tablesUsed: boolean;
  imagesUsed: boolean;
}

export type ValidationStatus =
  | "passed"
  | "passed_with_warnings"
  | "failed"
  | "inconclusive";

/**
 * Snapshot-centric query scope (wire shape).
 *
 *   - "project_active"     → every attached document's active snapshot
 *   - "document_active"    → one document's active snapshot
 *   - "snapshot_explicit"  → a fixed allowlist of snapshot ids
 *
 * Run is intentionally not a scope. The FE resolves a run to its
 * producing snapshot (``run.targetSnapshotId``) and sends
 * ``snapshot_explicit`` with that single id.
 */
export type QueryScope =
  | { type: "project_active" }
  | { type: "document_active"; documentId: string }
  | { type: "snapshot_explicit"; snapshotIds: string[] };

export interface ManualTestQueryRequest {
  question: string;
  topK?: number;
  mode?: string;
  citationRequired?: boolean;
  includeRaw?: boolean;
  // Opt into LLM answer synthesis. Default true on the server.
  // Set false for fast retrieval-only debug runs (skips the LLM call
  // entirely, response carries `llm.called=false`, `synthesizedAnswer=null`).
  synthesize?: boolean;
  /**
   * Snapshot-centric scope. UI callers MUST send this; the legacy
   * ``validationScope`` text token is only honoured server-side for
   * back-compat and is REFUSED for the ``"run"`` value (Run is
   * execution metadata, not a knowledge unit).
   */
  scope?: QueryScope;
  /**
   * Diagnostic escape hatch. When ``true``, the server allows
   * ``validationScope="run"`` for operators inspecting raw
   * run-keyed artifacts. UI paths NEVER set this — the typed
   * ``scope`` field is the supported route.
   */
  allowRunScope?: boolean;
  /**
   * @deprecated Use ``scope`` instead. Kept for the diagnostic
   * surfaces that still send the legacy string token.
   */
  validationScope?: "run" | "active";
}

/**
 * Lineage-hardening diagnostics surfaced on every manual-query
 * response. Lets a tester answer "WHY did synthesis fall back?"
 * without opening the raw payload drawer. All fields are
 * server-computed; the FE just renders them.
 */
export interface ManualQueryDebug {
  retrievedCount: number;
  evidenceItemsBeforeFilter: number;
  evidenceItemsAfterFilter: number;
  artifactTypesBeforeFilter: string[];
  artifactTypesAfterFilter: string[];
  totalContextChars: number;
  topEvidencePreview: string;
  /**
   * Categorical reason synthesis didn't produce an answer.
   * `null` when synthesis succeeded.
   */
  fallbackReason:
    | null
    | "synthesis_disabled"  // request opted out OR no LLM wired
    | "no_retrieval"         // retriever found nothing
    | "no_evidence"          // hits found, but evidence builder filtered everything
    | "llm_abstained"        // synthesizer ran but returned no answer
    | "llm_error";           // synthesizer raised
  /**
   * Artifact kinds present in retrieval but excluded from the
   * synthesizer's context because the artifact-type policy
   * downranked them. Helps operators distinguish "policy at work"
   * from "bug".
   */
  deprioritizedKinds: string[];
  // Free-form server-stamped fields. The validation service emits
  // additional debug keys with snake_case names (matching the
  // backend's free-form dict shape) so the FE can render
  // synthesize-toggle state, sectioned response, retrieval-debug
  // block, etc. without forcing every key into a strict type.
  [key: string]: unknown;
}

export interface LLMTrace {
  called: boolean;
  provider?: string | null;
  model?: string | null;
  latencyMs?: number | null;
  promptTokens?: number | null;
  completionTokens?: number | null;
  error?: string | null;
}

// One evidence block as actually sent to the LLM. The FE renders
// these in the "Evidence Sent to LLM" panel so testers can verify
// exactly what the model saw. Distinct from `retrievedChunks` (the
// engine's metadata-only projection where `preview` is the artifact
// title, not the body).
export interface EvidenceBlock {
  artifactId: string;
  artifactType: string;
  text: string;
  chunkId?: string | null;
  score?: number | null;
  pageStart?: number | null;
  pageEnd?: number | null;
  section?: string | null;
  sourceLocation?: string | null;
}

export interface ManualTestQueryResponse {
  requestId: string;
  /**
   * Lineage / debug metadata only. Populated for the run-keyed
   * endpoint (``POST /ingestion-runs/{id}/test-query``); EMPTY for
   * the snapshot-centric document- and project-level endpoints.
   *
   * The FE MUST NOT use this for routing, query identity, or UI
   * branching — the snapshot id (in the request scope and in each
   * retrieved chunk) is the load-bearing identifier now. Treat
   * ``runId`` like an opt-in audit tag, never like a key.
   */
  runId: string;
  question: string;
  answer: string;
  modeUsed: string;
  retrievedChunks: ValidationRetrievedChunk[];
  citations: ValidationCitation[];
  checks: ValidationCheck[];
  validationStatus: ValidationStatus;
  evidenceFlags: ValidationEvidenceFlags;
  rawResponse?: Record<string, unknown> | null;
  // LLM-synthesized final answer (when `synthesize=true` and a client
  // is wired). `null` when synthesis was skipped or the LLM errored —
  // check `llm.error` to distinguish.
  synthesizedAnswer?: string | null;
  llm?: LLMTrace | null;
  // The clean evidence (with real body text) actually sent to the
  // LLM. Empty array when synthesis was skipped.
  evidenceSentToLlm?: EvidenceBlock[];
  /**
   * Lineage-hardening debug fields. Optional — older deployments
   * predate the field. The FE should render the debug panel
   * conditionally on its presence.
   */
  debug?: ManualQueryDebug;
}


// ---- /dev/query-trace payload --------------------------------
//
// Raw operator-surface shape: the orchestrator's QueryTrace
// rendered as JSON. Every stage of the new query pipeline lands
// here in one envelope so a developer can answer "why did the
// query fail" without instrumentation.

export interface QueryPlanShape {
  normalized_question: string;
  intent: string;
  anchors: string[];
  requested_fields: string[];
  answer_shape: string;
  synthesis_mode: string;
  retrieval_jobs: Array<{
    route: string;
    query: string;
    max_results: number;
    filters: Record<string, unknown>;
    label: string;
  }>;
  required_groups: Array<{
    name: string;
    description: string;
    anchors: string[];
    required: boolean;
  }>;
  sufficiency: {
    min_required_groups: number;
    min_total_blocks: number;
    fail_when_no_candidates: boolean;
  };
  quality: {
    required_fields: string[];
    answer_shape: string;
    fail_on_refusal: boolean;
  };
  intent_confidence: number;
  domain_id: string;
}

export interface EvidenceCandidateShape {
  route: string;
  artifact_id: string;
  artifact_kind: string;
  chunk_id: string | null;
  text_preview: string;
  score: number;
  matched_anchors: string[];
  run_id: string | null;
  document_id: string | null;
  project_id: string;
  extra: Record<string, unknown>;
}

export interface EvidenceBlockShape {
  candidate: EvidenceCandidateShape;
  body: string;
  group: string | null;
  rank_in_group: number;
}

export interface DroppedCandidateShape {
  candidate: EvidenceCandidateShape;
  reason: string;
}

export interface RouteExecutionRecordShape {
  route: string;
  query: string;
  label: string;
  duration_ms: number;
  candidates: EvidenceCandidateShape[];
  error: string | null;
}

export interface GateResultShape {
  name: string;
  passed: boolean;
  severity: string;
  reason: string | null;
  detail: Record<string, unknown>;
}

/**
 * Snapshot-scope diagnostics on every query trace. The orchestrator
 * stamps this block so operators can verify BM25 + RAGAnything
 * queried the same eligibility boundary. Absent on traces from
 * older deployments — render fallbacks treat undefined as "unknown".
 */
export interface QueryTraceSnapshotScope {
  eligible_snapshot_ids: string[];
  queried_raganything_snapshot_ids: string[];
  bm25_allowed_snapshot_ids: string[];
  used_global_workspace: boolean;
}

export interface QueryTraceShape {
  question: string;
  normalized_question: string;
  plan: QueryPlanShape;
  routes_executed: RouteExecutionRecordShape[];
  all_candidates: EvidenceCandidateShape[];
  selected: EvidenceBlockShape[];
  dropped: DroppedCandidateShape[];
  groups_covered: string[];
  groups_missing: string[];
  llm_evidence: EvidenceBlockShape[];
  answer: string;
  citations: EvidenceBlockShape[];
  gate_results: GateResultShape[];
  final_status: string;
  duration_ms: number;
  snapshot_scope?: QueryTraceSnapshotScope;
}

export interface QueryTracePayload {
  final_status: string;
  answer: string;
  message: string | null;
  trace: QueryTraceShape;
}

// ---- Imported test cases (auxiliary Validation Tab helper) ----
//
// Generated test cases were deleted in the 2026-05-14 product
// change. The Validation Tab now hosts a compact Imported Test
// Cases section: upload a CSV per document, execute it against the
// document's latest succeeded run, and render summary cards +
// per-question status. Detail lives in Manual Test Query.

export type ImportedTestCaseStatus =
  | "not_run"
  | "answered"
  | "no_answer"
  | "no_sources"
  | "scope_error"
  | "error";

export type ImportedTestCaseOverallStatus =
  | "good"
  | "needs_review"
  | "poor";

export interface ImportedTestCase {
  testCaseId: string;
  question: string;
  expectedAnswer?: string | null;
  expectedSources: string[];
  testType?: string | null;
  notes?: string | null;
}

export interface ImportedTestCaseSet {
  documentId: string;
  importedAt: string;
  sourceFilename?: string | null;
  cases: ImportedTestCase[];
}

export interface ImportedTestCaseResult {
  testCaseId: string;
  question: string;
  status: ImportedTestCaseStatus;
  hasSources: boolean;
  scopeOk: boolean;
  error?: string | null;
  runId?: string | null;
}

export interface ImportedTestCaseSummary {
  total: number;
  answered: number;
  withSources: number;
  scopeIssues: number;
  errors: number;
  overall: ImportedTestCaseOverallStatus;
}

export interface ImportedTestCaseExecution {
  documentId: string;
  executedAt: string;
  runId?: string | null;
  summary: ImportedTestCaseSummary;
  results: ImportedTestCaseResult[];
}

// ---- Run summary -------------------------------------------------

export interface ReviewQualitySummary {
  overallConfidence?: number | null;
  warningCount: number;
  lowConfidenceCount: number;
}

export interface ReviewRunSummary {
  runId: string;
  status: string;
  durationMs?: number | null;
  documentIds: string[];
  steps: ReviewStepResult[];
  artifactCounts: Record<string, number>;
  totalBytes: number;
  warnings: ReviewWarning[];
  qualitySummary?: ReviewQualitySummary | null;
  availableViews: ReviewAvailableViews;
}

// ---- Quality report ----------------------------------------------

export interface ReviewModalityConfidence {
  modality: string;
  confidence: number;
  sampleCount?: number | null;
}

export interface ReviewSkippedStep {
  step: string;
  reason?: string | null;
  policy?: string | null;
}

export interface ReviewFailedOptionalStep {
  step: string;
  reason?: string | null;
  errorType?: string | null;
}

export interface ReviewLowConfidenceFinding {
  score: number;
  category: string;
  message?: string | null;
  page?: number | null;
  chunkId?: string | null;
  artifactId?: string | null;
}

export interface ReviewQualityReport {
  overallConfidence?: number | null;
  modalityConfidences: ReviewModalityConfidence[];
  warnings: ReviewWarning[];
  skippedSteps: ReviewSkippedStep[];
  failedOptionalSteps: ReviewFailedOptionalStep[];
  lowConfidenceFindings: ReviewLowConfidenceFinding[];
  /** Only populated when caller explicitly opted in via `?includeRaw=true`. */
  rawDebug?: Record<string, unknown> | null;
}

// ---- Chunks --------------------------------------------

export interface ReviewLinkedAsset {
  artifactId: string;
  kind?: string | null;
}

export interface ReviewChunkPreview {
  chunkId: string;
  /** Short excerpt (≤240 chars) — already squashed for whitespace. */
  preview: string;
  pageStart?: number | null;
  pageEnd?: number | null;
  section?: string | null;
  title?: string | null;
  tokenCount?: number | null;
  /** 0..1 score when the producer set one. */
  confidence?: number | null;
  metadata: Record<string, unknown>;
  linkedAssets: ReviewLinkedAsset[];
  sourceArtifactId?: string | null;
}

export interface ReviewChunkDetail {
  chunkId: string;
  /** Full chunk text. */
  body: string;
  pageStart?: number | null;
  pageEnd?: number | null;
  section?: string | null;
  title?: string | null;
  tokenCount?: number | null;
  confidence?: number | null;
  metadata: Record<string, unknown>;
  linkedAssets: ReviewLinkedAsset[];
  sourceArtifactId?: string | null;
  /** Lineage projection from the producing artifact + workflow. */
  lineage: Record<string, unknown>;
}

export interface ReviewChunkPage {
  items: ReviewChunkPreview[];
  page: number;
  pageSize: number;
  total: number;
}

export interface ReviewChunkListQuery {
  page?: number;
  pageSize?: number;
  status?: string;
  /** Strict floor — chunks without a confidence score are excluded
 * when this is set. */
  minConfidence?: number;
}

// ---- Artifacts ------------------------------------------

export interface ReviewArtifactRecord {
  artifactId: string;
  kind: string;
  /** Server-side path (`<area>/<filename>`). Opaque to the FE; used
 * only as a label / filename hint. */
  location: string;
  contentHash: string;
  byteSize: number;
  status: string;
  reviewStatus: string;
  version: number;
  createdAt: string;
  updatedAt: string;
  sourceDocumentIds: string[];
  sourceArtifactIds: string[];
  metadata: Record<string, unknown>;
}

export interface ReviewArtifactPage {
  items: ReviewArtifactRecord[];
  page: number;
  pageSize: number;
  total: number;
}

export interface ReviewArtifactListQuery {
  kind?: string;
  page?: number;
  pageSize?: number;
}

/**
 * Bytes + metadata returned by `getRunArtifactContent`.
 *
 * Component code receives a `Blob` it can hand to `URL.createObjectURL`
 * for inline previews, OR turn into text via `blob.text` for JSON
 * / markdown viewers. Cleanup of object URLs is the caller's
 * responsibility.
 */
export interface ReviewArtifactContent {
  blob: Blob;
  contentType: string;
  /** Suggested download filename from `Content-Disposition`, or null
 * when the artifact was served inline. */
  filename: string | null;
  /** ETag value WITHOUT the surrounding quotes, or null when absent. */
  etag: string | null;
}

// ---- Graph --------------------------------------------

export interface ReviewGraphEntity {
  id: string;
  label: string;
  type?: string | null;
  description?: string | null;
  sourceChunkIds: string[];
  sourceArtifactIds: string[];
  metadata: Record<string, unknown>;
}

export interface ReviewGraphRelation {
  id: string;
  sourceEntityId: string;
  targetEntityId: string;
  label?: string | null;
  type?: string | null;
  description?: string | null;
  weight?: number | null;
  sourceChunkIds: string[];
  sourceArtifactIds: string[];
  metadata: Record<string, unknown>;
}

export interface ReviewGraphStats {
  /** Full count BEFORE truncation. The FE compares against
 * `truncated.limits` to know if a re-fetch with a higher cap is
 * worthwhile. */
  entityCount: number;
  relationCount: number;
  sourceArtifactIds: string[];
}

export interface ReviewGraphTruncation {
  entities: boolean;
  relations: boolean;
  limits: { maxNodes: number; maxEdges: number };
}

export interface ReviewGraphUnavailable {
  reason: string;
}

export interface ReviewGraphSnapshot {
  stats: ReviewGraphStats;
  entities: ReviewGraphEntity[];
  relations: ReviewGraphRelation[];
  truncated: ReviewGraphTruncation;
  /** Populated only when the run produced no graph data (skipped /
 * planner-skipped / failed). When set, the FE should render the
 * skipped empty state — entities + relations are guaranteed empty. */
  unavailable: ReviewGraphUnavailable | null;
}

export interface ReviewGraphQuery {
  /** Per-list cap (1..50_000). Server clamps to its own absolute max. */
  maxNodes?: number;
  maxEdges?: number;
}
