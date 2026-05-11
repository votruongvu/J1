# Final ingestion report

The `final_ingestion_report` is the single end-to-end summary of an
ingestion run. It aggregates every prior stage's typed artifact
into one wire payload — the FE's preferred fetch on the run-detail
page, and the operator's authoritative reference when triaging.

## When it's written

The workflow persists the report at terminal — on the success path
AND on every failure path:

| Terminal path | When the report is written |
|---|---|
| Successful run | After `final_summary` is persisted (so the builder picks up the just-written summary) |
| `_BusinessRejection` (required-step failure) | Right before `_safe_finalize` runs |
| `ApplicationError` propagating from an activity | Same — best-effort before terminal `emit_run_terminal` |
| Unexpected `Exception` | Same |

This is best-effort: a failed report write is recorded in the
activity's `ArtifactActivityResult.error` but never blocks the
workflow's terminal exit. The FE falls back to per-artifact endpoints
when the report is unavailable (pre- runs, in-flight runs,
persist failures).

## Endpoint contract

`GET /ingestion-runs/{run_id}/final-ingestion-report`

```jsonc
{
 "runId": "run-1",
 "documentId": "doc-1",
 "documentName": "spec.pdf",
 "status": "completed", // or "unavailable"
 "unavailableReason": null, // operator-readable when status="unavailable"
 "artifactId": "art-fir-1", // present when status="completed"
 "report": {... typed FinalIngestionReport... }
}
```

`status="unavailable"` cases:

- `"final_ingestion_report_not_available — this run predates or hasn't reached terminal yet"` — legacy / in-flight runs
- `"final_ingestion_report artifact has an unexpected shape"` — malformed payload
- `"final_ingestion_report artifact exists but could not be read"` — file IO / permissions failure

## Payload shape

The `report` payload is the typed
`FinalIngestionReport.to_dict` shape ([`src/j1/processing/final_ingestion_report.py`](../../src/j1/processing/final_ingestion_report.py)):

```jsonc
{
 "schema_version": "1.0",
 "run_id": "run-1",
 "document_id": "doc-1",
 "document_name": "spec.pdf",
 "tenant_id": "acme",
 "project_id": "alpha",
 "domain_profile_id": "civil_engineering",
 "started_at": "2026-05-11T12:00:00+00:00",
 "completed_at": "2026-05-11T12:01:00+00:00",
 "duration_ms": 60000,
 "final_status": "completed_with_enrichment", // INGESTION_STATUS_* literal
 "final_status_reason": "enrichment overlay produced",

 "stages": [
 { "stage_id": "assessment", "label": "Preparing document", "status": "succeeded" },
 { "stage_id": "compile", "label": "Base compile", "status": "succeeded" },
 { "stage_id": "compile_result_normalization", "label": "Compile result summary", "status": "succeeded" },
 { "stage_id": "post_compile_analysis", "label": "Compile quality analysis", "status": "succeeded" },
 { "stage_id": "enrichment", "label": "Domain enrichment", "status": "succeeded" },
 { "stage_id": "finalization", "label": "Finalize", "status": "succeeded" }
 ],

 "compile_summary": {
 "compile_engine": "raganything",
 "compile_status": "succeeded",
 "chunks_count": 42,
 "page_count": 10,
 "extracted_text_chars": 15000,
 "detected_tables_count": 2,
 "detected_images_count": 1,
 "quality_verdict": "good",
 "retry_count": 0,
 "warnings": [],
 "errors": [],
 "artifact_refs": ["raw-1"] // raw compile output preserved
 },

 "enrichment_summary": {
 "should_enrich": true,
 "enrichment_status": "succeeded",
 "policy": "auto",
 "require_enrichment_success": false,
 "selected_modules": ["metadata_enrichment",...],
 "skipped_modules": [],
 "module_outcomes": [ /* one per module run */ ],
 "what_enrichment_added": ["Document metadata: 3 fields", "Terminology entries: 12"],
 "warnings": [],
 "errors": [],
 "retry_count": 0,
 "skipped_reason": null,
 "artifact_refs": []
 },

 "artifact_refs": {
 "initial_execution_plan": "art-init-1",
 "compile_result_summary": "art-cmp-1",
 "post_compile_enrich_plan": "art-pcp-1",
 "enrichment_result": "art-enr-1",
 "final_summary": "art-fs-1",
 "raw_compile_artifact_refs": "raw-1, raw-2"
 },

 "warnings": [],
 "errors": [],
 "retry_counts": { "compile": 0, "enrichment": 0 },
 "operator_notes": []
}
```

## Final status vocabulary (A–F)

`final_status` is the `INGESTION_STATUS_*` literal — eight
values cover every terminal:

| `final_status` | What happened | Compile output? | Enrichment result? | Raw compile usable? | FE shows | Operator should inspect |
|---|---|---|---|---|---|---|
| `completed_with_enrichment` | Everything succeeded | yes | yes (RUN) | yes | success badge · "completed with enrichment" | nothing required |
| `completed_without_enrichment` | Plan said SKIP | yes | yes (status=skipped) | yes | warning-toned badge · "completed without enrichment" + skip reason | the skipped reason on `enrichment_summary` |
| `completed_with_enrichment_warnings` | Enrichment ran with warnings / partial failure (and `require_enrichment_success=False`) | yes | yes (with warnings) | yes | warning badge · "completed with warnings" + per-module list | `module_outcomes[]` for FAILED / PARTIAL modules |
| `failed_compile` | Compile didn't produce a usable output | no (or empty) | no | n/a | error badge · "Base compile failed" | `compile_summary.errors` + `error_report` artifact |
| `failed_enrichment_required` | Compile ok, required enrichment failed | yes | yes (FAILED) | **yes** | error badge · "required enrichment did not complete; raw compile output preserved" | `enrichment_summary.errors` + the prior compile output |
| `failed_finalization` | Compile + enrichment ok, finalize failed | yes | yes | yes | error badge · "finalize failed after a successful pipeline" | `error_report` artifact |
| `failed` (unknown) | Failure without a recognised failure code | maybe | maybe | maybe | error badge · generic | `error_report` artifact |
| `cancelled` | Operator cancelled the run | partial | partial | partial | neutral badge · "run cancelled by operator" | timeline for cancel timing |

The mapping is pinned in
[`final_status.py::project_final_status`](../../src/j1/processing/final_status.py)
and the FE state machine mirror in
[`runState.ts::projectUiState`](../../frontend/src/lib/runState.ts).
The test suite enforces parity.

## Retry semantics

The report carries two retry counts:

- `retry_counts.compile` — attempts beyond the first compile try
 (0 = single-attempt success).
- `retry_counts.enrichment` — reserved for future limiter-driven
 module-retry accounting; currently always 0.

Key invariants:

- Compile retry is **separate** from enrichment retry. A compile
 retry never triggers an enrichment retry.
- Enrichment failure **never** re-runs compile.
- Optional enrichment failure **never** destroys the compile result
 (it's a typed overlay — see [Enrichment overlay](./enrichment-overlay.md)).
- Required enrichment failure → run lands at
 `failed_enrichment_required`. Compile output remains preserved
 and traceable via `compile_summary.artifact_refs`.
- Raw compile artifacts are **never** overwritten on retry —
 pinned by `test_raw_compile_artifacts_are_not_overwritten_on_enrichment_retry`.

## FE consumption

The FE's [`RunDetailPage`](../../frontend/src/pages/RunDetailPage.tsx)
fetches the report alongside the per-artifact endpoints. The
[`PrimaryStatusPanel`](../../frontend/src/pages/run-detail/PrimaryStatusPanel.tsx)
prefers the report:

```ts
projectUiStateFromReport(run, finalReport, enrichmentSignals)
```

1. **Report available** → use `report.final_status` +
 `report.final_status_reason` directly.
2. **Report null** (pre- / in-flight runs) → fall back to the
 per-artifact projection (`projectUiState(run,
 enrichmentSignals)` derives from `run.status` + `run.final` +
 `enrichment_result`).
3. **Both null** → run status enum alone.

The per-artifact panels (`InitialExecutionPlanPanel`,
`CompileResultPanel`, `EnrichmentResultPanel`) continue to fetch
their own data — the report is an aggregate overview, NOT a
replacement.

## Why `final_summary` still exists

`final_summary` is the older summary artifact. It carries
the executed-step table + artifact-kind counts + the failure-code
trio. The `final_ingestion_report` is the **preferred
aggregate**; `final_summary` remains for backward compatibility with
older runs and for the legacy `_persist_final_summary` activity.
Both are written at terminal; consumers should prefer
`final_ingestion_report` when available.

## Related pages

- [Ingestion pipeline](./ingestion-pipeline.md)
- [Enrichment overlay](./enrichment-overlay.md)
- [Artifact reference](../reference/artifacts.md)
