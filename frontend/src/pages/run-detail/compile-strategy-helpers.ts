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

/**
 * Operator-facing recommended processing path. Two-mode model:
 * STANDARD_COMPILE / DEEP_COMPILE are the canonical values plus
 * SKIP_EMPTY_DOCUMENT / FAILED for the terminal verdicts.
 *
 * Wire values are stable — dashboards key off them; the FE
 * renders human-readable labels via `recommendedPathLabel`.
 *
 * Legacy values (`fast_text_compile`, `multimodal_compile`,
 * `ocr_parse`) are still listed in the union for back-compat
 * with historical artifact payloads. The label/description
 * helpers map them onto the canonical names; production code
 * NEVER emits them.
 */
export type RecommendedProcessingPath =
  | "standard_compile"
  | "deep_compile"
  | "skip_empty_document"
  | "failed"
  // Legacy values — read-only round-trip.
  | "fast_text_compile"
  | "multimodal_compile"
  | "ocr_parse";

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
  /** Added in the API/UI-shape refactor. Older reports omit it;
   * the panel falls back to deriving from `mode` when missing. */
  recommended_path?: RecommendedProcessingPath;
}

/**
 * Extraction evidence — what the PARSER actually extracted, kept
 * distinct from chunking/index status. Reading this answers
 * "did parsing work?"; reading the Chunks tab + index status
 * answers "did chunking land?". The two must be verifiable
 * separately so a non-empty document can't be reported COMPLETED
 * with zero chunks.
 */
export interface ExtractionEvidence {
  parser?: string;
  parser_method?: string | null;
  text_char_count?: number | null;
  content_block_count?: number | null;
  detected_content_types?: string[];
  page_count?: number | null;
  /** "pending_verification" — chunk count is NEVER claimed here.
   * The FE renders this status with a "verified during compile/
   * index" hint instead of a number. */
  chunking_status?: string;
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
  /** Added in the API/UI-shape refactor. Older reports omit it. */
  extraction_evidence?: ExtractionEvidence | null;
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


// ---- Assessment Plan rendering helpers ----------------------------


export type ConfidenceBucket = "high" | "medium" | "low" | "unknown";

/** Bucket a confidence value (0..1) for color-coding the badge. */
export function confidenceBucket(value: number | undefined): ConfidenceBucket {
  if (value == null) return "unknown";
  if (value >= 0.85) return "high";
  if (value >= LOW_CONFIDENCE_THRESHOLD) return "medium";
  return "low";
}

/** "85%" / "—" — null/undefined safe. */
export function formatConfidence(value: number | undefined): string {
  if (value == null) return "—";
  return `${Math.round(value * 100)}%`;
}

/** Did compile safety retry escalate the mode? `initial !== final`
 * AND both are populated. Drives the "Initial → Final" caption on
 * the AssessmentPlanPanel. */
export function hasModeEscalation(report: CompileStrategyReport): boolean {
  return Boolean(
    report.initial_mode
      && report.final_mode
      && report.initial_mode !== report.final_mode,
  );
}

/** Operator-friendly one-liner explaining what each compile mode
 * actually does. Used as a subtitle under the mode badge. */
export function modeDescription(mode: string | undefined): string {
  switch (mode) {
    case "fast":
      return "Plain-text extraction; skips VLM. Cheap, fast, "
        + "loses image / table understanding.";
    case "standard":
      return "Auto parsing with VLM for layout + tables + figures. "
        + "Balanced default for most documents.";
    case "deep":
      return "OCR + VLM on every page. Slowest but recovers content "
        + "from scanned / image-heavy PDFs.";
    default:
      return "Unknown mode.";
  }
}

/** Pretty-print a capability id (`text_extraction` → "Text extraction"). */
export function capabilityLabel(cap: string): string {
  if (!cap) return cap;
  return cap
    .replace(/_/g, " ")
    .replace(/^./, (c) => c.toUpperCase());
}

/** Resolved compile config for the FINAL attempt — extracted from
 * the last attempt's `mapped_compile_config`. Returns an empty
 * object when no attempts exist (mid-flight / failure before
 * any attempt landed).
 */
export function resolvedCompileConfig(
  report: CompileStrategyReport,
): { parse_method?: string | null; assessment_mode?: string | null;
     unhandled_capabilities?: string[] } {
  const last = report.attempts[report.attempts.length - 1];
  return last?.mapped_compile_config ?? {};
}

/**
 * Coerce legacy `RecommendedProcessingPath` values onto the
 * canonical two-mode vocabulary. Used wherever a payload reads a
 * `recommended_path` field — old `compile_strategy_report`
 * artifacts may carry `fast_text_compile` / `multimodal_compile`
 * / `ocr_parse` and the UI must not show those raw strings.
 *
 *   fast_text_compile  → standard_compile
 *   multimodal_compile → standard_compile
 *   ocr_parse          → deep_compile
 *   (anything else)    → passes through
 */
export function canonicalRecommendedPath(
  path: RecommendedProcessingPath | undefined,
): RecommendedProcessingPath | undefined {
  switch (path) {
    case "fast_text_compile":
    case "multimodal_compile":
      return "standard_compile";
    case "ocr_parse":
      return "deep_compile";
    default:
      return path;
  }
}

/**
 * Operator-friendly label for `RecommendedProcessingPath`. Legacy
 * values are mapped onto the canonical two-mode labels with a
 * "(migrated)" suffix so reviewers can tell at a glance that the
 * artifact was written before the two-mode refactor.
 */
export function recommendedPathLabel(
  path: RecommendedProcessingPath | undefined,
): string {
  switch (path) {
    case "standard_compile":
      return "Standard compile";
    case "deep_compile":
      return "Deep compile";
    case "skip_empty_document":
      return "Skip — empty document";
    case "failed":
      return "Assessment failed";
    case "fast_text_compile":
    case "multimodal_compile":
      return "Standard compile (migrated)";
    case "ocr_parse":
      return "Deep compile (migrated)";
    case undefined:
      return "—";
    default:
      return path;
  }
}

/**
 * One-line operator hint explaining what each recommended path
 * means. Used as a subtitle under the path badge so reviewers
 * don't have to leave the page to understand the verdict.
 */
export function recommendedPathDescription(
  path: RecommendedProcessingPath | undefined,
): string {
  switch (path) {
    case "standard_compile":
      return "Reliable default processing for normal text-first documents. "
        + "Still runs full quality gates — not a shortcut.";
    case "deep_compile":
      return "Richer processing for scanned, layout-heavy, multimodal, "
        + "or low-confidence documents.";
    case "skip_empty_document":
      return "Document has pages but no usable content blocks. "
        + "Downstream enrichment would have nothing to operate on.";
    case "failed":
      return "Assessment couldn't complete; check warnings + retry.";
    case "fast_text_compile":
    case "multimodal_compile":
      return "Legacy plan recorded before the two-mode refactor. "
        + "Treated as Standard compile for this run.";
    case "ocr_parse":
      return "Legacy plan recorded before the two-mode refactor. "
        + "Treated as Deep compile for this run.";
    case undefined:
      return "No recommendation available yet.";
    default:
      return "Unknown path.";
  }
}

/**
 * Backstop: if a report predates `recommended_path`, derive a
 * reasonable value from the mode + capabilities so the FE never
 * shows a blank slot. Older runs still render correctly.
 *
 * Two-mode aware: the result is always one of the canonical
 * values (standard_compile / deep_compile / skip_empty_document
 * / failed) — never a legacy string.
 */
export function recommendedPathFromReport(
  report: CompileStrategyReport,
): RecommendedProcessingPath {
  const explicit = report.assessment_plan?.recommended_path;
  if (explicit) {
    const canonical = canonicalRecommendedPath(explicit);
    if (canonical) return canonical;
  }
  const mode = report.assessment_plan?.mode;
  const caps = new Set([
    ...(report.assessment_plan?.required_capabilities ?? []),
    ...(report.assessment_plan?.optional_capabilities ?? []),
  ]);
  if (caps.has("ocr")) return "deep_compile";
  if (mode === "deep") return "deep_compile";
  // Legacy mode="fast" + current mode="standard" both → standard.
  return "standard_compile";
}

/**
 * Pretty-print a `detected_content_types` token. Mirrors backend
 * tokens emitted by `_build_extraction_evidence`.
 */
export function contentTypeLabel(token: string): string {
  switch (token) {
    case "text":
      return "Text";
    case "images":
      return "Images";
    case "tables":
      return "Tables";
    case "equations":
      return "Equations";
    case "scanned_pages":
      return "Scanned pages";
    default:
      return token;
  }
}
