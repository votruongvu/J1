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

A production worker typically wires its own processor maps:

```python
import asyncio
from j1 import (
    Bootstrap, ProjectProcessingWorkflow, DocumentProcessingWorkflow,
    WorkerSpec, build_client, run_worker, load_temporal_settings,
)

async def main():
    boot = Bootstrap.from_env().build()           # validates env + LLMs + providers
    temporal = load_temporal_settings()
    client = await build_client(temporal)

    spec = WorkerSpec(
        workflows=[ProjectProcessingWorkflow, DocumentProcessingWorkflow],
        activities=[
            *boot.processing_activities.all_activities(),
            *boot.lifecycle_activities.all_activities(),
            # ...
        ],
        max_concurrent_activities=int(
            os.environ.get("J1_WORKER_MAX_CONCURRENT_ACTIVITIES", "5"),
        ),
    )
    await run_worker(client, temporal, spec)

asyncio.run(main())
```

> **NEEDS VERIFICATION** — the exact `WorkerSpec` constructor
> parameters, including which activities to register, depend on the
> deployment's bootstrap. Confirm against
> [`src/j1/orchestration/temporal/worker.py`](../../src/j1/orchestration/temporal/worker.py)
> + [`deploy/dev/_wiring.py`](../../deploy/dev/_wiring.py) before
> copying.

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

The full project pipeline. State machine:

```
RUNNING ──┬─► PAUSED ──► RUNNING                          (pause / resume signal)
          ├─► WAITING_FOR_BUDGET_APPROVAL ──► RUNNING     (approve_budget signal)
          │                            └──► CANCELLED      (reject_budget / cancel signal)
          ├─► WAITING_FOR_REVIEW ──► RUNNING               (approve_review signal)
          │                    └──► CANCELLED              (reject_review / cancel signal)
          ├─► COMPLETED
          ├─► CANCELLED
          ├─► FAILED_RECOVERABLE
          └─► FAILED_FINAL
```

Stages:

1. Validate the project context.
2. List pending documents.
3. Compile each (with optional review gate after).
4. Enrich each (with optional review gate after).
5. Build the graph (with optional review gate after).
6. Build the search index (with optional review gate after).
7. Finalize.

Budget gates fire whenever recorded spend approaches a configured
ceiling; the workflow pauses and waits for `approve_budget`.

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
| Activity retry policy | `RetryPolicySpec(initial=1s, backoff=2.0, max_interval=60s, max_attempts=5)` | Per-activity override |
| Non-retryable errors | `DocumentNotFoundError`, unknown processor `kind` (raises Temporal `ApplicationError(non_retryable=True)`) | Add new types in your activity wrappers |
| Workflow execution timeout | _(Temporal default — unset by J1)_ | Pass via `client.start_workflow(..., execution_timeout=...)` |
| Activity start-to-close timeout | _(per-activity; defined in payload schemas)_ | Override at registration |
| Heartbeat | _(used by long-running activities — see `j1.heartbeat`)_ | NEEDS VERIFICATION — confirm which activities heartbeat |

Definitions live in
[`src/j1/orchestration/temporal/retries.py`](../../src/j1/orchestration/temporal/retries.py).
Override per-activity if a stage needs different behaviour (e.g.
graph build is expensive — give it more headroom).

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

Query: `get_status` returns the current `WorkflowStatus`
(`state`, `documents_total`, `documents_completed`, `error`,
`produced_artifact_ids`, …).

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
| `ApplicationError(non_retryable=True)` for `DocumentNotFoundError` | Document ID typo or wrong tenant / project — check the registry |
| `Unknown processor kind` | Activity received a `kind` not registered in the worker's processor map — verify the worker's `compilers=` / `enrichers=` / `graph_builders=` parameter |
| Workflow stuck in `WAITING_FOR_BUDGET_APPROVAL` | Check cost log + send `approve_budget` (or `reject_budget`) via Temporal client / REST |
| Workflow stuck in `WAITING_FOR_REVIEW` | Reviewer hasn't acted — see `GET /reviews` + `POST /reviews/{id}/decision` |
| Activity heartbeat timeout | NEEDS VERIFICATION — confirm activity heartbeat configuration |
| Web UI shows the workflow as `Failed` with `BusinessRejection` | Workflow ended in `FAILED_FINAL` state (terminal business rejection); investigate the upstream cause via the audit log + activity attempt history |

For REST-side issues see [`docs/troubleshooting.md`](../troubleshooting.md).

---

## 11. Cross-references

- [`docs/architecture.md`](../architecture.md) §§ 6–7 — workflow + activity surface
- [`docs/configuration/environment.md`](../configuration/environment.md) §§ 2, 14 — Temporal + worker env vars
- [`docs/development/onboarding.md`](../development/onboarding.md) — local stack quickstart
- [`deploy/dev/README.md`](../../deploy/dev/README.md) — Docker compose walkthrough
- [`src/j1/orchestration/temporal/`](../../src/j1/orchestration/temporal/) — client + worker + retry primitives
- [`src/j1/orchestration/workflows/`](../../src/j1/orchestration/workflows/) — workflow source
- [`src/j1/orchestration/activities/`](../../src/j1/orchestration/activities/) — activity classes
