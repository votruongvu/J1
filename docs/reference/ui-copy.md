# UI / operator copy guide

The ingestion pipeline ships with an operator-readable vocabulary
that the FE renders verbatim. This guide pins the preferred phrases
and lists wording the new architecture has explicitly retired.

## Preferred wording

| Stage / state | Operator-readable copy |
|---|---|
| Compile macro stage | **Base compile** |
| Compile-quality / post-compile analysis | **Compile quality analysis** |
| Enrichment macro stage | **Domain enrichment** |
| Enrichment overlay artifact | **Enrichment overlay** |
| Compile output preserved on disk | **Raw compile output preserved** |
| Required enrichment failed | **Required enrichment failed** / "the active domain policy requires enrichment to succeed" |
| Compile output exists despite enrichment failure | **Compile result remains available** |
| Skipped enrichment reason copy | **Enrichment skipped: \<reason\>** (where reason flows from the plan / policy) |
| Final-status: completed_with_enrichment | "completed with enrichment" |
| Final-status: completed_without_enrichment | "completed without enrichment" |
| Final-status: completed_with_enrichment_warnings | "completed with warnings" |
| Final-status: failed_compile | "Base compile failed" |
| Final-status: failed_enrichment_required | "required enrichment did not complete" |
| Final-status: failed_finalization | "finalize failed after a successful pipeline" |
| Module status badges | "Ran" / "Skipped" / "Partial" / "Failed" |

## Retired wording

These phrases describe the legacy pre-compile pipeline and **must
not** reappear in operator-visible surfaces (badges, banners, panel
copy, error messages, audit-log strings):

| Retired | Replace with |
|---|---|
| "split mode" / "SplitMode" / "split_mode" | (no replacement — concept removed) |
| "insert_content" | (no replacement — concept removed) |
| "pre-compile gating" / "pre_compile_gating" | "post-compile analysis" |
| "graph gating" / "index gating" | (no replacement — pre-compile gating is gone) |
| "pre-compile final decision" | "post-compile enrichment plan" |
| "IngestPlanner" / "old planning mode" | "InitialExecutionPlan" + "post-compile assessor" |
| "civil_engineering" / "RFI" / "BOQ" in builtin prompts | (move to `DomainPromptPack` per-domain) |

Both backend tests (`test__vocabulary`, the per-module
`*_has_no_legacy_vocabulary` tests) and FE tests
(`vocabulary.test.ts`) guard against the retired vocabulary
appearing in:

- `StatusDisplay` / `EventTypeDisplay` runtime labels
- Panel source files (`PrimaryStatusPanel`, `EnrichmentResultPanel`,
 `CompileResultPanel`, `InitialExecutionPlanPanel`)
- The `final_ingestion_report` payload (end-to-end test in
 `test__pipeline_hardening.py`)

## Naming the new macro events

The FE derives macro events from per-step `step.*` events via
`deriveMacroEventType` (mirrors backend `derive_macro_event_type`):

| Backend event | FE label |
|---|---|
| `compile.started` / `.completed` / `.failed` | "Base compile started/completed/failed" |
| `verification.started` / `.completed` / `.failed` | "Compile verification started/completed/failed" |
| `assess_enrichment.started` / `.completed` / `.skipped` | "Compile quality analysis …" |
| `enrich.started` / `.completed` / `.failed` / `.skipped` | "Domain enrichment …" |

## Module ids in operator surfaces

When the FE renders the enrichment summary's per-module list, the
adapter humanises module ids by splitting on `_` + title-casing
(`humaniseModuleId` in `EnrichmentResultPanel`):

| `module_id` | Rendered as |
|---|---|
| `metadata_enrichment` | "Metadata Enrichment" |
| `terminology_enrichment` | "Terminology Enrichment" |
| `validation` | "Validation" |
| `text_enrichment` | "Text Enrichment" |
| `classification_enrichment` | "Classification Enrichment" |
| `table_enrichment` | "Table Enrichment" |
| `image_enrichment` | "Image Enrichment" |

When adding a new module, pick a snake_case id that renders cleanly
under this transformation.

## Related pages

- [Final ingestion report](../architecture/final-ingestion-report.md)
- [Enrichment overlay](../architecture/enrichment-overlay.md)
