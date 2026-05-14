# Manual retest protocol

This file documents the manual end-to-end check an operator runs
against a real ingestion run to confirm the Re-Index / Resume contract
holds and the validation surface still answers questions correctly.
Goal: every result surface a user sees is scoped to a single,
immutable `run_id` — no cross-run leakage, no silent fallback to
prior-run state.

## Setup

1. Upload a fresh document and let the ingestion workflow reach a
   terminal `succeeded` / `succeeded_with_warnings` state.
2. Capture `document_id` and the run record's `run_id` (= the
   document's `active_snapshot_id`'s `created_by_run_id`).
3. Pick one in-domain question the operator already knows the answer
   to (sanity check) and one out-of-domain question (negative
   control).

## What to capture per question

The manual-query endpoint is `POST /ingestion-runs/{run_id}/test-query`.
For each `(question, validation_scope)` pair, capture:

| Field | Source |
|---|---|
| `response.synthesized_answer` | Final answer the operator sees in the UI |
| `response.validation_status` | `passed` / `failed` / `partial` |
| `response.run_id` | Must match the `run_id` you POSTed against |
| `response.debug.query_engine` | Always `"smart_query_orchestrator"` |
| `response.debug.orchestrator_final_status` | `passed` / `failed` |
| `response.debug.orchestrator_message` | Human-readable summary |
| `response.debug.orchestrator_trace` | Full retrieval / synthesis trace |
| `response.retrieved_chunks[*].run_id` | Every chunk's run_id must equal `response.run_id` |
| `response.citations[*].run_id` | Same — every citation must come from the same run |

## Scenarios to run

### Scenario A — `validation_scope="run"`

Operator says "validate THIS run's output." Retrieval is locked to
`RunScope(run_id)`.

**Expected:**
- `response.retrieved_chunks` only contains chunks tagged with the
  requested `run_id`.
- `response.citations` only references artifacts produced by the
  requested `run_id`.
- `response.validation_status` is `passed` for the in-domain question.

### Scenario B — `validation_scope="active"` against the active run

Operator says "validate what users currently see." Retrieval resolves
`ActiveScope(document_id)` →
`Document.active_snapshot_id` →
`DocumentSnapshot.created_by_run_id` → `RunScope`.

**Expected:**
- Same answer + citations as Scenario A (the active run is this run).
- The resolver returned the `created_by_run_id` of the document's
  active snapshot — not a sentinel.

### Scenario C — `validation_scope="active"` after a re-index

Operator re-indexes the document via
`POST /documents/{document_id}/reindex`, lets the new run finish, and
re-runs the same question with `validation_scope="active"`.

**Expected:**
- `response.run_id` matches the **new** run's id (not the prior).
- Every chunk / citation `run_id` matches the new run.
- No chunk / citation from the prior run appears anywhere in the
  response.

### Scenario D — Native LightRAG debug

Hit `POST /ingestion-runs/{run_id}/native-debug-query` with the same
question. Pure `rag.aquery` — no BM25, no orchestrator gates.

**Expected:**
- `response.workspace_path` ends with
  `…/snapshots/{tenant}/{project}/{document_id}/{snapshot_id}` — i.e.
  the per-snapshot directory, not a per-run or document-wide
  workspace.
- `response.workspace_id` matches that snapshot id.
- `response.native_query_used` is `true` (native reachable) **or**
  `false` with a clear `native_query_failed_reason` (e.g.
  `native_provider_not_wired` if the deployment doesn't have
  LightRAG configured).

## Reading the results

A "good" outcome across A–D:

- Same factual answer in A and B (run-scoped and active-scoped both
  point at the same run).
- A and B reference the **same** chunk / citation ids.
- After re-index, C never surfaces a prior-run id anywhere.
- D's `workspace_path` includes the **snapshot_id**.

A "needs attention" outcome:

- A and B produce different answers → the active-scope resolver is
  not pointing at the run you think it is. Check:
  - `Document.active_snapshot_id` in the registry.
  - The corresponding snapshot's `created_by_run_id` in the
    snapshot store.
- C's response includes any `run_id` that isn't the new one → tab
  endpoints are leaking prior-run data; inspect
  `_resolve_run_artifacts` and the eligibility resolver.
- D's `workspace_path` is `null` → `target_snapshot_id` isn't threaded
  through the validation service for this run. Check
  `IngestionRun.target_snapshot_id` for the run record.

## What this does NOT check

- Re-index from a missing source file — that's a unit test
  (`tests/test_reindex_isolation.py::test_H_*`); the runtime check
  fails fast with HTTP 409 before any workflow starts.
- Internal Temporal retries inside the same run — those are activity-
  level fault tolerance, not user-visible affordances.
- Run-level resume / re-index / rebuild-index endpoints — those return
  HTTP 410 by contract; covered by
  `tests/test_rest_resume_from_checkpoint.py`.
