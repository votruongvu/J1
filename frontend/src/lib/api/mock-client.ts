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

import type {
  ExecutionPlan,
  IngestionRun,
  ProgressEvent,
  ProgressEventType,
  RunListItem,
  RunListQuery,
  RunListResult,
  RunStatus,
  Stage,
} from "@/types/ingestion";
import type { MockScenario, ProjectContext } from "@/types/ui";
import {
  ApiError,
  type IngestionClient,
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

  async getEvents(_runId: string): Promise<ProgressEvent[]> {
    await delay(100);
    return [...this.eventBuffer];
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

      if (adjusted.event === "run.completed" || adjusted.event === "run.failed") {
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
