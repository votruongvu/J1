# Ingestion performance trace

A dedicated structured log for investigating ingestion latency,
retries, stuck stages, and accidental cross-run reuse. **Disabled by
default** — production deployments never emit it; operators turn it
on while debugging a specific run.

This is **separate** from the business audit log (`audit/events.jsonl`)
and from the process-wide stderr logger. The trace exists to answer
questions like:

- Which stage is slow?
- Which stage failed, and why?
- Which stage retried, and how many times?
- Did this run reuse anything from an older run?
- Did parse / compile / enrichment / artifact registration actually
  run, or did something short-circuit?
- How long did each stage take?

## Enable locally

Set the env vars before starting the API / worker:

```env
J1_INGEST_TRACE_ENABLED=true
J1_INGEST_TRACE_LEVEL=INFO
J1_INGEST_TRACE_SLOW_STAGE_MS=1000
J1_INGEST_TRACE_OUTPUT=logs/ingest_trace.jsonl
```

| Var | Default | Meaning |
|---|---|---|
| `J1_INGEST_TRACE_ENABLED` | `false` | Master switch. When false, every helper is a no-op and no file is created. |
| `J1_INGEST_TRACE_LEVEL` | `INFO` | `INFO` records stage-level timing; `DEBUG` may include additional safe metadata. |
| `J1_INGEST_TRACE_SLOW_STAGE_MS` | `30000` | Stages whose `duration_ms` ≥ this value are flagged `slow=true` and emit one structured warning on the normal logger. |
| `J1_INGEST_TRACE_OUTPUT` | `logs/ingest_trace.jsonl` | Where the JSONL is written. The helper creates the parent directory. |

Run a small ingestion, then watch the file:

```bash
tail -f logs/ingest_trace.jsonl
```

Find the slow stages:

```bash
grep '"slow": true' logs/ingest_trace.jsonl
```

Trace events for one run:

```bash
grep '"run_id": "RUN_ID_HERE"' logs/ingest_trace.jsonl
```

## Event shape

Every line is one JSON object. Always present: `timestamp`,
`trace_event`, `stage`, `status`. Available when the surface knows
them: `tenant_id`, `project_id`, `document_id`, `run_id`,
`target_snapshot_id`, `snapshot_id`, `workflow_id`, `activity`,
`attempt`. Timed stages add `duration_ms` + `slow`. Failed events add
`error_type` + `error_message`. Safe summaries land in `metadata`.

```json
{
  "timestamp": "2026-05-14T08:25:12.123+00:00",
  "trace_event": "ingest.compile.completed",
  "stage": "compile",
  "status": "completed",
  "tenant_id": "acme",
  "project_id": "alpha",
  "document_id": "doc_789",
  "run_id": "run_abc",
  "target_snapshot_id": "snapshot_xyz",
  "workflow_id": "wf-run_abc",
  "duration_ms": 82431,
  "slow": true,
  "metadata": {
    "chunk_count": 12,
    "warning_count": 2
  }
}
```

### Status values

| Status | Meaning |
|---|---|
| `started` | Stage entered. Paired with `completed` or `failed`. |
| `completed` | Stage finished successfully; `duration_ms` is present. |
| `failed` | Stage raised an exception; `duration_ms`, `error_type`, `error_message` are present. |
| `skipped` | Stage didn't run because the planner / policy said so. |
| `retry_scheduled` | A compile/enrich attempt failed and the next attempt was scheduled. |

### Event name convention

`ingest.<stage>.<status>` for paired timed events
(`ingest.compile.started` / `ingest.compile.completed`). Lifecycle
events use the same prefix:

- `ingest.run.created`, `ingest.workflow.started`,
  `ingest.snapshot.allocated`
- `ingest.run.completed`, `ingest.run.failed`, `ingest.run.cancelled`

Compile retry mirrors the existing audit-action names so operators
can grep both surfaces the same way:

- `j1.ingestion.compile.attempt.started`
- `j1.ingestion.compile.attempt.completed`
- `j1.ingestion.compile.retry.scheduled`

## Where trace points live

| Boundary | Trace event | Where emitted |
|---|---|---|
| Document reindex POSTed | `ingest.run.created` + `ingest.snapshot.allocated` | `src/j1/adapters/rest/app.py` (`post_document_reindex`) |
| Workflow dispatched | `ingest.workflow.started` | same handler, after `starter()` returns |
| Per-stage timing (assess, compile, enrich, graph, index, ...) | `ingest.<stage>.started` / `.completed` / `.failed` | `DiagnosticRecorder.stage()` |
| Compile attempts + retries | `j1.ingestion.compile.attempt.*` | `DiagnosticRecorder.record_attempt_event()` |
| Run terminal | `ingest.run.completed` / `.failed` / `.cancelled` | `RunsActivities._persist_run_terminal()` |

The workflow itself does not write trace lines — Temporal's sandbox
forbids file I/O from workflow code. Every trace point lives in
activity or REST code.

## Confirm run isolation

Re-index must start from scratch (parse the original file again, no
reuse of old chunks / compile output / enrichment / graph). Trace
makes that visible: the REST `ingest.run.created` event carries:

```json
{
  "metadata": {
    "run_type": "reindex",
    "parent_run_id": "<prior run>",
    "fresh_run": true,
    "reused_existing_compile": false,
    "reused_existing_chunks": false,
    "reused_existing_enrichment": false
  }
}
```

For each subsequent stage, the `metadata` is the
`DiagnosticRecorder._StageRecord.counters` dict — small integers and
plain strings only. If you see `cache_hit: true` or `reused_...:
true` in a re-index trace, that's a bug — re-index is supposed to be
from scratch.

To trace one re-index end-to-end:

```bash
# 1. start with a clean file
: > logs/ingest_trace.jsonl

# 2. trigger the reindex
curl -X POST http://localhost:8000/documents/doc-id/reindex \
  -H 'X-Tenant-Id: acme' -H 'X-Project-Id: alpha'

# 3. wait, then inspect events for the new run only
RUN_ID=$(jq -r 'select(.trace_event=="ingest.run.created") | .run_id' \
  logs/ingest_trace.jsonl | tail -1)

jq -c "select(.run_id == \"$RUN_ID\")" logs/ingest_trace.jsonl
```

Expected:

- one `ingest.run.created` with `fresh_run: true`
- one `ingest.snapshot.allocated`
- one `ingest.workflow.started`
- a sequence of `ingest.<stage>.started` + `ingest.<stage>.completed`
  pairs (compile, then enrich, graph, index — whichever the planner
  enabled)
- a final `ingest.run.completed` / `.failed`

If compile reuses prior-run artifacts you'd see a `cache_hit: true`
flag in its `metadata` block; that should never happen on the reindex
path.

## What must not be logged

The writer strips these metadata keys before writing the JSONL line:
`text`, `content`, `chunks`, `embedding`, `embeddings`, `prompt`,
`prompts`, `response`, `responses`, `ocr_output`, `image_bytes`,
`raw_bytes`. Any non-blacklisted string value over 240 chars is
truncated; `error_message` is truncated to 300 chars.

Allowed safe summaries:

- File extension, file size, page count
- Parser name, parse method, compile mode
- Artifact count, chunk count, detected images / tables / equations
  counts
- Retry count, warning count, warning codes (short strings)
- Selected domain id, enrichment policy verdict
- Short error type, short error message

If you find yourself wanting to log a chunk or a prompt, that's not
trace — that's a debugger session.

## Trace vs audit log

| | Audit log (`audit/events.jsonl`) | Ingest trace (`logs/ingest_trace.jsonl`) |
|---|---|---|
| Audience | Compliance / FE timeline | Operator / developer debugging |
| Always on | Yes | No (off by default) |
| Per-run scoped | Yes (correlation_id) | Yes (run_id) |
| Records business decisions | Yes | No — pure observability |
| Records timing | Some events | Every paired stage |
| Survives reset | Yes | No (operators can delete the file) |
| Slow-stage warnings | No | Yes — `ingest.trace.slow_stage` on the normal logger |

Trace is purely additive; turning it off must not change ingestion
behaviour or audit-log content.

## Local verification

```bash
pytest tests/test_ingest_trace.py -q
```

Then, with trace enabled:

```bash
export J1_INGEST_TRACE_ENABLED=true
export J1_INGEST_TRACE_SLOW_STAGE_MS=1000
export J1_INGEST_TRACE_OUTPUT=logs/ingest_trace.jsonl
# upload a small document and watch:
tail -f logs/ingest_trace.jsonl
```

Confirm:

- Each line is valid JSON.
- Each event has `timestamp`, `trace_event`, `stage`, `status`.
- `run_id` is present whenever the surface knows it.
- Compile has started + completed (or failed).
- Completed timed events have `duration_ms`.
- Slow stages have `slow: true` and produce one
  `ingest.trace.slow_stage` warning on the normal logger.
- No document text, chunks, embeddings, prompts, or responses appear
  anywhere in the file.
