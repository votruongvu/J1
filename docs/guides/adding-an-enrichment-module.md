# Adding an enrichment module

This guide walks through adding a new `EnrichmentModule` —
something like "graph-entity extraction" or "compliance check"
that consumes the compile result + domain pack and produces a
typed overlay record.

For the protocol surface see
[`enrichment-overlay.md`](../architecture/enrichment-overlay.md).

## The shape

Every module conforms to one Protocol:

```python
@runtime_checkable
class EnrichmentModule(Protocol):
    module_id: str
    def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]: ...
    def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome: ...
```

LLM-backed modules additionally expose `get_typed_outputs()` so
the runner can merge typed records (e.g. `ClassificationResult`,
`TableSummary`, retrieval hints) onto the aggregated
`EnrichmentResult` after `run()` returns.

## Step 1 — Define the module id

Stable + lowercase + snake_case. Should match the post-compile
plan's task ids when the assessor recommends/denies your module.
Add it to the recognised set in
[`enrich_assessment.py`](../../src/j1/processing/enrich_assessment.py):

```python
TASK_GRAPH_ENTITY_EXTRACTION = "graph_entity_extraction"
```

## Step 2 — Pick the typed output

Decide where your output lands on the `EnrichmentResult` surface
([`enrichment_overlay.py`](../../src/j1/processing/enrichment_overlay.py)):

| What you produce | Field |
|---|---|
| Document-level classification | `EnrichmentResult.classification_result: ClassificationResult` |
| Per-table summary | `EnrichmentResult.table_summaries[]: TableSummary[]` |
| Per-image caption | `EnrichmentResult.image_summaries[]: ImageSummary[]` |
| Terminology entries | `EnrichmentResult.terminology_map[]: TerminologyEntry[]` |
| Validation findings | `EnrichmentResult.validation_result: ValidationResult` |
| Document metadata key-values | `EnrichmentResult.document_metadata_overlay` |
| Free-form retrieval hints | `EnrichmentResult.retrieval_hints: tuple[str, ...]` |
| Confidence notes | `EnrichmentResult.confidence_notes: tuple[str, ...]` |

If your output doesn't fit any existing field, add a new typed
record to `enrichment_overlay.py` first — keep it frozen + carry
`ProvenanceLink`. Extending the wire shape is a coordinated FE +
final-report change; do it in its own slice.

## Step 3 — Write the module

Place the module in
[`src/j1/processing/`](../../src/j1/processing/). Skeleton modules
(pure projection) live in
[`enrichment_modules.py`](../../src/j1/processing/enrichment_modules.py);
LLM-backed adapters live in
[`legacy_enricher_modules.py`](../../src/j1/processing/legacy_enricher_modules.py).

Minimal skeleton example:

```python
@dataclass(frozen=True)
class GraphEntityModule:
    module_id: str = "graph_entity_extraction"

    def can_run(self, ctx: EnrichmentContext) -> tuple[bool, str]:
        if self.module_id not in ctx.enrich_plan.recommended_tasks:
            return False, "plan did not recommend graph entity extraction"
        if (ctx.compile_result.extracted_text_chars or 0) <= 0:
            return False, "compile produced no extracted text"
        return True, "ready"

    def run(self, ctx: EnrichmentContext) -> EnrichmentModuleOutcome:
        # ... do the work; produce typed records ...
        provenance = ProvenanceLink(
            source_artifact_id=(
                ctx.compile_result.raw_artifact_refs[0]
                if ctx.compile_result.raw_artifact_refs else None
            ),
            source_kind="compile",
            relation="extracted_from",
        )
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason="extracted N entities",
            source_refs=(provenance,),
            model_usage=ModelUsageRecord(),
        )
```

LLM-backed modules use `_LegacyWrapperBase` for prompt resolution
+ limiter routing. Example skeleton:

```python
class GraphEntityEnrichmentModule(_LegacyWrapperBase):
    module_id = "graph_entity_extraction"
    _PROMPT_FIELD = "graph_entity_prompt"      # add to DomainPromptPack
    _BUILTIN_PROMPT = "Extract entities ..."   # generic; no domain vocab
    _OUTPUT_SCHEMA = {...}

    def can_run(self, ctx):
        if self._text_client is None:
            return False, "no text LLM client configured"
        return True, "ready"

    def run(self, ctx):
        self._typed_outputs = {}
        started = perf_counter()
        prompt = self._resolve_prompt(ctx.domain_pack)
        try:
            parsed, usage = self._llm_call(
                self._text_client.extract,
                prompt, self._OUTPUT_SCHEMA,
                metadata={
                    "module_id": self.module_id,
                    "document_id": ctx.document_id,
                },
            )
        except Exception as exc:
            return EnrichmentModuleOutcome(
                module_id=self.module_id,
                status=EnrichmentModuleStatus.FAILED,
                reason=f"graph entity LLM call failed: {type(exc).__name__}",
                duration_ms=int((perf_counter() - started) * 1000),
                errors=(str(exc),),
            )
        # ... project parsed → typed records ...
        # self._typed_outputs = {...}
        return EnrichmentModuleOutcome(
            module_id=self.module_id,
            status=EnrichmentModuleStatus.RUN,
            reason="ok",
            source_refs=(self._make_provenance(ctx),),
            model_usage=_model_usage_from(usage, role="text"),
        )
```

## Step 4 — Register the module

Add the module to the runner construction in
[`activities/processing.py::run_enrichment_stage`](../../src/j1/orchestration/activities/processing.py):

```python
runner = CompositeEnrichmentRunner(modules=[
    MetadataEnrichmentModule(),
    TerminologyEnrichmentModule(),
    ValidationEnrichmentModule(),
    *legacy_modules,
    GraphEntityEnrichmentModule(...),    # ← new
])
```

If the module needs LLM clients, thread them through
`build_legacy_enricher_modules` or construct it explicitly with
`text_client=self._enrichment_text_client` /
`vision_client=vision_adapter` /
`llm_call_limiter=self._enrichment_llm_call_limiter`.

## Step 5 — Project typed outputs

If you cached typed records on `self._typed_outputs` in `run()`,
extend the runner ([`enrichment_modules.py::CompositeEnrichmentRunner.run`](../../src/j1/processing/enrichment_modules.py))
to merge them into the aggregated `EnrichmentResult`:

```python
if hasattr(module, "get_typed_outputs"):
    typed = module.get_typed_outputs() or {}
    entities = typed.get("graph_entities") or ()
    for e in entities:
        if isinstance(e, GraphEntity):
            graph_entities.append(e)
```

Add the new field to `EnrichmentResult` (frozen dataclass + to_payload
+ from_payload) and update the report's enrichment summary projector
in [`final_ingestion_report.py::_build_enrichment_summary`](../../src/j1/processing/final_ingestion_report.py)
if you want it on the FE-facing summary.

## Prompt resolution

LLM-backed modules MUST resolve prompts via
`resolve_module_prompt(domain_pack, prompt_field, builtin_default)`:

```
domain_pack.prompt_pack.<prompt_field>   if set
↓ else
<_BUILTIN_PROMPT>                         (generic, no domain vocab)
↓
domain_pack.prompt_addon is prepended to whichever wins
```

The `_BUILTIN_PROMPT` must NOT contain civil-engineering or any other
domain-specific vocabulary. Domain specialisation goes through the
`DomainPromptPack` per-domain. Tests in
`tests/test_legacy_enricher_modules.py` enforce this.

## Limiter

LLM-backed modules MUST route through the shared
`LLMCallLimiter`. For text-shaped modules, use the `_llm_call`
helper on `_LegacyWrapperBase`:

```python
parsed, usage = self._llm_call(
    self._text_client.extract, prompt, schema, metadata={...},
)
```

For per-image vision modules, pass the limiter into
`PerImageVisionAdapter(..., llm_call_limiter=...)` at construction
time and the adapter wraps each per-image call (Wave 11B).

## Provenance

Every typed output MUST carry a `ProvenanceLink`. The base helper
`_make_provenance(ctx)` produces a link to the first raw compile
artifact:

```python
provenance = self._make_provenance(ctx)   # source_artifact_id=raw[0]
# ... or build it inline if you need per-record provenance:
provenance = ProvenanceLink(
    source_artifact_id=record_artifact_id,
    source_chunk_id=chunk_id,              # when applicable
    source_kind="compile",
    relation="extracted_from",
)
```

## Skip + failure behaviour

| Outcome | Status | When |
|---|---|---|
| RUN | `EnrichmentModuleStatus.RUN` | normal success |
| PARTIAL | `EnrichmentModuleStatus.PARTIAL` | LLM returned a parseable response missing a required field — module surfaces a warning, doesn't fail the run |
| SKIPPED | `EnrichmentModuleStatus.SKIPPED` | `can_run` returned False — reason is operator-readable |
| FAILED | `EnrichmentModuleStatus.FAILED` | LLM call raised. If `require_enrichment_success=True` → run lands at `failed_enrichment_required`; otherwise `completed_with_enrichment_warnings` |

NEVER:

- raise out of `run()` — return a FAILED outcome instead. The
  runner has a defensive catch, but explicit is clearer.
- mutate `ctx.compile_result` or the raw compile artifacts.
- mutate the `DomainPack`.

## Tests to add

Mirror the patterns in
[`tests/test_legacy_enricher_modules.py`](../../tests/test_legacy_enricher_modules.py):

- protocol conformance (`isinstance(YourModule(), EnrichmentModule)`)
- skip behaviour per documented `can_run` reason
- domain prompt override + prompt_addon prepending
- limiter routing (count `limiter.calls`)
- typed output projection + provenance assertion
- failure semantics (LLM raise → FAILED + error message)
- no compile-result mutation
- vocabulary guard (no civil-engineering vocab in module source)

## Related pages

- [Enrichment overlay](../architecture/enrichment-overlay.md)
- [Domain profiles](../architecture/domain-profiles.md)
