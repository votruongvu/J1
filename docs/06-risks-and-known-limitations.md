# 06. Risks and Known Limitations

> Audience: tech leads, reviewers, and stakeholders calibrating
> expectations.
> [Back to README](../README.md).

This document is intentionally honest. If the code and the intended
architecture have drifted, you should hear about it here before
hitting it in production.

## Architectural drift

### Snapshot-centered model: implemented, but lazy paths remain

- The structural invariant ("`active_snapshot_id` is the only
  visibility key") is enforced. `active_run_id` was deleted from
  `DocumentRecord`.
- The compile / enrich / index / promote pipeline threads
  `target_snapshot_id` end-to-end via `ProjectProcessingRequest`.
- Two snapshot-allocation paths still coexist:
  - Single-doc REST flows → allocate up-front at the REST boundary.
  - Bulk-job workflow → allocates per-document inside
    `_process_document` via the `allocate_target_snapshot`
    activity.
- The legacy lazy `get_or_create_for_run` on the snapshot service
  has been **deleted**. If you see it re-introduced, it's a bug.

### RunDetailPage tabs

- The Validation tab (formerly "Generated Test Cases") has been
  rewritten to be a compact imported-CSV summary + Manual Test
  Query console. The legacy LLM-judging pipeline is gone.
- The Manual Query Trace tab is now gated on terminal run status.
  Mid-flight runs see the tab disabled with an "Available once
  the document is processed." hint.
- "Continue from compiled result" and "Rebuild index" were
  removed from the run-level controls. The corresponding REST
  endpoints (`resume-from-checkpoint`, `rebuild-index`) still
  exist on the backend; the document-level reindex / manual
  Run Domain Enrichment entry points use them under the hood.
- `POST /ingestion-runs/{run_id}/refresh-enrichment` is
  **deprecated** in favour of
  `POST /documents/{id}/manual-actions/run-domain-enrichment`.
  The legacy route stays mounted but is flagged
  `deprecated: true` in the OpenAPI schema.

## Compile / RAGAnything

- RAGAnything is a single black box from J1's perspective. We do
  not control its internal retries, prompt budgets, or model
  rotation policy. Failures surface as compile failures.
- LightRAG's per-document workspace is on local disk. The current
  containers mount it from a named Docker volume; clustering or
  multi-host worker deployment requires a shared filesystem (NFS
  / EFS / Cloud Filestore) or a different vendor.
- MinerU + vision-LLM compile is expensive. The compile retry
  policy (`COMPILE_RETRY`) is 2 attempts by default — set it down
  for cost-conscious deployments.
- The compile activity heartbeats every 30 s. A worker pod with a
  hung MinerU process will be killed by Temporal but not by the
  process supervisor — wire a Kubernetes liveness probe on the
  worker if you can.

## Domain enrichment

- Two packs ship: `general` (no-op overlay) and `civil_engineering`
  (worked example). Other domains are not yet pre-built.
- The post-compile enrichment plan is rule-based, not learned.
  Tasks the plan declines are not retried.
- Per-enricher prompts come from `DomainPromptPack` overrides plus
  the pack-wide `prompt_addon`. There is no per-document override
  surface; if you need to change behaviour for one document, you
  need a code change.
- Fast-LLM consult is opt-in (`is_consult_warranted`). When
  enabled it can refine the plan; when disabled the rule-based
  plan ships unchanged.

## Query + citations

- The synthesizer prompts are shape-specific but not yet
  per-domain at the *prompt* level (the domain addon is appended,
  the core prompt is shared).
- Citations are validated to be a subset of the selected pack but
  the binder cannot detect "the model summarised X correctly but
  cited the wrong block".
- Answers are not streamed. The REST endpoint blocks until the
  orchestrator finishes. The wire shape is ready for streaming
  (the trace + answer are separable) but no implementation yet.
- "Multi-document" answers are unioned, not jointly inferenced.
  Each document's LightRAG workspace is queried independently;
  the orchestrator combines results but does not run cross-doc
  graph traversal.

## Snapshot / versioning

- Snapshot promotion is CAS-guarded but the *cleanup* of a failed
  candidate is best-effort. A worker crash between "snapshot
  marked READY" and "promote returned" leaves a candidate in
  READY without `promoted_at` — the cleanup sweep catches these
  but doesn't run automatically yet.
- The display-version label is per-document, per-day, one-based.
  Two reindexes started in the same UTC second will get the same
  prefix and a +1 suffix; the suffix is allocated against an
  in-memory counter, so a worker restart in the same second can
  produce a duplicate. Open issue.
- There is no on-disk versioning of the snapshot store schema.
  Field additions are tolerated (read with `.get(...)`), but
  type-changing migrations require reset + re-ingest.

## Multi-KB

- See [08-multi-kb-model.md](08-multi-kb-model.md). The model is
  designed; the wire-level "select KB by profile" route is not
  yet exposed. Multi-KB today means "multiple projects" — each
  with its own KB.
- Cross-project queries are not supported. The eligibility resolver
  refuses to admit documents from outside the request's
  `(tenant, project)`.

## Deployment / scaling

- The dev docker-compose stands up everything in one host. There
  is no production manifest yet (Helm / Terraform / Pulumi).
- Temporal workers are stateless and horizontally scalable, but
  the RAGAnything workspace lives on a shared volume — naive
  scale-out will produce contention. Plan for either:
  - Sticky activity queues per document (worker affinity), or
  - A vendor backend for graph / vector that lifts the on-disk
    workspace requirement.
- LLM concurrency is bounded by the `LLM_*_MAX_CONCURRENT` env
  knobs in the boot registry. The defaults are conservative; in
  production you'll likely raise them. Cost budgets are not
  enforced beyond the per-run `budget_*` knobs.
- Redis cache + Postgres metadata + MinIO artifacts + LightRAG
  workspace + Temporal cluster — that's five state stores. A
  production deployment must back each with managed storage. See
  [07-deployment-and-scaling.md](07-deployment-and-scaling.md).

## Concurrency / cost control

- `compile_retry_*` knobs control compile-stage retries. There is
  no global "stop spending money on this document" kill switch
  yet.
- `J1_WORKER_MAX_CONCURRENT_ACTIVITIES` caps per-worker concurrency.
  Set this carefully — MinerU OCR is CPU-heavy and a too-aggressive
  cap will starve the queue.
- `J1_RAG_MAX_CONCURRENT_DOCUMENTS` caps documents in flight
  through LightRAG simultaneously. Raise it cautiously.

## Validation surface

- The current Validation Tab is intentionally minimal: Manual Test
  Query + Imported Test Cases.
- Imported CSV execution captures only answer-present /
  sources-present / scope-ok / error per question. There is no
  LLM-based judge.
- The summary's "overall: good / needs_review / poor" rollup uses
  simple counts; thresholds are documented in
  `j1.validation.imported_test_cases._compute_overall`.

## Documentation drift

- This doc set replaces an older one. Code comments may still
  reference deprecated terms ("split mode", `active_run_id` as
  a visibility key, generated test cases, "rebuild index" as a
  primary action). When you spot one, prefer the doc set; the
  comments will be cleaned up over time.
- Memory files under `~/.claude/projects/-Users-vuvo-J1/memory/`
  describe the current state of the refactor and are kept in sync
  during sessions.

## Remaining legacy code worth deleting

These are not blockers but worth tracking:

- `_persist_run_terminal` still carries some `RunStatus.SUCCEEDED`
  vs `RunStatus.SUCCEEDED_WITH_WARNINGS` branching that could be
  collapsed.
- The REST endpoints `resume-from-checkpoint` and `rebuild-index`
  remain on the backend even though the UI no longer surfaces
  them. They're reachable via the document-level reindex flow
  and via external API; if you decide they're not needed, both
  routes + their handlers in `app.py` can be removed.
- The `Profile` concept is loaded by `ProfileLoader(DEFAULT_PROFILE_ID)`
  but most call sites use the default; there is no per-project
  profile override surface yet.
- `BatchOrchestrationWorkflow` (the batch parent workflow) is
  used by the multi-upload REST endpoint. It dispatches one
  per-document child workflow per file. If the multi-upload
  endpoint is retired, the batch orchestration goes with it.

## How to treat this list

- Anything in **Architectural drift** that says "still coexists"
  is a known gap, not a bug. Don't paper over it with comments;
  fix the gap or document it here.
- Anything in **Compile / RAGAnything** or **Query + citations**
  is a deliberate trade-off. Changes require product discussion.
- **Snapshot / versioning** entries are open issues. Fixing them
  is welcome.
- **Deployment / scaling** entries are pre-production work the
  team is aware of.
