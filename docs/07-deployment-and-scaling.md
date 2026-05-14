# 07. Deployment and Scaling

> Audience: ops + platform engineers.
> [Back to README](../README.md). See also
> [05-developer-onboarding.md](05-developer-onboarding.md),
> [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md).

## Deployment shapes

J1 ships in two officially-supported shapes today:

1. **Dev Compose**. The `deploy/dev/docker-compose.yml` stack —
   Postgres, Redis, MinIO, Qdrant, Neo4j, Temporal, API, worker,
   frontend, init containers. Used for local dev and small
   on-prem proofs of concept. Single host.
2. **Custom production**. Wire each service against your own
   managed infrastructure: managed Postgres + Redis + S3 + a
   Temporal cluster. There is no production-ready Helm chart or
   Terraform module yet — that lives in your platform.

A third shape ("dev mode on host, infra in docker") is documented
in [05-developer-onboarding.md](05-developer-onboarding.md) for
faster iteration.

## Worker deployment model

- The Temporal worker (`python -m deploy.dev.worker`) registers
  every activity class plus the two workflows
  (`ProjectProcessingWorkflow`, `BatchOrchestrationWorkflow`) on
  the `j1-processing` task queue.
- Workers are stateless from Temporal's perspective; they can scale
  horizontally.
- **Caveat**: the RAGAnything compile artifacts live on a shared
  filesystem path (the workspace). For a multi-host worker pool
  you need either:
  - A network filesystem all workers can read/write to
    (NFS, EFS, Filestore), or
  - A vendor backend for graph + vector that removes the on-disk
    workspace dependency entirely.
  Plain "spin up N pods with local disks" will produce
  cross-worker contention and missing artifact failures.

The API process (`python -m deploy.dev.api`) is stateless and can
sit behind any reverse proxy.

## Storage requirements

| Component | Local dev | Production direction |
| --- | --- | --- |
| Document registry, snapshot store, audit log | JSON/JSONL on disk under `J1_DATA_ROOT` | Same JSON/JSONL today; migration to Postgres-backed stores planned. |
| Postgres | Single container | Managed Postgres (RDS / Cloud SQL / Aurora). 16+ recommended. |
| Postgres FTS evidence index | Same Postgres instance | Same. Reuses `J1_METADATA_DSN`. |
| Redis | Single container | Managed Redis (ElastiCache / MemoryDB). Cache-only — no persistence required. |
| MinIO (S3-compatible) artifact bucket | `minio` container | S3 / GCS / OCI Object Storage. |
| RAGAnything workspace (LightRAG state, MinerU intermediates) | Docker named volume `raganything_workdir` | Shared filesystem (NFS / EFS) or vendor backend. |
| Temporal | `temporalio/auto-setup` container | Managed Temporal Cloud or self-hosted Temporal cluster on managed Postgres + Cassandra. |
| Qdrant / Neo4j | Containers | Optional — current code does not depend on them for the answer path; reserved for future vector / graph adapter work. |

Capacity rule of thumb (per document):

- **Bytes raw**: the file you uploaded.
- **MinerU intermediates**: roughly 2× the raw bytes during parse,
  cleaned afterwards.
- **Chunks + embeddings**: ~3-10 KB per 1 KB of source text.
- **Knowledge graph**: highly variable; LightRAG's graphml +
  vector store can be larger than the source for dense docs.
- **Postgres FTS**: ~1.5-2× the chunked text bytes.

## Temporal worker scaling direction

- One worker can host every activity. For larger fleets, separate
  workers by queue or by activity class:
  - Compile-heavy workers with extra CPU + memory.
  - Enrichment-only workers (cheap, scale wide).
  - Index / search workers (DB-bound, scale to match Postgres
    capacity).
- The current code registers everything on `j1-processing`. To
  separate queues, split `build_worker_spec` in `deploy/dev/_wiring.py`
  into multiple specs and run separate processes.
- `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` caps per-process concurrency.
  Tune against your hardware — MinerU OCR is CPU-heavy.

## LLM concurrency control

- The boot registry (`src/j1/compose.py::bootstrap_from_env`)
  registers LLM clients for the FAST, TEXT, and VLM roles.
- Each role takes `<ROLE>_MAX_CONCURRENT` env knobs (e.g.
  `J1_FAST_LLM_MAX_CONCURRENT`). Defaults are conservative.
- Per-document concurrency is bounded by
  `J1_RAG_MAX_CONCURRENT_DOCUMENTS` (default 4).
- There is no global hard cost ceiling. Cost telemetry lands in
  `CostBreakdownPayload` events on the audit log; downstream
  systems can enforce caps based on those.

## RAGAnything / compile workload considerations

- Compile is the most expensive activity.
  `COMPILE_ACTIVITY_TIMEOUT` is wide (minutes per PDF); the
  heartbeat is the real liveness check.
- Bound the compile cost per document by:
  - Tighter `compile_retry_min_text_chars` / `_min_chunks` so the
    retry doesn't fire on healthy parses.
  - `compile_max_attempts` (default 2).
  - Setting `assessment_failure_policy=fail_closed` if you'd
    rather fail loudly than fall back to a generic parse method
    that costs an LLM-vision pass.
- The two-phase compile flag (`two_phase_compile`) parks the
  workflow before compile dispatch — useful for high-cost approval
  workflows.

## Database requirements

- Postgres 14+ (16 recommended).
- The `j1.evidence_chunks` table is the FTS index. The schema is
  applied by `deploy/dev/postgres-init/` on first boot. Production
  uses the same SQL; mount it as an init script or run it via
  your migration tool.
- Use `pg_stat_statements` + the per-tenant `tenant_id` /
  `project_id` indexes to track per-customer load.
- The metadata DSN can be reused for evidence FTS (
  `J1_EVIDENCE_DSN` falls back to `J1_METADATA_DSN`). Splitting
  them is supported when the FTS workload outgrows the app DB.

## Artifact storage

- The artifact registry (`JsonArtifactRegistry`) is JSONL on disk
  under `J1_DATA_ROOT`. Blobs (chunk bodies, graph JSON,
  enriched assets) are stored in the configured artifact backend
  (`s3` / `local_fs`).
- Per-document path:
  `{workspace}/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}/{kind}/`.
- An MinIO `mc mb --ignore-existing` is run by the dev compose
  `minio-init` one-shot. In production, pre-create the bucket and
  set a lifecycle rule for old snapshot data if you want bounded
  storage cost.

## Horizontal scaling direction

| Layer | Approach |
| --- | --- |
| API | Stateless. Run N replicas behind a load balancer; sticky sessions not required. |
| Worker | Stateless from Temporal's perspective but tied to the shared workspace volume. See "Worker deployment model" above. |
| Postgres | Vertical first; read-replicas for the query path are possible once the workload justifies. |
| Redis | Single instance is plenty for the current cache use; cluster mode is a forward bet. |
| Temporal | Run as a separate cluster. Temporal Cloud is the easy default. |
| Storage | S3-compatible artifact store scales out of the box. RAGAnything workspace is the bottleneck — solve it via shared filesystem or vendor change. |

## Queue / worker separation

When you outgrow a single worker class, split:

- `j1-processing-compile` — compile-only, hosts on MinerU-capable
  pods (large memory, CPU).
- `j1-processing-enrich` — enrichment + index, smaller pods.
- `j1-processing-runs` — the RunsActivities class (snapshot
  promotion, telemetry). Lightweight; scale to keep latency
  bounded.

Update the worker registration in `deploy/dev/worker.py` (or your
production worker entrypoint) to register only the activity
classes belonging to that queue, and pass the matching
`task_queue` to the Temporal client.

## Environment variable strategy

- All env vars are read via `src/j1/config/runtime.py` and the
  per-component `Settings` dataclasses. Adding a new knob means
  adding it both to the loader and to `.env.example`.
- Production deployments should use a managed secret store and
  inject env vars at container start. The loader does not read
  filesystem secrets directly — adapters that need files (e.g. a
  vendor's service-account JSON) read paths from env vars.
- `J1_RUNTIME_PROFILE=prod` makes `RuntimeConfig.validate()` strict.
  Use it; dev fallbacks (sqlite-local metadata, in-memory cache,
  local-fs artifacts) will then be rejected.

## Observability and logging

- The Python services log to stdout in a structured format. Pipe
  to your platform's log aggregator.
- The audit log is JSONL on disk per tenant + project; production
  deployments should ship it to an external log store as well
  (the `EventPublisher` Protocol is the seam — wire a webhook or
  Kafka publisher).
- Temporal exposes workflow + activity history; the Temporal UI is
  the easiest first stop when debugging an ingestion.
- Per-stage metrics (timing, retry count, LLM token spend) are
  emitted as audit events. Wire a sink (Prometheus, Datadog,
  OpenTelemetry) by adding a publisher subscriber.

## Production-readiness checklist

- [ ] Managed Postgres provisioned with FTS schema applied.
- [ ] Managed Redis reachable from API + worker.
- [ ] S3-compatible bucket pre-created; lifecycle rule (or
      explicit retention) defined.
- [ ] Temporal cluster reachable; namespace created.
- [ ] LLM provider keys mounted via secret store; per-role
      concurrency knobs tuned.
- [ ] `J1_RUNTIME_PROFILE=prod` set; runtime validator passes.
- [ ] Shared filesystem (or vendor) chosen for RAGAnything
      workspace if running multi-worker.
- [ ] Worker liveness probe wired (HTTP `/healthz` if added, or a
      sidecar checking `temporal worker describe`).
- [ ] Audit-event publisher pointed at your event bus.
- [ ] Backups: Postgres dump + S3 object versioning enabled.
- [ ] Cost monitoring on LLM provider configured separately —
      J1 does not enforce hard caps.

When all the above are ticked, the deployment can host real
tenants. Anything left unchecked is technical risk acknowledged.
