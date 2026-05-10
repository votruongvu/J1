/**
 * Pure helpers extracted from `CompileStrategyPanel.tsx`.
 *
 * Kept side-effect-free + node-test friendly so the suite can pin
 * banner / applied-capability logic without standing up jsdom.
 * The panel imports these and renders the resulting strings —
 * any logic change should land here, with a corresponding test.
 */

export const COMPILE_STRATEGY_REPORT_KIND = "compile_strategy_report";

export interface CompileAttemptRecord {
  attempt_number: number;
  mode: string | null;
  parser: string;
  parse_method: string | null;
  started_at: string;
  completed_at: string | null;
  status: string;
  chunks_count: number;
  extracted_text_chars: number | null;
  quality: string;
  retry_reason: string | null;
  warnings: string[];
  mapped_compile_config: {
    parse_method?: string | null;
    assessment_mode?: string | null;
    unhandled_capabilities?: string[];
  };
}

export interface AssessmentPlanPayload {
  document_id?: string;
  mode?: string;
  document_type?: string;
  complexity?: string;
  confidence?: number;
  required_capabilities?: string[];
  optional_capabilities?: string[];
  risk_flags?: string[];
  fallback_policy?: string;
  reason?: string;
}

export interface CompileStrategyReport {
  schema_version: string;
  run_id: string;
  document_id: string | null;
  initial_mode: string | null;
  final_mode: string | null;
  retry_used: boolean;
  attempts_count: number;
  attempts: CompileAttemptRecord[];
  final_compile_quality: string;
  final_retry_reason: string | null;
  final_warnings: string[];
  assessment_plan: AssessmentPlanPayload;
  initial_assessment_plan: AssessmentPlanPayload;
  plan_warnings: string[];
  unhandled_capabilities: string[];
}

export interface BannerSpec {
  kind: "warn" | "err";
  message: string;
  testid: string;
}

/** Confidence threshold below which a `low_confidence` banner fires.
 * Sized to flag plans the deterministic profiler couldn't pin down
 * (it tops out at ~0.7 when major signals are unknown). */
export const LOW_CONFIDENCE_THRESHOLD = 0.7;

/**
 * Compute the ordered list of banners for a compile strategy report.
 *
 * Banners surface, in priority order:
 *   1. No AssessmentPlan attached
 *   2. Low-confidence plan (when plan exists)
 *   3. Unhandled capabilities present
 *   4. Compile safety retry triggered
 *   5. Final quality LOW
 *   6. Final quality FAILED
 */
export function bannersForReport(
  report: CompileStrategyReport,
): BannerSpec[] {
  const banners: BannerSpec[] = [];
  if (!report.assessment_plan.mode) {
    banners.push({
      kind: "warn",
      message:
        "No AssessmentPlan was attached to this compile run; bridge fell back to env defaults.",
      testid: "banner-no-plan",
    });
  } else if (
    report.assessment_plan.confidence != null
    && report.assessment_plan.confidence < LOW_CONFIDENCE_THRESHOLD
  ) {
    const pct = (report.assessment_plan.confidence * 100).toFixed(0);
    banners.push({
      kind: "warn",
      message:
        `Low-confidence plan (${pct}%). Operator review recommended.`,
      testid: "banner-low-confidence",
    });
  }
  if (report.unhandled_capabilities.length > 0) {
    banners.push({
      kind: "warn",
      message: `Unhandled capabilities: ${report.unhandled_capabilities.join(", ")}. Compile may have produced lower-quality output for these areas.`,
      testid: "banner-unhandled",
    });
  }
  if (report.retry_used) {
    banners.push({
      kind: "warn",
      message: `Compile safety retry triggered (initial=${report.initial_mode} → final=${report.final_mode}).`,
      testid: "banner-retry-used",
    });
  }
  if (report.final_compile_quality === "low") {
    banners.push({
      kind: "warn",
      message:
        "Final compile quality is LOW. Consider Re-process with explicit higher mode or check the source document.",
      testid: "banner-low-quality",
    });
  }
  if (report.final_compile_quality === "failed") {
    banners.push({
      kind: "err",
      message: "Compile FAILED after all retry attempts.",
      testid: "banner-failed",
    });
  }
  return banners;
}

/**
 * Resolve the "applied capabilities" set: required minus unhandled.
 * The mapper's contract: a capability the deployment claimed to
 * support but the parser-level switch couldn't honour shows up in
 * `unhandled_capabilities`; everything else is presumed applied.
 */
export function appliedCapabilities(
  report: CompileStrategyReport,
): string[] {
  const required = new Set(report.assessment_plan.required_capabilities ?? []);
  const unhandled = new Set(report.unhandled_capabilities ?? []);
  return [...required].filter((c) => !unhandled.has(c));
}

/**
 * Returns true when the report indicates the run had no
 * AssessmentPlan (legacy path / mid-flight). The panel renders a
 * "config source: fallback" hint based on this; downstream callers
 * use the same predicate to decide whether to suppress
 * plan-specific UI sections.
 */
export function isFallbackOnly(report: CompileStrategyReport): boolean {
  return !report.assessment_plan.mode;
}
