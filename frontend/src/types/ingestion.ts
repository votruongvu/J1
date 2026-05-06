/**
 * Ingestion-domain type definitions.
 *
 * One source of truth for the shapes the UI consumes. Both the Mock
 * and Live clients return objects that conform to these types — the
 * Live client translates J1's camelCase REST envelopes into this
 * shape inside `lib/api/translate.ts`, so component code never
 * branches on data origin.
 *
 * If the backend contract changes, edit ONLY the types here + the
 * translator. Components stay untouched.
 */

// ---- Run status enum -------------------------------------------------

/**
 * Run lifecycle status.
 *
 * Two equivalent name pairs exist for historical reasons:
 *   - SUCCEEDED                ↔  COMPLETED
 *   - SUCCEEDED_WITH_WARNINGS  ↔  COMPLETED_WITH_WARNINGS
 *   - REQUIRES_HUMAN_REVIEW    ↔  AWAITING_HUMAN_REVIEW
 *
 * The J1 API uses the SUCCEEDED-form names. The mock client uses
 * the COMPLETED-form. The display layer (`lib/display.ts`) carries
 * entries for both so either is rendered correctly without
 * normalisation.
 */
export type RunStatus =
  | "CREATED"
  | "ASSESSING"
  | "PLAN_READY"
  | "WAITING_FOR_CONFIRMATION"
  | "RUNNING"
  | "PAUSED"
  | "CANCELLING"
  | "COMPLETED"
  | "COMPLETED_WITH_WARNINGS"
  | "SUCCEEDED"
  | "SUCCEEDED_WITH_WARNINGS"
  | "FAILED"
  | "AWAITING_HUMAN_REVIEW"
  | "REQUIRES_HUMAN_REVIEW"
  | "CANCELLED";

// ---- Stage / decision / severity ------------------------------------

export type Stage = "COMPILE" | "ENRICH" | "GRAPH" | "INDEX";

export type Decision = "RUN" | "SKIP" | "CONDITIONAL";

export type Severity = "INFO" | "WARNING" | "ERROR";

export type RiskLevel = "LOW" | "MEDIUM" | "HIGH";

export type CostTier = "S" | "M" | "L" | "NONE" | "LOW" | "MEDIUM" | "HIGH";

// ---- Run record -----------------------------------------------------

/**
 * The frontend's view of one ingestion run. The mock and live clients
 * BOTH return this shape; component code reads it without checking
 * the source.
 *
 * Note the snake_case fields (`document_name`, `started_at`,
 * `progress_pct`) — preserved verbatim from the design prototype so
 * components work without rewriting. The translator maps from the
 * J1 API's camelCase shape into these names.
 */
export interface IngestionRun {
  runId: string;
  document_name: string;
  mode: string;
  policy: string;
  status: RunStatus;
  started_at: string | null;
  completed_at?: string | null;
  progress_pct: number;
  warning_count: number;
  current_stage?: Stage | null;
  current_step?: string | null;
  /** Terminal-state details. Populated only on FAILED / completed-with-warnings / awaiting-review. */
  final?: RunFinal | null;
}

export interface RunFinal {
  /** FAILED: error code from the backend (e.g. `J1_INGEST_REQUIRED_STEP_FAILED`). */
  failure_code?: string;
  /** FAILED: human-readable detail. */
  failure_message?: string;
  /** FAILED: which step caused the failure. */
  failed_step?: string;
  /** SUCCEEDED_WITH_WARNINGS: count of warnings recorded. */
  warning_count?: number;
  /** SUCCEEDED_WITH_WARNINGS: human-readable summary. */
  warning_summary?: string;
  /** AWAITING_HUMAN_REVIEW: reason text. */
  reason?: string;
  /** AWAITING_HUMAN_REVIEW: stage / step that triggered review. */
  stage?: Stage;
  step?: string;
  /** Free-form. */
  detail?: string;
}

// ---- Execution plan -------------------------------------------------

/**
 * One step in the execution plan, as the Plan card renders it.
 */
export interface PlanStep {
  /** Stable identifier (matches the step's `name` if no separate id is set). */
  id: string;
  stage: Stage;
  /** Display name. */
  name: string;
  decision: Decision;
  /** Why was this decision made? Surfaces as the reason chip below the title. */
  reason: string;
  risk_level: RiskLevel;
  estimated_cost_tier: CostTier;
  /** Optional engine string (e.g. `pdfium`, `MinerU`, `voyage-3`). */
  expected_engine?: string | null;
  /** Optional provider string (e.g. `anthropic`, `voyage`, `internal`). */
  expected_provider?: string | null;
  /** Optional inline warning attached to a step (e.g. low-confidence detection). */
  warning?: string;
  /** none|fast|standard|premium — the LLM class chosen for this step. */
  llm_class?: LlmClass;
}

/** LLM model class chosen per step. Mirrors the backend's
 * `LLM_CLASS_*` constants. */
export type LlmClass = "none" | "fast" | "standard" | "premium";

export interface PlanSummary {
  total: number;
  run: number;
  skip: number;
  conditional: number;
  /** Stage labels in display order — `COMPILE`, `ENRICH`, `GRAPH`, `INDEX`. */
  stages: Stage[];
}

export interface ExecutionPlan {
  runId: string;
  summary: PlanSummary;
  steps: PlanStep[];
  /** True when any enabled step needs the vision LLM. Drives the
   * "Vision LLM" indicator on the plan card — vision is OFF by
   * default; this surfaces the operator-visible exception. */
  requires_vision?: boolean;
  /** True when any enabled step uses the premium LLM class. */
  requires_premium_llm?: boolean;
}

// ---- Progress events ------------------------------------------------

export type ProgressEventType =
  | "run.created"
  | "document.received"
  | "assessment.started"
  | "assessment.completed"
  | "plan.generated"
  | "plan.confirmed"
  | "step.started"
  | "step.progress"
  | "step.skipped"
  | "step.warning"
  | "step.completed"
  | "step.failed"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "human_review.required";

/**
 * Event types that close the SSE stream. Mirrors the backend's
 * `_TERMINAL_PROGRESS_TYPES` (in `j1.adapters.rest.app`) — keep in
 * sync. The FE uses this in two places:
 *
 *   - `RunDetailPage` to flip the "stay closed" flag so a clean
 *     stream end after a terminal event doesn't trigger a reconnect.
 *   - `MockClient.openStream` to end the scripted timeline so mock
 *     mode behaves identically to live mode.
 */
export const TERMINAL_EVENT_TYPES: ReadonlySet<ProgressEventType> = new Set([
  "run.completed",
  "run.failed",
  "run.cancelled",
  "human_review.required",
]);

export function isTerminalEvent(eventType: ProgressEventType | string): boolean {
  return TERMINAL_EVENT_TYPES.has(eventType as ProgressEventType);
}

/**
 * Free-form payload carried by every progress event. Optional fields
 * vary by event type — `step.progress` carries `progress` / `current` /
 * `total`; `step.failed` / `run.failed` carry `failure_code` /
 * `failure_message`; `step.skipped` carries `reason`; etc.
 */
export interface ProgressEventData {
  runId: string;
  message?: string;
  severity?: Severity;
  stage?: Stage;
  step?: string;
  /** 0..1 fraction. */
  progress?: number;
  current?: number;
  total?: number;
  engine?: string;
  provider?: string;
  failure_code?: string;
  failure_message?: string;
  failed_step?: string;
  reason?: string;
  warning?: string;
  warning_count?: number;
  warning_summary?: string;
}

export interface ProgressEvent {
  eventId: string;
  event: ProgressEventType;
  /** Server timestamp in ms-since-epoch. The translator parses ISO strings. */
  ts: number;
  data: ProgressEventData;
}

// ---- All Runs list view ---------------------------------------------

export interface RunListItem {
  runId: string;
  documentName: string;
  status: RunStatus;
  mode: string;
  policy: string;
  currentStage: Stage | null;
  currentStep: string | null;
  progressPercent: number;
  warningCount: number;
  startedAt: string | null;
  updatedAt: string | null;
  completedAt: string | null;
  failureCode: string | null;
  failureMessage: string | null;
}

export interface RunListResult {
  items: RunListItem[];
  page: number;
  pageSize: number;
  total: number;
}

export interface RunListQuery {
  page?: number;
  pageSize?: number;
  q?: string;
  status?: RunStatus | "";
  stage?: Stage | "";
}
