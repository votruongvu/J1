# 03. Query Flow

> Audience: engineers + technical product owners.
> [Back to README](../README.md). See also
> [02-ingestion-flow.md](02-ingestion-flow.md),
> [04-core-data-model.md](04-core-data-model.md),
> [10-domain-configuration.md](10-domain-configuration.md).

## Surfaces

J1 exposes one production query path and two diagnostic surfaces.
All three live in `src/j1/validation/service.py` and are wired
through the REST adapter:

- **Manual Test Query** — `POST /ingestion-runs/{run_id}/test-query`.
  The interactive surface inside the Validation Tab. Returns the
  synthesised answer, retrieved chunks, citations, evidence flags,
  and a deterministic check report.
- **Dev query trace** — `POST /dev/query-trace`. The same
  orchestrator but returns the trace verbatim (plan, route results,
  evidence groups, gates). Operator diagnostic tool.
- **Native-debug query** — `POST /ingestion-runs/{run_id}/native-debug-query`.
  Calls LightRAG `aquery` directly with no BM25, no reranking, no
  coverage selection. Used to isolate whether a problem is
  indexing-side or pipeline-side.

There are also imported-test-case execute (`POST
/documents/{id}/imported-test-cases/execute`) and the public
`/answer` route — both delegate to the same orchestrator under the
hood.

## Scope: tenant → project → snapshot

Every query is anchored to `(tenant_id, project_id)` from the
request headers. Inside that scope, the query path resolves to one
of three `QueryScope` shapes:

- `WorkspaceScope` — every attached document in the project.
- `ActiveScope(document_id)` — one specific document. Conceptually
  "validate what users would see for this document right now."
- `RunScope(run_id)` — one specific run. Used by the diagnostic
  surfaces. The gated production path no longer maps `RunScope` to
  a real result set; callers that want run-level scoping must opt
  into `unchecked=True` (validation diagnostic only).

The eligibility resolver (`j1.query.eligibility`) translates a scope
into a set of `(document_id, snapshot_id)` pairs the orchestrator is
allowed to read. A document is eligible iff it is `attached`, has
`active_snapshot_id` set, and `lifecycle_status` is `stable`.

`snapshot_id` is the only visibility key. The legacy `run_id`-based
fallback was deleted in the snapshot-centered refactor.

## The SmartQueryOrchestrator pipeline

Every query — manual test query, imported test case execution, the
public `/answer` route — goes through
`SmartQueryOrchestrator.run(OrchestratorRequest)` in
`src/j1/query/orchestrator.py`. The orchestrator owns these stages:

1. **Intent classification + retrieval plan.**
   `j1.retrieval.intent_router` picks one of ~16 generic intents
   (fact lookup, list extraction, stage progression, summary,
   etc.) from verb/shape signals on the question. The plan tells
   downstream stages which evidence shapes to prefer and which
   gates apply.
2. **Multi-route retrieval.** The plan dispatches:
   - **RAGAnything native** (LightRAG hybrid `aquery`) — the
     primary answering engine. Per-snapshot workspace.
   - **Auxiliary BM25 / Postgres FTS** — produces supporting
     evidence and feeds the data-quality inspection panel. BM25
     does not drive the answer text unless an explicit fallback
     engine is selected (see "Engine modes" below).
   - **Artifact lookup** for graph / table / image artifacts that
     a structured intent needs.
3. **Evidence pack assembly.** Hits are grouped, scope-filtered
   against eligible snapshots, reranked, and trimmed to a budget.
   Each block is loaded with its real body via the chunk projector.
4. **Sufficiency gate.** Refuses to call the LLM on a thin or
   incoherent evidence pack. Failure surfaces as
   `final_status="evidence_insufficient"`.
5. **Synthesis.** A shape-specific prompt + only the selected
   evidence blocks. The synthesizer is `j1.query.answer_synthesizer.AnswerSynthesizer`.
6. **Citation binder.** The cited set is a *subset* of the
   selected pack. The orchestrator enforces this — answers cannot
   claim citations to chunks that weren't sent to the LLM.
7. **Answer-quality gate.** Refusals don't pass via aggregate
   override; short answers aren't auto-approved. The gate's
   verdict drives `validation_status` on the Manual Test Query
   response.

The trace (`QueryTrace`) carries plan, routes, candidates with
kept/dropped reasons, evidence groups covered/missing, the exact
blocks sent to the LLM, citations, and every gate result.
`/dev/query-trace` returns this verbatim; the manual-query response
embeds it under `debug.orchestrator_trace`.

## Engine modes (J1_QUERY_ENGINE)

The orchestrator can be configured with one of four engines. The
default is `lightrag_native` — BM25 stays out of the answer path
unless an operator explicitly opts in.

| Engine | Answer source | BM25 role | When to use |
| --- | --- | --- | --- |
| `lightrag_native` (default) | LightRAG `aquery` only | None | Production / normal. |
| `lightrag_native_with_quality_evidence` | LightRAG `aquery` | Auxiliary data-quality / evidence inspection | Diagnostics where you want both panels. |
| `bm25_quality_debug` | BM25 (debug only) | Primary | Inspect whether artifacts carry the expected lineage metadata. Never user-facing. |
| `hybrid_ab` | BM25 | Observability | A/B compare BM25 vs native. |

`enable_bm25_fallback` is a flag that lets the native engines use
BM25 *as the answer* when native fails. The response then carries
`bm25_participated_in_answer=true` so it's obvious in audit.

## How evidence is selected

- All retrieved candidates flow through the same eligibility
  resolver — the orchestrator never bypasses scope.
- The reranker runs against bodies (not previews), then the
  coverage selector picks blocks to satisfy the plan's evidence
  shape (e.g. "one block per stage in a stage-progression query").
- Boilerplate / repeated text is demoted at this layer.
- The final budget is enforced per the active profile (token cap
  + max blocks).

## How the LLM synthesises the answer

- The synthesizer prompt is shape-specific. A fact-lookup intent
  gets a different prompt than a stage-progression query.
- The model only sees the selected evidence — never the broader
  retrieved set.
- The synthesizer emits structured output: an answer plus a list
  of citations referencing block ids.
- The citation binder validates citations against the selected
  pack. Citations to blocks the model didn't receive are dropped.
- The LLM client is supplied by the boot registry. The TEXT role
  drives synthesis; the FAST role drives intent classification +
  the optional pre-synthesis consult.

## Citations and source references

Citations on the wire have the shape (see `CitationRecord`):

- `artifactId` — the chunk's owning artifact.
- `artifactType` — its kind (`chunk`, `graph_json`, etc.).
- `sourceDocumentId` — the document the chunk came from.
- `sourceLocation` — section path or page range when known.
- `chunkId` / `runId` — diagnostic.

`runId` on a citation is metadata; the visibility decision was
made earlier by the eligibility resolver. UI code that needs to
display "which snapshot this came from" should resolve via the
chunk's `metadata.snapshot_id`, not via run id.

## How domain context affects the query

The active project's domain pack (or the workspace default if no
pack matched at ingest time) supplies:

- **Retrieval hints**: candidate entity types, terminology, section
  names the rerank can favour.
- **Prompt addon**: a paragraph appended to every synthesizer
  prompt for that domain.
- **Per-enricher prompt overrides** (used during ingestion, not
  query, but the resulting artifacts feed retrieval).

Domain context is treated as a *testing lens / interpretation
hint* — never as evidence. The synthesizer must ground claims in
retrieved chunks, not in domain text.

See [10-domain-configuration.md](10-domain-configuration.md) for
how to configure or add a pack.

## Imported Test Cases — what they really do

The Validation Tab's Imported Test Cases section runs every
imported question through the same orchestrator (`RunScope` of the
document's latest succeeded run) and captures only the signals the
summary needs:

- Was the answer non-empty?
- Were citations / evidence chunks attached?
- Did the trace surface any out-of-scope evidence?
- Did the call raise?

It does **not** do LLM-based answer judging. The Manual Test Query
surface is the place for detailed inspection — the imported flow
is a quick confidence summary.

## Current limitations

Honest acknowledgement so reviewers calibrate expectations.

- **Per-run LightRAG workspace isolation.** Implemented, but a
  failed cleanup path can leave behind workspaces that the active
  document no longer points at. Audit before retention sweeps
  enforce harder semantics.
- **BM25 fallback is opt-in.** If LightRAG is down, the default
  engine returns `final_status="retrieval_insufficient"` instead
  of falling back. Set `J1_ENABLE_BM25_FALLBACK=true` to allow
  fallback in production-like setups.
- **No streaming answers yet.** Manual Test Query is synchronous.
  The wire is ready (the trace + answer cleanly separate) but the
  REST endpoint blocks until the orchestrator finishes.
- **Reranker is heuristic, not learned.** Coverage scoring uses
  generic verb/shape signals plus boilerplate demotion. It is
  good enough for the current corpus shapes but is a known area
  for replacement.
- **Multi-document answers don't yet aggregate cross-doc graphs.**
  Each document's LightRAG workspace is queried independently; the
  orchestrator unions the results but does not run cross-doc
  inference.

See [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md)
for the full list.
