# Artifact reference

The new ingestion pipeline persists several typed artifacts per
run. Each one has a stable kind, a documented producer + consumer,
and (where exposed) a REST endpoint with a predictable
`status="unavailable"` sentinel for pre- runs.

## Pipeline artifacts

| Kind | Producer | Consumer | Endpoint | Notes |
|---|---|---|---|---|
| `initial_execution_plan` | `build_initial_execution_plan` activity (pre-compile) | post-compile assessor; final report; FE | `GET /ingestion-runs/{id}/initial-execution-plan` | Cheap pre-compile plan. Domain pack + enrichment policy + candidate modules |
| `parsed_content_manifest` | `compile` activity (via RAGAnything bridge) | post-compile assessor; FE Compile Result panel | (via `/artifacts/{id}/content`) | Raw vendor manifest — preserved unchanged |
| `compile.image` | `compile` activity | `WorkspaceImageBytesProvider` → image enrichment module | (via `/artifacts/{id}/content`) | Raw image bytes for vision enrichment. Preserved unchanged |
| `chunk` | `compile` activity | indexer; FE chunks tab | `GET /ingestion-runs/{id}/chunks` | Compile chunks |
| `compile_result_summary` | `persist_compile_result_summary` activity | post-compile assessor; final report; FE Compile Result panel | `GET /ingestion-runs/{id}/compile-result` | Typed `NormalizedCompileResult` |
| `compile_strategy_report` | `persist_compile_strategy_report` activity | FE Assessment Plan + Compile Strategy panels | (via `/artifacts/{id}/content`) | Per-attempt timeline + final quality verdict |
| `post_compile_enrich_plan` | `persist_post_compile_enrich_plan` activity | enrichment activity; final report; FE Enrich Plan panel | `GET /ingestion-runs/{id}/enrich-plan` | Typed `PostCompileEnrichPlan` |
| `enrichment_result` | `run_enrichment_stage` activity | final report; FE Enrichment Result panel | `GET /ingestion-runs/{id}/enrichment-result` | Typed `EnrichmentResult` overlay |
| `validation_report` | per-stage validators | FE Validation tab | (via artifacts list) | Per-stage validation rollup |
| `stage_validation_report` | `validate_stage` activity | workflow gate; FE | (via artifacts list) | Per-stage validation contract result |
| `final_summary` | `persist_final_summary` activity | FE Overview tab; legacy aggregate | (via artifacts list) | aggregate — preserved for back-compat |
| `final_ingestion_report` | `persist_final_ingestion_report` activity | FE PrimaryStatusPanel + RunDetailPage; operator CLI | `GET /ingestion-runs/{id}/final-ingestion-report` | **Preferred aggregate** |
| `error_report` | `persist_error_report` activity | FE error banner | (via artifacts list) | Failure-path detail |

## Endpoint contract

All typed-artifact endpoints share one wire shape:

```jsonc
{
 "runId": "run-1",
 "documentId": "doc-1",
 "documentName": "spec.pdf",
 "status": "completed" | "unavailable",
 "unavailableReason": "operator-readable string" | null,
 "artifactId": "art-..." | absent,
 "plan": {... } | null // (or "report" for final-ingestion-report)
}
```

The envelope is wrapped in the standard `{requestId, data, meta}`
J1 API response.

### `status="unavailable"` cases

| Endpoint | Reason copy |
|---|---|
| `/initial-execution-plan` | `"No initial execution plan was persisted for this run yet. The run may predate the pre-compile planner, profiling may have failed, or persistence failed."` |
| `/compile-result` | `"No compile result summary was persisted for this run yet. Compile may not have completed, the run may predate the normalizer, or persistence failed."` |
| `/enrich-plan` | `"Enrich plan is not available for this run yet."` |
| `/enrichment-result` | `"No enrichment result was persisted for this run yet. Enrichment may have been skipped by policy, the run may predate the enrichment overlay, or persistence failed."` |
| `/final-ingestion-report` | `"final_ingestion_report_not_available — this run predates or hasn't reached terminal yet"` |

The FE state machine branches on the exact `"unavailable"` literal;
operators see the `unavailableReason` copy verbatim in the panel.

### `status="completed"` shape

The `plan` (or `report`) field carries the typed payload —
`InitialExecutionPlan.to_payload` / `NormalizedCompileResult.to_payload`
/ `PostCompileEnrichPlan.to_payload` /
`EnrichmentResult.to_payload` /
`FinalIngestionReport.to_dict` respectively. The
`artifactId` field is the durable registry id the FE deep-links
to via `/ingestion-runs/{id}/artifacts/{artifactId}/content`.

## Backward compatibility for old runs

Runs that completed before each artifact's introduction:

- `final_ingestion_report` — pre- runs: endpoint returns
 `"final_ingestion_report_not_available"`. FE falls back to the
 per-artifact projection (`projectUiState(run, enrichmentSignals)`).
- `enrichment_result` — pre- runs: endpoint returns
 `"unavailable"`. FE renders the panel's neutral
 `"Enrichment overlay is not available for this run yet."` state.
- `compile_result_summary` — pre- runs: same. FE falls back
 to reading the compile metrics off events / older surfaces.
- `initial_execution_plan` — pre- runs: same.
- `post_compile_enrich_plan` — pre- runs: same.

The FE never crashes on missing artifacts. Per-panel state machines
(loading / unavailable / ready / error) absorb the absence.

## Raw artifact references

`NormalizedCompileResult.raw_artifact_refs[]` carries the registry
ids of the raw vendor output (the `parsed_content_manifest`,
`compile.image` files, etc.). The final report surfaces these as
`compile_summary.artifact_refs[]` so operators can:

1. Read `final_ingestion_report.compile_summary.artifact_refs`.
2. For each id, fetch `GET /ingestion-runs/{id}/artifacts/{artifactId}/content`.
3. Get the raw vendor bytes back — never mutated, never overwritten
 by enrichment retries.

This is the durable trace path. Pinned by tests:

- `test_raw_compile_artifacts_are_not_overwritten_on_enrichment_retry`
- `test_image_enrichment_does_not_mutate_raw_compile_artifacts`
- `test_artifact_refs_include_raw_compile_pointers`

## Related pages

- [Final ingestion report](../architecture/final-ingestion-report.md)
- [Ingestion pipeline](../architecture/ingestion-pipeline.md)
- [REST API](../rest-api.md)
