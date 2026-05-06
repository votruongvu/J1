/**
 * Field translation between the J1 REST API (camelCase, J1-shaped)
 * and the frontend's component contract (a partly-snake_case shape
 * preserved from the design prototype).
 *
 * This file is the ONLY place the frontend depends on backend field
 * names. When the backend contract changes, edit here — components
 * stay untouched.
 */

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
    eventType === "step.warning"
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
