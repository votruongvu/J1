/**
 * User-facing processing-step vocabulary.
 *
 * J1's internal pipeline names (`compile`, `enrich`, `graph`, …) are
 * stable on the wire and in the audit log, but they're too technical
 * for the operator-facing Run Detail page. This module owns the
 * single mapping from internal names → user-facing labels:
 *
 *   profile / assessment / assess_compile_strategy
 *                                  → "Assess Compile Strategy" (pre-compile)
 *   compile / parse / parser /
 *   chunk / chunks / chunking /
 *   build_content_inventory /
 *   generate_knowledge_chunks      → "Compile" (the sealed RAGAnything stage)
 *   post_compile_assess /
 *   enrich_assessment              → "Assess Enrichment" (post-compile)
 *   enrich / enrichment            → "Enrich Extracted Content"
 *   graph / build_graph            → "Build Knowledge Graph"
 *   index / finalize / complete    → "Finalize Ingestion"
 *
 * The canonical step list is six items — one per macro phase of the
 * post-split-mode pipeline. The mapping table tolerates legacy
 * synonyms so old runs in history still project to the right label.
 *
 * Use the helper functions everywhere a step name reaches the user
 * — Timeline, PrimaryStatusPanel, tab labels — so a backend rename
 * only changes ONE place.
 */

export const PROCESSING_STEP_IDS = [
  "assess_compile_strategy",
  "compile",
  "assess_enrichment",
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
 *   assess compile strategy → compile (sealed black-box, includes
 *   parse + chunk + index inside the adapter) → assess enrichment
 *   → enrich → graph → finalize.
 *
 * Earlier versions of this list included `parse_source_content`,
 * `build_content_inventory`, and `generate_knowledge_chunks` as
 * separate steps — they were split-mode artifacts back when J1 ran
 * compile as multiple activities. The pipeline now runs compile as
 * one indivisible activity (`process_document_complete`), so all
 * three fold into the single "Compile" step.
 */
export const PROCESSING_STEPS: readonly ProcessingStepDef[] = [
  {
    id: "assess_compile_strategy",
    label: "Assess Compile Strategy",
    description:
      "Profile the document and build the AssessmentPlan that drives "
      + "compile config (parse method, capability toggles).",
  },
  {
    id: "compile",
    label: "Compile",
    description:
      "Run the sealed RAGAnything compile stage — parse the source, "
      + "produce the parsed-content manifest, generate knowledge "
      + "chunks, and index. One indivisible activity; the FE Content "
      + "Inventory + Chunks tabs unlock when this completes.",
  },
  {
    id: "assess_enrichment",
    label: "Assess Enrichment",
    description:
      "Rule-based assessment of compile output to decide which "
      + "enrichment tasks should run downstream.",
  },
  {
    id: "enrich_extracted_content",
    label: "Enrich Extracted Content",
    description:
      "Run optional enrichment (image understanding, table "
      + "interpretation, domain-specific extraction).",
  },
  {
    id: "build_knowledge_graph",
    label: "Build Knowledge Graph",
    description:
      "Build entity / relationship graph from compile output and "
      + "enriched chunks.",
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
 * (`compile_doc`), or a legacy split-mode id (`parse_source_content`).
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

  // Pre-compile assessment.
  if (
    key === "assess_compile_strategy"
    || key === "assessment"
    || key === "assessment.created"
    || key === "ingestion.assessment.created"
    || key === "profile"
    || key === "profile_document"
    || key === "assessment_plan"
  ) {
    return "assess_compile_strategy";
  }

  // Compile — the sealed black-box stage. Folds the retired
  // split-mode sub-steps (parse + build inventory + chunks) onto
  // a single canonical id.
  if (
    key === "compile"
    || key === "parse"
    || key === "parser"
    || key === "compile_parse"
    || key === "raganything_compile"
    // Legacy split-mode aliases — kept so historical runs still
    // project to "Compile" instead of falling through to the raw
    // capitalised label.
    || key === "parse_source_content"
    || key === "parsed_content_manifest"
    || key === "content_inventory"
    || key === "content_list"
    || key === "parsed_content_list"
    || key === "build_content_inventory"
    || key === "chunk"
    || key === "chunks"
    || key === "chunking"
    || key === "chunk_task"
    || key === "compile_chunks"
    || key === "generate_knowledge_chunks"
  ) {
    return "compile";
  }

  // Post-compile enrichment assessment.
  if (
    key === "assess_enrichment"
    || key === "post_compile_assess"
    || key === "enrich_assessment"
    || key === "post_compile_enrich_plan"
    || key === "ingestion.post_compile.enrich_assessment"
  ) {
    return "assess_enrichment";
  }

  // Domain enrichment.
  if (
    key === "enrich"
    || key === "enrichment"
    || key === "enrich_stage"
    || key === "llm_enrich"
    || key === "multimodal_enrich"
  ) {
    return "enrich_extracted_content";
  }

  // Graph build.
  if (
    key === "graph"
    || key === "graph_build"
    || key === "build_graph"
    || key === "graph_adapter"
    || key === "knowledge_graph"
  ) {
    return "build_knowledge_graph";
  }

  // Terminal — index + finalize collapse onto the same surface.
  if (
    key === "complete"
    || key === "finalize"
    || key === "index"
    || key === "indexing"
    || key === "run.completed"
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
  // mislabel.
  const trimmed = raw.trim();
  if (trimmed.length === 0) return "—";
  const first = trimmed.charAt(0);
  return first.toUpperCase() + trimmed.slice(1);
}

/**
 * Derived per-step status used across the run-detail surfaces.
 *
 *   `pending`   — the run hasn't reached this step yet
 *   `running`   — the most recent event for this step is `step.started`
 *   `completed` — the most recent event for this step is `step.completed`,
 *                 OR the matching artifact is present in the summary
 *   `skipped`   — `step.skipped` event OR the run summary records skipped
 *   `failed`    — `step.failed` event
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
