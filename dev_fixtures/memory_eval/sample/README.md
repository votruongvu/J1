# Civil Engineering Sample — Memory Query Evaluation

Phase 6B sample dataset + workflow for running the Phase 6
memory-aware query A/B harness against a small, controlled J1
project. Companion to `j1.tools.run_memory_eval_sample` and
`j1.tools.evaluate_memory_query`.

## Contents

```
dev_fixtures/memory_eval/sample/
├── README.md                                     # this file
├── memory_query_eval.yaml                        # Phase 6 fixture
└── data/
    ├── CE-001_BOQ_quantity_schedule.md
    ├── CE-002_site_inspection_report.md
    ├── CE-003_NCR_corrective_action.md
    ├── CE-004_structural_calculation_summary.md
    └── CE-005_drawing_register_and_specification.md
```

Five linked civil-engineering documents describing a fictional
river-crossing bridge substructure package. Designed to exercise
the Phase 4 / 5A / 5B Knowledge Memory query path across BOQ,
inspection, NCR, calculation, and drawing/spec artifacts.

The documents share cross-references so a project-active query
benefits from Knowledge Memory's cross-document linkage:

```
CE-002 inspection Finding F-12
  → CE-003 NCR-007 (root cause: low concrete grade C30/37)
  → CE-001 BOQ rows 03.03-03.06 (concrete C30/37)
  → CE-005 specification 03 30 00
  → CE-005 drawing D-302 (Slab S-205 Reinforcement Plan, rev B)
  → CE-004 deflection check (C25/30 FAIL → C40/50 PASS)
```

## Fixture

`memory_query_eval.yaml` carries 12 queries across 12 categories:

| Category | Sample question | Phase tested |
|---|---|---|
| `risk` | What risks or issues were found across the project? | 5A expansion + 5B injection |
| `requirement` | Which requirements mention inspection or testing? | 5A + 5B |
| `action_item` | Which action items are still open? | 5A |
| `boq` | Which BOQ rows look incomplete or have quantity problems? | 5B (table refs deferred) |
| `inspection_finding` | Which inspection findings need action on slab S-205? | 5B |
| `ncr` | Any NCR issues and their corrective actions? | 5A alias expansion (NCR → Non-Conformance Report) |
| `drawing_revision` | Which drawings mention slab reinforcement? | 5B (image refs deferred) |
| `test_result` | What calculation checks failed acceptance criteria? | 5B |
| `calculation` | Show the deflection calculation results for slab S-205. | 5A |
| `general_summary` | What is this project mainly about? | base + summary-context entries |
| `source_lookup` | Where is concrete grade C30/37 specified? | 5B injection |
| `cross_document` | What links the inspection report to the NCR? | the load-bearing memory query — cross-document linkage |

The `expected_terms` in each fixture entry are phrases the
answer should contain when the memory-aware mode works — the
harness uses them as a quality proxy, NOT as ground-truth
labels. Adjust them when re-running against a different sample.

## Workflow

### 1. Stage the sample dataset

```bash
python -m j1.tools.run_memory_eval_sample \
    --data-root /tmp/j1-memory-eval-sample \
    --output-dir artifacts/memory_eval/civil_sample \
    --tenant-id <tenant> --project-id <project>
```

The script:

1. Copies `data/` + `memory_query_eval.yaml` to `--data-root`.
2. Writes `sample_project_manifest.json` listing each staged file.
3. Runs a real preflight against the dev runtime (env vars +
   `IngestionValidationService` construct probe).
4. Either prints exact manual next-steps OR — when
   `--run-evaluation` is set AND preflight is green — invokes
   the Phase 6 A/B harness as a subprocess.
5. Writes `sample_ingest_status.json` to the output directory so
   later automation can pick up where this run left off.

The script NEVER fabricates evaluation results: when preflight
fails it exits non-zero with a precise next-step.

### 2. Ingest the documents

After the sample files are staged on disk, the documents must be
ingested through the standard J1 pipeline. Use whichever route
the dev deployment supports:

* Upload each file via the dev UI (`/upload`) or REST
  (`POST /documents` with the file attached).
* Wait for compile to succeed (the run progress surface or
  `GET /documents/{id}` will reflect this).
* Trigger post-compile domain enrichment for each document
  (manual action, or set `J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT=true`
  on the worker to auto-rebuild memory after enrichment).
* Build or rebuild Knowledge Memory: manual action
  `build_knowledge_memory` via `POST /documents/{id}/manual-actions/build-knowledge-memory`
  or set `J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED=true` so the
  worker builds after every compile.

Verify each document has `knowledge_memory_status =
updated_with_domain_insights` via
`GET /documents/{id}/knowledge-memory` or the Document Detail
panel before moving to step 3.

### 3. Run the A/B harness

When all five documents are ingested + enriched + have current
Knowledge Memory artifacts, run the Phase 6 harness:

```bash
python -m j1.tools.evaluate_memory_query \
    --project-id <project> \
    --tenant-id <tenant> \
    --fixture /tmp/j1-memory-eval-sample/memory_query_eval.yaml \
    --output-dir artifacts/memory_eval/civil_sample
```

For a document-scoped run:

```bash
python -m j1.tools.evaluate_memory_query \
    --project-id <project> \
    --tenant-id <tenant> \
    --document-id <document-id> \
    --fixture /tmp/j1-memory-eval-sample/memory_query_eval.yaml \
    --output-dir artifacts/memory_eval/civil_sample/<document-id>
```

The harness writes `memory_query_eval_report.json` and
`memory_query_eval_report.md` under `--output-dir`.

### 4. Analyse

Apply the Phase 6A analysis prompt to the generated reports:

* Read the Markdown summary table — improved / unchanged /
  worsened counts.
* Read the per-query rows — verdicts, deltas, memory diagnostic
  blocks.
* Read the Recommendation banner (pinned vocabulary:
  `keep_disabled`, `enable_in_dev_only`, `enable_in_preview`,
  `enable_by_default_for_document_scope`,
  `enable_by_default_for_project_scope`, `needs_more_data`).
* Cross-check the JSON report for any safety violation codes:
  `direct_memory_citation`, `memory_provider_failure`,
  `latency_regression`.

## Runtime flags

The harness toggles these per-query inside its own process:

```env
J1_QUERY_KNOWLEDGE_MEMORY_ENABLED   # flipped per mode by the harness
J1_QUERY_EXPANSION_ENABLED          # flipped per mode by the harness
```

The following flags affect WHETHER memory exists at all — set
them on the worker BEFORE ingesting the sample documents so the
A/B run sees real memory artifacts:

```env
J1_KNOWLEDGE_MEMORY_AUTO_BUILD_ENABLED=true
J1_KNOWLEDGE_MEMORY_REBUILD_AFTER_ENRICHMENT=true
```

Phase 6B does NOT change any default in code or `env.example`.

## Known limitations

* The script's "assisted mode" is currently the only supported
  mode. Full automation (upload + compile + enrich + memory
  build + eval in one command) is deferred because it requires
  the dev REST API to be reachable from the script, which is a
  deployment concern. See `manual_next_steps()` in the script
  for the exact sequence to run by hand.
* `expected_terms` quality proxy is a substring match. It does
  not verify semantic correctness — operators should still read
  the answers in the Markdown report.
* `expected_artifact_types` checks against retrieved chunk
  metadata, not against actual evidence selection. A type that
  appears in retrieval but is dropped by the evidence builder
  still counts as "present".
* Phase 5B's table-ref + page-only-ref resolution is deferred —
  the BOQ and drawing-revision queries can only benefit from
  Phase 5A expansion until that lands.
