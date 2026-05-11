# Ingestion pipeline (post-compile architecture)

The new ingestion pipeline runs five named stages per document. Each
stage produces a typed artifact that downstream stages — and the
final report — consume. Stage decisions live with the data the stage
already has; nothing in the pipeline reaches outside its inputs to
fetch context.

```
┌────────────────┐   ┌────────────────┐   ┌────────────────────┐
│ 1. Assessment  │ → │ 2. Compile     │ → │ 3. Post-compile    │
│ (cheap, no     │   │ (RAGAnything   │   │    analysis        │
│ LLM / OCR /    │   │ as black-box;  │   │ (rule-based; opt-  │
│ MinerU)        │   │ raw output     │   │ ional fast LLM)    │
└────────────────┘   │ preserved)     │   └─────────┬──────────┘
                     └────────────────┘             │
                                                    ▼
┌────────────────┐   ┌────────────────────────────────────────┐
│ 5. Finalize    │ ← │ 4. Enrichment (post-compile overlay)   │
│ + final report │   │ metadata · terminology · validation ·  │
│                │   │ text · classification · table · image  │
└────────────────┘   └────────────────────────────────────────┘
```

## Stage 1 — Assessment

**Goal:** produce an `InitialExecutionPlan` cheaply so the operator
can see what the run will attempt before any LLM / parser cost is
spent.

The plan carries:

- `domain_profile_id` — resolved from `domain_override` → workspace
  default → `general` fallback (no auto-detection at this stage).
- `enrichment_policy` (`auto` / `always` / `never`) and
  `require_enrichment_success` — surfaced from the resolved
  `DomainEnrichmentPolicy`.
- Cheap signals (page count, extension, document name).
- Candidate enrichment modules — what *could* run, not what *will*.

**Why this is deliberately cheap:** the assessor runs without LLM,
without OCR, without MinerU, without vision. The final decision
about whether to enrich at all comes AFTER compile evidence is
visible. Pre-compile heuristics about enrichment caused incorrect
gating in the legacy pipeline and were removed.

## Stage 2 — Compile

**Goal:** turn the source document into a normalised, typed compile
result the rest of the pipeline can consume.

The compile activity dispatches to RAGAnything (the default compile
engine) and treats it as a **black box**: J1 does not reach inside
the vendor's parsing, chunking, or content-extraction logic. RAGAnything
produces:

- The chunked corpus (registered as `chunk` artifacts).
- One or more `compile.image` artifacts when images are detected.
- `parsed_content_manifest.json` carrying the per-element manifest.
- A `compile_metrics` blob with parser-side counters.

The activity then normalises these into a typed
`NormalizedCompileResult` and persists it as the
`compile_result_summary` artifact. Raw vendor output is preserved on
disk + referenced by `raw_artifact_refs[]` — operators can always
deep-link back to it.

### Compile retry safety

The workflow runs a small bounded retry loop around the compile
activity (`compile_retry.evaluate_compile_quality`). On low-quality
output the workflow escalates the parse mode (fast → standard → deep)
and re-dispatches. Retry counts surface on
`final_ingestion_report.retry_counts.compile`. Retries DO NOT cascade
to later stages — a compile retry never triggers an enrichment retry.

## Stage 3 — Post-compile analysis

**Goal:** decide whether enrichment should run AT ALL, and which
modules to attempt. This is the only stage that gets to say "no".

The post-compile assessor consumes:

- The typed `NormalizedCompileResult` (chunks, detected tables/
  images, quality verdict, warnings).
- The active `DomainPack` (enrichment policy + force-recommended /
  optional / denied task lists).
- The Wave-5 closure signals (text sufficiency, layout complexity).

It produces a `PostCompileEnrichPlan` carrying:

- `overall_recommendation` ∈ `{SKIP, OPTIONAL, RECOMMENDED, REQUIRED}`
- `should_enrich: bool` (derived: True for OPTIONAL+).
- `recommended_tasks[]` / `skipped_tasks[]` / `blocking_issues[]`
- `require_enrichment_success: bool` — resolved from the domain
  policy + per-run override.

When the recommendation is SKIP, the workflow short-circuits the
enrichment stage with `build_skipped_enrichment_result()` so an
explicit "enrichment skipped" record reaches the final report —
silence here would be ambiguous with persistence failure.

## Stage 4 — Enrichment (post-compile overlay)

**Goal:** add domain-aware overlay data on top of the compile result
WITHOUT mutating it.

The enrichment stage runs a `CompositeEnrichmentRunner` over a fixed
module list:

| Module id | Source | What it adds |
|---|---|---|
| `metadata_enrichment` | rule-based projection | `DocumentMetadataOverlay` (target fields the operator wants extracted) |
| `terminology_enrichment` | rule-based projection | `TerminologyEntry[]` (glossary / retrieval normalisation) |
| `validation` | rule-based projection | `ValidationResult` (per-rule findings) |
| `text_enrichment` | LLM (text) | `retrieval_hints[]` + `confidence_notes[]` |
| `classification_enrichment` | LLM (text) | `ClassificationResult` |
| `table_enrichment` | LLM (text) | `TableSummary[]` |
| `image_enrichment` | LLM (vision) | `ImageSummary[]` |

The four LLM-backed modules are **legacy-compatible adapters** —
they implement the new `EnrichmentModule` protocol while preserving
the prompt + JSON-schema contracts the legacy `j1.enrichers`
implementations used. They consume:

- A `DomainPromptPack` for per-module prompt overrides (with the
  pack's `prompt_addon` prepended consistently).
- A shared `LLMCallLimiter` — bounds concurrent LLM calls across the
  whole worker. Per-image vision calls are individually bounded
  (Wave 11B).
- Typed analysis-client protocols (`TextAnalysisClient`,
  `VisionAnalysisClient`) — the bootstrap adapts production LLM
  clients onto these contracts.

The runner aggregates per-module `EnrichmentModuleOutcome` records
+ the typed overlay payloads into a single `EnrichmentResult`. The
result is persisted as the `enrichment_result` artifact and **never
overwrites** compile output: provenance links (`ProvenanceLink`)
point back to the source compile artifact ids so operators can
trace every overlay entry to its origin.

### Why overlay, not mutation

If enrichment fails or returns warnings, the raw compile output
must remain usable for retrieval. Treating enrichment as a typed
overlay (not a mutation) keeps the compile result the source of
truth and lets the final-status projection say
"completed_with_enrichment_warnings — raw compile output preserved"
honestly.

## Stage 5 — Finalization + report

The workflow's terminal stage persists the
[`final_ingestion_report`](./final-ingestion-report.md) — the typed
aggregate covering every prior stage's outcome. This is the FE's
preferred single fetch on the run-detail page.

When finalization itself fails after a clean pipeline, the workflow
still attempts a best-effort report write (`framework_final_status
= "failed"`, `failure_code = "FINALIZATION_FAILED"`) so the operator
sees the prior stages' state.

## Stage → Temporal mapping

Each stage maps to one or more Temporal activities. The
`ProjectProcessingWorkflow` orchestrates them; activity definitions
live in [`src/j1/orchestration/activities/processing.py`](../../src/j1/orchestration/activities/processing.py).

| Stage | Activity name |
|---|---|
| Assessment | `j1.processing.build_initial_execution_plan` |
| Compile | `j1.processing.compile` |
| Compile result normalize | `j1.processing.persist_compile_result_summary` |
| Post-compile analysis | `j1.processing.persist_post_compile_enrich_plan` (+ optional `j1.processing.fast_llm_consult_enrich`) |
| Enrichment | `j1.processing.run_enrichment_stage` |
| Finalize / report | `j1.processing.persist_final_summary` + `j1.processing.persist_final_ingestion_report` |

Per-document failures land as `_BusinessRejection` exceptions; the
workflow's terminal handlers map them onto stable failure codes
(see [Final ingestion report](./final-ingestion-report.md)).

## Stage → UI / SSE mapping

The FE consumes SSE `step.*` events and projects them onto the macro
stages via the client-side helper `deriveMacroEventType` (mirrors
the backend's `derive_macro_event_type` in
[`src/j1/runs/reporter.py`](../../src/j1/runs/reporter.py)):

| Workflow stage | SSE macro event |
|---|---|
| Compile | `compile.started` / `compile.completed` / `compile.failed` |
| Compile verification | `verification.started` / `verification.completed` / `verification.failed` |
| Post-compile analysis | `assess_enrichment.started` / `assess_enrichment.completed` / `assess_enrichment.skipped` |
| Enrichment | `enrich.started` / `enrich.completed` / `enrich.failed` / `enrich.skipped` |

The FE's [`PrimaryStatusPanel`](../../frontend/src/pages/run-detail/PrimaryStatusPanel.tsx)
projects the `final_ingestion_report.final_status` literal onto its
6-state UI surface (PENDING / RUNNING / COMPLETED /
COMPLETED_WITH_WARNINGS / FAILED / CANCELLED) — see
[`final-ingestion-report.md`](./final-ingestion-report.md) for the
full state table.

## Cross-cutting principles

1. **Raw output is sacred.** No stage mutates the compile result or
   the raw vendor files. Overlays carry provenance; consumers
   resolve overlay → compile id when they need to read back.
2. **Domain logic lives in data.** `DomainPack` + `DomainPromptPack`
   + `DomainEnrichmentPolicy` carry every per-domain decision.
   Workflow / activity / module code is domain-neutral — no
   `if domain == "civil"` branches.
3. **Decisions live with the evidence.** Pre-compile gating is gone;
   the post-compile assessor sees the actual compile output before
   recommending enrichment.
4. **Observability is loud.** Skipped modules emit explicit SKIPPED
   outcomes with operator-readable reasons; missing LLM clients,
   missing image bytes, and unreachable artifacts all surface in
   the final report.
