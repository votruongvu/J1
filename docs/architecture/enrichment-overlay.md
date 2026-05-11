# Enrichment overlay

Enrichment is **a post-compile overlay**, not a mutation. The
compile result is the source of truth for retrieval; the
`EnrichmentResult` adds typed records (`ClassificationResult`,
`TableSummary`, `ImageSummary`, terminology entries, metadata
fields, retrieval hints, confidence notes, validation findings)
that consumers branch on, with `ProvenanceLink`s back to the
source compile artifact.

```
 ┌───────────────────────┐
 │ NormalizedCompileResult│ (immutable source-of-truth)
 └───────────┬───────────┘
 │
 ▼
 ┌───────────────────────┐
 │ CompositeEnrichmentRunner │
 │ ( skeletons + │
 │ LLM adapters) │
 └───────────┬───────────┘
 │
 ▼
 ┌───────────────────────┐
 │ EnrichmentResult │ (typed overlay)
 │ ├ module_outcomes[] │
 │ ├ classification │
 │ ├ table_summaries[] │
 │ ├ image_summaries[] │
 │ ├ retrieval_hints[] │
 │ └ provenance refs │
 └───────────────────────┘
```

## Why overlay, not mutation

If enrichment fails or returns warnings, the raw compile output
must remain usable for retrieval. Treating enrichment as a typed
overlay (not a mutation) keeps the compile result the source of
truth and lets the final-status projection say
"completed_with_enrichment_warnings — raw compile output preserved"
honestly. Concretely:

- `NormalizedCompileResult` is a frozen dataclass — adapters can't
 mutate it.
- `raw_artifact_refs[]` carries the IDs of the on-disk vendor
 output. Compile bytes are never rewritten by enrichment.
- Every typed overlay record carries a `ProvenanceLink` pointing
 back to the source compile artifact, so a downstream consumer
 that wants to read the raw evidence can.
- A failed enrichment → `EnrichmentResult.status = "failed"` →
 the workflow still completes (unless
 `require_enrichment_success = True`) with the compile output
 intact.

## Module protocol

Every module — skeleton or LLM-backed — conforms to the
`EnrichmentModule` Protocol in [`enrichment_overlay.py`](../../src/j1/processing/enrichment_overlay.py):

```python
@runtime_checkable
class EnrichmentModule(Protocol):
 module_id: str
 def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]:...
 def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome:...
```

`can_run` is the skip gate; `run` produces the structured outcome
(`status` ∈ `RUN / PARTIAL / SKIPPED / FAILED`). LLM-backed
adapter modules additionally expose `get_typed_outputs` so the
runner can merge typed records onto the aggregated
`EnrichmentResult` after `run` returns.

See [Adding an enrichment module](../guides/adding-an-enrichment-module.md)
for the recipe.

## Per-module guarantees

| Property | Where it's pinned |
|---|---|
| Can't mutate `NormalizedCompileResult` | `NormalizedCompileResult` is a frozen dataclass + test `test_wrappers_do_not_mutate_compile_result` |
| Can't mutate raw compile artifacts | `_handle_artifact_output` writes a new artifact; tests assert per-byte hash + size unchanged after enrichment retries |
| Every typed output carries provenance | `test_text_wrapper_carries_provenance_in_outcome`, `test_classification_wrapper_carries_provenance_on_result`, `test_table_wrapper_carries_provenance_on_each_summary`, `test_image_summary_image_id_is_artifact_backed` |
| LLM calls bounded by shared limiter | `test_factory_threads_shared_limiter_into_every_wrapper` + `test_per_image_limiter_acquires_once_per_image` |
| Skip reasons are operator-readable | every wrapper's `can_run` returns `(False, "...reason...")` and tests assert specific phrasing |
| Failure produces FAILED outcome, not raise | `test_text_wrapper_records_failed_outcome_on_llm_exception` (the runner also catches raises) |

## Prompt resolution precedence

LLM-backed adapters resolve their prompts through one helper:

```python
resolve_module_prompt(
 domain_pack=ctx.domain_pack,
 prompt_field="text_enrichment_prompt",
 builtin_default=DEFAULT_TEXT_ENRICHMENT_PROMPT,
)
```

The helper's precedence is:

1. `domain_pack.prompt_pack.<prompt_field>` if set (non-empty).
2. `builtin_default` (the adapter's `_BUILTIN_PROMPT`).

Then `domain_pack.prompt_addon` is prepended to whichever base
prompt won. So the final prompt sent to the model is:

```
<prompt_addon>\n\n<override or builtin>
```

This is the **only** prompt-resolution path. Adapters must not
read domain-specific strings any other way — pinned by tests
listed in [`domain-profiles.md`](./domain-profiles.md#what-must-not-happen).

## Shared LLM-call limiter

A single `LLMCallLimiter` is constructed by the bootstrap
and threaded into every LLM-backed adapter. The limiter:

- Bounds concurrent worker LLM calls by `J1_ENRICHMENT_MAX_CONCURRENT_LLM_CALLS`.
- Wraps each `text_client.extract` call in
 `TextEnrichmentModule` / `ClassificationEnrichmentModule` /
 `TableEnrichmentModule`.
- Wraps each per-image `vision_client.analyze_image` call inside
 the `PerImageVisionAdapter` (per-image bounding).

When the limiter is `None` (operator disabled it), adapters call
the client directly. Tests:

- `test_factory_threads_shared_limiter_into_every_wrapper`
- `test_per_image_limiter_one_image_one_acquisition`
- `test_per_image_limiter_acquires_once_per_image`
- `test_failed_per_image_call_still_releases_limiter_and_continues`
- `test_shared_limiter_reaches_text_and_image_paths`

## Skip behavior

Each adapter short-circuits when its input is missing:

| Module | Skip reason |
|---|---|
| `text_enrichment` | `"no text LLM client configured"` · `"compile produced no extracted text"` · `"compile produced no chunks"` |
| `classification_enrichment` | `"no text LLM client configured"` · `"compile produced no extracted text"` |
| `table_enrichment` | `"no text LLM client configured"` · `"compile detected no tables"` |
| `image_enrichment` | `"no vision LLM client configured"` · `"compile detected no images"` |

Skipped → `EnrichmentModuleOutcome.status = SKIPPED` with the
reason. The runner aggregates skip outcomes onto
`EnrichmentResult.module_outcomes[]` and the final report's
`enrichment_summary.module_outcomes`.

## Failure behavior

| Failure shape | Outcome | Run-level effect |
|---|---|---|
| Adapter's LLM call raises | `EnrichmentModuleOutcome.status = FAILED` + `errors=(str(exc),)` | If `require_enrichment_success=True` → `failed_enrichment_required`. Otherwise → `completed_with_enrichment_warnings`. |
| Adapter returns no parseable output (e.g. classifier without `category`) | `EnrichmentModuleOutcome.status = PARTIAL` + warning | Bumps `warnings[]`; doesn't fail. |
| Single image vision call raises | Per-image entry carries `metadata.error`; batch continues | Image module remains RUN (other images succeeded) or PARTIAL. |
| Runner-level exception inside `module.run` | `EnrichmentModuleOutcome.status = FAILED` (runner's defensive catch) + error message | Same `require_enrichment_success` semantics. |

## Final-report integration

The runner's output is persisted as the `enrichment_result`
artifact. The [`final_ingestion_report`](./final-ingestion-report.md)
projects the run-level summary off of:

- `module_outcomes[]` → `enrichment_summary.module_outcomes`
- `EnrichmentResult.status` → `enrichment_summary.enrichment_status`
- `EnrichmentResult.skipped_reason` (or older `reason`) →
 `enrichment_summary.skipped_reason`
- `document_metadata` field count + `terminology[]` length →
 `enrichment_summary.what_enrichment_added`
- `module_outcomes[].output_artifact_refs` →
 `enrichment_summary.artifact_refs`

## Related pages

- [Adding an enrichment module](../guides/adding-an-enrichment-module.md)
- [Domain profiles](./domain-profiles.md)
- [Final ingestion report](./final-ingestion-report.md)
