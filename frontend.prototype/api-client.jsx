// Live API client.
//
// Mirrors the MockClient interface so the UI swaps between mock and
// live without component changes. Connects to the J1 REST surface:
//
//   POST /ingestion-runs                    — upload + create run
//   GET  /ingestion-runs/{run_id}            — status snapshot
//   GET  /ingestion-runs/{run_id}/plan       — execution plan
//   POST /ingestion-runs/{run_id}/confirm    — confirm plan
//   GET  /ingestion-runs/{run_id}/events     — historical events
//   GET  /ingestion-runs/{run_id}/events/stream  — live SSE
//
// Tenant/Project headers are mandatory per the J1 contract.
// Native EventSource cannot send custom headers, so the SSE stream
// is implemented with fetch + ReadableStream — same pattern the
// design README recommends.

class ApiClient {
  constructor(opts) {
    // opts: { baseUrl, getCtx(): {tenant,project}, getAuth(): {kind,value} }
    this._opts = opts;
  }

  // ---- Header injection (single source of truth) ----------------

  _headers(extra) {
    const ctx = this._opts.getCtx();
    const auth = this._opts.getAuth();
    const h = {
      "X-Tenant-Id": ctx.tenant || "",
      "X-Project-Id": ctx.project || "",
      ...(extra || {}),
    };
    if (auth.value) {
      if (auth.kind === "bearer") h["Authorization"] = `Bearer ${auth.value}`;
      else h["X-API-Key"] = auth.value;
    }
    return h;
  }

  _url(path) {
    const base = (this._opts.baseUrl || "").replace(/\/+$/, "");
    return `${base}${path}`;
  }

  async _json(resp) {
    const text = await resp.text();
    let parsed;
    try { parsed = text ? JSON.parse(text) : {}; }
    catch { throw apiError(resp.status, text || `HTTP ${resp.status}`); }
    if (!resp.ok) {
      // J1 uses an envelope shape `{ error: { code, message } }`.
      const msg = parsed?.error?.message || parsed?.message || `HTTP ${resp.status}`;
      throw apiError(resp.status, msg);
    }
    // J1 success envelope: `{ requestId, data, meta }`.
    return parsed?.data ?? parsed;
  }

  // ---- listRuns ---------------------------------------------------
  // J1 backend does not yet ship a GET /ingestion-runs list endpoint.
  // Return an empty page with a friendly hint in `meta.live_unsupported`
  // so the All Runs view can render an explanatory empty state.
  async listRuns(_ctx, _opts) {
    return {
      items: [],
      page: 1,
      pageSize: 0,
      total: 0,
      _liveUnsupported:
        "List view is not available in live mode (the J1 API doesn't ship "
        + "a GET /ingestion-runs list endpoint yet). Use 'New ingestion run' "
        + "to create a run; the run-detail page will work end-to-end.",
    };
  }

  // ---- upload (POST /ingestion-runs) ------------------------------
  async upload(file, ctx) {
    if (!ctx?.tenant || !ctx?.project) {
      throw apiError(400, "Tenant and Project are required.");
    }
    const fd = new FormData();
    // Real File object from the dropzone, or a name-only stub from the
    // demo button.
    if (file && file instanceof File) {
      fd.append("file", file);
    } else {
      const blob = new Blob(["demo content"], { type: "text/plain" });
      fd.append("file", blob, file?.name || "demo.txt");
    }
    // The backend requires a compilerKind (or a configured default).
    // The dev backend uses "mock" out of the box; deployments configured
    // for raganything override via the auth dialog or env.
    fd.append("compilerKind", "mock");
    const resp = await fetch(this._url("/ingestion-runs"), {
      method: "POST",
      headers: this._headers(),  // Don't set Content-Type — browser fills boundary.
      body: fd,
    });
    const data = await this._json(resp);
    return { runId: data.runId };
  }

  // ---- getRun (GET /ingestion-runs/{run_id}) ----------------------
  async getRun(runId) {
    const resp = await fetch(this._url(`/ingestion-runs/${encodeURIComponent(runId)}`), {
      headers: this._headers(),
    });
    const data = await this._json(resp);
    return _runFromApi(data);
  }

  // ---- getPlan (GET /ingestion-runs/{run_id}/plan) ----------------
  async getPlan(runId) {
    const resp = await fetch(this._url(`/ingestion-runs/${encodeURIComponent(runId)}/plan`), {
      headers: this._headers(),
    });
    const data = await this._json(resp);
    return _planFromApi(data);
  }

  // ---- confirm (POST /ingestion-runs/{run_id}/confirm) ------------
  async confirm(runId) {
    const resp = await fetch(this._url(`/ingestion-runs/${encodeURIComponent(runId)}/confirm`), {
      method: "POST",
      headers: this._headers(),
    });
    await this._json(resp);
    return { ok: true };
  }

  // ---- getEvents (GET /ingestion-runs/{run_id}/events) ------------
  async getEvents(runId) {
    const resp = await fetch(this._url(`/ingestion-runs/${encodeURIComponent(runId)}/events`), {
      headers: this._headers(),
    });
    const data = await this._json(resp);
    return (data.events || []).map(_eventFromApi);
  }

  // ---- openStream (GET .../events/stream — fetch-based SSE) -------
  // Native EventSource cannot send custom headers. fetch + ReadableStream
  // gives us full control over headers and reconnect behaviour.
  openStream(runId, handlers) {
    const controller = new AbortController();
    const url = this._url(`/ingestion-runs/${encodeURIComponent(runId)}/events/stream`);
    const headers = this._headers({ Accept: "text/event-stream" });
    if (handlers.lastEventId) {
      headers["Last-Event-Id"] = handlers.lastEventId;
    }

    let cancelled = false;

    (async () => {
      let resp;
      try {
        resp = await fetch(url, { headers, signal: controller.signal });
      } catch (err) {
        if (cancelled) return;
        handlers.onError?.(err);
        return;
      }
      if (!resp.ok) {
        handlers.onError?.(new Error(`Stream HTTP ${resp.status}`));
        return;
      }
      handlers.onOpen?.();

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      try {
        while (!cancelled) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            const ev = _parseSseFrame(frame);
            if (ev) handlers.onEvent?.(_eventFromApi(ev));
          }
        }
      } catch (err) {
        if (!cancelled) handlers.onError?.(err);
      } finally {
        handlers.onClose?.();
      }
    })();

    return {
      close: () => {
        cancelled = true;
        try { controller.abort(); } catch {}
      },
    };
  }
}


// ---- Field translation: J1 API → design-prototype shapes -------
//
// The design's components use a partly-snake_case shape (`run.document_name`,
// `run.progress_pct`, `run.started_at`). The J1 API returns camelCase
// (`documentId`, `progressPercent`, `startedAt`). Translate at the
// client boundary so component code stays unchanged.

function _runFromApi(api) {
  return {
    runId: api.runId,
    // The J1 API exposes documentId; prefer the metadata-level
    // documentName when available, otherwise fall back to the doc id.
    document_name: api.metadata?.documentName || api.documentId || api.runId,
    mode: api.metadata?.mode || api.metadata?.policy || "STANDARD",
    policy: api.metadata?.policy || "auto",
    status: _runStatusFromApi(api.status),
    started_at: api.startedAt,
    completed_at: api.completedAt,
    progress_pct: api.progressPercent || 0,
    warning_count: api.warningCount || 0,
    current_stage: api.currentStage,
    current_step: api.currentStep,
    final: api.failureCode ? {
      failure_code: api.failureCode,
      failure_message: api.failureMessage,
    } : null,
  };
}

// Map J1 RunStatus → the prototype's status enum (which uses
// COMPLETED/COMPLETED_WITH_WARNINGS/AWAITING_HUMAN_REVIEW). The design's
// StatusDisplay carries entries for both shapes already.
function _runStatusFromApi(s) {
  if (!s) return "ASSESSING";
  // J1 RunStatus uses lowercase strings; uppercase to match prototype.
  const upper = String(s).toUpperCase();
  if (upper === "SUCCEEDED") return "COMPLETED";
  if (upper === "SUCCEEDED_WITH_WARNINGS") return "COMPLETED_WITH_WARNINGS";
  if (upper === "REQUIRES_HUMAN_REVIEW") return "AWAITING_HUMAN_REVIEW";
  return upper;
}

function _planFromApi(api) {
  // Backend ExecutionPlanRecord has steps with `decision`, `reason`,
  // `stage`, `expected_engine`, etc. Build the summary the prototype
  // expects on the fly.
  const steps = (api.steps || []).map((s) => ({
    id: s.stepId || s.name,
    stage: s.stage,
    name: s.name,
    decision: s.decision,
    reason: s.reason,
    risk_level: (s.riskLevel || "low").toUpperCase(),
    estimated_cost_tier: s.estimatedCostTier || "NONE",
    expected_engine: s.expectedEngine,
    expected_provider: s.expectedProvider,
    warning: s.warning,
  }));
  const stages = Array.from(new Set(steps.map((s) => s.stage)));
  const counts = { run: 0, skip: 0, conditional: 0 };
  for (const s of steps) {
    if (s.decision === "RUN") counts.run += 1;
    else if (s.decision === "SKIP") counts.skip += 1;
    else if (s.decision === "CONDITIONAL") counts.conditional += 1;
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
  };
}

function _eventFromApi(api) {
  // Backend ProgressEventRecord uses camelCase; design timeline uses
  // `event` + `data`. Pack into the prototype's shape.
  return {
    eventId: api.eventId,
    event: api.eventType || api.event,
    ts: api.timestamp ? Date.parse(api.timestamp) : Date.now(),
    data: {
      runId: api.runId,
      message: api.message,
      severity: api.severity || "INFO",
      stage: api.stage,
      step: api.step,
      progress: api.progressPercent != null ? api.progressPercent / 100 : undefined,
      current: api.current,
      total: api.total,
      engine: api.engine,
      provider: api.provider,
      // Failure details flow through metadata on terminal events.
      failure_code: api.metadata?.failureCode,
      failure_message: api.metadata?.failureMessage,
      reason: api.metadata?.reason,
      warning: api.metadata?.warning,
    },
  };
}


// ---- SSE frame parser ------------------------------------------

function _parseSseFrame(frame) {
  // Frame is a single multi-line SSE message:
  //   id: <event_id>
  //   event: <type>
  //   data: <json>
  // Comments (lines starting with `:`) and unknown fields are skipped.
  let id, type, dataLines = [];
  for (const raw of frame.split("\n")) {
    if (!raw || raw.startsWith(":")) continue;
    const colon = raw.indexOf(":");
    if (colon < 0) continue;
    const field = raw.slice(0, colon);
    let value = raw.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") type = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  let payload;
  try { payload = JSON.parse(dataLines.join("\n")); }
  catch { return null; }
  // Surface as an envelope the api-translation layer recognises.
  return { ...payload, eventType: type || payload.eventType, eventId: id || payload.eventId };
}


function apiError(status, message) {
  const err = new Error(message);
  err.status = status;
  return err;
}


window.ApiClient = ApiClient;
