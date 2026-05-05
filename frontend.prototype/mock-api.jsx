// Mock API + scripted run lifecycle.
// Mirrors the API contract from the brief without any backend.

const MOCK_TENANT = "demo-tenant";
const MOCK_PROJECT = "demo-project";

// ── Plan template (deterministic) ──────────────────────────────────
function makePlan() {
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
      // COMPILE
      {
        id: "text_extract", stage: "COMPILE", name: "Text extraction",
        decision: "RUN", reason: "Source document is a PDF; native text layer present.",
        risk_level: "LOW", estimated_cost_tier: "S",
        expected_engine: "pdfium", expected_provider: null,
      },
      {
        id: "ocr", stage: "COMPILE", name: "OCR (image-only fallback)",
        decision: "SKIP", reason: "Native text layer detected — OCR not required.",
        risk_level: "LOW", estimated_cost_tier: "M",
        expected_engine: "tesseract", expected_provider: null,
      },
      {
        id: "layout_prepare", stage: "COMPILE", name: "Layout preparation",
        decision: "RUN", reason: "Multi-column layout detected; segmentation required for downstream.",
        risk_level: "MEDIUM", estimated_cost_tier: "M",
        expected_engine: "layoutlmv3", expected_provider: "internal",
      },
      {
        id: "table_extract", stage: "COMPILE", name: "Table extraction",
        decision: "RUN", reason: "12 candidate tables detected via heuristics.",
        risk_level: "MEDIUM", estimated_cost_tier: "M",
        expected_engine: "camelot", expected_provider: null,
        warning: "Some tables span pages — extraction quality may vary.",
      },
      // ENRICH
      {
        id: "entity_link", stage: "ENRICH", name: "Entity linking",
        decision: "RUN", reason: "Domain knowledge graph available for tenant.",
        risk_level: "LOW", estimated_cost_tier: "S",
        expected_engine: "spacy-ner", expected_provider: "internal",
      },
      {
        id: "summarize", stage: "ENRICH", name: "Section summarization",
        decision: "RUN", reason: "Document length 84 pages exceeds summary threshold.",
        risk_level: "LOW", estimated_cost_tier: "L",
        expected_engine: "haiku-4.5", expected_provider: "anthropic",
      },
      {
        id: "translate", stage: "ENRICH", name: "Translation",
        decision: "SKIP", reason: "Document language matches project default (en).",
        risk_level: "LOW", estimated_cost_tier: "L",
        expected_engine: "deepl", expected_provider: "deepl",
      },
      {
        id: "pii_scan", stage: "ENRICH", name: "PII redaction",
        decision: "SKIP", reason: "Project policy 'allow-pii' set; redaction disabled.",
        risk_level: "HIGH", estimated_cost_tier: "S",
        expected_engine: "presidio", expected_provider: "internal",
      },
      // GRAPH
      {
        id: "graph_build", stage: "GRAPH", name: "Knowledge graph build",
        decision: "RUN", reason: "Entity links produced; graph projection enabled.",
        risk_level: "MEDIUM", estimated_cost_tier: "M",
        expected_engine: "neo4j-projection", expected_provider: "internal",
      },
      {
        id: "graph_dedupe", stage: "GRAPH", name: "Cross-document deduplication",
        decision: "CONDITIONAL", reason: "Will run if ≥3 nodes overlap with existing tenant graph.",
        risk_level: "LOW", estimated_cost_tier: "S",
        expected_engine: "graph-merge", expected_provider: "internal",
      },
      // INDEX
      {
        id: "vector_index", stage: "INDEX", name: "Vector index",
        decision: "RUN", reason: "Project search enabled; embedding model configured.",
        risk_level: "LOW", estimated_cost_tier: "M",
        expected_engine: "voyage-3", expected_provider: "voyage",
      },
    ],
  };
}

// ── Event timeline (scripted) ──────────────────────────────────────
function makeEventScript(runId) {
  // returns ordered events with relative delay (ms after previous).
  const t0 = Date.now();
  let i = 0;
  const id = () => `evt_${(++i).toString().padStart(4, "0")}`;
  const ev = (type, data, delay) => ({ eventId: id(), event: type, ts: 0, delay, data: { runId, ...data } });

  return [
    ev("run.created", { message: "Run created.", severity: "INFO" }, 0),
    ev("document.received", { message: "Received earnings-2024-q4.pdf · 84 pages · 12.4 MB", severity: "INFO" }, 400),
    ev("assessment.started", { message: "Assessing document characteristics…", severity: "INFO", stage: "COMPILE" }, 500),
    ev("assessment.completed", { message: "Assessment complete. 11 candidate steps identified.", severity: "INFO", stage: "COMPILE" }, 900),
    ev("plan.generated", { message: "Execution plan generated. Awaiting confirmation.", severity: "INFO", plan_summary: { run: 7, skip: 3, conditional: 1 } }, 600),
    // ↓ confirmation gate — script pauses here until user confirms
    ev("plan.confirmed", { message: "Plan confirmed by user.", severity: "INFO" }, 0, { gate: "confirm" }),

    // COMPILE
    ev("step.started", { message: "Starting text extraction.", stage: "COMPILE", step: "text_extract", engine: "pdfium" }, 500),
    ev("step.progress", { stage: "COMPILE", step: "text_extract", engine: "pdfium", progress: 0.5, current: 42, total: 84, message: "Extracting page 42 of 84…" }, 700),
    ev("step.completed", { message: "Text extracted from 84 pages.", stage: "COMPILE", step: "text_extract", engine: "pdfium", severity: "INFO" }, 900),

    ev("step.skipped", { message: "OCR skipped: native text layer detected.", stage: "COMPILE", step: "ocr", reason: "Native text layer detected — OCR not required.", severity: "INFO" }, 300),

    ev("step.started", { message: "Preparing layout…", stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", provider: "internal" }, 500),
    ev("step.progress", { stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", provider: "internal", progress: 0.25, current: 21, total: 84, message: "Segmenting columns…" }, 600),
    ev("step.progress", { stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", provider: "internal", progress: 0.5, current: 42, total: 84, message: "Resolving figure boundaries…" }, 700),
    ev("step.progress", { stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", provider: "internal", progress: 0.8, current: 67, total: 84, message: "Linking captions to figures…" }, 700),
    ev("step.progress", { stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", provider: "internal", progress: 1.0, current: 84, total: 84, message: "Layout complete." }, 600),
    ev("step.completed", { message: "Layout prepared.", stage: "COMPILE", step: "layout_prepare", engine: "layoutlmv3", severity: "INFO" }, 400),

    ev("step.started", { message: "Extracting tables…", stage: "COMPILE", step: "table_extract", engine: "camelot" }, 400),
    ev("step.warning", {
      message: "Cross-page table on pages 47–49 may have inconsistent column count.",
      stage: "COMPILE", step: "table_extract", severity: "WARNING",
      warning: "Table 7 spans pages 47–49 with 6 columns on p.47 and 5 on p.49. Manual review suggested.",
    }, 700),
    ev("step.completed", { message: "Extracted 11 of 12 tables.", stage: "COMPILE", step: "table_extract", severity: "WARNING" }, 600),

    // ENRICH
    ev("step.started", { message: "Linking entities…", stage: "ENRICH", step: "entity_link", engine: "spacy-ner", provider: "internal" }, 500),
    ev("step.progress", { stage: "ENRICH", step: "entity_link", engine: "spacy-ner", progress: 0.6, current: 312, total: 520, message: "312/520 mentions resolved" }, 700),
    ev("step.completed", { message: "Linked 487 entities.", stage: "ENRICH", step: "entity_link", severity: "INFO" }, 700),

    ev("step.started", { message: "Summarizing sections…", stage: "ENRICH", step: "summarize", engine: "haiku-4.5", provider: "anthropic" }, 500),
    ev("step.progress", { stage: "ENRICH", step: "summarize", engine: "haiku-4.5", provider: "anthropic", progress: 0.4, current: 8, total: 21, message: "Section 8 of 21" }, 800),
    ev("step.progress", { stage: "ENRICH", step: "summarize", engine: "haiku-4.5", provider: "anthropic", progress: 0.85, current: 18, total: 21, message: "Section 18 of 21" }, 900),
    ev("step.completed", { message: "Summaries written for 21 sections.", stage: "ENRICH", step: "summarize", severity: "INFO" }, 600),

    ev("step.skipped", { message: "Translation skipped: matches project default.", stage: "ENRICH", step: "translate", reason: "Document language matches project default (en).", severity: "INFO" }, 300),
    ev("step.skipped", { message: "PII redaction skipped: tenant policy 'allow-pii'.", stage: "ENRICH", step: "pii_scan", reason: "Project policy 'allow-pii' set; redaction disabled.", severity: "WARNING" }, 300),

    // GRAPH
    ev("step.started", { message: "Building knowledge graph…", stage: "GRAPH", step: "graph_build", engine: "neo4j-projection", provider: "internal" }, 600),
    ev("step.progress", { stage: "GRAPH", step: "graph_build", engine: "neo4j-projection", progress: 0.5, current: 244, total: 487, message: "244/487 nodes projected" }, 900),
    ev("step.completed", { message: "Graph built: 487 nodes, 1,204 edges.", stage: "GRAPH", step: "graph_build", severity: "INFO" }, 700),

    ev("step.skipped", { message: "Cross-document dedup skipped: only 1 overlapping node.", stage: "GRAPH", step: "graph_dedupe", reason: "Threshold not met (1 < 3 nodes).", severity: "INFO" }, 400),

    // INDEX
    ev("step.started", { message: "Embedding documents…", stage: "INDEX", step: "vector_index", engine: "voyage-3", provider: "voyage" }, 500),
    ev("step.progress", { stage: "INDEX", step: "vector_index", engine: "voyage-3", provider: "voyage", progress: 0.3, current: 124, total: 412, message: "124/412 chunks embedded" }, 800),
    ev("step.progress", { stage: "INDEX", step: "vector_index", engine: "voyage-3", provider: "voyage", progress: 0.7, current: 289, total: 412, message: "289/412 chunks embedded" }, 900),
    ev("step.completed", { message: "Index ready. 412 chunks indexed.", stage: "INDEX", step: "vector_index", severity: "INFO" }, 700),

    ev("run.completed", {
      message: "Run completed with 1 warning.",
      severity: "WARNING",
      warning_count: 1,
      warning_summary: "Table 7 column-count mismatch on pages 47–49.",
    }, 500),
  ];
}

// ── Mock client API ────────────────────────────────────────────────
class MockClient {
  constructor() {
    this._listeners = new Map();
    this._streamHandle = null;
    this._eventBuffer = [];
    this._gateResolved = { confirm: false };
    this._runIndex = 0;
    this._scenario = "warnings"; // 'warnings' | 'failure' | 'review'
  }

  setScenario(s) { this._scenario = s; }

  // GET /ingestion-runs  (list)
  async listRuns(ctx, { page = 1, pageSize = 20, q = "", status = "", stage = "" } = {}) {
    await delay(180);
    if (!ctx.tenant || !ctx.project) {
      throw apiError(400, "Tenant and Project are required. Please set them in the context bar.");
    }
    let items = mockListData(ctx);
    // Include the current live run if there is one
    if (this._currentRun) {
      const live = mapInternalToListItem(this._currentRun);
      items = [live, ...items.filter(x => x.runId !== live.runId)];
    }
    // Filter
    if (q) {
      const Q = q.toLowerCase();
      items = items.filter(x =>
        x.documentName.toLowerCase().includes(Q) ||
        x.runId.toLowerCase().includes(Q)
      );
    }
    if (status) items = items.filter(x => x.status === status);
    if (stage) items = items.filter(x => x.currentStage === stage);
    // Paginate
    const total = items.length;
    const start = (page - 1) * pageSize;
    const paged = items.slice(start, start + pageSize);
    return { items: paged, page, pageSize, total };
  }

  // POST /ingestion-runs
  async upload(file, ctx) {
    await delay(700);
    if (!ctx.tenant || !ctx.project) {
      throw apiError(400, "Tenant and Project are required. Please set them in the context bar.");
    }
    const runId = "run_" + Math.random().toString(36).slice(2, 14);
    this._currentRun = {
      runId,
      document_name: file?.name || "earnings-2024-q4.pdf",
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
    this._eventBuffer = [];
    this._gateResolved.confirm = false;
    return { runId };
  }

  // GET /ingestion-runs/{id}
  async getRun(runId) {
    await delay(120);
    if (!this._currentRun) throw apiError(404, "Run not found.");
    return { ...this._currentRun };
  }

  // GET /ingestion-runs/{id}/plan
  async getPlan(runId) {
    await delay(150);
    return makePlan();
  }

  // POST /ingestion-runs/{id}/confirm
  async confirm(runId) {
    await delay(200);
    if (this._currentRun) {
      this._currentRun.status = "RUNNING";
    }
    this._gateResolved.confirm = true;
    return { ok: true };
  }

  // GET /ingestion-runs/{id}/events  (history backfill)
  async getEvents(runId) {
    await delay(100);
    return [...this._eventBuffer];
  }

  // SSE stream — fetch-based simulation.
  // Returns a handle: { close() }
  openStream(runId, { onEvent, onOpen, onError, onClose, lastEventId }) {
    let cancelled = false;
    let timer = null;

    const script = makeEventScript(runId);
    let cursor = 0;

    // Skip events already buffered (Last-Event-Id support).
    if (lastEventId) {
      const idx = script.findIndex(e => e.eventId === lastEventId);
      if (idx >= 0) cursor = idx + 1;
    } else {
      cursor = this._eventBuffer.length;
    }

    onOpen?.();

    const tick = () => {
      if (cancelled) return;
      if (cursor >= script.length) { onClose?.(); return; }
      const next = script[cursor];

      // Confirmation gate: pause until confirmed
      if (next.gate === "confirm" && !this._gateResolved.confirm) {
        // poll
        timer = setTimeout(tick, 200);
        return;
      }

      // Apply optional scenario overrides for the failure / review demo.
      const adjusted = this._applyScenario(next, cursor, script);

      cursor++;
      adjusted.ts = Date.now();
      this._eventBuffer.push(adjusted);
      this._updateRunFromEvent(adjusted);
      onEvent?.(adjusted);

      // Stop after terminal events.
      if (["run.completed", "run.failed"].includes(adjusted.event)) {
        onClose?.();
        return;
      }

      timer = setTimeout(tick, adjusted.delay ?? 400);
    };

    timer = setTimeout(tick, 100);

    return {
      close: () => { cancelled = true; if (timer) clearTimeout(timer); },
    };
  }

  _applyScenario(ev, cursor, script) {
    const e = { ...ev, data: { ...ev.data } };
    if (this._scenario === "failure" && e.event === "step.completed" && e.data.step === "graph_build") {
      // Replace with a failure
      return {
        eventId: e.eventId,
        event: "step.failed",
        ts: e.ts,
        data: {
          runId: e.data.runId,
          message: "Graph build failed: relation 'mentioned_in' violates uniqueness constraint.",
          stage: "GRAPH",
          step: "graph_build",
          severity: "ERROR",
          failure_code: "GRAPH_CONSTRAINT_VIOLATION",
          failure_message: "Constraint 'mentioned_in_unique' violated by 4 rows.",
        },
      };
    }
    if (this._scenario === "failure" && e.event === "run.completed") {
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
    if (this._scenario === "review" && e.event === "step.completed" && e.data.step === "summarize") {
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

  _updateRunFromEvent(e) {
    const r = this._currentRun;
    if (!r) return;
    const t = e.event;
    if (t === "plan.generated") r.status = "PLAN_READY";
    if (t === "plan.confirmed") r.status = "RUNNING";
    if (t === "step.started") {
      r.status = "RUNNING";
      r.current_stage = e.data.stage || r.current_stage;
      r.current_step = e.data.step;
    }
    if (t === "step.warning" || (t === "step.completed" && e.data.severity === "WARNING")) {
      r.warning_count = (r.warning_count || 0) + 1;
    }
    if (t === "step.progress" && typeof e.data.progress === "number") {
      // Coarse run-level progress: rough average across 7 RUN steps in plan.
      const planRun = 7;
      const stepNames = ["text_extract", "layout_prepare", "table_extract", "entity_link", "summarize", "graph_build", "vector_index"];
      const stepIdx = stepNames.indexOf(e.data.step);
      if (stepIdx >= 0) {
        r.progress_pct = Math.min(99, Math.round(((stepIdx + e.data.progress) / planRun) * 100));
      }
    }
    if (t === "step.completed") {
      const stepNames = ["text_extract", "layout_prepare", "table_extract", "entity_link", "summarize", "graph_build", "vector_index"];
      const stepIdx = stepNames.indexOf(e.data.step);
      if (stepIdx >= 0) {
        r.progress_pct = Math.min(99, Math.round(((stepIdx + 1) / stepNames.length) * 100));
      }
    }
    if (t === "run.completed") {
      r.status = e.data.warning_count > 0 ? "COMPLETED_WITH_WARNINGS" : "COMPLETED";
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

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }
function apiError(status, message) {
  const e = new Error(message);
  e.status = status;
  return e;
}

// ── Mock list data ─────────────────────────────────────────────────
function mapInternalToListItem(r) {
  let mappedStatus = r.status;
  // Map internal-only statuses to the list contract
  if (mappedStatus === "COMPLETED") mappedStatus = "SUCCEEDED";
  if (mappedStatus === "COMPLETED_WITH_WARNINGS") mappedStatus = "SUCCEEDED_WITH_WARNINGS";
  if (mappedStatus === "AWAITING_HUMAN_REVIEW") mappedStatus = "REQUIRES_HUMAN_REVIEW";
  return {
    runId: r.runId,
    documentName: r.document_name,
    status: mappedStatus,
    mode: r.mode,
    policy: r.policy,
    currentStage: r.current_stage,
    currentStep: r.current_step,
    progressPercent: r.progress_pct || 0,
    warningCount: r.warning_count || 0,
    startedAt: r.started_at,
    updatedAt: r.started_at,
    completedAt: ["COMPLETED","COMPLETED_WITH_WARNINGS","FAILED","SUCCEEDED","SUCCEEDED_WITH_WARNINGS"].includes(r.status) ? new Date().toISOString() : null,
    failureCode: r.final?.failure_code || null,
    failureMessage: r.final?.failure_message || null,
  };
}

function mockListData(ctx) {
  const now = Date.now();
  const ago = (mins) => new Date(now - mins * 60_000).toISOString();
  return [
    {
      runId: "run_8K2pQ7vXmN3wJ1",
      documentName: "fy24-annual-report.pdf",
      status: "SUCCEEDED_WITH_WARNINGS",
      mode: "STANDARD", policy: "allow-pii",
      currentStage: "INDEX", currentStep: "vector_index",
      progressPercent: 100, warningCount: 2,
      startedAt: ago(124), updatedAt: ago(98), completedAt: ago(98),
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_3mLk9bRtY8sQ2X",
      documentName: "vendor-contract-acme.docx",
      status: "RUNNING",
      mode: "STANDARD", policy: "redact-pii",
      currentStage: "ENRICH", currentStep: "summarize",
      progressPercent: 64, warningCount: 0,
      startedAt: ago(8), updatedAt: ago(0), completedAt: null,
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_aZ7nDpV2cM4yL5",
      documentName: "research-paper-quantum.pdf",
      status: "PLAN_READY",
      mode: "FAST", policy: "allow-pii",
      currentStage: "COMPILE", currentStep: null,
      progressPercent: 0, warningCount: 0,
      startedAt: ago(2), updatedAt: ago(1), completedAt: null,
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_qW3eR4tY5uI6oP",
      documentName: "compliance-audit-q3.pdf",
      status: "REQUIRES_HUMAN_REVIEW",
      mode: "THOROUGH", policy: "redact-pii",
      currentStage: "ENRICH", currentStep: "pii_scan",
      progressPercent: 78, warningCount: 3,
      startedAt: ago(45), updatedAt: ago(12), completedAt: null,
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_xC8vB7nM6kJ5hG",
      documentName: "legacy-handbook-v2.docx",
      status: "FAILED",
      mode: "STANDARD", policy: "allow-pii",
      currentStage: "GRAPH", currentStep: "graph_build",
      progressPercent: 56, warningCount: 1,
      startedAt: ago(204), updatedAt: ago(180), completedAt: ago(180),
      failureCode: "GRAPH_CONSTRAINT_VIOLATION",
      failureMessage: "Constraint 'mentioned_in_unique' violated by 4 rows.",
    },
    {
      runId: "run_lP9kJ8hG7fD6sA",
      documentName: "product-spec-v3.md",
      status: "SUCCEEDED",
      mode: "FAST", policy: "allow-pii",
      currentStage: "INDEX", currentStep: "vector_index",
      progressPercent: 100, warningCount: 0,
      startedAt: ago(360), updatedAt: ago(320), completedAt: ago(320),
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_zX2cV3bN4mK5jH",
      documentName: "investor-deck-q4.pdf",
      status: "WAITING_FOR_CONFIRMATION",
      mode: "STANDARD", policy: "allow-pii",
      currentStage: "COMPILE", currentStep: null,
      progressPercent: 0, warningCount: 0,
      startedAt: ago(5), updatedAt: ago(4), completedAt: null,
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_yT6rE5wQ4aS3dF",
      documentName: "internal-wiki-export.html",
      status: "ASSESSING",
      mode: "STANDARD", policy: "allow-pii",
      currentStage: "COMPILE", currentStep: null,
      progressPercent: 0, warningCount: 0,
      startedAt: ago(1), updatedAt: ago(0), completedAt: null,
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_gH7jK8lP9oI0uY",
      documentName: "press-release-launch.txt",
      status: "CANCELLED",
      mode: "FAST", policy: "allow-pii",
      currentStage: "COMPILE", currentStep: "text_extract",
      progressPercent: 12, warningCount: 0,
      startedAt: ago(1440), updatedAt: ago(1420), completedAt: ago(1420),
      failureCode: null, failureMessage: null,
    },
    {
      runId: "run_uI9oP0aS1dF2gH",
      documentName: "customer-support-faqs.md",
      status: "SUCCEEDED",
      mode: "STANDARD", policy: "allow-pii",
      currentStage: "INDEX", currentStep: "vector_index",
      progressPercent: 100, warningCount: 0,
      startedAt: ago(2880), updatedAt: ago(2810), completedAt: ago(2810),
      failureCode: null, failureMessage: null,
    },
  ];
}

// ── Display mappings (centralized) ─────────────────────────────────
const StatusDisplay = {
  CREATED:                  { label: "Created",          tone: "neutral", pulse: false },
  ASSESSING:                { label: "Assessing",        tone: "info",    pulse: true  },
  PLAN_READY:               { label: "Plan ready",       tone: "accent",  pulse: false },
  WAITING_FOR_CONFIRMATION: { label: "Awaiting confirm", tone: "accent",  pulse: true  },
  RUNNING:                  { label: "Running",          tone: "info",    pulse: true  },
  COMPLETED:                { label: "Completed",        tone: "success", pulse: false },
  COMPLETED_WITH_WARNINGS:  { label: "Completed · warnings", tone: "warning", pulse: false },
  SUCCEEDED:                { label: "Succeeded",        tone: "success", pulse: false },
  SUCCEEDED_WITH_WARNINGS:  { label: "Succeeded · warnings", tone: "warning", pulse: false },
  FAILED:                   { label: "Failed",           tone: "error",   pulse: false },
  AWAITING_HUMAN_REVIEW:    { label: "Human review",     tone: "warning", pulse: true  },
  REQUIRES_HUMAN_REVIEW:    { label: "Human review",     tone: "warning", pulse: true  },
  CANCELLED:                { label: "Cancelled",        tone: "neutral", pulse: false },
};

const DecisionDisplay = {
  RUN:         { label: "Run",         className: "decision--run" },
  SKIP:        { label: "Skip",        className: "decision--skip" },
  CONDITIONAL: { label: "Conditional", className: "decision--conditional" },
};

const SeverityDisplay = {
  INFO:    "info",
  WARNING: "warning",
  ERROR:   "error",
};

const StageDisplay = {
  COMPILE: "Compile",
  ENRICH:  "Enrich",
  GRAPH:   "Graph",
  INDEX:   "Index",
};

const EventTypeDisplay = {
  "run.created":           "Run created",
  "document.received":     "Document received",
  "assessment.started":    "Assessment started",
  "assessment.completed":  "Assessment completed",
  "plan.generated":        "Plan generated",
  "plan.confirmed":        "Plan confirmed",
  "step.started":          "Step started",
  "step.progress":         "Progress",
  "step.skipped":          "Step skipped",
  "step.warning":          "Step warning",
  "step.completed":        "Step completed",
  "step.failed":           "Step failed",
  "run.completed":         "Run completed",
  "run.failed":            "Run failed",
  "human_review.required": "Human review required",
};

window.MockClient = MockClient;
window.client = window.client || new MockClient();
window.StatusDisplay = StatusDisplay;
window.DecisionDisplay = DecisionDisplay;
window.SeverityDisplay = SeverityDisplay;
window.StageDisplay = StageDisplay;
window.EventTypeDisplay = EventTypeDisplay;
window.MOCK_TENANT = MOCK_TENANT;
window.MOCK_PROJECT = MOCK_PROJECT;
