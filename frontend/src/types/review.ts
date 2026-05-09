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

// ---- Planning Report (richer projection over IngestPlan) -------

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
}

// ---- Validation (Phase 1: manual test query) --------------------

export interface ValidationCheck {
  name: string;
  severity: "required" | "optional";
  passed: boolean;
  detail?: string | null;
  expected?: unknown;
  actual?: unknown;
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
  // Phase 4 — artifact kind verbatim from the FTS index. Lets the
  // FE branch on modality (e.g. table icon for `enriched.tables`).
  // Optional because pre-Phase-4 backends don't surface it.
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

export interface ManualTestQueryRequest {
  question: string;
  topK?: number;
  mode?: string;
  citationRequired?: boolean;
  includeRaw?: boolean;
}

export interface ManualTestQueryResponse {
  requestId: string;
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
}

// ---- Validation sets and runs (Phase 2) -------------------------

export type ValidationTestType =
  | "retrieval"
  | "answer"
  | "citation"
  | "negative"
  | "table"
  | "image"
  | "graph";

export type ValidationPriority = "smoke" | "normal" | "deep";

export type ExpectedBehavior =
  | "answer_with_citations"
  | "abstain"
  | "retrieve_evidence"
  | "validate_relationship";

export type ValidationSetSource = "generated" | "manual" | "imported";
export type ValidationSetStatus = "draft" | "ready" | "archived";

export type ExecutionStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ValidationTestCase {
  testCaseId: string;
  question: string;
  type: ValidationTestType;
  priority: ValidationPriority;
  expectedBehavior: ExpectedBehavior;
  expectedAnswerPoints: string[];
  expectedChunks: string[];
  expectedPages: number[];
  expectedArtifacts: string[];
  expectedGraphNodes: string[];
  expectedGraphEdges: string[];
  citationRequired: boolean;
  sourceTraceability: string[];
  metadata: Record<string, unknown>;
}

export interface ValidationSet {
  validationSetId: string;
  runId: string;
  documentIds: string[];
  source: ValidationSetSource;
  status: ValidationSetStatus;
  createdAt: string;
  createdBy?: string | null;
  generatorVersion?: string | null;
  artifactsContentHash?: string | null;
  testCases: ValidationTestCase[];
  metadata: Record<string, unknown>;
}

export interface ValidationSetListItem {
  validationSetId: string;
  runId: string;
  source: ValidationSetSource;
  status: ValidationSetStatus;
  createdAt: string;
  createdBy?: string | null;
  caseCount: number;
}

export interface ValidationCoverage {
  byType: Record<string, number>;
  byPriority: Record<string, number>;
  bySection: Record<string, number>;
}

export interface ValidationSummary {
  total: number;
  passed: number;
  warning: number;
  failed: number;
  skipped: number;
  coverage: ValidationCoverage;
  mainIssues: string[];
  recommendedAction?: string | null;
}

export type ValidationResultStatus =
  | "passed"
  | "warning"
  | "failed"
  | "skipped";

export interface ValidationResult {
  resultId: string;
  testCaseId: string;
  status: ValidationResultStatus;
  question: string;
  answer: string;
  retrievedChunks: ValidationRetrievedChunk[];
  citations: ValidationCitation[];
  checks: ValidationCheck[];
  judgeNotes?: string | null;
  failureReason?: string | null;
  testerVerdict?: "pass" | "warning" | "fail" | null;
  testerNotes?: string | null;
}

export interface ValidationRun {
  validationRunId: string;
  validationSetId: string;
  runId: string;
  // The split: executionStatus is the JOB status; validationStatus is
  // the TEST OUTCOME. A `completed` + `failed` pair means "the runner
  // job finished, but the document didn't pass". They MUST NOT be
  // collapsed in the UI.
  executionStatus: ExecutionStatus;
  validationStatus: ValidationStatus;
  startedAt: string;
  completedAt?: string | null;
  actor: string;
  summary: ValidationSummary;
  results: ValidationResult[];
  failureMessage?: string | null;
  metadata: Record<string, unknown>;
}

export interface ValidationRunListItem {
  validationRunId: string;
  validationSetId: string;
  runId: string;
  executionStatus: ExecutionStatus;
  validationStatus: ValidationStatus;
  startedAt: string;
  completedAt?: string | null;
  summary: ValidationSummary;
}

export interface GenerateValidationSetRequest {
  maxCases?: number;
  citationRequired?: boolean;
  force?: boolean;
}

export interface StartValidationRunRequest {
  validationSetId: string;
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

// ---- Chunks (Phase 8) --------------------------------------------

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

// ---- Artifacts (Phase 9) ------------------------------------------

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
 * for inline previews, OR turn into text via `blob.text()` for JSON
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

// ---- Graph (Phase 10) --------------------------------------------

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
