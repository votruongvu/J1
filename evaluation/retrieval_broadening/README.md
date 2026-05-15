# Retrieval Broadening Evaluation

End-to-end operator workflow for measuring whether alias-driven
query expansion improves retrieval. The tooling is all read-only,
runs against an existing project / snapshot, and emits structured
JSON reports that can be summarised, compared across runs,
validated in CI, or exported to Markdown for PR review.

## What This Evaluates

* **Retrieval behaviour.** How many candidate chunks come back,
  how many of them survive the evidence builder, whether
  enrichment-derived aliases broadened the search.
* **Trace diagnostics.** What expansion variants the orchestrator
  actually dispatched, which aliases matched, the pack-vs-
  enrichment provenance split.
* **Regressions across runs.** Side-by-side diff of two reports
  to surface query-level wins / losses.

## What This Does NOT Evaluate

* **Answer correctness / quality.** No LLM-based answer grading
  lives in this workflow.
* **Citation truthfulness.** Citations are inspected via the
  orchestrator trace; this workflow doesn't score them.
* **Statistical significance.** The summarizer + comparator use
  simple counts. No A/B significance testing model.
* **Cross-document graph traversal.** `UnsupportedGraphExpansion`
  remains the default — these tools intentionally do not measure
  graph expansion.

## Query Set Format

The harness consumes JSON or JSONL. The canonical shape:

```json
{
  "queries": [
    {
      "id": "alias_boq_001",
      "question": "What is BOQ?",
      "category": "alias",
      "notes": "Should test BOQ -> bill of quantities."
    }
  ]
}
```

Every entry must carry an `id` and a non-empty `question`.
Categories (`alias`, `canonical_to_alias`, `domain_synonym`, …)
are advisory but pinned by the validation test on
[`sample_queries.json`](sample_queries.json) — see that file
for the full vocabulary.

JSONL is equivalent: one JSON object per line.

## Running the A/B Harness

The harness runs each query twice — once with
`J1_QUERY_EXPANSION_ENABLED=false` (baseline) and once with it
`true` (variant) — and emits a structured JSON report.

```sh
python -m j1.tools.evaluate_retrieval_broadening \
  --tenant-id acme \
  --project-id alpha \
  --document-id doc-1 \
  --queries-file evaluation/retrieval_broadening/sample_queries.json \
  --output broadening-ab-report.json
```

The harness restores the env after each query so the surrounding
process state is unchanged. No artifacts are written, no
snapshots promoted, no run history mutated.

## Summarizing a Report

Compact terminal output:

```sh
python -m j1.tools.summarize_retrieval_broadening_report \
  --input broadening-ab-report.json
```

The summary lists scope, counts (queries up / down / same /
enrichment-aliases-not-applied), and the top suspicious cases
keyed on five heuristics — `decreased_retrieval`,
`enrichment_available_not_applied`, `has_warnings`,
`missing_counts`, `retrieval_up_evidence_flat`.

## Comparing Two Reports

Diff two existing reports to spot regressions and improvements
between PRs:

```sh
python -m j1.tools.compare_retrieval_broadening_reports \
  --base report_before.json \
  --candidate report_after.json
```

Pass `--format json` to emit a deterministic JSON payload (CI
consumers + the comparator are the audience). Query matching
keys on `query_id`; if a side has entries with no id, they
fall back to question-text match and the report records a
warning.

## CI Guardrails

The validator wraps the summarizer with opt-in guardrails. Every
flag is off by default — running it with no flags prints
`PASSED` and exits `0`.

```sh
python -m j1.tools.validate_retrieval_broadening_report \
  --input broadening-ab-report.json \
  --max-warning-count 0 \
  --fail-on-missing-counts \
  --fail-on-broadening-regressions
```

Exit codes:

* `0` — every configured guardrail passed.
* `1` — at least one guardrail failed; failure messages go to
  stdout for CI capture.
* `2` — the report file couldn't be read or isn't valid JSON.

Available guardrails:

| Flag | Fires when |
| --- | --- |
| `--max-warning-count <int>` | warning count exceeds the threshold |
| `--fail-on-missing-counts` | any query has null `retrieved_count` |
| `--fail-on-broadening-regressions` | any query's alias-broadening retrieved count decreased |
| `--min-queries-with-enrichment-aliases-applied <int>` | fewer queries hit enrichment aliases than the floor |
| `--min-query-count <int>` | too few queries ran overall |

## Markdown Export

Generate a PR-friendly summary:

```sh
python -m j1.tools.summarize_retrieval_broadening_report \
  --input broadening-ab-report.json \
  --format markdown \
  --output broadening-ab-summary.md
```

The Markdown shape is deterministic — same input produces
byte-identical output — so the file can be re-rendered before
review without spurious diffs.

## Interpreting Results

* **More retrieved chunks is NOT automatically better.** Alias
  broadening can pull in extra noise. Compare the retrieved
  count delta against the evidence count delta. A query flagged
  `retrieval_up_evidence_flat` means broadening surfaced more
  candidates but the evidence builder didn't promote any —
  inspect the rerank.
* **Evidence count matters but isn't a final answer score.** It
  reports how many chunks reached the LLM; whether the answer is
  correct still requires manual review or future LLM grading
  (out of scope).
* **Missing diagnostics are an instrumentation issue, not a
  retrieval issue.** A query flagged `missing_counts` usually
  means the orchestrator response shape is wrong, not that the
  retrieval failed. Fix the producer side first.
* **Scope safety is mandatory.** Aliases never leak across
  snapshots or documents — the loader enforces this. Reports
  that show cross-scope hits are a bug, not a feature.
* **Graph expansion is still unsupported.** The default service
  reports `graph_expansion_supported: false` honestly; do not
  fake it in any evaluation step.

## Adding New Queries

Append to `sample_queries.json`. Each entry MUST carry:

* `id` — unique, snake_case. Stable across PRs because the
  comparator matches on it.
* `question` — non-empty.

Recommended optional fields:

* `category` — one of the categories enumerated below.
* `notes` — operator-facing intent.

The category vocabulary (pinned by
`tests/test_sample_queries_retrieval_broadening.py`):

| Category | Purpose |
| --- | --- |
| `alias` | Acronym → canonical (e.g. `BOQ` → `bill of quantities`) |
| `canonical_to_alias` | Canonical → acronym (mirror) |
| `domain_synonym` | Domain term without an obvious alias pair |
| `multi_word_concept` | Multi-word phrase that shouldn't trigger expansion |
| `unrelated` | Off-topic question — verifies the system doesn't fabricate |
| `should_not_broaden` | Generic process query — expansion list should be empty |
| `scope_safety` | Snapshot-scoped query — verifies loader stays scoped |
| `lowercase_common` | Lowercase tokens that must not be mistaken for aliases |
| `negative_stoplist` | `PDF` / `HTTP` / `USA` etc. — stoplist filters them |
| `civil_engineering` | Worked examples for the civil-engineering pack |

## Known Limitations

* The harness restores `J1_QUERY_EXPANSION_ENABLED` after each
  query. Other env vars are not touched. If a deployment relies
  on additional env state for retrieval, the operator must set
  it before invoking the harness.
* `query_id` matching in the comparator is exact — renaming a
  query id reads as "removed + added" in the diff. Treat
  shipped ids as immutable.
* The summarizer's suspicious-case heuristics are intentionally
  simple. A query can be in the "suspicious" list and still be
  fine; the list flags candidates for manual review.
* No fixtures here ship customer data. The civil-engineering
  examples in `sample_queries.json` are generic.

## Full Workflow Cheat Sheet

```sh
# 1. Run the harness against the sample query set.
python -m j1.tools.evaluate_retrieval_broadening \
  --tenant-id acme \
  --project-id alpha \
  --document-id doc-1 \
  --queries-file evaluation/retrieval_broadening/sample_queries.json \
  --output broadening-ab-report.json

# 2. Inspect the report in the terminal.
python -m j1.tools.summarize_retrieval_broadening_report \
  --input broadening-ab-report.json

# 3. (Optional) Compare against a prior run.
python -m j1.tools.compare_retrieval_broadening_reports \
  --base report_before.json \
  --candidate broadening-ab-report.json

# 4. (Optional) Enforce CI guardrails.
python -m j1.tools.validate_retrieval_broadening_report \
  --input broadening-ab-report.json \
  --max-warning-count 0 \
  --fail-on-missing-counts

# 5. (Optional) Export Markdown for the PR description.
python -m j1.tools.summarize_retrieval_broadening_report \
  --input broadening-ab-report.json \
  --format markdown \
  --output broadening-ab-summary.md
```

## Future Work (Out of Scope for This Track)

These are deliberate non-goals — separate, larger initiatives:

* UI dashboard for browsing reports.
* Statistical-significance scoring across runs.
* LLM-based answer grading.
* Automatic test-query generation.
* Real `GraphExpansionService` implementation.
