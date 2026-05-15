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

## Real-Project Workflow

The sample query set is generic. The point of Phase 4's evaluation
track is to run the harness against your **own indexed project**
and decide — from evidence — whether alias broadening helps or
hurts retrieval before approving heavier intelligence levels
(LLM rewrite, graph expansion).

This section is the operator runbook. A new engineer should be
able to follow it without asking; if a step is unclear, treat
that as a doc bug and patch this file.

### 1. Pre-flight checklist

Before pointing the harness at a project, confirm:

* **Project is ingested.** At least one document has reached the
  promoted state — visible in the Home dashboard's Document List
  as "Active" / has an active snapshot. The harness queries the
  active snapshot path; an empty project produces an empty report.
* **Representative documents are present.** Pick documents whose
  vocabulary covers the alias shapes you care about. For the
  civil-engineering pack: at least one document that defines
  acronyms inline (`bill of quantities (BOQ)`, `request for
  information (RFI)`). For other domains: documents that introduce
  domain terms with their abbreviations.
* **Alias artifact exists.** The producer runs at the tail of
  enrichment; check the Final Ingestion Report's
  `alias_summary.persisted` field on a recent run. If
  `persisted: false`, the document body didn't match the
  conservative patterns — pick a different document or accept the
  report will only exercise pack-static aliases.
* **`J1_ASSESSMENT_ENABLED=true`** (default). The assessment plan
  drives compile; without it, the run reverts to legacy parse
  behaviour and the alias producer doesn't run.
* **You are NOT enabling LLM rewrite or graph expansion as part
  of this tuning.** Those are Levels 2 and 3 of the
  [retrieval-intelligence roadmap](../../docs/12-retrieval-intelligence-roadmap.md);
  this workflow measures Level 1 only. Mixing them in defeats the
  whole point of evidence-before-intelligence.

### 2. Pick a query set

Two options:

* **Use the shipped sample.** Good for a first pass; covers all
  10 categories with 16 generic queries.
* **Author a project-specific set.** Copy `sample_queries.json`
  to a project-private location and replace the questions with
  ones operators actually ask. Keep the same category vocabulary
  so the report's category-level diagnostics stay consistent.
  Add 5-10 queries minimum.

The harness reads any file matching the same JSON shape (see
"Query Set Format" above). The tests in
`tests/test_sample_queries_retrieval_broadening.py` enforce the
shape for the shipped sample; copy them into your project's CI
if you ship a project-private set.

### 3. Run the harness

```sh
python -m j1.tools.evaluate_retrieval_broadening \
  --tenant-id acme \
  --project-id alpha \
  --document-id doc-civil-eng-spec \
  --queries-file ./my-project-queries.json \
  --output ./reports/2026-05-20-baseline.json
```

Three things to verify in stderr / stdout while it runs:

* `J1_QUERY_EXPANSION_ENABLED` is toggled per query (off → on)
  and restored at exit. No leaked env state.
* Per-query exceptions log a warning and the run continues —
  one bad query does NOT abort the whole report.
* Total query count in the final report equals the input set's
  count. Mismatch = some queries crashed silently.

### 4. Summarize and interpret

```sh
python -m j1.tools.summarize_retrieval_broadening_report \
  --input ./reports/2026-05-20-baseline.json
```

Read the SuspicionFlags section. The five flags map directly to
tuning decisions:

| Flag | What it suggests | What to look at |
| --- | --- | --- |
| `decreased_retrieval` | Broadening shrank retrieved count for this query. Either the alias is wrong OR retrieval scoring downranked the expanded results. | The query's `aliases` field in the JSON report. Verify the alias mapping is correct. |
| `enrichment_available_not_applied` | Enrichment aliases existed in the artifact but didn't match this query's terms. Often a tokenization or casing mismatch. | The query's question vs. the alias's `canonical` / `alias` fields. Lowercase the query and re-check. |
| `has_warnings` | Per-query warnings — usually "missing diagnostics". | Look upstream; the orchestrator likely skipped a stage. |
| `missing_counts` | Either baseline or variant has null `retrieved_count`. Instrumentation gap, not a retrieval problem. | The orchestrator response shape — fix the producer side first. |
| `retrieval_up_evidence_flat` | More candidates retrieved, no extra evidence promoted. Broadening added noise the reranker correctly filtered, OR the evidence builder is too aggressive. | Inspect a few sample queries with this flag — if the alias mapping is legit but evidence stayed flat, the rerank is doing its job. |

A small number of suspicious cases is normal. A flood (>30% of
queries flagged) signals a real tuning need.

### 5. Tune alias rules — only with evidence

Every tuning change must be motivated by a numbered finding in
the report, not a hunch. The tunable knobs live in
[src/j1/processing/enrichment_aliases.py](../../src/j1/processing/enrichment_aliases.py):

| Finding | Knob | Where |
| --- | --- | --- |
| A specific acronym is producing noise across queries (e.g. `IS`, `OF`) | `_STOPLIST_ALIASES` — add the acronym | `enrichment_aliases.py` ~L118 |
| Reports include over-long matches like `"SOMETHINGREALLYLONG"` mapping to bad canonicals | `_ALIAS_RE` length cap (currently `{1,7}`) | `enrichment_aliases.py` ~L96 |
| Single-uppercase-letter aliases (`A`, `I`) sneak through | `_ALIAS_RE` minimum length (currently 2 chars total) | `enrichment_aliases.py` ~L96 |
| `the bill of quantities` stored as canonical | `_LEADING_DETERMINERS` already strips this; verify the determiner is in the set | `enrichment_aliases.py` ~L129 |
| One pathological document emits hundreds of false positives | `_MAX_ALIASES_PER_ARTIFACT` (currently 64) | `enrichment_aliases.py` ~L91 |
| Confidence threshold downstream is too lax | `_DEFAULT_CONFIDENCE` (currently `0.86`) | `enrichment_aliases.py` ~L83 |

**Tuning discipline:**

* **No over-fitting.** Every change MUST have a test that holds
  for a synthetic example *outside* the document that motivated
  the change. The existing fixture corpus in
  `tests/test_enrichment_alias_*` is the canonical home for
  regression pins.
* **Don't add new alias categories.** The two pattern families
  (`ALIAS (canonical)` and `canonical (ALIAS)`) are deliberate.
  Adding a third (e.g. dash-separated, dictionary-style) belongs
  in a separate PR with its own design note.
* **Don't change the artifact payload shape.** Producer and
  consumer share `parse_alias_payload`; a shape change is a
  cross-PR migration, not a tuning.

### 6. Compare against a baseline

Before merging any tune, run the comparator against the
pre-tune report:

```sh
python -m j1.tools.compare_retrieval_broadening_reports \
  --base ./reports/2026-05-20-baseline.json \
  --candidate ./reports/2026-05-20-after-stoplist-IS.json
```

The diff splits queries into `regressions` (got worse) and
`improvements` (got better). A tune that produces no
regressions and at least one improvement is shippable. A tune
with both is a judgment call — read the rationale into the PR
description.

### 7. Worked example

A realistic session for the civil-engineering pack on a 30-page
spec PDF:

```sh
$ python -m j1.tools.evaluate_retrieval_broadening \
    --tenant-id acme --project-id civil-spec \
    --document-id doc-civil-001 \
    --queries-file ./eval/civil_queries.json \
    --output ./reports/civil-baseline.json
processed 24 queries in 4.7s
2 queries logged warnings (see report)

$ python -m j1.tools.summarize_retrieval_broadening_report \
    --input ./reports/civil-baseline.json
Retrieval-broadening A/B report summary
Scope: tenant=acme project=civil-spec document=doc-civil-001
Counts:
  total queries: 24
  more results : 14
  same         :  7
  fewer        :  3
  enrichment available, not applied: 2
SuspicionFlags:
  decreased_retrieval (3): query_id=alias_is_001, ...
  enrichment_available_not_applied (2): query_id=lowercase_input_001, ...

$ # Inspect alias_is_001 — turns out the doc says "in short (IS)"
$ # and the alias producer is treating `IS` as a domain acronym.
$ # Tune: add `IS` to _STOPLIST_ALIASES, re-ingest doc, re-run.

$ python -m j1.tools.evaluate_retrieval_broadening ... \
    --output ./reports/civil-after-IS-stoplist.json

$ python -m j1.tools.compare_retrieval_broadening_reports \
    --base ./reports/civil-baseline.json \
    --candidate ./reports/civil-after-IS-stoplist.json
Regressions: 0
Improvements: 3 (alias_is_001, alias_is_002, alias_is_003)
```

Ship the tune.

### 8. When NOT to tune

Some report patterns are NOT alias-rule problems. Don't reach
for `enrichment_aliases.py` if the report shows:

* **`missing_counts` dominating.** That's an orchestrator
  instrumentation bug. Fix the producer side; the alias rules
  are working.
* **`has_warnings` on every query.** Likely a wiring issue
  (eligibility resolver, snapshot scope). Inspect the warning
  text first.
* **Variant retrieval consistently higher BUT evidence
  consistently flat.** The reranker is filtering noise correctly.
  That's healthy behaviour, not a tuning trigger.
* **All queries unchanged.** The flag was off, OR no alias
  artifact exists for this document. Check the Final Ingestion
  Report's `alias_summary.persisted` field.

If the report shows none of the five SuspicionFlags above a
threshold (no flag firing on >5% of queries), broadening is
working as intended for this corpus. No tuning needed. Move on.

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
