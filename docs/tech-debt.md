# Technical debt — ingestion pipeline ( snapshot)

This page records known asymmetries + deferred work in the new
ingestion pipeline. Nothing here is load-bearing — each item is
either an internal wire-shape inconsistency, a documentation gap,
or an explicit "deferred to a future wave" deferral.

## A. `skipped_reason` vs `module_outcomes[].reason`

**Current behaviour:**
- The runtime SKIP path (assessor said `should_enrich=False`) writes
 the reason to `EnrichmentResult.skipped_reason` — a top-level
 field on the typed overlay.
- Per-module SKIPPED outcomes (e.g. `"no text LLM client configured"`)
 write the reason to `EnrichmentModuleOutcome.reason` — inside
 `module_outcomes[]`.

**Final-report builder reads both:**
- `_enrichment_skipped_reason_from_payload` checks
 `enrichment_result.skipped_reason` first, falls back to `reason`
 for older payloads.
- `_build_enrichment_summary` does the same.

**Future cleanup:** unify the field name on the wire payload (one
of: rename `skipped_reason` → `reason`, or always emit both, or
move per-module skip reasons up alongside `skipped_reason`). Any of
these is a coordinated FE + report consumer change; not urgent.

## B. `image_summaries[].metadata.error`

**Current behaviour:** when a per-image vision call raises inside
`PerImageVisionAdapter._invoke_one`, the adapter records a
fallback entry:

```jsonc
{
 "image_id": "art-1",
 "caption": null,
 "metadata": { "error": "TimeoutError:..." }
}
```

The FE renders this as a missing-caption row; the operator sees
the issue through `image_enrichment.outcome.warnings` (provider-
side warnings) but not directly via the typed summary.

** small cleanup (shipped):** the runner now projects
`image_summaries[].metadata.error` entries onto
`ImageSummary.warnings[]` so the typed overlay is the operator's
trace path. See `enrichment_clients.py::PerImageVisionAdapter`.

## C. `DetectedImage.image_id` vs `ArtifactRecord.artifact_id`

**Current behaviour:**
- `NormalizedCompileResult.detected_images[].image_id` is the
 parser's internal identifier (e.g. MinerU's image counter).
- `ImageSummary.image_id` and `provenance.source_artifact_id` use
 the registry-side `ArtifactRecord.artifact_id` of the matching
 `compile.image` artifact.
- The two **do not correlate today**. The image module keys
 outputs on the durable artifact id; the parser-internal id is
 surfaced on the `image_summaries[].metadata` only.

**Why:** the parser doesn't (and shouldn't) know about the
artifact registry. The producer that writes the `compile.image`
artifacts decides their ids independently of the parser-internal
counter.

**Future cleanup:** if a future producer guarantees `compile.image`
artifact ids carry the parser-internal id (e.g. `compile.image:p3-img-2`),
the wiring would automatically benefit — no module change required.
A test (`test_parser_internal_image_id_mismatch_does_not_break_enrichment`)
asserts the current behaviour stays correct under mismatch.

## D. No staging / prod worker entrypoint

**Current state:** `deploy/dev/worker.py` + `deploy/dev/_wiring.py`
are the only worker entrypoints. They follow the documented Wave
10.6 / 11A / 11B wiring pattern.

**Risk:** when staging / prod deployments are built, they MUST
follow the same shape — bootstrap → raw clients → per-run image
adapter inside the activity. A copy-paste that wraps the vision
client at bootstrap ('s now-superseded pattern) would
silently revert image enrichment to the empty-provider state.

**Documentation:** see [Production worker wiring runbook](./operations/production-worker-wiring.md).
The inline comments in `deploy/dev/_wiring.py` also document the
expected shape.

**Future cleanup:** add a `j1.compose.worker_spec` factory that
encapsulates the wiring so deployment artefacts can't accidentally
omit a dep. Today the dev wiring is the reference implementation;
the factory extraction is deferred.

## E. `final_summary` vs `final_ingestion_report`

**Current state:**
- `final_summary` carries the executed-step table +
 artifact-kind counts + the failure-code trio. Persisted at every
 terminal.
- `final_ingestion_report` is the typed aggregate
 preferred by the FE + the operator runbook.

**Both are written:** the workflow calls `_persist_final_summary`
THEN `_persist_final_ingestion_report`. Consumers should prefer
the report; `final_summary` remains for backward compatibility
with pre- tooling.

**Future cleanup:** when no consumers read `final_summary` directly,
deprecate the write. Not urgent — `final_summary` is small + the
write is best-effort.

## F. Limiter doesn't bound per model tier

**Current state:** the `LLMCallLimiter` is a single semaphore that
spans all enrichment LLM calls (text + classification + table +
image). Per-image vision calls are individually bounded,
but the bound applies to the SAME global semaphore.

**Why:** the limiter was built to bound a single deployment's total
LLM-cost surface. Per-tier semaphores (premium ≤ N, fast ≤ M)
would let operators tune cost differently per tier.

**Future cleanup:** add a `tier` arg to `LLMCallLimiter.run` that
acquires from a per-tier sub-semaphore. Requires a model-selector
that produces tier labels — interacts with the existing
`select_model_tier` helper.

## G. Empty `vision_image_provider` in dev wiring (now resolved)

**Resolved.** Previously the dev wiring constructed
`PerImageVisionAdapter(raw_vision, image_provider=lambda: [])` at
worker startup, which meant image enrichment skipped on every run
even with a vision client wired.

**Current state:** the activity constructs the adapter per run
with `WorkspaceImageBytesProvider`. The dev wiring passes the raw
vision client through. Production / staging deployments must
follow the same shape — see runbook.

## H. Compile retry-count surfaces; enrichment retry-count is 0

**Current state:**
- `final_ingestion_report.retry_counts.compile` is sourced from
 `compile_result_summary.retry_attempts[]`.
- `final_ingestion_report.retry_counts.enrichment` is always 0
 today.

**Why:** didn't ship per-module retry accounting inside
the limiter. The field is reserved for that work.

**Future cleanup:** when the limiter ships retry stats per
`module_id`, the report builder can read them. Today the schema
slot is in place but always 0.

## I. Per-image limiter releases on raise — relies on limiter's `try/finally`

**Current state:** `PerImageVisionAdapter._invoke_one` uses
`self._llm_call_limiter.run(_call)`. The production
`LLMCallLimiter` releases the semaphore in its own `try/finally`
even when `_call` raises. The fake limiters in tests record
acquisitions but don't enforce the symmetry directly.

**Pinned by:** the existing limiter unit tests already
prove release-on-raise; the suite asserts the
acquisition count under failures.

**Future cleanup:** none required. Documented for the audit trail.

## J. `j1.enrichers` legacy enrichers still exist

**Current state:** the adapters DON'T invoke the
classes in `j1/enrichers.py` directly — they re-implement the same
prompt + JSON-schema vocabulary against the typed analysis-client
contracts. The legacy enrichers continue to run via
`CompositeEnricher.from_default` for non-protocol consumers (the
old workflow `enrich` activity).

**Future cleanup:** when the `enrich` activity is removed (or
migrated to the protocol), the legacy `j1/enrichers.py` file can
be pruned. Not urgent — having both paths means the protocol-based
adapter can roll back to the legacy class if a prompt drift is
ever discovered.

## Cross-cutting principle

Every item above is a CLEAN debt with a clear shape + a clear
deferral reason — not a hidden surprise. The pipeline's
correctness invariants (raw output preserved, overlay never
mutates compile, every per-image LLM call individually bounded,
required enrichment failure maps to `failed_enrichment_required`)
are pinned by tests. The items here are operational refinements
that future waves can pick up without re-architecting.
