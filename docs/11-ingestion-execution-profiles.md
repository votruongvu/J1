# Ingestion Execution Profiles — Investigation Report

**Date:** 2026-05-15
**Scope:** Audit current "standard" mode end-to-end; classify every operation by cost; determine whether RAGAnything/LightRAG can support a true `minimum_queryable` path; propose the cleanest implementation.

This document is the Part 1 deliverable for the execution-profile refactor.

---

## TL;DR

1. The **workflow itself** (post compile-first refactor) already gates enrich / graph / index reasonably — graph and indexer only run when the caller supplies `graph_builder_kind` / `indexer_kind`, and enrich runs only when the `PostCompileEnrichPlan` recommends it.
2. The **hidden heavy cost in "standard"** lives inside the `compile` stage, not after it. `RAGAnything.process_document_complete(...)` and the fast-path `lightrag.ainsert(...)` both run LightRAG's **stage-2 LLM entity + relationship extraction** unconditionally. LightRAG exposes no `disable_entity_extraction` switch. The adapter's `_force_persist_chunks` workaround already documents this in code comments at [_bridge.py:1750–1768](../src/j1/providers/raganything/_bridge.py#L1750).
3. **`standard` cannot honestly be called "minimum" today** — every compile, even of a 4-page text PDF on the fast PDF path, fires N LightRAG entity-extraction LLM calls (one per chunk).
4. **A true `minimum_queryable` path IS achievable** by injecting a no-op `llm_model_func` at LightRAG construction time when that profile is selected. Chunks + embeddings still persist (the fast paths already force-flush them); entity/relationship extraction returns empty results instantly; `aquery(mode="hybrid")` falls back to vector retrieval. No fork of RAGAnything/LightRAG required.
5. **Recommended naming:** keep `standard` and `advanced` as today, add `minimum_queryable` as the new floor. `standard` becomes "compile + vector index, no enrichment, no graph build, but LightRAG's built-in entity extraction still fires inside compile" — explicitly documented, not pretending to be cheap. `advanced` keeps full enrichment + graph build.

---

## Current Pipeline: User Clicks Index → Document Is Queryable

The workflow has been refactored to "compile-first" (see [project_compile_first_refactor.md memory](../memory/project_compile_first_refactor.md) for context). Current execution order:

| # | Stage | File | LLM cost | Gated by | Skippable today? |
|---|---|---|---|---|---|
| 1 | REST handler — `POST /ingestion-runs` allocates run + snapshot, dispatches workflow | [app.py:4428](../src/j1/adapters/rest/app.py#L4428) | none | — | n/a |
| 2 | `profile_document` activity (pypdf, no I/O to LLM) | [project_processing.py:2757](../src/j1/orchestration/workflows/project_processing.py#L2757) | none | `request.planner_enabled` | yes (default off) |
| 3 | `build_initial_execution_plan` activity → `DefaultAssessmentPlanner.assess()` (rule-based) | [project_processing.py:2843](../src/j1/orchestration/workflows/project_processing.py#L2843), [assessment.py:417](../src/j1/processing/assessment.py#L417) | none | `planner_enabled` | yes |
| 4 | Optional confirmation gate (`WAITING_FOR_CONFIRMATION`) | [app.py:3979](../src/j1/adapters/rest/app.py#L3979) | none | `two_phase_compile` or `require_confirmation` | yes |
| 5 | **`compile` activity → RAGAnything bridge → MinerU + LightRAG `ainsert`** | [project_processing.py:3037](../src/j1/orchestration/workflows/project_processing.py#L3037), [_bridge.py:371](../src/j1/providers/raganything/_bridge.py#L371) | **vision-LLM if images, text-LLM for entity extraction (always)** | always runs | **no — this is the keystone problem** |
| 6 | `persist_compile_strategy_report` (artifact only) | [project_processing.py:3244](../src/j1/orchestration/workflows/project_processing.py#L3244) | none | — | n/a |
| 7 | `persist_normalized_compile_result` (artifact only) | [project_processing.py:3279](../src/j1/orchestration/workflows/project_processing.py#L3279) | none | — | n/a |
| 8 | `_run_post_compile_enrich_assessment` → `PostCompileEnrichPlan` (rule-based) | [project_processing.py:3294](../src/j1/orchestration/workflows/project_processing.py#L3294), [enrich_assessment.py:325](../src/j1/processing/enrich_assessment.py#L325) | none (optional fast-LLM consult is off by default) | always runs but rule-based | n/a |
| 9 | `run_enrichment_stage` — composite enricher (vision LLM, requirement/risk LLM) | [project_processing.py:3332](../src/j1/orchestration/workflows/project_processing.py#L3332) | vision-LLM + text-LLM | `_stage_enabled("enrich", enrich_plan=...)` | yes |
| 10 | `build_graph` activity — entity/relation graph build | [project_processing.py:3366](../src/j1/orchestration/workflows/project_processing.py#L3366) | text-LLM (graph extraction) | `request.graph_builder_kind` present | yes |
| 11 | `index` activity — embeddings + vector store + FTS | [project_processing.py:984](../src/j1/orchestration/workflows/project_processing.py#L984) | embeddings | `request.indexer_kind` present | partially — but query needs SOMETHING |
| 12 | `finalize` + terminal artifact persistence | [project_processing.py:1005](../src/j1/orchestration/workflows/project_processing.py#L1005) | none | — | n/a |

### Cost classification per stage (today, `standard` mode, default config)

```text
required_for_query                  : 5 (compile), 11 (index)
optional_quality_improvement        : 9 (enrich), 10 (graph)
optional_domain_enrichment          : 9 (domain pack tasks)
optional_validation                 : n/a (validation is import-only, see memory)
unknown_requires_investigation      : — (resolved)
```

The headline finding: stage 5 (compile) is in `required_for_query` AND fires N+1 LLM calls per document. That's the hidden tax on every "standard" ingest.

---

## The Hidden Cost in Compile

The bridge calls one of three paths inside `compile`:

1. **Plain-text fast path** ([_bridge.py:1644](../src/j1/providers/raganything/_bridge.py#L1644)) — for `.txt`/`.md`/`.json`, skips MinerU and calls `lightrag.ainsert(input=text, ...)` directly.
2. **Text-extractable PDF fast path** ([_bridge.py:1693](../src/j1/providers/raganything/_bridge.py#L1693)) — extracts via pypdf, then `lightrag.ainsert(...)`. Saves 3–5 min of MinerU but…
3. **Full `process_document_complete`** ([_bridge.py:371](../src/j1/providers/raganything/_bridge.py#L371)) — MinerU layout/OCR/formula pipeline + `lightrag.ainsert(...)`. Vision-LLM fires here when images are present.

**All three paths end in `lightrag.ainsert(...)`.** LightRAG's `apipeline_process_enqueue_documents` is documented in our own bridge:

> "writes chunks to in-memory storage in stage 1, then runs **LLM-driven entity extraction in stage 2**, and only calls `_insert_done` … on stage-2 success."
> — [_bridge.py:1754–1758](../src/j1/providers/raganything/_bridge.py#L1754)

So even when `parse_method=txt` and `enable_image_processing=false` and no `graph_builder_kind` is supplied, **the compile stage still pays the LLM cost of entity + relationship extraction** for every chunk. This is what makes `standard` slow even on a trivial text file.

### Why `_force_persist_chunks` matters

`_force_persist_chunks` ([_bridge.py:1750](../src/j1/providers/raganything/_bridge.py#L1750)) already flushes chunks + embeddings to disk regardless of stage-2 extraction success or failure. That means the adapter has already proven that **chunks alone are enough for a queryable document**. We just need to make extraction return empty *immediately* instead of failing slowly.

---

## RAGAnything / LightRAG Adapter Control Matrix

| Control | Status | Source / Citation |
|---|---|---|
| disable text extraction | n/a (always required) | — |
| disable image processing | **partial** — `enable_image_processing` is `setattr()`-applied to `RAGAnythingConfig` when the installed version exposes it | [_bridge.py:1310–1362](../src/j1/providers/raganything/_bridge.py#L1310), [plan_mapper.py:117–147](../src/j1/providers/raganything/plan_mapper.py#L117) |
| disable table processing | **partial** — `enable_table_processing` same mechanism | [plan_mapper.py:117](../src/j1/providers/raganything/plan_mapper.py#L117) |
| disable equation processing | **partial** — `enable_equation_processing` same mechanism | [plan_mapper.py:117](../src/j1/providers/raganything/plan_mapper.py#L117) |
| skip MinerU for text | **supported** — plain-text + text-extractable PDF fast paths | [_bridge.py:1644](../src/j1/providers/raganything/_bridge.py#L1644), [_bridge.py:1693](../src/j1/providers/raganything/_bridge.py#L1693) |
| disable LightRAG entity extraction | **unsupported by library, BUT bypassable via no-op `llm_model_func` injection** | [_bridge.py:922–924](../src/j1/providers/raganything/_bridge.py#L922) — `llm_model_func=` is a J1-controlled callable |
| disable LightRAG relationship extraction | **same as entity extraction** | same |
| disable graph file emission | **unsupported by library** — emitted as side effect of `ainsert()` | [_bridge.py:1950–2069](../src/j1/providers/raganything/_bridge.py#L1950) |
| vector-only retrieval mode | **achievable** — `aquery(mode="hybrid")` already falls back to vector when graph is empty | [retrieval.py:59](../src/j1/providers/raganything/retrieval.py#L59), [_bridge.py:842](../src/j1/providers/raganything/_bridge.py#L842) |
| inject custom `llm_model_func` per construction | **supported** — J1 already controls this | [_bridge.py:914, 924, 957, 1282](../src/j1/providers/raganything/_bridge.py#L914) |

### Implication

We don't need to fork RAGAnything or LightRAG. The library lets J1 inject the LLM callable. For `minimum_queryable`, J1 swaps in a `NoOpExtractionLLM` that returns immediately with empty JSON (the shape LightRAG expects for "no entities found"). Chunks still persist via `_force_persist_chunks`. Queries still work via vector fallback.

This is what Part 1 of the task called "Option B: Bypass RAGAnything for minimum_queryable and use a J1-owned text/vector index path." We can do it without leaving the adapter — the bypass is **inside** the adapter layer, swapping one callable.

---

## Proposed Execution Profiles

```text
minimum_queryable  →  required_for_query only
standard           →  required_for_query + LightRAG-internal extraction (cannot be disabled but logged honestly)
advanced           →  full quality: enrich + graph build + multimodal
```

| Profile | MinerU | LightRAG ainsert | LightRAG entity extraction (LLM) | Vision-LLM (images) | Enrich stage | Graph build | Index |
|---|---|---|---|---|---|---|---|
| **`minimum_queryable`** | text fast paths only | yes | **no-op** (injected) | skipped | skipped | skipped | yes (vector) |
| **`standard`** | auto | yes | yes (built-in, unavoidable) | only if images required | skipped by default | only if `graph_builder_kind` | yes |
| **`advanced`** | auto / ocr | yes | yes | yes if images | yes (enrich_plan-gated) | yes | yes |

### Honesty test

A profile is only `minimum_queryable` if it can ingest a 100-page text PDF and produce a queryable index **without** firing N LightRAG entity-extraction LLM calls. With the no-op `llm_model_func` injection, this is true. With anything else, it is not.

---

## Naming Decisions

- Keep enum value `standard` — already in flight elsewhere (`CompileMode.STANDARD`, REST metadata, audit logs). Reuse for the profile.
- New value `minimum_queryable` — explicit and self-documenting.
- Keep `advanced` — explicit.
- The legacy `CompileMode.FAST` stays as a deprecated round-trip-only enum value ([assessment.py:67–68](../src/j1/processing/assessment.py#L67)); we do **not** resurrect it for the profile system.
- `J1IngestMode` (Temporal search attribute) currently mirrors `CompileMode`. After this refactor it should mirror the **selected execution profile** so dashboards filter on what the user actually chose, not what the planner suggested.

---

## Proposed Implementation (Phasing)

### Phase A — Profile contract + adapter hook (this PR series)

1. New `j1.processing.execution_profile`:
   - `ExecutionProfile` StrEnum: `minimum_queryable`, `standard`, `advanced`.
   - `ProfileCapabilities` dataclass capturing per-stage flags (`run_enrich`, `run_graph`, `run_index`, `multimodal_processing`, `lightrag_entity_extraction`, etc.).
   - `RECOMMENDED_PROFILE_FOR_ASSESSMENT(plan: AssessmentPlan) → ExecutionProfile` rule.
2. New `NoOpExtractionLLM` callable in `j1.providers.raganything._noop_llm`, drop-in for `_make_text_callable`.
3. `RAGAnythingSettings` / bridge: accept a `disable_entity_extraction: bool` flag. When true, replace `llm_model_func` with the no-op callable.
4. Adapter exposes `unsupported_profile_controls: list[dict]` on compile-result metadata when a profile asks for something the installed library cannot honour (e.g., `enable_image_processing` field missing on `RAGAnythingConfig`).

### Phase B — Workflow gating by selected profile

1. Add `selected_execution_profile: ExecutionProfile | None` to `ProjectProcessingRequest`.
2. `_stage_enabled(stage, ..., selected_profile=...)` short-circuits when the profile disables the stage. Profile beats `enrich_plan` beats caller-kind.
3. Persist `assessment_recommended_profile`, `selected_execution_profile`, `profile_selected_by`, `profile_selection_source` on `IngestionRun.metadata`.

### Phase C — Two-step REST API

1. `POST /documents/{document_id}/assessment-plan` — runs the profiler + `DefaultAssessmentPlanner` inline, returns recommendation + profile catalogue + cost warnings. **Does not start a workflow.**
2. `POST /documents/{document_id}/index` — accepts `{ assessment_plan_id, selected_profile }`. Validates safety flags (`J1_ALLOW_ADVANCED_INGEST`, etc.) before dispatching.
3. Backend safety overrides: hard env flags can downgrade a request, but never silently — error or warning is returned, never a silent reshape.

### Phase D — Observability

1. New audit events: `ingest.profile.recommended`, `ingest.profile.selected`, `ingest.profile.execution_started`, `ingest.stage.skipped`, `ingest.stage.llm_call_started`, `ingest.stage.heavy_operation_detected`.
2. Every `llm_model_func` call logs `{provider, model, purpose, stage, selected_profile}`. The no-op extraction callable logs `purpose=entity_extraction_noop_minimum_queryable` so it's visible.

### Phase E — UI

1. `IndexButton` → split into two-step flow: open `AssessmentPlanModal` first.
2. `AssessmentPlanModal` shows recommendation + 3 profile cards with cost/speed/quality copy.
3. Confirm button on the modal calls `POST /index` with the chosen profile.

### Phase F — Cleanup

1. Strip the old prompt doc `j1-minimal-queryable-ingestion-settings-prompt.md` once the new system supersedes it.
2. Remove unused planner code (the orphan `PlanningActivities.build_planning_result` still exists per memory — sweep it).

---

## Open Risks

- **Stage 2 timeout when no-op is mis-injected.** If `llm_model_func` returns a non-JSON-parseable response, LightRAG raises and chunks may not persist. Mitigation: the no-op returns the exact JSON shape LightRAG expects for "no entities", and we still rely on `_force_persist_chunks` as a belt.
- **Vector-only query quality.** Without the graph, `aquery(mode="hybrid")` falls back to vector retrieval. Some questions that only the graph could answer will degrade. This is the documented trade-off of `minimum_queryable` — users see it on the profile card.
- **Installed-version drift.** `RAGAnythingConfig` field availability varies. Adapter must classify each requested control and surface unsupported ones to run metadata.
- **Per-run LightRAG workspace.** Already exists (`working_dir_override` per run). The no-op injection must happen **per `compile()` call**, not at module load, since the same worker may serve `minimum_queryable` and `advanced` compiles back-to-back.

---

## Open Questions (resolved by code, not by asking)

- *Does the workflow already gate enrich on a plan?* Yes — `_stage_enabled("enrich", enrich_plan=...)` reads SKIP/RECOMMENDED/REQUIRED. We add `selected_profile` as an authoritative override.
- *Is there already a `J1IngestMode` user-facing knob?* No — it's a Temporal search attribute mirroring the planner's compile-mode decision. We repurpose it to mirror the **selected profile** after the refactor.
- *Is there a pre-compile artifact we can reuse for the recommendation API response?* Yes — `initial_execution_plan`. We wrap it with the profile catalogue.
- *Will compile cache invalidate on profile change?* Compile cache key today is `(document_hash, processor_kind, mode)`. We extend it to `(document_hash, processor_kind, mode, profile)` so a `minimum_queryable` ingest doesn't return a cached `advanced` artifact.
