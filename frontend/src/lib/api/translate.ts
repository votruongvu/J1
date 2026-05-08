/**
 * Field translation between the J1 REST API (camelCase, J1-shaped)
 * and the frontend's component contract (a partly-snake_case shape
 * preserved from the design prototype).
 *
 * This file is the ONLY place the frontend depends on backend field
 * names. When the backend contract changes, edit here — components
 * stay untouched.
 */

import { EVENT_TYPES } from "@/lib/constants/events";
import type {
  CostTier,
  Decision,
  ExecutionPlan,
  IngestionRun,
  LlmClass,
  PlanStep,
  ProgressEvent,
  ProgressEventData,
  ProgressEventType,
  RiskLevel,
  RunListItem,
  RunStatus,
  Stage,
} from "@/types/ingestion";
import type {
  ReviewArtifactPage,
  ReviewArtifactRecord,
  ReviewAvailability,
  ReviewAvailableViews,
  ReviewChunkDetail,
  ReviewChunkPage,
  ReviewChunkPreview,
  ReviewGraphEntity,
  ReviewGraphRelation,
  ReviewGraphSnapshot,
  ReviewLinkedAsset,
  ReviewQualityReport,
  ReviewRunSummary,
  ReviewStepResult,
  ReviewWarning,
} from "@/types/review";

// ---- Backend response shapes (loose) -------------------------------

/** Raw envelope shape J1 returns from `GET /ingestion-runs/{id}`. */
export interface ApiRunRecord {
  runId: string;
  documentId?: string;
  workflowId?: string;
  workflowRunId?: string | null;
  status?: string;
  startedAt?: string | null;
  updatedAt?: string | null;
  completedAt?: string | null;
  currentStage?: string | null;
  currentStep?: string | null;
  progressPercent?: number;
  warningCount?: number;
  failureCode?: string | null;
  failureMessage?: string | null;
  metadata?: Record<string, unknown>;
}

/** Raw envelope from `GET /ingestion-runs/{id}/plan`. */
export interface ApiPlanRecord {
  runId: string;
  documentId?: string;
  mode?: string;
  policy?: string;
  confidence?: number;
  estimatedCostLevel?: string;
  fastLlmUsed?: boolean;
  warnings?: string[];
  steps?: ApiPlanStep[];
  profile?: Record<string, unknown>;
  /** True when any enabled step uses the premium LLM class. */
  requiresPremiumLlm?: boolean;
  /** True when any enabled step needs the vision LLM. */
  requiresVision?: boolean;
  /** Per-image vision triage decisions; empty when the parser
   * doesn't surface per-image metadata. */
  visionDecisions?: Array<Record<string, unknown>>;
}

export interface ApiPlanStep {
  stepId?: string;
  name: string;
  stage: string;
  decision: string;
  reason?: string;
  required?: boolean;
  source?: string;
  dependencyStepIds?: string[];
  estimatedCostTier?: string;
  expectedEngine?: string | null;
  expectedProvider?: string | null;
  riskLevel?: string;
  warning?: string | null;
  metadata?: Record<string, unknown>;
  /** none|fast|standard|premium — the LLM class chosen for this step. */
  llmClass?: string;
}

/** Raw envelope item from `GET /ingestion-runs` list endpoint. */
export interface ApiRunListItem {
  runId: string;
  documentId?: string;
  documentName?: string | null;
  mode?: string | null;
  policy?: string | null;
  status?: string;
  startedAt?: string | null;
  updatedAt?: string | null;
  completedAt?: string | null;
  currentStage?: string | null;
  currentStep?: string | null;
  progressPercent?: number;
  warningCount?: number;
  failureCode?: string | null;
  failureMessage?: string | null;
}

/** Raw envelope from `GET /ingestion-runs/{id}/events` items. */
export interface ApiProgressEvent {
  eventId: string;
  runId: string;
  eventType: string;
  timestamp: string;
  severity?: string;
  stage?: string | null;
  step?: string | null;
  status?: string | null;
  progressPercent?: number | null;
  current?: number | null;
  total?: number | null;
  message?: string | null;
  engine?: string | null;
  provider?: string | null;
  metadata?: Record<string, unknown>;
}

// ---- Translators ----------------------------------------------------

/**
 * Map J1 RunStatus to the prototype's status enum (which uses
 * COMPLETED / COMPLETED_WITH_WARNINGS / AWAITING_HUMAN_REVIEW).
 * `StatusDisplay` carries entries for both shapes already, so the
 * UI handles either — but we normalise here for consistency.
 */
function translateStatus(s: string | undefined): RunStatus {
  if (!s) return "ASSESSING";
  const upper = String(s).toUpperCase() as RunStatus;
  if (upper === "SUCCEEDED") return "COMPLETED";
  if (upper === "SUCCEEDED_WITH_WARNINGS") return "COMPLETED_WITH_WARNINGS";
  if (upper === "REQUIRES_HUMAN_REVIEW") return "AWAITING_HUMAN_REVIEW";
  return upper;
}

export function runListItemFromApi(api: ApiRunListItem): RunListItem {
  return {
    runId: api.runId,
    documentName: api.documentName ?? api.documentId ?? api.runId,
    status: translateStatus(api.status),
    // Backend reads `mode` / `policy` from `run.metadata`. Fall back
    // to sensible labels when the metadata is missing rather than
    // showing literal "undefined" in the All Runs row meta line.
    mode: api.mode ?? "STANDARD",
    policy: api.policy ?? "auto",
    currentStage: (api.currentStage as Stage | null | undefined) ?? null,
    currentStep: api.currentStep ?? null,
    progressPercent: api.progressPercent ?? 0,
    warningCount: api.warningCount ?? 0,
    startedAt: api.startedAt ?? null,
    updatedAt: api.updatedAt ?? null,
    completedAt: api.completedAt ?? null,
    failureCode: api.failureCode ?? null,
    failureMessage: api.failureMessage ?? null,
  };
}

export function runFromApi(api: ApiRunRecord): IngestionRun {
  const documentName =
    (api.metadata?.["documentName"] as string | undefined) || api.documentId || api.runId;
  const mode =
    (api.metadata?.["mode"] as string | undefined) ||
    (api.metadata?.["policy"] as string | undefined) ||
    "STANDARD";
  const policy = (api.metadata?.["policy"] as string | undefined) || "auto";

  return {
    runId: api.runId,
    document_name: documentName,
    mode,
    policy,
    status: translateStatus(api.status),
    started_at: api.startedAt ?? null,
    completed_at: api.completedAt ?? null,
    progress_pct: api.progressPercent ?? 0,
    warning_count: api.warningCount ?? 0,
    current_stage: (api.currentStage as Stage | undefined) ?? null,
    current_step: api.currentStep ?? null,
    final: api.failureCode
      ? {
          failure_code: api.failureCode,
          failure_message: api.failureMessage ?? undefined,
        }
      : null,
  };
}

export function planFromApi(api: ApiPlanRecord): ExecutionPlan {
  const steps: PlanStep[] = (api.steps ?? []).map((s) => ({
    id: s.stepId ?? s.name,
    stage: s.stage as Stage,
    name: s.name,
    decision: s.decision as Decision,
    reason: s.reason ?? "",
    risk_level: (s.riskLevel ?? "low").toUpperCase() as RiskLevel,
    estimated_cost_tier: (s.estimatedCostTier ?? "NONE") as CostTier,
    expected_engine: s.expectedEngine ?? null,
    expected_provider: s.expectedProvider ?? null,
    warning: s.warning ?? undefined,
    llm_class: (s.llmClass as LlmClass | undefined) ?? "none",
  }));

  // Build summary from steps if the backend doesn't include one.
  const stages = Array.from(new Set(steps.map((s) => s.stage))) as Stage[];
  const counts = { run: 0, skip: 0, conditional: 0 };
  for (const step of steps) {
    if (step.decision === "RUN") counts.run += 1;
    else if (step.decision === "SKIP") counts.skip += 1;
    else if (step.decision === "CONDITIONAL") counts.conditional += 1;
  }
  return {
    runId: api.runId,
    summary: {
      total: steps.length,
      run: counts.run,
      skip: counts.skip,
      conditional: counts.conditional,
      stages,
    },
    steps,
    requires_vision: api.requiresVision ?? false,
    requires_premium_llm: api.requiresPremiumLlm ?? false,
  };
}

// ---- Review surface (Phase 7+) -------------------------------------
//
// The backend already returns camelCase + the shapes match the FE
// types directly. These translators do defensive normalisation
// (defaults for absent fields, list/dict guards) so a sparse run
// never crashes the FE — never re-shape the contract.

function availabilityFromApi(raw: unknown): ReviewAvailability {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    available: Boolean(obj.available),
    reason: typeof obj.reason === "string" ? obj.reason : null,
  };
}

function availableViewsFromApi(raw: unknown): ReviewAvailableViews {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    chunks: availabilityFromApi(obj.chunks),
    assets: availabilityFromApi(obj.assets),
    graph: availabilityFromApi(obj.graph),
    quality: availabilityFromApi(obj.quality),
    rawArtifacts: availabilityFromApi(obj.rawArtifacts),
    // Tolerant of older backend snapshots that don't yet emit a
    // `validation` field — `availabilityFromApi` returns a safe
    // disabled stub when the input is undefined.
    validation: availabilityFromApi(obj.validation),
  };
}

function stepResultFromApi(raw: unknown): ReviewStepResult {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const error = obj.error as Record<string, unknown> | null | undefined;
  return {
    step: String(obj.step ?? ""),
    status: String(obj.status ?? ""),
    required: Boolean(obj.required),
    source: String(obj.source ?? ""),
    startedAt: (obj.startedAt as string | null | undefined) ?? null,
    completedAt: (obj.completedAt as string | null | undefined) ?? null,
    durationMs: (obj.durationMs as number | null | undefined) ?? null,
    reason: (obj.reason as string | null | undefined) ?? null,
    error: error
      ? {
          type: String(error.type ?? ""),
          message: String(error.message ?? ""),
          retryable: Boolean(error.retryable),
        }
      : null,
    artifactCount: typeof obj.artifactCount === "number" ? obj.artifactCount : 0,
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

function warningFromApi(raw: unknown): ReviewWarning {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    code: String(obj.code ?? ""),
    message: String(obj.message ?? ""),
    severity: String(obj.severity ?? "warning"),
    step: (obj.step as string | null | undefined) ?? null,
    documentId: (obj.documentId as string | null | undefined) ?? null,
    page: (obj.page as number | null | undefined) ?? null,
    chunkId: (obj.chunkId as string | null | undefined) ?? null,
    artifactId: (obj.artifactId as string | null | undefined) ?? null,
  };
}

export function runSummaryFromApi(raw: unknown): ReviewRunSummary {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const stepsArr = Array.isArray(obj.steps) ? obj.steps : [];
  const warningsArr = Array.isArray(obj.warnings) ? obj.warnings : [];
  const docsArr = Array.isArray(obj.documentIds) ? obj.documentIds : [];
  const counts = (obj.artifactCounts ?? {}) as Record<string, number>;
  const quality = obj.qualitySummary as Record<string, unknown> | null | undefined;

  return {
    runId: String(obj.runId ?? ""),
    status: String(obj.status ?? ""),
    durationMs: (obj.durationMs as number | null | undefined) ?? null,
    documentIds: docsArr.map((d) => String(d)),
    steps: stepsArr.map(stepResultFromApi),
    artifactCounts: { ...counts },
    totalBytes: typeof obj.totalBytes === "number" ? obj.totalBytes : 0,
    warnings: warningsArr.map(warningFromApi),
    qualitySummary: quality
      ? {
          overallConfidence:
            (quality.overallConfidence as number | null | undefined) ?? null,
          warningCount:
            typeof quality.warningCount === "number" ? quality.warningCount : 0,
          lowConfidenceCount:
            typeof quality.lowConfidenceCount === "number"
              ? quality.lowConfidenceCount
              : 0,
        }
      : null,
    availableViews: availableViewsFromApi(obj.availableViews),
  };
}

export function qualityReportFromApi(raw: unknown): ReviewQualityReport {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const modalities = Array.isArray(obj.modalityConfidences)
    ? obj.modalityConfidences
    : [];
  const warnings = Array.isArray(obj.warnings) ? obj.warnings : [];
  const skipped = Array.isArray(obj.skippedSteps) ? obj.skippedSteps : [];
  const failedOpt = Array.isArray(obj.failedOptionalSteps)
    ? obj.failedOptionalSteps
    : [];
  const findings = Array.isArray(obj.lowConfidenceFindings)
    ? obj.lowConfidenceFindings
    : [];
  return {
    overallConfidence:
      (obj.overallConfidence as number | null | undefined) ?? null,
    modalityConfidences: modalities.map((m) => {
      const o = (m ?? {}) as Record<string, unknown>;
      return {
        modality: String(o.modality ?? ""),
        confidence: typeof o.confidence === "number" ? o.confidence : 0,
        sampleCount: (o.sampleCount as number | null | undefined) ?? null,
      };
    }),
    warnings: warnings.map(warningFromApi),
    skippedSteps: skipped.map((s) => {
      const o = (s ?? {}) as Record<string, unknown>;
      return {
        step: String(o.step ?? ""),
        reason: (o.reason as string | null | undefined) ?? null,
        policy: (o.policy as string | null | undefined) ?? null,
      };
    }),
    failedOptionalSteps: failedOpt.map((f) => {
      const o = (f ?? {}) as Record<string, unknown>;
      return {
        step: String(o.step ?? ""),
        reason: (o.reason as string | null | undefined) ?? null,
        errorType: (o.errorType as string | null | undefined) ?? null,
      };
    }),
    lowConfidenceFindings: findings.map((f) => {
      const o = (f ?? {}) as Record<string, unknown>;
      return {
        score: typeof o.score === "number" ? o.score : 0,
        category: String(o.category ?? ""),
        message: (o.message as string | null | undefined) ?? null,
        page: (o.page as number | null | undefined) ?? null,
        chunkId: (o.chunkId as string | null | undefined) ?? null,
        artifactId: (o.artifactId as string | null | undefined) ?? null,
      };
    }),
    rawDebug: (obj.rawDebug as Record<string, unknown> | null | undefined) ?? null,
  };
}

// ---- Chunks (Phase 8) ---------------------------------------------

function linkedAssetsFromApi(raw: unknown): ReviewLinkedAsset[] {
  if (!Array.isArray(raw)) return [];
  const out: ReviewLinkedAsset[] = [];
  for (const entry of raw) {
    const o = (entry ?? {}) as Record<string, unknown>;
    const id = o.artifactId;
    if (typeof id !== "string" || !id) continue;
    out.push({
      artifactId: id,
      kind: typeof o.kind === "string" ? o.kind : null,
    });
  }
  return out;
}

function chunkPreviewFromApi(raw: unknown): ReviewChunkPreview {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    chunkId: String(obj.chunkId ?? ""),
    preview: typeof obj.preview === "string" ? obj.preview : "",
    pageStart: (obj.pageStart as number | null | undefined) ?? null,
    pageEnd: (obj.pageEnd as number | null | undefined) ?? null,
    section: (obj.section as string | null | undefined) ?? null,
    title: (obj.title as string | null | undefined) ?? null,
    tokenCount: (obj.tokenCount as number | null | undefined) ?? null,
    confidence: (obj.confidence as number | null | undefined) ?? null,
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
    linkedAssets: linkedAssetsFromApi(obj.linkedAssets),
    sourceArtifactId:
      (obj.sourceArtifactId as string | null | undefined) ?? null,
  };
}

export function chunkPageFromApi(raw: unknown): ReviewChunkPage {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(obj.items) ? obj.items : [];
  return {
    items: items.map(chunkPreviewFromApi),
    page: typeof obj.page === "number" ? obj.page : 1,
    pageSize: typeof obj.pageSize === "number" ? obj.pageSize : 50,
    total: typeof obj.total === "number" ? obj.total : 0,
  };
}

// ---- Artifacts (Phase 9) ------------------------------------------

function artifactRecordFromApi(raw: unknown): ReviewArtifactRecord {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    artifactId: String(obj.artifactId ?? ""),
    kind: String(obj.kind ?? ""),
    location: String(obj.location ?? ""),
    contentHash: String(obj.contentHash ?? ""),
    byteSize: typeof obj.byteSize === "number" ? obj.byteSize : 0,
    status: String(obj.status ?? ""),
    reviewStatus: String(obj.reviewStatus ?? ""),
    version: typeof obj.version === "number" ? obj.version : 1,
    createdAt: String(obj.createdAt ?? ""),
    updatedAt: String(obj.updatedAt ?? ""),
    sourceDocumentIds: Array.isArray(obj.sourceDocumentIds)
      ? obj.sourceDocumentIds.map((d) => String(d))
      : [],
    sourceArtifactIds: Array.isArray(obj.sourceArtifactIds)
      ? obj.sourceArtifactIds.map((d) => String(d))
      : [],
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

export function artifactPageFromApi(raw: unknown): ReviewArtifactPage {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(obj.items) ? obj.items : [];
  return {
    items: items.map(artifactRecordFromApi),
    page: typeof obj.page === "number" ? obj.page : 1,
    pageSize: typeof obj.pageSize === "number" ? obj.pageSize : 50,
    total: typeof obj.total === "number" ? obj.total : 0,
  };
}

/**
 * Extract the `filename=` value from a `Content-Disposition` header.
 *
 * Handles the common shapes the J1 backend emits:
 *   `attachment; filename="abc.png"`
 *   `attachment; filename=abc.png`
 *
 * Does NOT yet handle RFC 5987 `filename*=UTF-8''…` — none of the
 * filenames J1 generates today need it. Returns null when the
 * header is absent / unrecognised.
 */
export function parseFilename(
  contentDisposition: string | null,
): string | null {
  if (!contentDisposition) return null;
  const m =
    contentDisposition.match(/filename="([^"]+)"/) ??
    contentDisposition.match(/filename=([^;]+)/);
  if (!m) return null;
  return m[1]!.trim() || null;
}

/**
 * Strip surrounding quotes from an `ETag` header value.
 * Returns null when the header is absent.
 */
export function parseEtag(etag: string | null): string | null {
  if (!etag) return null;
  const trimmed = etag.trim();
  if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1);
  }
  return trimmed || null;
}

export function chunkDetailFromApi(raw: unknown): ReviewChunkDetail {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    chunkId: String(obj.chunkId ?? ""),
    body: typeof obj.body === "string" ? obj.body : "",
    pageStart: (obj.pageStart as number | null | undefined) ?? null,
    pageEnd: (obj.pageEnd as number | null | undefined) ?? null,
    section: (obj.section as string | null | undefined) ?? null,
    title: (obj.title as string | null | undefined) ?? null,
    tokenCount: (obj.tokenCount as number | null | undefined) ?? null,
    confidence: (obj.confidence as number | null | undefined) ?? null,
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
    linkedAssets: linkedAssetsFromApi(obj.linkedAssets),
    sourceArtifactId:
      (obj.sourceArtifactId as string | null | undefined) ?? null,
    lineage: (obj.lineage as Record<string, unknown> | undefined) ?? {},
  };
}

// ---- Graph (Phase 10) ---------------------------------------------

function strArray(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x)) : [];
}

function graphEntityFromApi(raw: unknown): ReviewGraphEntity {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(obj.id ?? ""),
    label: String(obj.label ?? obj.id ?? ""),
    type: (obj.type as string | null | undefined) ?? null,
    description: (obj.description as string | null | undefined) ?? null,
    sourceChunkIds: strArray(obj.sourceChunkIds),
    sourceArtifactIds: strArray(obj.sourceArtifactIds),
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

function graphRelationFromApi(raw: unknown): ReviewGraphRelation {
  const obj = (raw ?? {}) as Record<string, unknown>;
  return {
    id: String(obj.id ?? ""),
    sourceEntityId: String(obj.sourceEntityId ?? ""),
    targetEntityId: String(obj.targetEntityId ?? ""),
    label: (obj.label as string | null | undefined) ?? null,
    type: (obj.type as string | null | undefined) ?? null,
    description: (obj.description as string | null | undefined) ?? null,
    weight: (obj.weight as number | null | undefined) ?? null,
    sourceChunkIds: strArray(obj.sourceChunkIds),
    sourceArtifactIds: strArray(obj.sourceArtifactIds),
    metadata: (obj.metadata as Record<string, unknown> | undefined) ?? {},
  };
}

export function graphSnapshotFromApi(raw: unknown): ReviewGraphSnapshot {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const stats = (obj.stats ?? {}) as Record<string, unknown>;
  const truncated = (obj.truncated ?? {}) as Record<string, unknown>;
  const limits = (truncated.limits ?? {}) as Record<string, unknown>;
  const unavailable = obj.unavailable as
    | Record<string, unknown>
    | null
    | undefined;
  const entities = Array.isArray(obj.entities) ? obj.entities : [];
  const relations = Array.isArray(obj.relations) ? obj.relations : [];

  return {
    stats: {
      entityCount:
        typeof stats.entityCount === "number" ? stats.entityCount : 0,
      relationCount:
        typeof stats.relationCount === "number" ? stats.relationCount : 0,
      sourceArtifactIds: strArray(stats.sourceArtifactIds),
    },
    entities: entities.map(graphEntityFromApi),
    relations: relations.map(graphRelationFromApi),
    truncated: {
      entities: Boolean(truncated.entities),
      relations: Boolean(truncated.relations),
      limits: {
        maxNodes:
          typeof limits.maxNodes === "number" ? limits.maxNodes : 5000,
        maxEdges:
          typeof limits.maxEdges === "number" ? limits.maxEdges : 5000,
      },
    },
    unavailable: unavailable
      ? { reason: String(unavailable.reason ?? "") }
      : null,
  };
}

export function eventFromApi(api: ApiProgressEvent): ProgressEvent {
  // Metadata keys are camelCase on the wire — the audit-to-record
  // translator camelizes the payload bag at serialisation time so
  // the FE has exactly one naming convention to think about.
  const meta = api.metadata ?? {};
  const eventType = api.eventType as ProgressEventType;

  // `step.failed` carries `errorType` / `errorMessage`; `run.failed`
  // carries `failureCode` / `failureMessage`. Collapse both shapes
  // onto the single FE pair so the timeline/final panels don't have
  // to branch.
  const failureCode =
    (meta["failureCode"] as string | undefined) ?? (meta["errorType"] as string | undefined);
  const failureMessage =
    (meta["failureMessage"] as string | undefined) ??
    (meta["errorMessage"] as string | undefined);

  // `step.warning` puts the warning text on the top-level `message`
  // field (the reporter doesn't write a separate `warning` key). The
  // timeline component looks for `data.warning` to render the
  // emphasised warning panel, so mirror it here.
  const warning =
    eventType === EVENT_TYPES.STEP_WARNING
      ? (api.message ?? undefined)
      : ((meta["warning"] as string | undefined) ?? undefined);

  const data: ProgressEventData = {
    runId: api.runId,
    message: api.message ?? undefined,
    severity: (api.severity as ProgressEventData["severity"]) ?? "INFO",
    stage: (api.stage as Stage | null) ?? undefined,
    step: api.step ?? undefined,
    progress: api.progressPercent != null ? api.progressPercent / 100 : undefined,
    current: api.current ?? undefined,
    total: api.total ?? undefined,
    engine: api.engine ?? undefined,
    provider: api.provider ?? undefined,
    failure_code: failureCode,
    failure_message: failureMessage,
    reason: meta["reason"] as string | undefined,
    warning,
  };

  return {
    eventId: api.eventId,
    event: eventType,
    ts: api.timestamp ? Date.parse(api.timestamp) : Date.now(),
    data,
  };
}
