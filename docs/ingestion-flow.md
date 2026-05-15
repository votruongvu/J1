# Ingestion Flow

> Canonical end-to-end description of how a document goes from an
> uploaded file to queryable knowledge in J1.
>
> [Back to README](../README.md). See also
> [unified-memory-contract.md](unified-memory-contract.md),
> [04-core-data-model.md](04-core-data-model.md),
> [10-domain-configuration.md](10-domain-configuration.md).

## 1. Purpose

Ingestion is the act of turning an uploaded file into **queryable
knowledge** inside a project's knowledge base. The output of a
successful ingestion is a promoted `DocumentSnapshot` whose compile
artifacts (chunks, embeddings, graph workspace, index rows) the
query layer can read.

Two product invariants drive everything in this document:

1. **Compile produces basic queryability.** A document is queryable
   as soon as compile succeeds, the active run promotes, and the
   compile artifacts are durable. Domain Enrichment is **not**
   required for basic queryability.
2. **Domain Enrichment is optional, post-compile augmentation.** It
   runs as an explicit operator action after compile succeeds, can
   be disabled per deployment, and its output is exposed through the
   logical memory view as optional metadata — never as a substitute
   for compile output.

## 2. Core Concepts

| Term | Definition |
| --- | --- |
| **Tenant** | The hard isolation boundary. Every store, workspace, and audit row is keyed by `tenant_id`. |
| **Project / Knowledge Base** | The operator-facing workspace inside a tenant. The KB is exactly the project's attached, snapshot-promoted documents (see §6). |
| **Document** | A `DocumentRecord` for one uploaded file. Carries `knowledge_state` (`attached` / `detached` / `removed`) and `active_snapshot_id`. |
| **DocumentSnapshot** | The versioned knowledge state. Promoted atomically; the previous snapshot stays on disk for diagnostics but stops driving answers. |
| **Ingestion Run** | One processing attempt for a document. A trace identifier and lifecycle record — **not** a visibility key. |
| **Active Run** | The run whose `target_snapshot_id == document.active_snapshot_id`. The "current producing run" for the active snapshot. |
| **Active Snapshot** | The single snapshot the query layer is allowed to read for a given document. The canonical visibility key. |
| **Compile Artifact** | Any artifact compile (or downstream stages) produces under a snapshot — chunks, graph workspace, final report, evidence index rows. Every artifact carries `metadata.snapshot_id`. |
| **Unified Memory View** | The logical projection the query layer reads to answer "what knowledge is eligible for this scope?" (see [unified-memory-contract.md](unified-memory-contract.md)). |
| **Domain Enrichment Artifact** | An `enriched.*` artifact produced by the optional Domain Enrichment manual action. Augmentation only — does not satisfy queryability. |

## 3. End-to-End Flow

The target ingestion flow is:

```text
Upload Document
  → Create Document Record + Allocate Snapshot Candidate
  → Create Ingestion Run (target_snapshot_id pre-allocated)
  → Assessment Plan (cheap, deterministic; pypdf-based profile)
  → RAGAnything Compile (single black-box call)
  → Persist Compile Outputs (chunks, graph workspace, final report)
  → Promote Active Run / Active Snapshot (CAS-guarded)
  → Basic Queryable Knowledge Ready
  → (Optional) Manual Run Domain Enrichment
  → Enrichment Artifacts Available to UI and Query Layer
```

Step-level mechanics:

1. **Upload** — `POST /documents` or `POST /documents/{id}/ingest`
   accepts the file under `(tenant, project)` and writes it to the
   workspace `raw/` area. The intake registry appends a
   `DocumentRecord(status=PENDING, knowledge_state=attached,
   active_snapshot_id=None)`. A `DocumentVersion` is allocated for
   the content hash.
2. **Allocate candidate snapshot** — the REST handler creates the
   matching `IngestionRun` with `target_snapshot_id` pre-allocated.
   The candidate snapshot is in `BUILDING` state.
3. **Workflow dispatch** — `ProjectProcessingWorkflow` is started in
   Temporal with `ProjectProcessingRequest.target_snapshot_id` set.
4. **Assessment Plan** — see §4.
5. **Compile** — see §5.
6. **Persist outputs** — compile artifacts and the structured
   `final_ingestion_report` are stamped with `snapshot_id` and
   written to the artifact registry. Index rows are written with
   `(document_id, snapshot_id)`.
7. **Promote** — on terminal success, the runs activity CAS-promotes
   the candidate to `document.active_snapshot_id`. The previous
   snapshot is moved to `SUPERSEDED`. Cleanup of the previous
   snapshot's index rows is best-effort.
8. **Queryable** — the document is now queryable. The query layer
   resolves it through the Unified Memory View; see §6.
9. **(Optional) Run Domain Enrichment** — the operator may trigger
   `POST /documents/{id}/manual-actions/run-domain-enrichment` from
   the Document Detail page. See §7.

## 4. Assessment Plan

**Why it exists.** Compile is the expensive stage. The assessment
plan picks the cheapest viable parse + compile shape **before** the
worker pays for MinerU / vision-LLM cost. It also surfaces an
honest "what this profile can and cannot do" preview so the
operator can pick a heavier profile if the assessment looks thin.

**What it decides.**
- Parse method (default `mineru`, sometimes plain-text).
- Required vs optional capabilities (text / tables / images /
  scanned content).
- Selected `ExecutionProfile` (`minimum_queryable` / `standard` /
  `advanced`) — driven by deployment policy + operator pick.
- Whether the run should proceed at all (refusal → structured
  message).

**What it can NOT control.**
- RAGAnything's internal retry / prompt / model rotation policy.
  When the assessment surfaces a control that RAGAnything cannot
  honour, it must be reported as an
  **unsupported control warning** in the plan, never silently
  applied.
- LightRAG's internal entity-extraction inside compile — only the
  `minimum_queryable` profile suppresses it (via the no-op
  `llm_model_func` keystone). `standard` documents this trade-off
  on the profile card; it does not pretend to be cheap.

**Why assessment must not pre-parse.** Running MinerU just to
profile the file would double the cost of every ingest. The
assessment uses the deterministic `pypdf`-based profiler
(`DeterministicDocumentProfiler`) which reads page count,
has-images, has-tables, scanned-page indicators, and the
text-extractable ratio without invoking the heavy parse pipeline.

**FAST mode must NOT be reintroduced.** A `CompileMode.FAST` value
exists in the enum for legacy Temporal replay only; the planner
never emits it and the safety belt coerces any FAST it sees to
STANDARD. FAST was removed because its quality floor was below the
floor a queryable knowledge base requires. New code MUST NOT branch
on `CompileMode.FAST`.

## 5. Compile Stage

**RAGAnything is the single compile engine.** Compile is treated
as a black box:

- Input: `CompileActivityInput(scope, document_id, processor_kind,
  correlation_id, assessment_plan_payload, target_snapshot_id)`.
- Output: an `ArtifactActivityResult` listing the compile artifacts
  the activity registered.

Inside the black box, RAGAnything parses (MinerU by default),
chunks, embeds, and builds the per-snapshot LightRAG workspace at
`{workdir}/tenants/{t}/projects/{p}/documents/{d}/snapshots/{s}/`.
Chunks and a graph workspace land in the artifact registry stamped
with `snapshot_id`.

**Compile must not depend on Domain Enrichment.** The compile stage
reads `assessment_plan` + the source file only. Domain packs are
not consulted during compile; their `enrichment_policy` /
`extraction_hints` are used by the post-compile augmentation path,
not by compile itself. This is the load-bearing invariant: a
document is queryable when compile succeeds, regardless of whether
any domain pack ever runs against it.

**Supported compile modes.**
- `minimum_queryable` — text fast paths only; entity extraction
  is swapped for a no-op `llm_model_func`. Cheapest path; vector
  fallback only.
- `standard` — default. Full LightRAG compile, including its
  built-in entity extraction inside compile. No post-compile
  enrichment or graph build by default.
- `advanced` — `standard` plus the post-compile enrichment + graph
  build path enabled at run time.

The compile retry policy (`COMPILE_RETRY`, default 2 attempts)
lets the workflow re-run compile with a different shape if the
first pass produces a degenerate result.

`CompileResult` is normalised through `j1.processing.compile_result`
so downstream gates see the same shape regardless of which
processor produced it.

## 6. Unified Memory Contract

The query layer reads through a **logical projection**, not through
ad-hoc joins against `DocumentRecord`, `IngestionRun`, the artifact
registry, and the snapshot store. The projection is:

```text
UnifiedMemoryView = active snapshot
                  + active run
                  + compile outputs (artifacts + index refs)
                  + optional enrichment metadata
                  + queryable status + reason
                  + plan warnings + unsupported controls
```

**Rules** (full contract in
[unified-memory-contract.md](unified-memory-contract.md)):

- Physical storage stays split — `DocumentRecord`, `IngestionRun`,
  the artifact registry, and the snapshot store are not collapsed
  into one table. The projection composes them at read time.
- The query layer consumes the projection. It does not
  re-implement "find the active snapshot for this document".
- Only the active run and active snapshot are eligible by default.
  An old run can be inspected for audit but does not participate
  in active query.
- A deleted run's artifacts must be removed from every store
  (`artifact_registry`, evidence index, LightRAG workspace,
  enrichment artifacts).
- A removed document is invisible to the query layer regardless of
  what its snapshot store still contains.

## 7. Domain Enrichment

**Mental model.** Domain Enrichment is a separate manual action
that runs **after** the ingest workflow has finished and the
candidate snapshot has promoted. The default ingest path does not
run it. It exists to layer domain-specific metadata, claims,
aliases, and warnings over the already-queryable knowledge.

**Trigger.** `POST /documents/{id}/manual-actions/run-domain-enrichment`
from the Document Detail page. The button is:

- Visible only when `J1_ENABLE_MANUAL_ACTIONS` is true.
- Enabled only when the deployment has not disabled
  `J1_ENABLE_MANUAL_DOMAIN_ENRICHMENT`, the document has an active
  snapshot, and no other run is in flight against the document.
- Disabled with a clear reason in every other case.

**Mechanics.** The action allocates a candidate snapshot + an
`IngestionRun(run_type="run_domain_enrichment")` that REUSES the
active snapshot's compile artifacts via
`metadata.reused_compile_from_run_id`. No MinerU re-parse. The
workflow runs the enrichment activity against the reused compile
output, writes new `enriched.*` artifacts under the candidate, and
promotes the candidate on terminal success — same CAS contract as
re-index.

**What enrichment may produce.**
- Domain summaries.
- Extracted facts.
- Domain terms and aliases (see Phase-4 entity alias strategy).
- Detected standards / classifications.
- Quality warnings and notes.
- Recommended query expansions.

**What enrichment must NOT do.**
- It must not silently mutate the RAGAnything graph. The graph is
  RAGAnything-owned; enrichment writes its own artifacts.
- It must not be required for basic queryability. A deployment
  with enrichment turned off must still serve queries from
  compile-only knowledge.
- It must not auto-run as part of the ingest workflow. Auto-run is
  gated by `J1_DOMAIN_ENRICHMENT_AUTO_ENABLED=false` (default).

**Storage shape.** Enrichment artifacts are stamped with
`document_id`, `snapshot_id`, `domain_id`, and the producing
`manual_action_run_id`. The Unified Memory View exposes
enrichment_status + artifact refs as optional augmentation.

## 8. Query Readiness

A document is **queryable** when *all* of the following hold:

1. Compile succeeded for the run that produced the current active
   snapshot.
2. The document's `active_snapshot_id` is set.
3. `knowledge_state == attached` and `lifecycle_status == stable`.
4. The compile artifacts (chunks, graph workspace, index rows) for
   the active snapshot still exist in the artifact registry / index
   stores.

Domain Enrichment is **not** part of this list. The only way
enrichment can block queryability is when a domain policy
explicitly sets `require_enrichment_success=true` (rare,
domain-scoped, surfaced as a structured failure code on the run).

If any check fails the view reports `queryable=false` with a
`queryable_reason` the UI can render directly — never a generic
"unknown" state.

## 9. Failure and Retry Semantics

| Stage | Failure effect | Retry path |
| --- | --- | --- |
| Assessment | Run fails fast (`assessment_failure_policy=fail_closed`) or falls back to `settings.parse_method` (`fail_open`, default). | Re-index. |
| Compile | Run lands in `FAILED`. The candidate snapshot is left in `BUILDING`/`FAILED` state; the previous active is unchanged. | Re-index — fresh run, fresh snapshot, full parse from the original file. |
| Enrichment | The compile output is still queryable. Enrichment run lands `FAILED`. The candidate enrichment snapshot does NOT promote — the previous active stays live. | Re-run `POST /documents/{id}/manual-actions/run-domain-enrichment`. |
| Promotion CAS | The losing candidate stays in `READY` without `promoted_at`; cleanup sweep eventually removes it. | None — the winning candidate is correct; re-index again if needed. |

**Which failures block queryability:**
- Assessment hard-fail (no fallback).
- Compile fail of the producing run before any active snapshot
  exists. After a first successful active snapshot, a *later*
  compile failure leaves the previous good snapshot in place; the
  document remains queryable on the older snapshot.

**Which failures only show warnings:**
- Enrichment failure (unless a domain policy explicitly requires
  success).
- Plan-level "unsupported control" warnings.
- Cleanup failures of a superseded snapshot (artifacts linger but
  the active view is unaffected).

## 10. Observability

Expected logs / trace entries (every line is per-run and includes
`tenant_id`, `project_id`, `document_id`, `run_id`,
`target_snapshot_id`):

| Event | Where it fires |
| --- | --- |
| `ingest.run.created` | REST boundary at run dispatch. |
| `ingest.snapshot.allocated` | REST boundary when the candidate snapshot record is written. |
| `ingest.workflow.started` | After `JobStarter` returns the Temporal workflow id. |
| `j1.progress.assessment_plan_generated` | Once the assessment plan is built. |
| `j1.progress.assessment_plan_confirmed` | When the run leaves the confirmation gate. |
| `j1.progress.compile.{started,completed,failed}` | Compile attempt lifecycle. |
| `j1.progress.enrichment.{started,completed,failed}` | Enrichment activity lifecycle. |
| `j1.progress.snapshot.promoted` | After CAS succeeds. |
| `j1.progress.run.terminal` | Once the workflow reaches a terminal state. |
| `j1.ops.run.reindexed` / `j1.ops.run.deleted` | Operator audit events at the REST boundary. |

`final_ingestion_report` and `final_summary` artifacts capture the
per-stage outcome for the run history view.

## 11. Cleanup and Deletion Rules

**Re-index** (`POST /documents/{id}/reindex`).
- Allocates a fresh run + a fresh candidate snapshot.
- Runs the full workflow against the new snapshot from the
  original uploaded file.
- **Never reuses** old chunks / graph / enrichment artifacts. The
  compile activity treats this as a fresh parse.
- On success, atomically promotes the new snapshot; the previous
  snapshot is moved to `SUPERSEDED` and its index rows are stamped
  `search_state=superseded`.

**Refresh-enrichment** (`POST /ingestion-runs/{run_id}/refresh-enrichment`).
- **Deprecated** by the explicit Manual Actions surface. New
  surfaces should not render this as a primary control.
- Mechanically equivalent to a `run_domain_enrichment` manual
  action targeting the active snapshot's producing run.

**Clean-Up Run** (`POST /ingestion-runs/{id}/clean-up`).
- Removes a *non-active* run's artifacts, chunks, evidence rows,
  validation results, snapshots, and workspace files.
- Refuses to clean an active or in-flight run. The Clean Up button
  reads the eligibility endpoint and renders the reason verbatim.

**Remove Document** (`POST /documents/{id}/remove`).
- Gate-first: flips `lifecycle_status=removing`, clears
  `active_snapshot_id`, then runs synchronous hard cleanup.
- Removes the document, all of its runs, all of its snapshots,
  every artifact, every evidence row, and every enrichment record.
- The eligibility resolver refuses any document not in `stable`
  lifecycle, so a concurrent query cannot leak a removing
  document.

## 12. Known Limitations

These are honest constraints reviewers should expect. They are
**not** bugs.

- **RAGAnything fine-grained controls may be advisory only.** J1
  does not own the internal retry / prompt / model rotation policy
  of RAGAnything or LightRAG. Plan-level controls that the library
  cannot honour are surfaced as `unsupported_controls` warnings;
  they never silently fail.
- **Entity normalization is metadata-driven first.** Static aliases
  come from domain packs; runtime aliases come from enrichment
  artifacts. There is no graph-mutating entity resolver — adding
  one requires a stable RAGAnything API the library does not
  currently expose.
- **Physical storage stays split.** The Unified Memory View is a
  logical projection. `DocumentRecord`, `IngestionRun`, the
  artifact registry, and the snapshot store remain separate.
- **Per-document LightRAG workspace cleanup is best-effort.** A
  worker crash between "snapshot READY" and "promote returned" can
  leave a candidate workspace on disk; the cleanup sweep catches
  these but does not run automatically yet.
- **Multi-document answers are unioned, not jointly inferenced.**
  Each document's LightRAG workspace is queried independently and
  the orchestrator combines results — cross-doc graph inference is
  not yet implemented.
- **Domain Enrichment is run-specific.** It targets the active
  snapshot's compile output. Enriching a stale (non-active) run is
  not exposed as a primary action.

See [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md)
for the broader risk list.
