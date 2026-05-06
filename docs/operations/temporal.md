# Temporal Operations

Practical guide to running J1's Temporal-backed workflows in
development and production.

J1 currently uses Temporal as its durable workflow substrate. This
document covers how to start a worker, what workflows ship, the
retry / signal / recovery model, and notes for production
deployments.

> **Production scope.** Nothing in this document claims J1's
> orchestration is production-tuned for any specific scale. The
> framework is suitable for deployments that already operate
> Temporal — the heavy operational lifting is Temporal's, not J1's.

---

## 1. Why Temporal

Temporal provides what J1's pipeline needs without J1 having to
build it:

- **Durable execution** — workflow state survives worker restarts.
- **At-least-once activity execution** with deterministic retries.
- **Long-lived signals** — pause / resume / cancel / approve-budget
  / approve-review.
- **Versioning hooks** — for evolving workflow code over time.
- **Cluster + UI** — operators can see in-flight workflows without
  J1 building a job dashboard.

The framework's workflow contract is:

- **Workflows coordinate; activities act.** Workflow code is
  deterministic, holds only IDs / metadata, and decides ordering.
  Activities perform I/O.
- **Workflow state is small.** `WorkflowStatus` carries counts +
  IDs + an error string — never document bytes, embeddings, or
  artifact bodies.

---

## 2. Local Temporal setup

### 2.1 Docker (default)

The bundled stack at [`deploy/dev/`](../../deploy/dev/) brings up a
Temporal server (`temporalio/auto-setup` with Postgres for
Temporal's own storage) plus the Temporal Web UI:

```bash
cp .env.example .env
docker compose -f deploy/dev/docker-compose.yml up --build
```

Endpoints:

- Temporal gRPC: `localhost:7233`
- Temporal Web UI: <http://localhost:8080>

The default namespace is `default` (created on boot by `auto-setup`).

### 2.2 Standalone Temporal

If you already run Temporal elsewhere, point the J1 worker / API at
it via env vars:

```bash
export J1_TEMPORAL_TARGET=temporal.internal:7233
export J1_TEMPORAL_NAMESPACE=my-namespace
export J1_TEMPORAL_TASK_QUEUE=j1-processing
# Optional:
export J1_TEMPORAL_TLS=true
export J1_TEMPORAL_API_KEY=...    # Temporal Cloud / authenticated cluster
```

Full reference: [`docs/configuration/environment.md`](../configuration/environment.md) § 2.

---

## 3. Worker startup

### 3.1 Bundled dev worker

The minimal entrypoint is [`deploy/dev/worker.py`](../../deploy/dev/worker.py):

```bash
.venv/bin/python -m deploy.dev.worker
```

It builds an `ApplicationFacade`, registers the bundled workflows
(`ProjectProcessingWorkflow`, `DocumentProcessingWorkflow`) and the
default activity classes, then enters Temporal's worker run loop.

### 3.2 Production worker shape

`WorkerSpec` is a frozen dataclass with **two** fields — the
workflow types and the flat activity callable list:

```python
@dataclass(frozen=True)
class WorkerSpec:
    workflows: Sequence[type] = ()
    activities: Sequence[Callable] = ()
```

Concurrency and the activity executor are passed at *worker
construction time*, not on `WorkerSpec`:

```python
build_worker(client, settings, spec, *,
             activity_executor=None, max_concurrent_activities=None) -> Worker
run_worker(client, settings, spec, *,
           activity_executor=None, max_concurrent_activities=None) -> None
```

> **All shipped J1 activities are synchronous** (regular `def`, not
> `async def`). The Temporal SDK requires an `activity_executor`
> (typically a `concurrent.futures.ThreadPoolExecutor`) when sync
> activities are registered — pass one or the worker raises at
> startup.

A production worker typically follows the same shape as
[`deploy/dev/_wiring.py::build_worker_spec`](../../deploy/dev/_wiring.py)
— wire the activity classes, collect their `.all_activities()`
callables, and hand them to `WorkerSpec`:

```python
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from j1 import (
    AccountingActivities, DocumentProcessingWorkflow,
    KnowledgeProcessingActivities, ProcessingActivities,
    ProjectActivities, ProjectLifecycleActivities,
    ProjectProcessingWorkflow, ReviewActivities, SearchActivities,
    WorkerSpec, build_client, load_temporal_settings, run_worker,
)
# ... plus your wired-up registries, services, and processor maps

async def main():
    temporal = load_temporal_settings()
    client = await build_client(temporal)

    activities: list = []
    activities += ProjectLifecycleActivities(...).all_activities()
    activities += ProjectActivities(...).all_activities()
    activities += AccountingActivities(...).all_activities()
    activities += SearchActivities(indexers={...}).all_activities()
    activities += ReviewActivities(...).all_activities()
    activities += ProcessingActivities(
        compilers={...}, enrichers={...}, graph_builders={...},
        indexers={...}, query_providers={...},
        ...
    ).all_activities()
    activities += KnowledgeProcessingActivities(
        compilers={...}, enrichers={...}, graph_builders={...},
        ...
    ).all_activities()

    spec = WorkerSpec(
        workflows=[ProjectProcessingWorkflow, DocumentProcessingWorkflow],
        activities=activities,
    )

    max_conc = int(os.environ.get("J1_WORKER_MAX_CONCURRENT_ACTIVITIES", "5"))
    with ThreadPoolExecutor(max_workers=max_conc) as executor:
        await run_worker(
            client, temporal, spec,
            activity_executor=executor,
            max_concurrent_activities=max_conc,
        )

asyncio.run(main())
```

For the full reference wiring (registries, services, audit/cost
sinks, intake) consult
[`deploy/dev/_wiring.py`](../../deploy/dev/_wiring.py) — its
`build_worker_spec(workspace, *, compilers=, enrichers=, graph_builders=, indexers=, query_providers=)`
helper takes empty processor maps by default. Real deployments pass
their own (vendor-specific) processor maps in.

### 3.3 Scaling workers

Multiple worker processes can share the same task queue — Temporal
distributes activity tasks across them. Scale horizontally:

```bash
# N processes, all pointed at the same J1_TEMPORAL_TASK_QUEUE
.venv/bin/python -m deploy.dev.worker &
.venv/bin/python -m deploy.dev.worker &
.venv/bin/python -m deploy.dev.worker &
```

Important caveat: J1's bundled JSON registries
(`JsonSourceRegistry`, `JsonArtifactRegistry`, `JsonReviewQueue`)
use atomic-rename writes but no cross-process locking. For
many-worker deployments writing to the same project, either:

- Run a single worker per `(tenant_id, project_id)`, OR
- Plug in registry implementations that *are* concurrency-safe
  (Postgres / a real KV store) — both registries are behind
  Protocols, swap the impl.

---

## 4. API → workflow trigger path

The REST adapter triggers workflows via a deployment-supplied
`job_starter` callable injected into `create_rest_api(...)`:

```
POST /ingestion-jobs
  → REST handler
    → ApplicationFacade.job_control.start_project_job(...)
      → job_starter(workflow_request)            ← deployment glue
        → temporalio.Client.start_workflow(...)
          → ProjectProcessingWorkflow.run(...)
```

The job starter is **not** part of `j1.adapters.rest` — it lives in
deployment code (see
[`deploy/dev/_wiring.py`](../../deploy/dev/_wiring.py) for the
reference pattern). This keeps the REST adapter free of Temporal
client imports.

---

## 5. Workflows that ship

### 5.1 `ProjectProcessingWorkflow`

The full project pipeline. Internal state machine (visible via the
`get_status` query — distinct from the operator-facing `final_status`
on the workflow's return value):

```
RUNNING ──┬─► PAUSED ──► RUNNING                          (pause / resume signal)
          ├─► WAITING_FOR_BUDGET_APPROVAL ──► RUNNING     (approve_budget signal)
          │                            └──► CANCELLED      (reject_budget / cancel signal)
          ├─► WAITING_FOR_REVIEW ──► RUNNING               (approve_review signal)
          │                    └──► CANCELLED              (reject_review / cancel signal)
          ├─► COMPLETED                                    (returns ProjectProcessingResult)
          ├─► CANCELLED                                    (returns ProjectProcessingResult)
          ├─► FAILED_RECOVERABLE                           (raises ApplicationError, type=J1_INGEST_UNEXPECTED_ERROR)
          └─► FAILED_FINAL                                 (raises ApplicationError, type=J1_INGEST_REQUIRED_STEP_FAILED)
```

**Failure-propagation contract.** When the internal state ends in
either `FAILED_*`, the workflow **raises** `temporalio.exceptions.
ApplicationError` so Temporal sees the run as **Failed** in the UI.
It does NOT return a result with a failure-encoded status field. The
recorded internal state is still readable via the `get_status`
query — the `_BusinessRejection` distinction is preserved for
callers that care, but operators just see Failed.

Error types raised:

| `ApplicationError.type` | When | Retryable |
|---|---|---|
| `J1_INGEST_REQUIRED_STEP_FAILED` | Required step (validate / compile / enrich-when-explicit / graph-when-explicit / index) reported `status="failed"` | No |
| `J1_INGEST_UNEXPECTED_ERROR` | Unexpected exception escaped the workflow's stage handlers | Yes (parent workflow / operator may retry) |
| `J1_INGEST_LOOKUP_FAILED` | Activity-level: missing document / artifact / processor kind | No |

Stages:

1. Validate the project context.
2. List pending documents.
3. (Optional, when adaptive planning is enabled) Profile each
   document and build an `IngestPlan`. See
   [architecture § 8](../architecture.md#8-adaptive-ingestion-planning).
4. Compile each (with optional review gate after).
5. Enrich each (with optional review gate after).
6. Build the graph (with optional review gate after).
7. Build the search index (with optional review gate after).
8. Finalize.

Budget gates fire whenever recorded spend approaches a configured
ceiling; the workflow pauses and waits for `approve_budget`.

**Per-stage accounting.** Every stage produces a `StepResult` on the
returned `ProjectProcessingResult.step_results` (and on the
`get_status` query response). Each entry carries `status`, `required`,
`source` (caller / planner / policy / default / config), and an
optional `reason` for skips and `error` for failures — operators
don't need to dig into activity-attempt history to answer "what ran,
what was skipped, what failed, why?".

**Visibility.** The workflow always emits structured
`workflow.logger` events at every lifecycle transition with
operationally safe context (`tenant_id`, `project_id`,
`compiler_kind`, etc. — never document content). Logs are on by
default and need no setup.

The workflow can also publish typed search attributes for Temporal
UI filtering:

| Search attribute | Updates on |
|---|---|
| `J1IngestStage` | Each stage start, completion, and terminal exit (`completed` / `cancelled` / `failed`). |
| `J1IngestMode` | Set once per document when adaptive planning is enabled, to the chosen ingest mode (e.g. `text_only`). |

Search attributes are **opt-in (default off)**. The Temporal cluster
rejects upserts for attributes that aren't registered with the
namespace, and the rejection happens at workflow-activation
completion — server-side, after the workflow code returns — so a
try/except inside the workflow body cannot recover from it. If you
turn the flag on without first registering the attributes, every
workflow activation fails with `BadSearchAttributes` and the worker
gets stuck.

**To enable:**

1. Register the attributes with your Temporal namespace:

   ```bash
   temporal operator search-attribute create \
     --namespace default \
     --name J1IngestStage --type Keyword
   temporal operator search-attribute create \
     --namespace default \
     --name J1IngestMode --type Keyword
   ```

2. Set `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=true` in the deployment
   environment. The API / JobStarter reads this and passes it
   through to `ProjectProcessingRequest.search_attributes_enabled`.

For dev clusters using `temporalio/auto-setup`, the attributes
**must** be registered after the namespace is created (i.e. after
the first run of the auto-setup container). Until then, leave the
flag off — structured logs alone are enough for debugging.

### 5.2 `DocumentProcessingWorkflow`

Single-document path: compile → enrich → index. No gates, no
per-document loop. Useful when callers drive ingestion one
document at a time (e.g. an event-driven worker).

For the full architecture see
[`docs/architecture.md`](../architecture.md) §§ 6–7.

---

## 6. Retry / timeout assumptions

| Concern | Default | Configurable |
|---|---|---|
| Generic activity retry policy | `RetryPolicySpec(initial=1s, backoff=2.0, max_interval=60s, max_attempts=5)` (`DEFAULT_RETRY`) | Per-activity override |
| Compile-stage retry policy | `COMPILE_RETRY` — `max_attempts=2` (compile is many minutes per attempt; one second-attempt covers transient infra blips, more would multiply parse cost) | Override at registration |
| Non-retryable errors | `J1_INGEST_REQUIRED_STEP_FAILED`, `J1_INGEST_LOOKUP_FAILED`, `ConfigError`, `ValidationError`, `LLMConfigError`, `DocumentNotFoundError`, `UnknownProcessorError`, plus parser-specific: `UnsupportedFileType`, `CorruptedDocument`, `ParserValidationError`, `MissingSourceObject` (full list in [`retries.py`](../../src/j1/orchestration/temporal/retries.py)) | Add new types in your activity wrappers |
| Workflow execution timeout | _(Temporal default — unset by J1)_ | Pass via `client.start_workflow(..., execution_timeout=...)` |
| Activity start-to-close timeout | `COMPILE_ACTIVITY_TIMEOUT=1h` for compile (real PDFs through MinerU + raganything are minutes per doc); `DEFAULT_ACTIVITY_TIMEOUT=10m` for enrich / graph / index; `SHORT_ACTIVITY_TIMEOUT=30s` for validate / list / spend / finalize / profile / set_document_status | Override at registration |
| Activity heartbeat timeout | `HEARTBEAT_TIMEOUT=5m` on compile / enrich / graph / index | Override at registration |
| Heartbeat | Compile / enrich / graph / index wrap their synchronous body in `_heartbeating(...)` — a daemon thread that pumps `activity.heartbeat` every 30 s. See "Repeated processing of the same document" below | Add `with _heartbeating(...)` around any new long-running synchronous activity |

Definitions live in
[`src/j1/orchestration/temporal/retries.py`](../../src/j1/orchestration/temporal/retries.py).
Override per-activity if a stage needs different behaviour (e.g.
graph build is expensive — give it more headroom).

**Heartbeat helper.** [`j1.heartbeat`](../../src/j1/heartbeat.py)
exposes a single thin wrapper around `temporalio.activity.heartbeat`:

```python
from j1 import heartbeat

def my_long_running_activity(input):
    for chunk in iterate_corpus(input):
        process(chunk)
        heartbeat({"chunk": chunk.id})    # safe outside an activity context
```

The helper is a no-op outside an activity context (so unit tests
that call the activity function directly don't blow up). The two
long-running shipped activities — `compile` and `build_graph` —
heartbeat at start and finish (via `_safe_heartbeat()` in
[`activities/processing.py`](../../src/j1/orchestration/activities/processing.py)),
and the workflow declares `heartbeat_timeout=2m` for them. The
remaining shipped activities (`enrich`, `index`, `query`,
`profile_document`, lifecycle, accounting, review, search) are
short-lived and treated as bounded units of work. If you write a
custom activity that may exceed the configured start-to-close
timeout, call `heartbeat()` inside its loop and set the activity's
`heartbeat_timeout` accordingly when registering.

### 6.1 Repeated processing of the same document

Symptom: the worker keeps starting a fresh parse subprocess for the
same uploaded document (visible in worker logs as multiple `Started
local mineru-api at http://127.0.0.1:NNNNN` lines on different
ports for one upload).

Diagnosis order:

1. **Open Temporal UI → the workflow → the failing activity.** Look
   at "Attempt count" and the failure type of each attempt. If it's
   `ActivityTaskTimedOut` with `HEARTBEAT_TIMEOUT`, the activity is
   exceeding the heartbeat budget.
2. **`docker compose logs --tail=500 worker | grep -E "Started local
   mineru-api|TimeoutError|HeartbeatTimeout|j1\.processing\.compile"`**
   to count the actual subprocess starts and see the cause between
   them.
3. **Inspect the cache file** at `$J1_DATA_ROOT/<tenant>/<project>/
   audit/processing_results.jsonl`. Each row records one
   activity outcome — `completed` / `failed` — with the error
   message. Multiple `failed` rows for one `cache_key` mean retries
   ARE happening; one `completed` row means the cache is doing its
   job.

Mechanisms that prevent or detect this:

- **Heartbeat ticker**
  ([`activities/processing.py:_heartbeating`](../../src/j1/orchestration/activities/processing.py)).
  Compile / enrich / build_graph / index wrap their synchronous
  call in a context manager that emits `activity.heartbeat` every
  30 s. Combined with `heartbeat_timeout=5m`, this means a true
  worker hang is detected within ~5 minutes; legitimate long parses
  are not killed.
- **Bounded compile retries** (`COMPILE_RETRY` =
  `max_attempts=2`). One transient infrastructure blip (worker
  crash, brief network failure) gets one re-attempt; that's it. No
  multiplicative parse cost from a 5-attempt default.
- **Processing-result cache**
  ([`processing/cache.py`](../../src/j1/processing/cache.py)). Keyed
  on `(document_hash, processor_kind, processor_version, mode)` —
  any change in those produces a fresh cache row. On activity
  retry, a `completed` entry short-circuits the underlying
  processor call; the cached `artifact_ids` are returned directly.
  `failed` entries are recorded for operator visibility but do NOT
  block retry.
- **Deterministic workflow_id**
  ([`deploy/dev/api.py:make_per_document_starter`](../../deploy/dev/api.py)).
  Per-document workflow IDs derived from `(tenant, project,
  document_id)` where `document_id` is content-addressed via the
  intake service's checksum dedup. Re-uploading identical bytes
  collides on the workflow_id. The starter passes
  `id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING`, so a
  re-upload while a workflow with that id is still running returns
  the existing handle instead of erroring or starting a parallel
  run — the FE then polls the in-flight run. Once the original
  workflow completes, the cache short-circuits any new attempt to
  re-parse the same content.

Workflow vs activity replay:

- **Workflow code replays** during recovery — `workflow.logger`
  output is replay-aware and not duplicated to operators.
- **Activity retries actually run again** — that's a real
  re-execution of the activity body. The cache lookup is what
  makes that re-execution cheap when the underlying work was
  already done.

Custom long-running activities that you write should:

1. Wrap the synchronous body in `with _heartbeating({...}):` (or
   call `j1.heartbeat()` inside the loop with progress details).
2. Set `heartbeat_timeout` at registration.
3. Compute a deterministic cache key from the input that decides the
   output (NOT including run_id, attempt number, or anything
   ephemeral) and consult `JsonlProcessingResultCache` (or your own
   `ProcessingResultCache` Protocol implementation) before invoking
   the expensive call.

---

## 7. Signals and queries

`ProjectProcessingWorkflow` accepts the following signals:

| Signal | Effect |
|---|---|
| `pause` | Sets the pause flag; the workflow stops before the next activity. |
| `resume` | Clears the pause flag. |
| `cancel` | Marks for graceful cancellation; finishes the current activity, then exits as `CANCELLED`. |
| `approve_budget` / `reject_budget` | Resolves the `WAITING_FOR_BUDGET_APPROVAL` gate. |
| `approve_review` / `reject_review` | Resolves the `WAITING_FOR_REVIEW` gate. |

Query: `get_status` returns the current `WorkflowStatus`. Fields:

| Field | Meaning |
|---|---|
| `state` | Lower-level internal state (RUNNING / FAILED_FINAL / etc.) |
| `final_status` | Operator-facing outcome (`COMPLETED` / `PARTIAL_COMPLETED` / `FAILED` / `CANCELLED` / `TIMED_OUT`) — `None` while running, populated only on terminal exit |
| `current_operation` / `pending_operation` | Stage in flight |
| `completed_operations` | What's already finished |
| `documents_total` / `documents_completed` | Progress counters |
| `produced_artifact_ids` | Artifacts created so far |
| `step_results` | Per-stage `StepResult` entries (status, required, source, reason, error) — assert on these in tests rather than `state` for the operator-relevant truth |
| `review_required` / `review_gate` | Set when paused at a review gate |
| `budget_approval_required` | Set when paused at a budget gate |
| `error` | Failure summary string |

Send signals via the Temporal client:

```python
handle = client.get_workflow_handle("my-workflow-id")
await handle.signal("pause")
status = await handle.query("get_status")
```

… or via the REST surface (for those mapped to endpoints — see
[`docs/rest-api.md`](../rest-api.md)).

---

## 8. Recovery behavior

- **Worker crash** — Temporal redrives any in-flight activities up
  to the retry budget. No state loss.
- **Activity panic / unhandled exception** — Temporal records the
  attempt as failed, applies the retry policy, then either retries
  or fails the activity (which may fail the workflow depending on
  the workflow code).
- **Workflow worker upgrade with code changes** — guard
  non-deterministic changes behind Temporal's `patched()` /
  `versioning` API. J1's workflows are designed to be small and
  evolve carefully; large changes warrant a new workflow type.
- **Storage corruption** — durable: raw + compiled + enriched +
  graph artifacts; rebuildable: search index. Audit + cost JSONLs
  are append-only and survive process restarts.
- **Temporal cluster outage** — workers reconnect; the workflow
  history is durable in Temporal's storage, so workflows resume
  from where they paused (assuming the cluster comes back).

---

## 9. Production notes (non-exhaustive)

- The bundled compose file uses `temporalio/auto-setup` with
  Postgres. **Do not deploy `auto-setup` to production.** Use
  `temporalio/server` against Cassandra or Postgres, plus
  Elasticsearch for advanced search. See Temporal's own deployment
  guide.
- Mount `J1_DATA_ROOT` on durable shared storage (NFS, EFS, Azure
  Files) when multiple workers share a project. Single-writer JSON
  registries are fine when work is partitioned by project.
- Set retention on the Temporal namespace appropriate for your
  workflow lengths + audit needs.
- Wire authentication on the REST surface
  ([`docs/security.md`](../security.md)). Temporal's own access
  control is managed at the cluster / namespace level — separate
  concern.
- Plan for review-gate latency. Workflows in
  `WAITING_FOR_REVIEW` are cheap on Temporal but unbounded in
  wall-clock — make sure your retention covers the longest
  realistic review window.
- Alert on `FAILED_RECOVERABLE` and `FAILED_FINAL` workflow
  states. The latter is a business-rejection terminal state; the
  former indicates an unexpected exception that needs investigation.

---

## 10. Troubleshooting pointers

| Symptom | Look at |
|---|---|
| Workflow accepted but never runs | Worker isn't on the same task queue — verify `J1_TEMPORAL_TASK_QUEUE` matches between API + worker |
| Worker logs `connection refused` | Check `J1_TEMPORAL_TARGET` — from inside Docker use the service DNS name (`temporal:7233`), not `localhost` |
| `ApplicationError(type=J1_INGEST_LOOKUP_FAILED, non_retryable=True)` | Document / artifact / processor-kind ID not found — check the registry. Caller-side bug; not retryable. |
| `ApplicationError(type=J1_INGEST_REQUIRED_STEP_FAILED, non_retryable=True)` | A required ingestion step (compile / index / etc.) failed. Drill into the activity's attempt history for the underlying cause; the workflow's `step_results` (via `get_status`) names the failed stage. |
| `ApplicationError(type=J1_INGEST_UNEXPECTED_ERROR, non_retryable=False)` | Unexpected exception escaped the workflow's stage handlers. Check the audit log + the original exception class named in the message. Retryable — a parent workflow / operator may legitimately retry. |
| Workflow stuck in `WAITING_FOR_BUDGET_APPROVAL` | Check cost log + send `approve_budget` (or `reject_budget`) via Temporal client / REST |
| Workflow stuck in `WAITING_FOR_REVIEW` | Reviewer hasn't acted — see `GET /reviews` + `POST /reviews/{id}/decision` |
| Activity heartbeat timeout on `compile` / `build_graph` | The vendor call (typically mineru / raganything) hung longer than `HEARTBEAT_TIMEOUT=2m`. Investigate the parser logs; first-call mineru downloads can be slow on a fresh container — pre-cache via `J1_PRECACHE_MINERU_MODELS=vlm` or `=all` in the dev Dockerfile. |
| Activity heartbeat timeout on a custom activity | Your activity exceeded its `start_to_close_timeout` without heartbeating — call `j1.heartbeat()` periodically inside the activity body and set `heartbeat_timeout` when registering. |
| Web UI shows workflow as `Failed` with `J1_INGEST_*` type | The failure-propagation contract: a required step actually failed. Read `get_status().step_results` to find which stage and why; the audit log carries the same data with full payloads. |
| `BadSearchAttributes: Namespace … has no mapping defined for search attribute J1IngestStage` (or `J1IngestMode`) | Search attribute upserts are enabled but the namespace doesn't know about the keys. Either register them with `temporal operator search-attribute create --namespace … --name J1IngestStage --type Keyword` (and the same for `J1IngestMode`), or set `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED=false` to disable upserts entirely. The error happens at activation completion (server-side) — the worker can't recover from it without one of those two changes. |

For REST-side issues see [`docs/troubleshooting.md`](../troubleshooting.md).

---

## 11. Cross-references

- [`docs/architecture.md`](../architecture.md) §§ 6–8 — workflow + activity surface, adaptive ingestion planning
- [`docs/configuration/environment.md`](../configuration/environment.md) §§ 2, 16 — Temporal + worker env vars; § 8 FAST LLM role; § 9 adaptive planning toggles
- [`docs/development/onboarding.md`](../development/onboarding.md) — local stack quickstart
- [`deploy/dev/README.md`](../../deploy/dev/README.md) — Docker compose walkthrough
- [`src/j1/orchestration/temporal/`](../../src/j1/orchestration/temporal/) — client + worker + retry primitives
- [`src/j1/orchestration/workflows/`](../../src/j1/orchestration/workflows/) — workflow source
- [`src/j1/orchestration/activities/`](../../src/j1/orchestration/activities/) — activity classes

---

## 12. End-to-end smoke test

A single opt-in pytest exists to confirm worker / API / Temporal
wiring on a fresh checkout. It boots `deploy/dev/docker-compose.yml`,
uploads a tiny Markdown fixture, and polls
`/ingestion-runs/{id}` until the run reaches a terminal status.

```bash
J1_E2E=1 .venv/bin/pytest -m e2e -s
```

Requirements:

- Docker Desktop / Compose v2 on `PATH`.
- The default `J1_E2E_API_BASE=http://localhost:8000` matches the
  port published by `deploy/dev/docker-compose.yml`. Override the
  env var if you've remapped ports.
- The test always tears down the stack (`docker compose down -v`)
  even on failure.

The test is **not** in the default `pytest -q` run; CI must opt in
explicitly because it requires Docker.
