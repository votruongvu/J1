# Ingestion Run + Progress Surface

A user-facing progress layer that sits on top of the existing
Temporal-backed ingestion workflow. Temporal remains the orchestration
engine and stays useful for technical debugging; this layer is what
the frontend consumes to show a live execution plan, per-step
progress, warnings, and final outcomes.

> **Companion docs:**
> [`docs/architecture.md`](architecture.md) for the workflow surface;
> [`docs/operations/temporal.md`](operations/temporal.md) for the
> Temporal-side view; [`docs/configuration/environment.md`](configuration/environment.md)
> for the env vars that control planner / FAST role / search
> attributes.

---

## 1. Why a separate progress layer

Temporal UI is the right tool for SREs investigating a stuck workflow,
but it isn't a frontend. It surfaces workflow IDs, activity attempts,
retry counts, and JSON history — none of which a product user needs.
The progress layer translates the same lifecycle into:

- An **`IngestionRun`** record per document upload (one per Temporal
  workflow run, with a friendly ID and high-level status).
- An **`IngestPlan`** that documents what the system decided to do
  (run, skip, conditional) and *why*, before execution starts.
- A **`ProgressEvent`** timeline (`run.created`, `step.started`,
  `step.progress`, `step.skipped`, `step.warning`, `step.completed`,
  `step.failed`, `run.completed`, `run.failed`, etc.).
- An **SSE stream** so the UI can render progress live without
  polling.

All of this layers on top of the existing `AuditRecorder` —
correlation_id ties run, audit events, and progress events to one
ingestion attempt.

Raw logs do **not** flow into Temporal history. Temporal history is
for deterministic state transitions; logs go through structured
progress events (or normal application logs). This keeps Temporal
payloads small and replay-safe.

---

## 2. Data flow: upload → plan → progress

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend                                                        │
│                                                                  │
│  POST /documents       GET /ingestion-runs/{id}/plan             │
│       │                  │                                       │
│       │                  │   GET /ingestion-runs/{id}/events     │
│       │                  │     │                                 │
│       │                  │     │   GET .../events/stream (SSE)   │
│       ▼                  ▼     ▼      ▼                          │
│  ╔══════════════════════════════════════════════╗                │
│  ║  REST adapter (j1.adapters.rest)             ║                │
│  ╚══════════════════════════════════════════════╝                │
│       │                  ▲     ▲      ▲                          │
└───────┼──────────────────┼─────┼──────┼──────────────────────────┘
        ▼                  │     │      │
  ┌───────────────┐        │     │      │ tail
  │ Job starter   │        │     │      ▼
  │ (Temporal)    │        │     │ ┌─────────────────────────────┐
  └───────┬───────┘        │     │ │  Audit JSONL                │
          │                │     │ │  (workspace/audit/events)   │
          ▼                │     │ │                             │
  ┌─────────────────┐      │     └─┤  • workflow / activity      │
  │ ProjectProcessing│     │       │    audit events             │
  │ Workflow         │     │       │  • j1.progress.* events     │
  └────────┬────────┘      │       └────▲────────────────────────┘
           │               │            │
           ▼               │            │
  ┌─────────────────┐      │            │
  │ Activities       │     │            │ writes
  │ • compile        ├─────┼────────────┘
  │ • enrich         │     │
  │ • build_graph    │     │ snapshots
  │ • index          │     ▼
  │ (heartbeat at    │  ┌─────────────────────────────┐
  │  major bounds)   │  │  IngestionRun JSONL         │
  └────────┬─────────┘  │  (workspace/audit/          │
           │            │   ingestion_runs.jsonl)     │
           ▼            └─────────────────────────────┘
  ┌─────────────────┐
  │ ProgressReporter│ — abstraction the workflow + activities call;
  │                 │   composite of audit + temporal-heartbeat by
  │                 │   default
  └─────────────────┘
```

Key properties:

- **Single audit log, two views.** The same JSONL file backs both
  `GET /ingestion-jobs/{id}/events` (raw audit) and
  `GET /ingestion-runs/{id}/events` (progress projection). The runs
  view filters to actions starting with `j1.progress.*` and reshapes
  them into the frontend schema.
- **Run records are append-only.** Each state transition appends a
  fresh snapshot of the `IngestionRun` to `ingestion_runs.jsonl`;
  readers reconstruct latest-state-per-run-id by replaying the file.
  The append log doubles as a state-transition audit trail.
- **No new database.** All persistence sits in workspace-local JSONL,
  reusing the same retention / backup semantics as the audit log.

---

## 3. Lifecycle: status, plan, events

### 3.1 Run statuses

```
CREATED  →  ASSESSING  →  PLAN_READY
                              │
                              ├──▶  WAITING_FOR_CONFIRMATION
                              │             │
                              │             ▼ (POST .../confirm)
                              └──▶  RUNNING
                                       │
   ┌───────────────────────────────────┤
   ▼                                   ▼
   SUCCEEDED                           FAILED
   SUCCEEDED_WITH_WARNINGS             CANCELLED
   REQUIRES_HUMAN_REVIEW               (terminal)
   (terminal)
```

- `CREATED` — run record exists; assessment hasn't started yet.
- `ASSESSING` — `DocumentProfiler` running.
- `PLAN_READY` — plan generated, auto-run path will move to
  `RUNNING` immediately. Confirmation-required deployments park
  here.
- `WAITING_FOR_CONFIRMATION` — entered when the request asked for
  manual confirmation; the workflow waits for `POST .../confirm`.
- `RUNNING` — workflow executing the planned steps.
- Terminals:
  - `SUCCEEDED` — every required step succeeded, no warnings.
  - `SUCCEEDED_WITH_WARNINGS` — required steps OK, ≥1 optional step
    warned or failed under `continue_optional` policy.
  - `FAILED` — required step failed under `fail_fast` policy.
  - `CANCELLED` — workflow received a cancel signal.
  - `REQUIRES_HUMAN_REVIEW` — workflow paused at a review gate.

### 3.2 Execution plan

The `IngestPlan` (see [`docs/architecture.md` § 8](architecture.md#8-adaptive-ingestion-planning))
is generated by `DefaultIngestPlanner` from a `DocumentProfile` plus
an `IngestPolicy`. Each `PlannedStep` carries:

| Field | Purpose |
|---|---|
| `step_id` / `name` | Stable identifier. |
| `stage` | UI label (`COMPILE` / `ENRICH` / `GRAPH` / `INDEX`). |
| `decision` | `RUN` / `SKIP` / `CONDITIONAL`. |
| `reason` | Short human-readable explanation (skip path). |
| `required` | Whether failure fails the workflow. |
| `source` | Who decided: `caller` / `planner` / `policy` / `default` / `config`. |
| `dependency_step_ids` | Stages that must finish first. |
| `estimated_cost_tier` | `NONE` / `LOW` / `MEDIUM` / `HIGH`. |
| `expected_engine` | E.g. `MinerU`. |
| `expected_provider` | E.g. `raganything`. |
| `risk_level` | `low` / `medium` / `high`. Highlights dangerous skips (e.g. `SKIP index` is high-risk because it breaks searchability). |
| `warning` | Free-form note when policy and signals conflict (e.g. `text_only` policy on a scanned PDF). |

Caller-supplied kinds always override planner decisions; the
recorded `source=caller` makes the override explicit.

### 3.3 Progress events

| `event_type` | When emitted |
|---|---|
| `run.created` | A new ingestion run begins. |
| `document.received` | The document file has been received. |
| `assessment.started` / `.completed` | Document profiler running / done. |
| `plan.generated` | Plan recorded, awaiting confirm or auto-running. |
| `plan.confirmed` | User (or auto-run) confirmed the plan. |
| `step.started` | A step is about to execute. |
| `step.progress` | Throttled progress tick (5% delta minimum, plus 0%/100% boundaries). |
| `step.skipped` | A step was skipped — `reason` is mandatory. |
| `step.warning` | Recoverable issue (e.g. low-confidence table). |
| `step.completed` | Step finished cleanly. |
| `step.failed` | Step failed; payload carries `error_type`, `error_message`, `retryable`. |
| `run.completed` | Terminal success — `final_status` is `succeeded` or `succeeded_with_warnings`. |
| `run.failed` | Terminal failure — payload carries `failure_code`, `failure_message`. |
| `human_review.required` | Workflow paused at a review gate. |

Each event has:

```json
{
  "eventId": "evt_4f6a…",
  "runId": "run_a8c2…",
  "eventType": "step.progress",
  "timestamp": "2026-05-04T12:34:56Z",
  "severity": "INFO",
  "stage": "COMPILE",
  "step": "LAYOUT_PREPARATION",
  "status": "running",
  "progressPercent": 80,
  "current": 35,
  "total": 44,
  "message": "Layout preparation: 35/44 pages",
  "engine": "MinerU"
}
```

`severity` is one of `INFO` / `WARNING` / `ERROR`. The frontend uses
it for UI styling (severity badges).

---

## 4. Frontend integration

### 4.1 Polling vs. streaming

| Endpoint | Use for |
|---|---|
| `GET /ingestion-runs/{id}` | One-shot status snapshot — listing pages, lazy-loading detail. |
| `GET /ingestion-runs/{id}/plan` | Plan-review screen. Cache aggressively — plans don't change after generation. |
| `GET /ingestion-runs/{id}/events` | Backfill the timeline when the user opens a run page. |
| `GET /ingestion-runs/{id}/events/stream` | Live progress on the run-detail page. Open one EventSource per visible run. |

### 4.2 Subscribing to the SSE stream

```js
const source = new EventSource(`/ingestion-runs/${runId}/events/stream`);

source.addEventListener("step.progress", (msg) => {
  const event = JSON.parse(msg.data);
  updateStepProgress(event.stage, event.step, event.progressPercent, event.message);
});

source.addEventListener("run.completed", (msg) => {
  const event = JSON.parse(msg.data);
  setRunStatus(event.runId, "completed", event.metadata?.warningCount);
  source.close();
});

source.addEventListener("run.failed", (msg) => {
  source.close();
});
```

Browser `EventSource` automatically sends `Last-Event-Id` on
reconnect. The server respects that header and resumes the stream
from after the cursor.

### 4.3 Resume on disconnect

- Each SSE message carries `id: <event_id>` (the audit log's stable
  event ID) and `event: <event_type>`.
- On reconnect the server starts emitting events whose ID appears
  AFTER the supplied `Last-Event-Id`.
- The stream closes automatically on terminal events
  (`run.completed` / `run.failed`).

### 4.4 Combining the surfaces

The recommended pattern:

1. On run page open: `GET /ingestion-runs/{id}` → render header
   (status, document name, mode, policy).
2. `GET /ingestion-runs/{id}/plan` → render the plan card with per-
   step decisions.
3. `GET /ingestion-runs/{id}/events` → backfill the timeline.
4. `GET /ingestion-runs/{id}/events/stream` → append live events.
5. On terminal event, close the stream and refresh `GET /ingestion-runs/{id}`
   for the final summary.

---

## 5. ProgressReporter abstraction

```
ProgressReporter (Protocol)
├── AuditProgressReporter        — writes through AuditRecorder
├── TemporalHeartbeatReporter    — pumps activity heartbeats
├── CompositeProgressReporter    — fan-out
└── NoopProgressReporter         — for tests
```

Default deployment composition (recommended):

```python
from j1.audit.recorder import DefaultAuditRecorder
from j1.audit.sink import JsonlAuditSink
from j1.runs import (
    AuditProgressReporter, CompositeProgressReporter,
    TemporalHeartbeatReporter,
)

audit_recorder = DefaultAuditRecorder(JsonlAuditSink(workspace))
reporter = CompositeProgressReporter(
    AuditProgressReporter(audit_recorder),  # persists
    TemporalHeartbeatReporter(),            # operator visibility
)
```

The reporter is transport-free. It does NOT hold HTTP connections
or React state; the SSE endpoint reads the same audit log in tail-
and-publish mode.

Throttling (only `step.progress` is throttled):

- Sub-5% deltas are dropped to keep audit volume bounded.
- 0% (step start) and 100% (step end) are always emitted.
- Step boundaries (`step.started` / `step.completed` / `step.failed`)
  reset the throttle for the next iteration.

---

## 6. Temporal heartbeats and search attributes

The progress layer does NOT replace Temporal-side observability — it
complements it.

**Heartbeats.** `TemporalHeartbeatReporter` calls
`activity.heartbeat({...})` with a compact summary on every reported
event. This:

- Surfaces the current step + progress in Temporal UI's "Latest
  Heartbeat Details" panel — useful when a frontend isn't available.
- Makes `heartbeat_timeout` (configured at 2 minutes for
  `compile` / `build_graph`) fire if the activity stalls.

**Search attributes.** Update only at major boundaries — step
started / 25% / 50% / 75% / completed / failed — so visibility data
doesn't burn through Temporal's per-workflow event budget. The
existing `J1IngestStage` and `J1IngestMode` keys are sufficient;
the progress layer doesn't introduce new search attributes.

**Why not store progress in Temporal history?** Two reasons:

1. Temporal payloads are size-bounded; tens of thousands of
   `step.progress` events would balloon history.
2. Temporal events are part of the deterministic replay log;
   per-tick progress data is non-deterministic and would force
   the workflow to use side-effect markers.

---

## 7. MinerU progress integration

RAGAnything / MinerU emits progress as logger lines, not structured
callbacks. The integration:

1. `j1.providers.raganything._progress.MinerUProgressParser` parses
   known shapes (layout preparation, model fetch, transformer
   predictor cost) into structured `MinerUProgressEvent`s.
2. `attach_mineru_progress_handler(reporter, ctx, run_id)` (in
   `_log_bridge.py`) installs a `logging.Handler` on the `mineru`
   and `raganything` loggers that pipes parsed events into a
   `ProgressReporter`.

Wrap a raganything call as:

```python
from j1.providers.raganything._log_bridge import attach_mineru_progress_handler

with attach_mineru_progress_handler(reporter, ctx, run_id):
    await rag.process_document_complete(file_path=..., output_dir=...)
```

The handler is removed on `with` exit, so unrelated code paths don't
leak captured logs.

Recognised log shapes:

| Log line | Maps to |
|---|---|
| `Layout Preparation: 80% \| 35/44` | `step.progress`, stage=COMPILE, step=LAYOUT_PREPARATION |
| `Layout Preparation: 100%\|████\| 1/1 [00:00<00:00]` (tqdm) | same |
| `Fetching 13 files: 8%\|▊\| 1/13 [...]` | `step.progress`, step=MODEL_FETCH |
| `get transformers predictor cost: 50.12s` | `step.completed`, step=MODEL_LOAD |

Adding new shapes is one regex + builder pair in `_progress.py`.

---

## 8. Failure semantics

(See also [architecture.md § 6](architecture.md#6-temporal-orchestration).)

- **Required step fails** → workflow raises `ApplicationError` with
  `type=J1_INGEST_REQUIRED_STEP_FAILED`. Temporal sees a Failed
  workflow. The progress layer emits `step.failed` followed by
  `run.failed` with `failureCode` and `failureMessage`.
- **Optional step fails under `continue_optional` policy** →
  `step.warning` + run continues. Final `run.completed` carries
  `final_status: succeeded_with_warnings` and `warningCount`.
- **Skipped step** → `step.skipped` with mandatory `reason`. Never
  silently dropped.
- **Fallback used** (e.g. plain-text fast path bypassed mineru) →
  `step.warning` with the fallback details.

`final_status=COMPLETED` is reported ONLY when every required
enabled step completed successfully. The workflow can no longer
return Completed-with-an-error-string-inside.

---

## 9. Privacy / safety guarantees

- Progress event payloads NEVER carry document content, prompts,
  LLM responses, or extracted text.
- `metadata` is a small structured dict — short operational strings
  only.
- File paths in run records are workspace-relative — no host paths.
- Temporal search attributes / memo carry only stage / mode / IDs.

---

## 10. Curl examples

```bash
# Upload a document (existing endpoint — unchanged).
curl -X POST http://localhost:8000/documents \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -F file=@example.pdf

# (After deployment wires it: a run record gets created with
# `run_id` returned in the body.)

# Get current run status.
curl http://localhost:8000/ingestion-runs/run_abc123 \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"

# Get the execution plan.
curl http://localhost:8000/ingestion-runs/run_abc123/plan \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"

# Confirm the plan (manual-confirmation deployments).
curl -X POST http://localhost:8000/ingestion-runs/run_abc123/confirm \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"

# Get historical progress events.
curl http://localhost:8000/ingestion-runs/run_abc123/events \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha"

# Stream live progress (SSE).
curl -N http://localhost:8000/ingestion-runs/run_abc123/events/stream \
  -H "X-Tenant-Id: acme" -H "X-Project-Id: alpha" \
  -H "Accept: text/event-stream"
```

---

## 11. End-to-end wiring (live)

The progress layer is wired into three production code paths.
Each is opt-in: deployments that don't pass the relevant parameters
keep the legacy behaviour bit-for-bit. By convention, **`run_id`
== `correlation_id` == `workflow_id`** so the audit log, SSE
cursor, and Temporal IDs share one identifier — the frontend
never has to map between them.

### 11.1 `POST /ingestion-runs` — composite entry point

The user-facing upload endpoint. One call:

1. Registers the document via `facade.ingestion.register_document`.
2. Validates / resolves processor kinds (`compilerKind`,
   `enricherKind`, `graphBuilderKind`, `indexerKind`).
3. Allocates `run_id` (caller-supplied `correlation_id` wins;
   otherwise `uuid4().hex`).
4. Persists the initial `IngestionRun` record with `status=CREATED`.
5. Calls `reporter.report_run_created` and
   `reporter.report_document_received` if a reporter is wired.
6. Starts the workflow via the existing `JobStarter` contract.
7. Updates the run record with the resulting `workflow_id`.

Returns `IngestionRunCreatedRecord(runId, documentId, workflowId,
status)`. The frontend uses the `runId` to navigate to the
run-detail page and open the SSE stream immediately.

The legacy `POST /documents` and `POST /documents/{id}/ingest`
endpoints are unchanged — deployments that don't adopt the runs
surface keep working.

### 11.2 Activities emit progress events

`ProcessingActivities` accepts an optional `progress_reporter`
parameter. When set AND the request carries a `correlation_id`,
each activity:

- Calls `reporter.report_step_started` before the underlying
  service call.
- Calls `reporter.report_step_completed` (or `skipped` / `failed`
  based on `result.status`) after.
- Calls `reporter.report_step_failed` and re-raises on unhandled
  exception. **Telemetry never swallows errors** — the workflow's
  failure-propagation contract still fires.

Stage labels: `COMPILE`, `ENRICH`, `GRAPH`, `INDEX`. Step IDs:
`compile`, `enrich`, `build_graph`, `index`. The `query` activity
is intentionally NOT instrumented — it's a read path, not part of
the ingestion timeline.

### 11.3 Workflow exit + skipped stages → activities

Workflow code is replay-deterministic and cannot directly call
`AuditRecorder` (non-deterministic side effect). Two short-lived
activities in [`j1.orchestration.activities.runs`](../src/j1/orchestration/activities/runs.py)
bridge the gap:

- **`j1.runs.report_terminal`** — called by the workflow at every
  exit path (success, business rejection, unexpected exception,
  cancellation). Translates the workflow's `final_status` into
  `report_run_completed` / `report_run_failed`. The input carries
  a compact `step_summary` (one entry per stage, max ~5 entries)
  so the `run.completed` event payload supports a "what ran" recap
  without a follow-up `/events` fetch.
- **`j1.runs.report_step_skipped`** — called when the planner /
  policy / config skips a stage. Records the `step.skipped` event
  with a mandatory `reason` and `source` (`caller` / `planner` /
  `policy` / `default` / `config`). Triggered from the workflow's
  `_stage_enabled` branches at compile / enrich / graph / index.

Both activities are best-effort — telemetry must never block the
workflow. They no-op silently when no reporter is wired.

### 11.4 MinerU log lines → progress events

`RAGAnythingCompileRequest` has two optional fields:
`progress_reporter` and `run_id`. When both are present, the
bridge wraps `asyncio.run(_run_compile())` in
[`attach_mineru_progress_handler`](../src/j1/providers/raganything/_log_bridge.py),
which installs a `logging.Handler` on the `mineru` and
`raganything` loggers for the duration of the call. Each log line
is parsed by `MinerUProgressParser` and routed to the reporter as
`step.progress` (layout / model fetch) or `step.completed` (model
load). The handler is removed on context exit; no log spillover.

Pass-through:

```
RAGAnythingCompiler.compile(ctx, document_id,
                            progress_reporter=reporter,
                            run_id=run_id)
                ↓ via RAGAnythingCompileRequest
        _bridge.default_compile(request)
                ↓ wraps in attach_mineru_progress_handler
        rag.process_document_complete(...)
                ↓ logger lines via mineru/raganything loggers
        MinerUProgressParser
                ↓
        reporter.report_step_progress(stage="COMPILE",
                                      step="LAYOUT_PREPARATION",
                                      progress_percent=80,
                                      current=35, total=44,
                                      engine="MinerU")
                ↓
        AuditProgressReporter → audit JSONL
                ↓
        SSE stream picks it up via correlation_id filter
```

Throttling: the reporter drops sub-5% deltas (always emits 0%
and 100%); the parser de-duplicates exact-same-percent ticks.

### 11.5 Bootstrap factory

[`build_default_progress_reporter(audit_recorder)`](../src/j1/runs/__init__.py)
returns the standard composite:

```python
CompositeProgressReporter(
    AuditProgressReporter(audit_recorder),
    TemporalHeartbeatReporter(),
)
```

Deployment entrypoints (`deploy/dev/api.py`, `deploy/dev/worker.py`)
construct an `AuditRecorder` and call this factory; the result is
passed into:

- `create_rest_api(..., progress_reporter=...)` — for the
  `POST /ingestion-runs` flow.
- `ProcessingActivities(..., progress_reporter=...)` and
  `RunsActivities(progress_reporter=...)` — for the worker.

### 11.6 What's still optional (not blocking)

Two integration points remain explicitly opt-in:

- **Confirmation gate workflow signal.** `POST /ingestion-runs/{id}/confirm`
  currently flips the run status only. Promoting it to a real
  workflow signal (analogous to the existing review / budget gates)
  is a workflow-version change, not a data-shape change — additive
  whenever manual confirmation becomes a product requirement.
- **Per-page progress emission for non-MinerU vendors.** Other
  vendors that expose progress callbacks (rather than logger
  lines) can integrate by accepting a `ProgressReporter` and
  calling `report_step_progress` directly — no log-bridge needed.
