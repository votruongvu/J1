# Q4 / Q7 retest protocol

This file documents the manual retest the operator runs against a real
ingestion run after the debug-payload refinement landed. Goal: every
mode produces a comparable debug snapshot so a single question can be
A/B'd across modes without inferring "what came from where."

## Scope

- The test uses **one** validation question (Q4 or Q7) against **one**
  fresh ingestion run.
- Each mode is exercised with the SAME question on the SAME run.
- Captured fields are read off `response.debug` of the manual-query
  endpoint (`POST /ingestion-runs/{run_id}/test-query`).
- No code changes between modes — only the `J1_QUERY_PROVIDER_MODE`
  env var changes.

## Modes to run

1. `J1_QUERY_PROVIDER_MODE=bm25_primary` (production default)
2. `J1_QUERY_PROVIDER_MODE=hybrid_ab` (observability — BM25 stable, native side-by-side)
3. `J1_QUERY_PROVIDER_MODE=rag_native_primary` with `J1_RAG_NATIVE_QUERY_FALLBACK_TO_BM25=true` (default)

Restart the API process between each (env vars are read at service
construction time inside `deploy/dev/_wiring.build_validation_service`).

## What to capture per run

For each mode + question:

| Field | Where from |
|---|---|
| `response.synthesizedAnswer` (FE) / `response.synthesized_answer` (API) | The final answer the operator sees |
| `response.debug.answer_provider` | One of `"bm25"`, `"native"`, `"bm25_fallback"` |
| `response.debug.evidence_provider` | `"bm25"` (always today) |
| `response.debug.citation_provider` | `"bm25"` (always today) |
| `response.debug.native_query_used` | bool — was the native call SUCCESSFUL |
| `response.debug.bm25_query_used` | bool — was BM25 executed |
| `response.debug.fallback_used` | bool — did the dispatcher use BM25 as a fallback |
| `response.debug.native_query_failed_reason` | str / null — populated when native failed |
| `response.debug.citation_augmentation_used` | bool — true when answer is from one provider and citations are from another |
| `response.debug.bm25_answer_preview` | First 240 chars of BM25's deterministic answer |
| `response.debug.native_answer_preview` | First 240 chars of native's answer (or null) |
| `response.debug.selected_evidence_preview` | First 240 chars of the top evidence block sent to the LLM |
| `response.debug.selected_evidence_kinds` | Sorted list of kinds in the selected evidence |
| `response.debug.selected_evidence_count` | Count of evidence blocks reaching the LLM |
| `response.debug.requested_top_k` | What the FE/caller asked for |
| `response.debug.candidate_top_k_used` | What the engine was actually asked for |
| `response.debug.raw_candidate_count` | What the engine returned (= `fts_returned_count`) |
| `response.debug.evidence_max_blocks` | Final cap on evidence to the LLM |
| `response.debug.query_anchors_in_evidence` | bool — does the selected evidence contain ANY query term |
| `response.debug.scope_run_id` | The run id retrieval was scoped to |
| `response.checks` (filtered to required) | All five required isolation / presence checks |

## Expected shape per mode

### `bm25_primary`

```
answer_provider              = "bm25"
native_query_used            = false
bm25_query_used              = true
fallback_used                = false
citation_augmentation_used   = false
native_answer_preview        = null
bm25_answer_preview          = "<the BM25 answer ≤ 240 chars>"
```

`synthesized_answer` is produced by the local LLM synthesizer from
BM25-built evidence blocks. Citations come from BM25 sources.

### `hybrid_ab`

```
answer_provider              = "bm25"          (BM25 is stable)
native_query_used            = true if native succeeded
bm25_query_used              = true
fallback_used                = false
citation_augmentation_used   = false           (no native answer overrides)
native_answer_preview        = "<native preview>"  (debug-only)
bm25_answer_preview          = "<BM25 preview>"
```

`synthesized_answer` follows the BM25 path. The native answer is
visible in `native_answer_preview` only — operators read it to
compare.

### `rag_native_primary` (native succeeded)

```
answer_provider              = "native"
native_query_used            = true
bm25_query_used              = true             (citations augmentation)
fallback_used                = false
citation_augmentation_used   = true             (answer ≠ citations source)
native_answer_preview        = "<native answer>"
bm25_answer_preview          = "<BM25 preview>" (debug only)
```

`synthesized_answer == native_answer_preview` (truncation aside).
Citations are still from BM25. The groundedness judge runs against
native answer + BM25 citations — flag elevated false-positives are
possible here; see `citation_augmentation_used=true` as the warning.

### `rag_native_primary` (native failed → BM25 fallback)

```
answer_provider              = "bm25_fallback"
native_query_used            = false
bm25_query_used              = true
fallback_used                = true
native_query_failed_reason   = "<reason>"
citation_augmentation_used   = false
native_answer_preview        = null
bm25_answer_preview          = "<BM25 preview>"
```

`synthesized_answer` is whatever the local synthesizer produced from
BM25 evidence (or null if synthesis is disabled). Same as
`bm25_primary` for diagnostic purposes — but the `fallback_used=true`
flag tells the operator the native path was attempted first.

## Reading the result

A "good" outcome for the same question across all three modes:

- Answers convey the same factual claim (wording may differ; the
  groundedness judge tells you if they're consistent).
- `query_anchors_in_evidence` is `true` in every mode (the selected
  evidence contains at least one question term).
- All five required validation checks pass.
- `selected_evidence_count` ≤ `evidence_max_blocks` (cap held).
- `raw_candidate_count` ≥ `candidate_top_k_used` (no early starvation).

A "needs attention" outcome:

- `answer_provider` differs but answer content also differs → native
  vs BM25 disagree. Inspect `native_answer_preview` /
  `bm25_answer_preview` to triage.
- `query_anchors_in_evidence` is `false` → BM25 didn't surface
  question-relevant material, even if the question succeeded. Bump
  `candidate_top_k` and re-run.
- `native_query_used=true` but `selected_evidence_kinds` is empty →
  native answer is grounded in nothing visible; verify the
  workspace path is the per-run scoped one.
- `fallback_used=true` more than ~5% of validation runs → native
  is unstable; investigate `native_query_failed_reason` distribution
  before promoting `rag_native_primary` to default.

## What this does NOT change

- No architecture changes (`HybridQueryEngine` untouched).
- No `aquery_data` integration yet — citations still BM25-sourced.
- No new env vars beyond the ones documented in `.env.example`.
- Run/project isolation behaviour is unchanged.
