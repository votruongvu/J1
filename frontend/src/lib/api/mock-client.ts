/**
 * MockClient — scripted in-browser implementation of `IngestionClient`.
 *
 * Drives the entire flow (upload → assess → plan → confirm → live
 * progress → final result) without a backend. Three scenarios are
 * available: `warnings` (default — completes with one warning),
 * `failure` (graph build fails), `review` (human review required).
 *
 * The scripted timeline matches the design's prototype event
 * sequence verbatim — including timing, copy, and stage labels —
 * so demos / screenshots are reproducible.
 */

import {
  type ExecutionPlan,
  type IngestionRun,
  type ProgressEvent,
  type ProgressEventType,
  type RunListItem,
  type RunListQuery,
  type RunListResult,
  type RunStatus,
  type Stage,
  isTerminalEvent,
} from "@/types/ingestion";
import type {
  ContentInventory,
  GenerateValidationSetRequest,
  ManualTestQueryRequest,
  ManualTestQueryResponse,
  PlanningResult,
  ReviewArtifactContent,
  ReviewArtifactListQuery,
  ReviewArtifactPage,
  ReviewArtifactRecord,
  ReviewChunkDetail,
  ReviewChunkListQuery,
  ReviewChunkPage,
  ReviewChunkPreview,
  ReviewGraphQuery,
  ReviewGraphSnapshot,
  ReviewQualityReport,
  ReviewRunSummary,
  StartValidationRunRequest,
  ValidationRun,
  ValidationRunListItem,
  ValidationSet,
  ValidationSetListItem,
} from "@/types/review";
import type { MockScenario, ProjectContext } from "@/types/ui";
import {
  ApiError,
  type IngestionClient,
  type LLMHealthStatus,
  type RunControlResult,
  type StreamHandle,
  type StreamHandlers,
  type UploadFile,
} from "./client";

export const MOCK_TENANT = "demo-tenant";
export const MOCK_PROJECT = "demo-project";

// ---- Plan template (deterministic) ----------------------------------

function makePlan(): ExecutionPlan {
  return {
    runId: "run_2dD4kQ8mLxN9pQzV",
    summary: {
      total: 11,
      run: 7,
      skip: 3,
      conditional: 1,
      stages: ["COMPILE", "ENRICH", "GRAPH", "INDEX"],
    },
    steps: [
      {
        id: "text_extract",
        stage: "COMPILE",
        name: "Text extraction",
        decision: "RUN",
        reason: "Source document is a PDF; native text layer present.",
        risk_level: "LOW",
        estimated_cost_tier: "S",
        expected_engine: "pdfium",
      },
      {
        id: "ocr",
        stage: "COMPILE",
        name: "OCR (image-only fallback)",
        decision: "SKIP",
        reason: "Native text layer detected — OCR not required.",
        risk_level: "LOW",
        estimated_cost_tier: "M",
        expected_engine: "tesseract",
      },
      {
        id: "layout_prepare",
        stage: "COMPILE",
        name: "Layout preparation",
        decision: "RUN",
        reason: "Multi-column layout detected; segmentation required for downstream.",
        risk_level: "MEDIUM",
        estimated_cost_tier: "M",
        expected_engine: "layoutlmv3",
        expected_provider: "internal",
      },
      {
        id: "table_extract",
        stage: "COMPILE",
        name: "Table extraction",
        decision: "RUN",
        reason: "12 candidate tables detected via heuristics.",
        risk_level: "MEDIUM",
        estimated_cost_tier: "M",
        expected_engine: "camelot",
        warning: "Some tables span pages — extraction quality may vary.",
      },
      {
        id: "entity_link",
        stage: "ENRICH",
        name: "Entity linking",
        decision: "RUN",
        reason: "Domain knowledge graph available for tenant.",
        risk_level: "LOW",
        estimated_cost_tier: "S",
        expected_engine: "spacy-ner",
        expected_provider: "internal",
      },
      {
        id: "summarize",
        stage: "ENRICH",
        name: "Section summarization",
        decision: "RUN",
        reason: "Document length 84 pages exceeds summary threshold.",
        risk_level: "LOW",
        estimated_cost_tier: "L",
        expected_engine: "haiku-4.5",
        expected_provider: "anthropic",
      },
      {
        id: "translate",
        stage: "ENRICH",
        name: "Translation",
        decision: "SKIP",
        reason: "Document language matches project default (en).",
        risk_level: "LOW",
        estimated_cost_tier: "L",
        expected_engine: "deepl",
        expected_provider: "deepl",
      },
      {
        id: "pii_scan",
        stage: "ENRICH",
        name: "PII redaction",
        decision: "SKIP",
        reason: "Project policy 'allow-pii' set; redaction disabled.",
        risk_level: "HIGH",
        estimated_cost_tier: "S",
        expected_engine: "presidio",
        expected_provider: "internal",
      },
      {
        id: "graph_build",
        stage: "GRAPH",
        name: "Knowledge graph build",
        decision: "RUN",
        reason: "Entity links produced; graph projection enabled.",
        risk_level: "MEDIUM",
        estimated_cost_tier: "M",
        expected_engine: "neo4j-projection",
        expected_provider: "internal",
      },
      {
        id: "graph_dedupe",
        stage: "GRAPH",
        name: "Cross-document deduplication",
        decision: "CONDITIONAL",
        reason: "Will run if ≥3 nodes overlap with existing tenant graph.",
        risk_level: "LOW",
        estimated_cost_tier: "S",
        expected_engine: "graph-merge",
        expected_provider: "internal",
      },
      {
        id: "vector_index",
        stage: "INDEX",
        name: "Vector index",
        decision: "RUN",
        reason: "Project search enabled; embedding model configured.",
        risk_level: "LOW",
        estimated_cost_tier: "M",
        expected_engine: "voyage-3",
        expected_provider: "voyage",
      },
    ],
  };
}

// ---- Event timeline (scripted) --------------------------------------

interface ScriptEntry extends ProgressEvent {
  /** Delay (ms) between this event and the previous one. */
  delay: number;
  /** Optional gate name — script pauses here until it's resolved. */
  gate?: "confirm";
}

let evCounter = 0;
function nextId(): string {
  return `evt_${(++evCounter).toString().padStart(4, "0")}`;
}

function ev(
  type: ProgressEventType,
  data: Partial<ProgressEvent["data"]>,
  delay: number,
  extra?: { gate?: "confirm" },
): ScriptEntry {
  return {
    eventId: nextId(),
    event: type,
    ts: 0,
    delay,
    data: { runId: "", ...data } as ProgressEvent["data"],
    ...(extra ?? {}),
  };
}

function makeEventScript(runId: string): ScriptEntry[] {
  evCounter = 0;
  const fix = (entries: ScriptEntry[]): ScriptEntry[] =>
    entries.map((e) => ({ ...e, data: { ...e.data, runId } }));
  return fix([
    ev("run.created", { message: "Run created.", severity: "INFO" }, 0),
    ev(
      "document.received",
      { message: "Received earnings-2024-q4.pdf · 84 pages · 12.4 MB", severity: "INFO" },
      400,
    ),
    ev(
      "assessment.started",
      { message: "Assessing document characteristics…", severity: "INFO", stage: "COMPILE" },
      500,
    ),
    ev(
      "assessment.completed",
      {
        message: "Assessment complete. 11 candidate steps identified.",
        severity: "INFO",
        stage: "COMPILE",
      },
      900,
    ),
    ev(
      "plan.generated",
      { message: "Execution plan generated. Awaiting confirmation.", severity: "INFO" },
      600,
    ),
    ev("plan.confirmed", { message: "Plan confirmed by user.", severity: "INFO" }, 0, {
      gate: "confirm",
    }),

    // COMPILE
    ev(
      "step.started",
      {
        message: "Starting text extraction.",
        stage: "COMPILE",
        step: "text_extract",
        engine: "pdfium",
      },
      500,
    ),
    ev(
      "step.progress",
      {
        stage: "COMPILE",
        step: "text_extract",
        engine: "pdfium",
        progress: 0.5,
        current: 42,
        total: 84,
        message: "Extracting page 42 of 84…",
      },
      700,
    ),
    ev(
      "step.completed",
      {
        message: "Text extracted from 84 pages.",
        stage: "COMPILE",
        step: "text_extract",
        engine: "pdfium",
        severity: "INFO",
      },
      900,
    ),

    ev(
      "step.skipped",
      {
        message: "OCR skipped: native text layer detected.",
        stage: "COMPILE",
        step: "ocr",
        reason: "Native text layer detected — OCR not required.",
        severity: "INFO",
      },
      300,
    ),

    ev(
      "step.started",
      {
        message: "Preparing layout…",
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        provider: "internal",
      },
      500,
    ),
    ev(
      "step.progress",
      {
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        provider: "internal",
        progress: 0.25,
        current: 21,
        total: 84,
        message: "Segmenting columns…",
      },
      600,
    ),
    ev(
      "step.progress",
      {
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        provider: "internal",
        progress: 0.5,
        current: 42,
        total: 84,
        message: "Resolving figure boundaries…",
      },
      700,
    ),
    ev(
      "step.progress",
      {
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        provider: "internal",
        progress: 0.8,
        current: 67,
        total: 84,
        message: "Linking captions to figures…",
      },
      700,
    ),
    ev(
      "step.progress",
      {
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        provider: "internal",
        progress: 1.0,
        current: 84,
        total: 84,
        message: "Layout complete.",
      },
      600,
    ),
    ev(
      "step.completed",
      {
        message: "Layout prepared.",
        stage: "COMPILE",
        step: "layout_prepare",
        engine: "layoutlmv3",
        severity: "INFO",
      },
      400,
    ),

    ev(
      "step.started",
      {
        message: "Extracting tables…",
        stage: "COMPILE",
        step: "table_extract",
        engine: "camelot",
      },
      400,
    ),
    ev(
      "step.warning",
      {
        message: "Cross-page table on pages 47–49 may have inconsistent column count.",
        stage: "COMPILE",
        step: "table_extract",
        severity: "WARNING",
        warning:
          "Table 7 spans pages 47–49 with 6 columns on p.47 and 5 on p.49. Manual review suggested.",
      },
      700,
    ),
    ev(
      "step.completed",
      {
        message: "Extracted 11 of 12 tables.",
        stage: "COMPILE",
        step: "table_extract",
        severity: "WARNING",
      },
      600,
    ),

    // ENRICH
    ev(
      "step.started",
      {
        message: "Linking entities…",
        stage: "ENRICH",
        step: "entity_link",
        engine: "spacy-ner",
        provider: "internal",
      },
      500,
    ),
    ev(
      "step.progress",
      {
        stage: "ENRICH",
        step: "entity_link",
        engine: "spacy-ner",
        progress: 0.6,
        current: 312,
        total: 520,
        message: "312/520 mentions resolved",
      },
      700,
    ),
    ev(
      "step.completed",
      {
        message: "Linked 487 entities.",
        stage: "ENRICH",
        step: "entity_link",
        severity: "INFO",
      },
      700,
    ),

    ev(
      "step.started",
      {
        message: "Summarizing sections…",
        stage: "ENRICH",
        step: "summarize",
        engine: "haiku-4.5",
        provider: "anthropic",
      },
      500,
    ),
    ev(
      "step.progress",
      {
        stage: "ENRICH",
        step: "summarize",
        engine: "haiku-4.5",
        provider: "anthropic",
        progress: 0.4,
        current: 8,
        total: 21,
        message: "Section 8 of 21",
      },
      800,
    ),
    ev(
      "step.progress",
      {
        stage: "ENRICH",
        step: "summarize",
        engine: "haiku-4.5",
        provider: "anthropic",
        progress: 0.85,
        current: 18,
        total: 21,
        message: "Section 18 of 21",
      },
      900,
    ),
    ev(
      "step.completed",
      {
        message: "Summaries written for 21 sections.",
        stage: "ENRICH",
        step: "summarize",
        severity: "INFO",
      },
      600,
    ),

    ev(
      "step.skipped",
      {
        message: "Translation skipped: matches project default.",
        stage: "ENRICH",
        step: "translate",
        reason: "Document language matches project default (en).",
        severity: "INFO",
      },
      300,
    ),
    ev(
      "step.skipped",
      {
        message: "PII redaction skipped: tenant policy 'allow-pii'.",
        stage: "ENRICH",
        step: "pii_scan",
        reason: "Project policy 'allow-pii' set; redaction disabled.",
        severity: "WARNING",
      },
      300,
    ),

    // GRAPH
    ev(
      "step.started",
      {
        message: "Building knowledge graph…",
        stage: "GRAPH",
        step: "graph_build",
        engine: "neo4j-projection",
        provider: "internal",
      },
      600,
    ),
    ev(
      "step.progress",
      {
        stage: "GRAPH",
        step: "graph_build",
        engine: "neo4j-projection",
        progress: 0.5,
        current: 244,
        total: 487,
        message: "244/487 nodes projected",
      },
      900,
    ),
    ev(
      "step.completed",
      {
        message: "Graph built: 487 nodes, 1,204 edges.",
        stage: "GRAPH",
        step: "graph_build",
        severity: "INFO",
      },
      700,
    ),

    ev(
      "step.skipped",
      {
        message: "Cross-document dedup skipped: only 1 overlapping node.",
        stage: "GRAPH",
        step: "graph_dedupe",
        reason: "Threshold not met (1 < 3 nodes).",
        severity: "INFO",
      },
      400,
    ),

    // INDEX
    ev(
      "step.started",
      {
        message: "Embedding documents…",
        stage: "INDEX",
        step: "vector_index",
        engine: "voyage-3",
        provider: "voyage",
      },
      500,
    ),
    ev(
      "step.progress",
      {
        stage: "INDEX",
        step: "vector_index",
        engine: "voyage-3",
        provider: "voyage",
        progress: 0.3,
        current: 124,
        total: 412,
        message: "124/412 chunks embedded",
      },
      800,
    ),
    ev(
      "step.progress",
      {
        stage: "INDEX",
        step: "vector_index",
        engine: "voyage-3",
        provider: "voyage",
        progress: 0.7,
        current: 289,
        total: 412,
        message: "289/412 chunks embedded",
      },
      900,
    ),
    ev(
      "step.completed",
      {
        message: "Index ready. 412 chunks indexed.",
        stage: "INDEX",
        step: "vector_index",
        severity: "INFO",
      },
      700,
    ),

    ev(
      "run.completed",
      {
        message: "Run completed with 1 warning.",
        severity: "WARNING",
        warning_count: 1,
        warning_summary: "Table 7 column-count mismatch on pages 47–49.",
      },
      500,
    ),
  ]);
}

// ---- Mock list data -------------------------------------------------

interface InternalRun extends IngestionRun {
  tenant?: string;
  project?: string;
}

function mapInternalToListItem(r: InternalRun): RunListItem {
  let mapped: RunStatus = r.status;
  if (mapped === "COMPLETED") mapped = "SUCCEEDED";
  else if (mapped === "COMPLETED_WITH_WARNINGS") mapped = "SUCCEEDED_WITH_WARNINGS";
  else if (mapped === "AWAITING_HUMAN_REVIEW") mapped = "REQUIRES_HUMAN_REVIEW";
  const terminal: RunStatus[] = [
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "FAILED",
    "SUCCEEDED",
    "SUCCEEDED_WITH_WARNINGS",
  ];
  return {
    runId: r.runId,
    documentName: r.document_name,
    status: mapped,
    mode: r.mode,
    policy: r.policy,
    currentStage: r.current_stage ?? null,
    currentStep: r.current_step ?? null,
    progressPercent: r.progress_pct || 0,
    warningCount: r.warning_count || 0,
    startedAt: r.started_at ?? null,
    updatedAt: r.started_at ?? null,
    completedAt: terminal.includes(r.status) ? new Date().toISOString() : null,
    failureCode: r.final?.failure_code ?? null,
    failureMessage: r.final?.failure_message ?? null,
  };
}

function mockListData(): RunListItem[] {
  const now = Date.now();
  const ago = (mins: number) => new Date(now - mins * 60_000).toISOString();
  return [
    {
      runId: "run_8K2pQ7vXmN3wJ1",
      documentName: "fy24-annual-report.pdf",
      status: "SUCCEEDED_WITH_WARNINGS",
      mode: "STANDARD",
      policy: "allow-pii",
      currentStage: "INDEX",
      currentStep: "vector_index",
      progressPercent: 100,
      warningCount: 2,
      startedAt: ago(124),
      updatedAt: ago(98),
      completedAt: ago(98),
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_3mLk9bRtY8sQ2X",
      documentName: "vendor-contract-acme.docx",
      status: "RUNNING",
      mode: "STANDARD",
      policy: "redact-pii",
      currentStage: "ENRICH",
      currentStep: "summarize",
      progressPercent: 64,
      warningCount: 0,
      startedAt: ago(8),
      updatedAt: ago(0),
      completedAt: null,
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_aZ7nDpV2cM4yL5",
      documentName: "research-paper-quantum.pdf",
      status: "PLAN_READY",
      mode: "FAST",
      policy: "allow-pii",
      currentStage: "COMPILE",
      currentStep: null,
      progressPercent: 0,
      warningCount: 0,
      startedAt: ago(2),
      updatedAt: ago(1),
      completedAt: null,
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_qW3eR4tY5uI6oP",
      documentName: "compliance-audit-q3.pdf",
      status: "REQUIRES_HUMAN_REVIEW",
      mode: "THOROUGH",
      policy: "redact-pii",
      currentStage: "ENRICH",
      currentStep: "pii_scan",
      progressPercent: 78,
      warningCount: 3,
      startedAt: ago(45),
      updatedAt: ago(12),
      completedAt: null,
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_xC8vB7nM6kJ5hG",
      documentName: "legacy-handbook-v2.docx",
      status: "FAILED",
      mode: "STANDARD",
      policy: "allow-pii",
      currentStage: "GRAPH",
      currentStep: "graph_build",
      progressPercent: 56,
      warningCount: 1,
      startedAt: ago(204),
      updatedAt: ago(180),
      completedAt: ago(180),
      failureCode: "GRAPH_CONSTRAINT_VIOLATION",
      failureMessage: "Constraint 'mentioned_in_unique' violated by 4 rows.",
    },
    {
      runId: "run_lP9kJ8hG7fD6sA",
      documentName: "product-spec-v3.md",
      status: "SUCCEEDED",
      mode: "FAST",
      policy: "allow-pii",
      currentStage: "INDEX",
      currentStep: "vector_index",
      progressPercent: 100,
      warningCount: 0,
      startedAt: ago(360),
      updatedAt: ago(320),
      completedAt: ago(320),
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_zX2cV3bN4mK5jH",
      documentName: "investor-deck-q4.pdf",
      status: "WAITING_FOR_CONFIRMATION",
      mode: "STANDARD",
      policy: "allow-pii",
      currentStage: "COMPILE",
      currentStep: null,
      progressPercent: 0,
      warningCount: 0,
      startedAt: ago(5),
      updatedAt: ago(4),
      completedAt: null,
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_yT6rE5wQ4aS3dF",
      documentName: "internal-wiki-export.html",
      status: "ASSESSING",
      mode: "STANDARD",
      policy: "allow-pii",
      currentStage: "COMPILE",
      currentStep: null,
      progressPercent: 0,
      warningCount: 0,
      startedAt: ago(1),
      updatedAt: ago(0),
      completedAt: null,
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_gH7jK8lP9oI0uY",
      documentName: "press-release-launch.txt",
      status: "CANCELLED",
      mode: "FAST",
      policy: "allow-pii",
      currentStage: "COMPILE",
      currentStep: "text_extract",
      progressPercent: 12,
      warningCount: 0,
      startedAt: ago(1440),
      updatedAt: ago(1420),
      completedAt: ago(1420),
      failureCode: null,
      failureMessage: null,
    },
    {
      runId: "run_uI9oP0aS1dF2gH",
      documentName: "customer-support-faqs.md",
      status: "SUCCEEDED",
      mode: "STANDARD",
      policy: "allow-pii",
      currentStage: "INDEX",
      currentStep: "vector_index",
      progressPercent: 100,
      warningCount: 0,
      startedAt: ago(2880),
      updatedAt: ago(2810),
      completedAt: ago(2810),
      failureCode: null,
      failureMessage: null,
    },
  ];
}

// ---- Mock chunk fixtures (Phase 8) ----------------------------------
//
// Realistic-ish previews so the Chunks tab has something to render in
// mock mode. Bodies are synthesised on demand from the preview text
// so we don't pay storage for full text in this fixture.

function mockChunkList(): ReviewChunkPreview[] {
  const make = (
    n: number,
    opts: Partial<ReviewChunkPreview> & {
      preview: string;
      pageStart: number;
      pageEnd: number;
      tokenCount: number;
      confidence: number;
    },
  ): ReviewChunkPreview => ({
    chunkId: `ch_${String(n).padStart(3, "0")}`,
    section: opts.section ?? "Body",
    title: opts.title ?? null,
    metadata: opts.metadata ?? {},
    linkedAssets: opts.linkedAssets ?? [],
    sourceArtifactId: opts.sourceArtifactId ?? "art_compile_demo",
    pageStart: opts.pageStart,
    pageEnd: opts.pageEnd,
    tokenCount: opts.tokenCount,
    confidence: opts.confidence,
    preview: opts.preview,
  });
  return [
    make(1, {
      preview:
        "Quarterly performance summary highlighting growth in the cloud segment, partially offset by hardware shortages.",
      pageStart: 1, pageEnd: 1, section: "Executive summary",
      tokenCount: 96, confidence: 0.92,
    }),
    make(2, {
      preview:
        "Revenue grew 12.3% year-over-year, driven by recurring subscriptions and higher attach rates on enterprise tiers.",
      pageStart: 2, pageEnd: 2, section: "Financials",
      tokenCount: 134, confidence: 0.88,
      linkedAssets: [{ artifactId: "tab_revenue_q4", kind: "enriched.tables" }],
    }),
    make(3, {
      preview:
        "OCR confidence on table 3.1 was lower than expected — operators should manually review numeric figures on page 7.",
      pageStart: 7, pageEnd: 7, section: "Notes",
      tokenCount: 88, confidence: 0.55,
      metadata: { status: "needs_review" },
    }),
    make(4, {
      preview:
        "Forward-looking risk factors include sustained currency volatility and renewal headwinds in two key contracts.",
      pageStart: 9, pageEnd: 10, section: "Risks",
      tokenCount: 178, confidence: 0.81,
    }),
    make(5, {
      preview:
        "Operating margin held at 23.1% as automation investments offset incremental wage pressure across regions.",
      pageStart: 11, pageEnd: 11, section: "Financials",
      tokenCount: 102, confidence: 0.86,
    }),
  ];
}

function mockChunkBody(p: ReviewChunkPreview): string {
  // Repeat the preview a couple of times so the drawer's "Readable"
  // view shows substantive content for screenshots / e2e.
  const lines = [
    p.title ? `# ${p.title}\n\n` : "",
    `${p.preview}\n\n`,
    `${p.preview}\n\n`,
    `(Pages ${p.pageStart ?? "?"}–${p.pageEnd ?? "?"} · ${p.section ?? "Body"})\n`,
  ];
  return lines.join("");
}

// ---- Mock artifact fixtures (Phase 9) -------------------------------
//
// Five artifacts spanning chunk / table / visual / formula / graph
// shapes — one per kind the FE renders specially. Bytes are
// synthesised on demand by `mockArtifactContent` so the fixture
// stays small.

function mockArtifactList(): ReviewArtifactRecord[] {
  const now = new Date(Date.now() - 60_000).toISOString();
  const make = (
    over: Partial<ReviewArtifactRecord> & {
      artifactId: string;
      kind: string;
      location: string;
      byteSize: number;
    },
  ): ReviewArtifactRecord => ({
    contentHash: `sha256:mock-${over.artifactId}`,
    status: "succeeded",
    reviewStatus: "not_required",
    version: 1,
    createdAt: now,
    updatedAt: now,
    sourceDocumentIds: ["doc_demo"],
    sourceArtifactIds: [],
    metadata: { run_id: "run_mock" },
    ...over,
  });
  return [
    make({
      artifactId: "art_chunk_001",
      kind: "chunk",
      location: "compiled/chunk_001.json",
      byteSize: 412,
    }),
    make({
      artifactId: "art_table_revenue",
      kind: "enriched.tables",
      location: "enriched/revenue.json",
      byteSize: 786,
      metadata: { run_id: "run_mock", caption: "Revenue by segment" },
    }),
    make({
      artifactId: "art_visual_pixel",
      kind: "enriched.visuals",
      location: "enriched/pixel.png",
      byteSize: 71,
      metadata: { run_id: "run_mock", caption: "Tiny demo PNG" },
    }),
    make({
      artifactId: "art_formula_alpha",
      kind: "enriched.formulas",
      location: "enriched/alpha.json",
      byteSize: 188,
    }),
    make({
      artifactId: "art_graph_kv",
      kind: "graph_json",
      location: "graph/vdb_entities.json",
      byteSize: 1024,
    }),
  ];
}

/**
 * Synthesise the byte payload for a mock artifact. Mirrors what the
 * production `/artifacts/{id}/content` endpoint would return, with
 * a stable content-type per artifact kind.
 */
function mockArtifactContent(
  record: ReviewArtifactRecord,
): ReviewArtifactContent {
  const kind = record.kind;
  const filenameOnly = record.location.split("/").pop() ?? record.artifactId;

  if (kind === "enriched.tables") {
    const text = JSON.stringify(
      {
        title: "Revenue by segment",
        columns: ["segment", "q3", "q4"],
        rows: [
          ["Cloud", 412, 478],
          ["Hardware", 318, 296],
          ["Services", 207, 234],
        ],
      },
      null,
      2,
    );
    return {
      blob: new Blob([text], { type: "application/json" }),
      contentType: "application/json",
      filename: null,
      etag: record.contentHash,
    };
  }
  if (kind === "enriched.visuals") {
    // 1×1 transparent PNG — keeps the fixture tiny but the FE can
    // still render it as a real <img> via `URL.createObjectURL`.
    const pngBytes = new Uint8Array([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d,
      0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
      0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00,
      0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0x00, 0x01, 0x00, 0x00,
      0x05, 0x00, 0x01, 0x0d, 0x0a, 0x2d, 0xb4, 0x00, 0x00, 0x00, 0x00, 0x49,
      0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ]);
    return {
      blob: new Blob([pngBytes], { type: "image/png" }),
      contentType: "image/png",
      filename: null,
      etag: record.contentHash,
    };
  }
  if (kind === "enriched.formulas") {
    const text = JSON.stringify({ formulas: [{ tex: "E = mc^2" }] }, null, 2);
    return {
      blob: new Blob([text], { type: "application/json" }),
      contentType: "application/json",
      filename: null,
      etag: record.contentHash,
    };
  }
  if (kind === "chunk") {
    const text = JSON.stringify(
      { chunkId: record.artifactId, body: "Synthesised chunk body." },
      null,
      2,
    );
    return {
      blob: new Blob([text], { type: "application/json" }),
      contentType: "application/json",
      filename: null,
      etag: record.contentHash,
    };
  }
  if (kind === "graph_json") {
    const text = JSON.stringify(
      { entities: { e1: { __id__: "e1", __name__: "Demo entity" } } },
      null,
      2,
    );
    return {
      blob: new Blob([text], { type: "application/json" }),
      contentType: "application/json",
      filename: null,
      etag: record.contentHash,
    };
  }
  // Unknown kind → octet-stream + attachment filename.
  return {
    blob: new Blob([new Uint8Array([0])], {
      type: "application/octet-stream",
    }),
    contentType: "application/octet-stream",
    filename: filenameOnly,
    etag: record.contentHash,
  };
}

// ---- Mock graph fixtures (Phase 10) ---------------------------------

function mockGraphUnavailable(
  reason: string, opts?: ReviewGraphQuery,
): ReviewGraphSnapshot {
  return {
    stats: { entityCount: 0, relationCount: 0, sourceArtifactIds: [] },
    entities: [],
    relations: [],
    truncated: {
      entities: false,
      relations: false,
      limits: {
        maxNodes: opts?.maxNodes ?? 5000,
        maxEdges: opts?.maxEdges ?? 5000,
      },
    },
    unavailable: { reason },
  };
}

function mockGraphPopulated(opts?: ReviewGraphQuery): ReviewGraphSnapshot {
  const maxNodes = opts?.maxNodes ?? 5000;
  const maxEdges = opts?.maxEdges ?? 5000;
  const allEntities = [
    {
      id: "PERSON:Alice",
      label: "Alice",
      type: "PERSON",
      description: "Lead author of the quarterly report.",
      sourceChunkIds: ["ch_001", "ch_002"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "PERSON:Bob",
      label: "Bob",
      type: "PERSON",
      description: "Reviewer of the financial section.",
      sourceChunkIds: ["ch_003"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "ORG:Acme",
      label: "Acme Corporation",
      type: "ORG",
      description: "Subject organisation.",
      sourceChunkIds: ["ch_001", "ch_002", "ch_003"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "EVENT:Q4Earnings",
      label: "Q4 Earnings call",
      type: "EVENT",
      description: "Quarterly earnings briefing referenced throughout.",
      sourceChunkIds: ["ch_002"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "METRIC:OperatingMargin",
      label: "Operating margin",
      type: "METRIC",
      description: null,
      sourceChunkIds: ["ch_005"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
  ];
  const allRelations = [
    {
      id: "rel_001",
      sourceEntityId: "PERSON:Alice",
      targetEntityId: "ORG:Acme",
      label: "works_at",
      type: null,
      description: null,
      weight: 0.9,
      sourceChunkIds: ["ch_001"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "rel_002",
      sourceEntityId: "PERSON:Bob",
      targetEntityId: "ORG:Acme",
      label: "reviewed",
      type: null,
      description: "Bob signed off on the financial section.",
      weight: 0.7,
      sourceChunkIds: ["ch_003"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
    {
      id: "rel_003",
      sourceEntityId: "ORG:Acme",
      targetEntityId: "EVENT:Q4Earnings",
      label: "hosted",
      type: null,
      description: null,
      weight: 0.95,
      sourceChunkIds: ["ch_002"],
      sourceArtifactIds: ["art_graph_kv"],
      metadata: {},
    },
  ];
  // Apply caps so the FE truncation banner exercises in mock mode if
  // the operator passes a tiny `maxNodes` / `maxEdges`.
  const entities = allEntities.slice(0, maxNodes);
  const relations = allRelations.slice(0, maxEdges);
  return {
    stats: {
      entityCount: allEntities.length,
      relationCount: allRelations.length,
      sourceArtifactIds: ["art_graph_kv"],
    },
    entities,
    relations,
    truncated: {
      entities: allEntities.length > maxNodes,
      relations: allRelations.length > maxEdges,
      limits: { maxNodes, maxEdges },
    },
    unavailable: null,
  };
}

// ---- MockClient -----------------------------------------------------

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class MockClient implements IngestionClient {
  private currentRun: InternalRun | null = null;
  private eventBuffer: ProgressEvent[] = [];
  private gateResolved = { confirm: false };
  private scenario: MockScenario = "warnings";

  setScenario(s: MockScenario): void {
    this.scenario = s;
  }

  async listRuns(ctx: ProjectContext, opts: RunListQuery = {}): Promise<RunListResult> {
    await delay(180);
    if (!ctx.tenant || !ctx.project) {
      throw new ApiError(
        400,
        "Tenant and Project are required. Please set them in the context bar.",
      );
    }
    let items = mockListData();
    if (this.currentRun) {
      const live = mapInternalToListItem(this.currentRun);
      items = [live, ...items.filter((x) => x.runId !== live.runId)];
    }
    const { q, status, stage, page = 1, pageSize = 20 } = opts;
    if (q) {
      const Q = q.toLowerCase();
      items = items.filter(
        (x) => x.documentName.toLowerCase().includes(Q) || x.runId.toLowerCase().includes(Q),
      );
    }
    if (status) items = items.filter((x) => x.status === status);
    if (stage) items = items.filter((x) => x.currentStage === stage);
    const total = items.length;
    const start = (page - 1) * pageSize;
    return { items: items.slice(start, start + pageSize), page, pageSize, total };
  }

  async upload(file: UploadFile, ctx: ProjectContext): Promise<{ runId: string }> {
    await delay(700);
    if (!ctx.tenant || !ctx.project) {
      throw new ApiError(
        400,
        "Tenant and Project are required. Please set them in the context bar.",
      );
    }
    const runId = "run_" + Math.random().toString(36).slice(2, 14);
    const documentName =
      file instanceof File
        ? file.name
        : ((file as { name?: string }).name ?? "earnings-2024-q4.pdf");
    this.currentRun = {
      runId,
      document_name: documentName,
      mode: "STANDARD",
      policy: "allow-pii",
      status: "ASSESSING",
      started_at: new Date().toISOString(),
      tenant: ctx.tenant,
      project: ctx.project,
      progress_pct: 0,
      warning_count: 0,
      current_stage: "COMPILE",
      current_step: null,
    };
    this.eventBuffer = [];
    this.gateResolved.confirm = false;
    return { runId };
  }

  async getRun(_runId: string): Promise<IngestionRun> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    return { ...this.currentRun };
  }

  async getPlan(_runId: string): Promise<ExecutionPlan> {
    await delay(150);
    return makePlan();
  }

  async confirm(_runId: string): Promise<{ ok: true }> {
    await delay(200);
    if (this.currentRun) this.currentRun.status = "RUNNING";
    this.gateResolved.confirm = true;
    return { ok: true };
  }

  async pauseRun(runId: string): Promise<RunControlResult> {
    await delay(120);
    if (this.currentRun) this.currentRun.status = "PAUSED";
    return {
      runId, action: "pause", status: "PAUSED",
      stage: this.currentRun?.current_stage ?? null,
      message: "Pause requested.",
      updatedAt: new Date().toISOString(),
    };
  }

  async resumeRun(runId: string): Promise<RunControlResult> {
    await delay(120);
    if (this.currentRun) this.currentRun.status = "RUNNING";
    return {
      runId, action: "resume", status: "RUNNING",
      stage: this.currentRun?.current_stage ?? null,
      message: "Resume requested.",
      updatedAt: new Date().toISOString(),
    };
  }

  async cancelRun(runId: string): Promise<RunControlResult> {
    await delay(120);
    if (this.currentRun) this.currentRun.status = "CANCELLING";
    return {
      runId, action: "cancel", status: "CANCELLING",
      stage: this.currentRun?.current_stage ?? null,
      message: "Cancel requested.",
      updatedAt: new Date().toISOString(),
    };
  }

  async getEvents(_runId: string): Promise<ProgressEvent[]> {
    await delay(100);
    return [...this.eventBuffer];
  }

  // ---- Result-review surface (Phase 7) ----------------------------
  // Returns shapes that match `j1.ingestion_review.dtos`. Mock data
  // exercises every Results tab so the FE can be developed without
  // a worker connected.

  async getRunSummary(runId: string): Promise<ReviewRunSummary> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    const warningCount = this.currentRun.warning_count ?? 0;
    const status = String(this.currentRun.status).toLowerCase();
    return {
      runId,
      status,
      durationMs: 12_000,
      documentIds: ["doc_demo"],
      steps: [
        {
          step: "compile", status: "completed", required: true,
          source: "caller", durationMs: 2400, artifactCount: 4,
          metadata: { engine: "mineru" },
        },
        {
          step: "enrich",
          status: this.scenario === "review" ? "completed" : "completed",
          required: false, source: "planner", durationMs: 6800,
          artifactCount: 8, metadata: {},
        },
        {
          step: "graph",
          status: this.scenario === "failure" ? "failed" : "skipped",
          required: false,
          source: this.scenario === "failure" ? "planner" : "policy",
          reason: this.scenario === "failure"
            ? "Constraint violated"
            : "TEXT_ONLY mode",
          metadata: {}, artifactCount: 0,
          error: this.scenario === "failure"
            ? { type: "ConstraintError", message: "uniqueness", retryable: false }
            : null,
        },
        {
          step: "index", status: "completed", required: true,
          source: "default", durationMs: 800, artifactCount: 1, metadata: {},
        },
      ],
      artifactCounts: { chunk: 12, "enriched.tables": 3, "enriched.visuals": 2 },
      totalBytes: 184_320,
      warnings: warningCount > 0
        ? [{
            code: "step.warning",
            message: "page 7 OCR confidence low",
            severity: "warning",
            step: "EXTRACT_TABLES",
            page: 7,
            chunkId: null,
            artifactId: null,
            documentId: "doc_demo",
          }]
        : [],
      qualitySummary: {
        overallConfidence: this.scenario === "failure" ? 0.42 : 0.78,
        warningCount,
        lowConfidenceCount: this.scenario === "failure" ? 6 : 1,
      },
      availableViews: {
        chunks: { available: true, reason: null },
        assets: { available: true, reason: null },
        // Match `getRunGraph`'s scenario logic — `warnings` produces
        // a populated graph in mock mode, every other scenario shows
        // an empty / failure state. Without this alignment the Graph
        // tab button stays disabled even when the mock would happily
        // render a graph.
        graph: this.scenario === "warnings"
          ? { available: true, reason: null }
          : this.scenario === "failure"
            ? { available: false, reason: "Graph generation failed." }
            : { available: false, reason: "Graph generation was skipped by policy." },
        quality: { available: true, reason: null },
        rawArtifacts: { available: true, reason: null },
        // Validation tab is on for the success scenarios (run is
        // terminal-success and chunks exist). The `failure` scenario
        // disables it so the FE renders the disabled-with-tooltip
        // path the same way the live API would.
        validation: this.scenario === "failure"
          ? { available: false, reason: "Run did not complete successfully." }
          : { available: true, reason: null },
      },
    };
  }

  async getRunQualityReport(
    _runId: string,
    opts?: { includeRaw?: boolean },
  ): Promise<ReviewQualityReport> {
    await delay(150);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    return {
      overallConfidence: this.scenario === "failure" ? 0.42 : 0.78,
      modalityConfidences: [
        { modality: "tables", confidence: 0.86, sampleCount: 12 },
        { modality: "ocr", confidence: 0.55, sampleCount: 8 },
      ],
      warnings: (this.currentRun.warning_count ?? 0) > 0
        ? [{
            code: "step.warning",
            message: "page 7 OCR confidence low",
            severity: "warning",
            step: "EXTRACT_TABLES",
            page: 7,
            chunkId: null,
            artifactId: null,
            documentId: "doc_demo",
          }]
        : [],
      skippedSteps: this.scenario === "failure"
        ? []
        : [{ step: "graph", reason: "TEXT_ONLY mode", policy: "policy" }],
      failedOptionalSteps: this.scenario === "failure"
        ? [{ step: "graph", reason: "Constraint violated", errorType: "ConstraintError" }]
        : [],
      lowConfidenceFindings: [
        { score: 0.55, category: "low_confidence", message: "page 7 OCR uncertain",
          page: 7, chunkId: null, artifactId: "art_ocr_p7" },
      ],
      rawDebug: opts?.includeRaw ? { confidence_assessment: [{ modality: "tables" }] } : null,
    };
  }

  async listRunChunks(
    _runId: string, opts?: ReviewChunkListQuery,
  ): Promise<ReviewChunkPage> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    let items = mockChunkList();
    if (opts?.minConfidence != null) {
      const floor = opts.minConfidence;
      items = items.filter(
        (c) => c.confidence != null && c.confidence >= floor,
      );
    }
    if (opts?.status) {
      const needle = opts.status.toLowerCase();
      items = items.filter((c) => {
        const value = c.metadata?.status;
        return typeof value === "string" && value.toLowerCase() === needle;
      });
    }
    const page = opts?.page ?? 1;
    const pageSize = opts?.pageSize ?? 25;
    const start = (page - 1) * pageSize;
    const slice = items.slice(start, start + pageSize);
    return { items: slice, page, pageSize, total: items.length };
  }

  async getRunChunk(
    _runId: string, chunkId: string,
  ): Promise<ReviewChunkDetail> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    const all = mockChunkList();
    const preview = all.find((c) => c.chunkId === chunkId);
    if (!preview) throw new ApiError(404, "Chunk not found.");
    return {
      chunkId: preview.chunkId,
      body: mockChunkBody(preview),
      pageStart: preview.pageStart ?? null,
      pageEnd: preview.pageEnd ?? null,
      section: preview.section ?? null,
      title: preview.title ?? null,
      tokenCount: preview.tokenCount ?? null,
      confidence: preview.confidence ?? null,
      metadata: preview.metadata,
      linkedAssets: preview.linkedAssets,
      sourceArtifactId: preview.sourceArtifactId ?? null,
      lineage: {
        documentIds: ["doc_demo"],
        sourceArtifactId: preview.sourceArtifactId ?? "art_compile_demo",
        stage: "compile",
      },
    };
  }

  async listRunArtifacts(
    _runId: string, opts?: ReviewArtifactListQuery,
  ): Promise<ReviewArtifactPage> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    let items = mockArtifactList();
    if (opts?.kind) {
      items = items.filter((a) => a.kind === opts.kind);
    }
    const page = opts?.page ?? 1;
    const pageSize = opts?.pageSize ?? 50;
    const start = (page - 1) * pageSize;
    return {
      items: items.slice(start, start + pageSize),
      page,
      pageSize,
      total: items.length,
    };
  }

  async getRunArtifactContent(
    _runId: string, artifactId: string,
  ): Promise<ReviewArtifactContent> {
    await delay(80);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    const record = mockArtifactList().find((a) => a.artifactId === artifactId);
    if (!record) throw new ApiError(404, "Artifact not found.");
    return mockArtifactContent(record);
  }

  async getRunGraph(
    _runId: string, opts?: ReviewGraphQuery,
  ): Promise<ReviewGraphSnapshot> {
    await delay(140);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    // The `failure` and default scenarios match what the summary
    // mock surfaces — graph either failed (no data) or was skipped
    // by policy. The `warnings` / `review` scenarios DO produce a
    // demo graph so the FE can render the populated state too.
    if (this.scenario === "failure") {
      return mockGraphUnavailable("Graph generation failed.", opts);
    }
    if (this.scenario === "warnings") {
      return mockGraphPopulated(opts);
    }
    return mockGraphUnavailable(
      "Graph generation was skipped by policy.", opts,
    );
  }

  async getRunContentInventory(runId: string): Promise<ContentInventory> {
    await delay(120);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    // Mock surface returns the empty-state shape — the parsed-content
    // manifest comes from real compile output, which the mock pipeline
    // doesn't simulate. The FE's Content Inventory tab handles
    // `status="unavailable"` gracefully.
    return {
      runId,
      documentId: null,
      documentName: null,
      status: "unavailable",
      source: {},
      summary: {
        textBlockCount: 0,
        tableCount: 0,
        imageCount: 0,
        formulaCount: 0,
        otherCount: 0,
        totalItems: 0,
      },
      items: [],
      rawArtifactId: null,
      unavailableReason:
        "Mock mode does not produce parsed-content manifests.",
    };
  }

  async getRunPlanning(runId: string): Promise<PlanningResult> {
    await delay(140);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    // Same empty-state pattern as getRunContentInventory — the mock
    // pipeline doesn't run the post-compile planning activity, so
    // there's no PlanningResult to return.
    return {
      runId,
      documentId: null,
      documentName: null,
      status: "unavailable",
      generatedAt: null,
      revised: false,
      source: null,
      planningPhase: null,
      assessment: null,
      decisions: [],
      digest: null,
      llmRecommendation: { status: "disabled" },
      unavailableReason:
        "Mock mode does not run the post-compile planning activity.",
    };
  }

  async runManualTestQuery(
    runId: string,
    request: ManualTestQueryRequest,
  ): Promise<ManualTestQueryResponse> {
    await delay(220);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    // Mock answer mirrors the question for transparency. The
    // server-derived chunk_id / run_id round-trip lets the FE
    // exercise its citation rendering even in mock mode.
    const isFailureScenario = this.scenario === "failure";
    const retrieved: ManualTestQueryResponse["retrievedChunks"] =
      isFailureScenario
        ? []
        : [
            {
              artifactId: "mock-art-1",
              chunkId: "mock-chunk-1",
              runId,
              documentId: "mock-doc",
              sourceLocation: "p.1",
              score: 0.82,
              preview: "Demo chunk for the validation tab.",
            },
          ];
    const citations: ManualTestQueryResponse["citations"] = retrieved.map(
      (r) => ({
        artifactId: r.artifactId,
        artifactType: "chunk",
        sourceDocumentId: r.documentId,
        sourceLocation: r.sourceLocation,
        chunkId: r.chunkId,
        runId: r.runId,
      }),
    );
    const checks: ManualTestQueryResponse["checks"] = [
      {
        name: "answer_non_empty",
        severity: "required",
        passed: !isFailureScenario,
      },
      {
        name: "retrieved_chunks_present",
        severity: "required",
        passed: !isFailureScenario,
      },
      {
        name: "retrieved_chunks_belong_to_run",
        severity: "required",
        passed: true,
      },
      {
        name: "citations_belong_to_run",
        severity: "required",
        passed: true,
      },
      {
        name: "no_cross_tenant_or_cross_project_leak",
        severity: "required",
        passed: true,
      },
    ];
    if (request.citationRequired) {
      checks.splice(2, 0, {
        name: "citation_present",
        severity: "required",
        passed: !isFailureScenario,
      });
    }
    return {
      requestId: `mock-tq-${Math.random().toString(36).slice(2, 10)}`,
      runId,
      question: request.question,
      answer: isFailureScenario
        ? ""
        : `Mock answer for: ${request.question}`,
      modeUsed: request.mode ?? "auto",
      retrievedChunks: retrieved,
      citations,
      checks,
      validationStatus: isFailureScenario ? "failed" : "passed",
      evidenceFlags: {
        graphUsed: false,
        tablesUsed: false,
        imagesUsed: false,
      },
      rawResponse: request.includeRaw
        ? { mock: true, scenario: this.scenario }
        : null,
    };
  }

  // ---- Validation sets + runs (Phase 2 mock) ----------------------

  // Stash for set/run state across calls in mock mode. Keyed by
  // runId so a single MockClient can simulate multiple runs.
  private _mockSets: Map<string, ValidationSet> = new Map();
  private _mockRuns: Map<string, ValidationRun> = new Map();

  async generateValidationSet(
    runId: string,
    request: GenerateValidationSetRequest = {},
  ): Promise<ValidationSet> {
    await delay(160);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    const isFailureScenario = this.scenario === "failure";

    // Idempotency mock: same runId reuses the existing set unless
    // `force` is set. Mirrors the backend's hash-based contract.
    const existing = [...this._mockSets.values()].find(
      (s) => s.runId === runId,
    );
    if (existing && !request.force) {
      return existing;
    }

    const setId = `vs-${Math.random().toString(36).slice(2, 10)}`;
    const cases = isFailureScenario
      ? []
      : [
          {
            testCaseId: "tc-smoke",
            question: "What is this document about?",
            type: "retrieval" as const,
            priority: "smoke" as const,
            expectedBehavior: "answer_with_citations" as const,
            expectedAnswerPoints: [],
            expectedChunks: [],
            expectedPages: [],
            expectedArtifacts: [],
            expectedGraphNodes: [],
            expectedGraphEdges: [],
            citationRequired: !!request.citationRequired,
            sourceTraceability: [],
            metadata: {},
          },
          {
            testCaseId: "tc-1",
            question: "When is the proposal due?",
            type: "retrieval" as const,
            priority: "normal" as const,
            expectedBehavior: "answer_with_citations" as const,
            expectedAnswerPoints: ["20 May 2026"],
            expectedChunks: ["mock-chunk-1"],
            expectedPages: [1],
            expectedArtifacts: [],
            expectedGraphNodes: [],
            expectedGraphEdges: [],
            citationRequired: !!request.citationRequired,
            sourceTraceability: ["mock-chunk-1"],
            metadata: {},
          },
        ];
    const vset: ValidationSet = {
      validationSetId: setId,
      runId,
      documentIds: ["mock-doc"],
      source: "generated",
      status: "draft",
      createdAt: new Date().toISOString(),
      createdBy: "mock-tester",
      generatorVersion: "v1",
      artifactsContentHash: `sha256:mock-${runId}`,
      testCases: cases,
      metadata: {},
    };
    this._mockSets.set(setId, vset);
    return vset;
  }

  async listValidationSets(runId: string): Promise<ValidationSetListItem[]> {
    await delay(60);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    return [...this._mockSets.values()]
      .filter((s) => s.runId === runId)
      .map((s) => ({
        validationSetId: s.validationSetId,
        runId: s.runId,
        source: s.source,
        status: s.status,
        createdAt: s.createdAt,
        createdBy: s.createdBy ?? null,
        caseCount: s.testCases.length,
      }));
  }

  async getValidationSet(
    runId: string, validationSetId: string,
  ): Promise<ValidationSet> {
    await delay(60);
    const vset = this._mockSets.get(validationSetId);
    if (!vset || vset.runId !== runId) {
      throw new ApiError(404, "Validation set not found.");
    }
    return vset;
  }

  async runValidation(
    runId: string,
    request: StartValidationRunRequest,
  ): Promise<ValidationRun> {
    await delay(280);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    const vset = this._mockSets.get(request.validationSetId);
    if (!vset || vset.runId !== runId) {
      throw new ApiError(404, "Validation set not found.");
    }
    const isFailureScenario = this.scenario === "failure";
    const passed = !isFailureScenario;
    const vrunId = `vrun-${Math.random().toString(36).slice(2, 10)}`;
    const startedAt = new Date().toISOString();
    const completedAt = new Date().toISOString();
    const results = vset.testCases.map((tc) => ({
      resultId: `vr-${Math.random().toString(36).slice(2, 8)}`,
      testCaseId: tc.testCaseId,
      status: (passed ? "passed" : "failed") as
        | "passed"
        | "warning"
        | "failed"
        | "skipped",
      question: tc.question,
      answer: passed
        ? `Mock answer for: ${tc.question}`
        : "",
      retrievedChunks: passed
        ? [
            {
              artifactId: "mock-art-1",
              chunkId: "mock-chunk-1",
              runId,
              documentId: "mock-doc",
              sourceLocation: "p.1",
              score: 0.85,
              preview: "Demo retrieved chunk text.",
            },
          ]
        : [],
      citations: passed
        ? [
            {
              artifactId: "mock-art-1",
              artifactType: "chunk",
              sourceDocumentId: "mock-doc",
              sourceLocation: "p.1",
              chunkId: "mock-chunk-1",
              runId,
            },
          ]
        : [],
      checks: [
        {
          name: "answer_non_empty",
          severity: "required" as const,
          passed,
        },
        {
          name: "retrieved_chunks_present",
          severity: "required" as const,
          passed,
        },
      ],
      judgeNotes: null,
      failureReason: passed ? null : "no chunks retrieved (mock)",
      testerVerdict: null,
      testerNotes: null,
    }));
    const total = results.length;
    const passedCount = results.filter((r) => r.status === "passed").length;
    const failedCount = results.filter((r) => r.status === "failed").length;
    const vrun: ValidationRun = {
      validationRunId: vrunId,
      validationSetId: vset.validationSetId,
      runId,
      executionStatus: "completed",
      validationStatus: passed ? "passed" : "failed",
      startedAt,
      completedAt,
      actor: "mock-tester",
      summary: {
        total,
        passed: passedCount,
        warning: 0,
        failed: failedCount,
        skipped: 0,
        coverage: {
          byType: { retrieval: total },
          byPriority: { smoke: 1, normal: Math.max(0, total - 1) },
          bySection: {},
        },
        mainIssues: passed ? [] : ["mock failure"],
        recommendedAction: passed ? "ready" : "block release until resolved",
      },
      results,
      failureMessage: null,
      metadata: {},
    };
    this._mockRuns.set(vrunId, vrun);
    return vrun;
  }

  async listValidationRuns(runId: string): Promise<ValidationRunListItem[]> {
    await delay(60);
    if (!this.currentRun) throw new ApiError(404, "Run not found.");
    return [...this._mockRuns.values()]
      .filter((v) => v.runId === runId)
      .map((v) => ({
        validationRunId: v.validationRunId,
        validationSetId: v.validationSetId,
        runId: v.runId,
        executionStatus: v.executionStatus,
        validationStatus: v.validationStatus,
        startedAt: v.startedAt,
        completedAt: v.completedAt ?? null,
        summary: v.summary,
      }));
  }

  async getValidationRun(
    runId: string, validationRunId: string,
  ): Promise<ValidationRun> {
    await delay(60);
    const vrun = this._mockRuns.get(validationRunId);
    if (!vrun || vrun.runId !== runId) {
      throw new ApiError(404, "Validation run not found.");
    }
    return vrun;
  }

  async recordTesterVerdict(
    runId: string,
    validationRunId: string,
    resultId: string,
    body: { verdict: "pass" | "warning" | "fail"; notes?: string | null },
  ): Promise<ValidationRun> {
    await delay(80);
    const vrun = this._mockRuns.get(validationRunId);
    if (!vrun || vrun.runId !== runId) {
      throw new ApiError(404, "Validation run not found.");
    }
    const updated: ValidationRun = {
      ...vrun,
      results: vrun.results.map((r) =>
        r.resultId === resultId
          ? { ...r, testerVerdict: body.verdict, testerNotes: body.notes ?? null }
          : r,
      ),
    };
    this._mockRuns.set(validationRunId, updated);
    return updated;
  }

  async downloadValidationReport(
    runId: string,
    validationRunId: string,
    format: "markdown" | "json" = "markdown",
  ): Promise<{ content: string; mediaType: string; filename: string }> {
    await delay(80);
    const vrun = this._mockRuns.get(validationRunId);
    if (!vrun || vrun.runId !== runId) {
      throw new ApiError(404, "Validation run not found.");
    }
    if (format === "json") {
      return {
        content: JSON.stringify(vrun, null, 2),
        mediaType: "application/json",
        filename: `validation-${validationRunId}.json`,
      };
    }
    // Trivial Markdown render in mock mode — production format
    // is the backend's. Enough for the FE download flow to work
    // without backend access.
    const content =
      `# Validation Report — ${vrun.validationRunId}\n\n`
      + `- Run: ${vrun.runId}\n`
      + `- Set: ${vrun.validationSetId}\n`
      + `- Execution status: ${vrun.executionStatus}\n`
      + `- Validation status: ${vrun.validationStatus}\n`
      + `\n## Results (${vrun.results.length})\n`;
    return {
      content,
      mediaType: "text/markdown",
      filename: `validation-${validationRunId}.md`,
    };
  }

  openStream(runId: string, handlers: StreamHandlers): StreamHandle {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const script = makeEventScript(runId);
    let cursor = 0;

    if (handlers.lastEventId) {
      const idx = script.findIndex((e) => e.eventId === handlers.lastEventId);
      if (idx >= 0) cursor = idx + 1;
    } else {
      cursor = this.eventBuffer.length;
    }

    handlers.onOpen?.();

    const tick = () => {
      if (cancelled) return;
      if (cursor >= script.length) {
        handlers.onClose?.();
        return;
      }
      const next = script[cursor]!;

      if (next.gate === "confirm" && !this.gateResolved.confirm) {
        timer = setTimeout(tick, 200);
        return;
      }

      const adjusted = this.applyScenario(next);
      cursor++;
      adjusted.ts = Date.now();
      this.eventBuffer.push(adjusted);
      this.updateRunFromEvent(adjusted);
      handlers.onEvent?.(adjusted);

      // Mirror the backend's SSE terminal set (single source of
      // truth: `TERMINAL_EVENT_TYPES`) so mock-mode and live-mode
      // behave identically — closing on the same event types means
      // the FE's reconnect/dedupe logic doesn't have to special-case
      // mock runs.
      if (isTerminalEvent(adjusted.event)) {
        handlers.onClose?.();
        return;
      }

      timer = setTimeout(tick, next.delay ?? 400);
    };

    timer = setTimeout(tick, 100);

    return {
      close: () => {
        cancelled = true;
        if (timer) clearTimeout(timer);
      },
    };
  }

  async getLLMHealth(): Promise<LLMHealthStatus> {
    // Mock mode: pretend the LLM is always reachable. Real wiring
    // is exercised by the live API client; here we just keep the
    // FE banner quiet so mock-mode demos aren't cluttered.
    return {
      healthy: true,
      checkedAt: new Date().toISOString(),
      results: [],
    };
  }

  async refreshLLMHealth(): Promise<LLMHealthStatus> {
    // Mirrors getLLMHealth in mock mode — banner's "Retry now"
    // button still works; just always reports healthy.
    return this.getLLMHealth();
  }

  // ---- Scenario adjustments + run-state derivation ---------------

  private applyScenario(entry: ScriptEntry): ProgressEvent {
    const e: ProgressEvent = {
      eventId: entry.eventId,
      event: entry.event,
      ts: entry.ts,
      data: { ...entry.data },
    };
    if (
      this.scenario === "failure" &&
      e.event === "step.completed" &&
      e.data.step === "graph_build"
    ) {
      return {
        eventId: e.eventId,
        event: "step.failed",
        ts: e.ts,
        data: {
          runId: e.data.runId,
          message:
            "Graph build failed: relation 'mentioned_in' violates uniqueness constraint.",
          stage: "GRAPH",
          step: "graph_build",
          severity: "ERROR",
          failure_code: "GRAPH_CONSTRAINT_VIOLATION",
          failure_message: "Constraint 'mentioned_in_unique' violated by 4 rows.",
        },
      };
    }
    if (this.scenario === "failure" && e.event === "run.completed") {
      return {
        eventId: e.eventId,
        event: "run.failed",
        ts: e.ts,
        data: {
          runId: e.data.runId,
          message: "Run failed at GRAPH stage.",
          severity: "ERROR",
          failure_code: "GRAPH_CONSTRAINT_VIOLATION",
          failure_message: "Constraint 'mentioned_in_unique' violated by 4 rows.",
          failed_step: "graph_build",
        },
      };
    }
    if (
      this.scenario === "review" &&
      e.event === "step.completed" &&
      e.data.step === "summarize"
    ) {
      return {
        eventId: e.eventId,
        event: "human_review.required",
        ts: e.ts,
        data: {
          runId: e.data.runId,
          message: "Human review required: summary contains low-confidence claims.",
          severity: "WARNING",
          stage: "ENRICH",
          step: "summarize",
          reason: "3 sections produced summaries below confidence threshold (0.62).",
        },
      };
    }
    return e;
  }

  private updateRunFromEvent(e: ProgressEvent): void {
    const r = this.currentRun;
    if (!r) return;
    const t = e.event;
    if (t === "plan.generated") r.status = "PLAN_READY";
    if (t === "plan.confirmed") r.status = "RUNNING";
    if (t === "step.started") {
      r.status = "RUNNING";
      r.current_stage = (e.data.stage as Stage | undefined) ?? r.current_stage ?? null;
      r.current_step = e.data.step ?? null;
    }
    if (t === "step.warning" || (t === "step.completed" && e.data.severity === "WARNING")) {
      r.warning_count = (r.warning_count || 0) + 1;
    }
    const stepNames = [
      "text_extract",
      "layout_prepare",
      "table_extract",
      "entity_link",
      "summarize",
      "graph_build",
      "vector_index",
    ];
    if (t === "step.progress" && typeof e.data.progress === "number" && e.data.step) {
      const idx = stepNames.indexOf(e.data.step);
      if (idx >= 0) {
        r.progress_pct = Math.min(
          99,
          Math.round(((idx + e.data.progress) / stepNames.length) * 100),
        );
      }
    }
    if (t === "step.completed" && e.data.step) {
      const idx = stepNames.indexOf(e.data.step);
      if (idx >= 0) {
        r.progress_pct = Math.min(99, Math.round(((idx + 1) / stepNames.length) * 100));
      }
    }
    if (t === "run.completed") {
      r.status = (e.data.warning_count ?? 0) > 0 ? "COMPLETED_WITH_WARNINGS" : "COMPLETED";
      r.progress_pct = 100;
      r.warning_count = e.data.warning_count ?? r.warning_count;
      r.final = e.data;
    }
    if (t === "run.failed") {
      r.status = "FAILED";
      r.final = e.data;
    }
    if (t === "human_review.required") {
      r.status = "AWAITING_HUMAN_REVIEW";
      r.final = e.data;
    }
  }
}
