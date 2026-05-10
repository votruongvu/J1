/**
 * User-facing processing-step vocabulary.
 *
 * J1's internal pipeline names (`compile`, `enrich`, `graph`, …) are
 * stable on the wire and in the audit log, but they're too technical
 * for the operator-facing Run Detail page. This module owns the
 * single mapping from internal names → user-facing labels:
 *
 *   profile / assessment / assess_compile_strategy
 *                                → "Assess Compile Strategy"  (pre-compile)
 *   compile / parse / parser     → "Parse Source Content"
 *   parsed_content_manifest      → "Build Content Inventory"  (synthetic)
 *   post_compile_assess /
 *     enrich_assessment          → "Assess Enrichment"        (post-compile)
 *   chunking / chunks            → "Generate Knowledge Chunks" (synthetic)
 *   enrich / enrichment          → "Enrich Extracted Content"
 *   graph / build_graph          → "Build Knowledge Graph"
 *   index / finalize / complete  → "Finalize Ingestion"
 *
 * Use the helper functions everywhere a step name reaches the user
 * — Timeline, PrimaryStatusPanel, tab labels — so a backend rename
 * only changes ONE place.
 */

export const PROCESSING_STEP_IDS = [
  "assess_compile_strategy",
  "parse_source_content",
  "build_content_inventory",
  "assess_enrichment",
  "generate_knowledge_chunks",
  "enrich_extracted_content",
  "build_knowledge_graph",
  "finalize_ingestion",
] as const;

export type ProcessingStepId = (typeof PROCESSING_STEP_IDS)[number];

export interface ProcessingStepDef {
  id: ProcessingStepId;
  label: string;
  description: string;
}

/**
 * User-facing steps in canonical order. The compile-first journey:
 * profile + assess → compile → synthetic content inventory →
 * post-compile enrich assessment → synthetic chunks → enrich →
 * graph → finalize. Every emitted step.* event projects onto one
 * of these ids via `internalStepToUserFacing`.
 */
export const PROCESSING_STEPS: readonly ProcessingStepDef[] = [
  {
    id: "assess_compile_strategy",
    label: "Assess Compile Strategy",
    description:
      "Profile the document and build the AssessmentPlan that drives " +
      "compile config (parse method, capability toggles).",
  },
  {
    id: "parse_source_content",
    label: "Parse Source Content",
    description:
      "Read and parse the uploaded file using the configured parser.",
  },
  {
    id: "build_content_inventory",
    label: "Build Content Inventory",
    description:
      "Convert the parsed output into an inventory of text blocks, " +
      "tables, images, and headings.",
  },
  {
    id: "assess_enrichment",
    label: "Assess Enrichment",
    description:
      "Rule-based assessment of compile output to decide which " +
      "enrichment tasks should run downstream.",
  },
  {
    id: "generate_knowledge_chunks",
    label: "Generate Knowledge Chunks",
    description:
      "Generate searchable knowledge chunks from the parsed content.",
  },
  {
    id: "enrich_extracted_content",
    label: "Enrich Extracted Content",
    description:
      "Run optional enrichment (image understanding, table " +
      "interpretation, domain-specific extraction).",
  },
  {
    id: "build_knowledge_graph",
    label: "Build Knowledge Graph",
    description:
      "Build entity / relationship graph from compile output and " +
      "enriched chunks.",
  },
  {
    id: "finalize_ingestion",
    label: "Finalize Ingestion",
    description: "Record outcomes, summarise the run, and close out.",
  },
];

const PROCESSING_STEPS_BY_ID: Record<ProcessingStepId, ProcessingStepDef> =
  Object.freeze(
    Object.fromEntries(PROCESSING_STEPS.map((s) => [s.id, s])) as Record<
      ProcessingStepId,
      ProcessingStepDef
    >,
  );

export function processingStepById(
  id: ProcessingStepId,
): ProcessingStepDef {
  return PROCESSING_STEPS_BY_ID[id];
}

/**
 * Map an internal step / stage / activity string to the canonical
 * user-facing step id. Tolerant to synonyms — the same id can be
 * reached from a stage label (`COMPILE`), an activity name
 * (`compile_doc`), or a user-spec id (`generate_knowledge_chunks`).
 *
 * Returns `null` for strings the mapping doesn't recognise so
 * callers can fall back to the raw label rather than misattribute.
 */
export function internalStepToUserFacing(
  raw: string | null | undefined,
): ProcessingStepId | null {
  if (!raw) return null;
  const key = raw.trim().toLowerCase();
  if (!key) return null;

  // Pre-mapped — passes through.
  if (PROCESSING_STEP_IDS.includes(key as ProcessingStepId)) {
    return key as ProcessingStepId;
  }

  // Internal stage labels (uppercase) and activity / step names.
  if (
    key === "assess_compile_strategy" ||
    key === "assessment" ||
    key === "assessment.created" ||
    key === "ingestion.assessment.created" ||
    key === "profile" ||
    key === "profile_document" ||
    key === "assessment_plan"
  ) {
    return "assess_compile_strategy";
  }
  if (
    key === "compile" ||
    key === "parse" ||
    key === "parser" ||
    key === "compile_parse" ||
    key === "raganything_compile"
  ) {
    return "parse_source_content";
  }
  if (
    key === "parsed_content_manifest" ||
    key === "content_inventory" ||
    key === "content_list" ||
    key === "parsed_content_list" ||
    key === "build_content_inventory"
  ) {
    return "build_content_inventory";
  }
  if (
    key === "assess_enrichment" ||
    key === "post_compile_assess" ||
    key === "enrich_assessment" ||
    key === "post_compile_enrich_plan" ||
    key === "ingestion.post_compile.enrich_assessment"
  ) {
    return "assess_enrichment";
  }
  if (
    key === "chunk" ||
    key === "chunks" ||
    key === "chunking" ||
    key === "chunk_task" ||
    key === "compile_chunks" ||
    key === "generate_knowledge_chunks"
  ) {
    return "generate_knowledge_chunks";
  }
  if (
    key === "enrich" ||
    key === "enrichment" ||
    key === "llm_enrich" ||
    key === "multimodal_enrich"
  ) {
    return "enrich_extracted_content";
  }
  if (
    key === "graph" ||
    key === "graph_build" ||
    key === "build_graph" ||
    key === "graph_adapter" ||
    key === "knowledge_graph"
  ) {
    return "build_knowledge_graph";
  }
  if (
    key === "complete" ||
    key === "finalize" ||
    key === "index" ||
    key === "indexing" ||
    key === "run.completed"
  ) {
    return "finalize_ingestion";
  }
  return null;
}

/**
 * User-facing label for an internal step / stage. Returns the raw
 * string capitalised when no mapping applies — preserves provenance
 * for unrecognised names instead of swallowing them.
 */
export function userFacingStepLabel(raw: string | null | undefined): string {
  if (!raw) return "—";
  const id = internalStepToUserFacing(raw);
  if (id) return PROCESSING_STEPS_BY_ID[id].label;
  // Unknown internal name — preserve as-is rather than silently
  // mislabel. The Timeline already shows the raw stage badge for
  // diagnostic context.
  const trimmed = raw.trim();
  if (trimmed.length === 0) return "—";
  const first = trimmed.charAt(0);
  return first.toUpperCase() + trimmed.slice(1);
}

/**
 * Derived per-step status used across the run-detail surfaces.
 *
 * `pending`   — the run hasn't reached this step yet
 * `running`   — the most recent event for this step is `step.started`
 * `completed` — the most recent event for this step is `step.completed`,
 *                 OR the matching artifact is present in the summary
 * `skipped`   — `step.skipped` event OR the run summary records skipped
 * `failed`    — `step.failed` event
 *
 * Centralised here so the timeline, status badges, and the
 * Execution Plan tab all classify status the same way.
 */
export type ProcessingStepStatus =
  | "pending"
  | "running"
  | "completed"
  | "skipped"
  | "failed";

const STATUS_PRIORITY: Record<ProcessingStepStatus, number> = {
  pending: 0,
  running: 1,
  skipped: 2,
  completed: 3,
  failed: 4,
};

/** Pick the higher-priority of two statuses (failed > completed > …). */
export function maxStatus(
  a: ProcessingStepStatus,
  b: ProcessingStepStatus,
): ProcessingStepStatus {
  return STATUS_PRIORITY[a] >= STATUS_PRIORITY[b] ? a : b;
}
