# Ingestion Profiles (legacy)

> **⚠ Legacy document — pre-Wave-8 pipeline.** This page describes
> the historical `DefaultIngestPlanner` / `IngestPlan` /
> `IngestPolicy` pre-compile planning system, which has been
> **removed** from the currently shipping pipeline. The new
> pipeline makes enrichment decisions AFTER compile evidence is
> available, not before. Pages below remain for compatibility with
> older deployments that still reference them.
>
> **Authoritative replacements:**
> - Pipeline shape → [`architecture/ingestion-pipeline.md`](architecture/ingestion-pipeline.md)
> - Per-domain customisation → [`architecture/domain-profiles.md`](architecture/domain-profiles.md)
> - Post-compile enrichment plan → [`architecture/enrichment-overlay.md`](architecture/enrichment-overlay.md)
> - Adding a domain profile → [`guides/adding-a-domain-profile.md`](guides/adding-a-domain-profile.md)
>
> Concepts called out below as "active" or "default" are **not
> active** in the current code base. The `J1_INGEST_PLANNER_*` /
> `J1_POST_COMPILE_PLANNING_ENABLED` env vars no longer drive the
> pipeline; the post-compile assessor + the typed
> `InitialExecutionPlan` + `PostCompileEnrichPlan` are the current
> surface.

How J1 picks an ingestion strategy per file, what each strategy actually does, and how to debug when the wrong one fires.

This document is the operator's lens onto J1's existing ingestion pipeline — it explains the contract, not the implementation. The running code in [`src/j1/processing/planning.py`](../src/j1/processing/planning.py), [`src/j1/processing/ingestion_profiles.py`](../src/j1/processing/ingestion_profiles.py), and [`src/j1/orchestration/workflows/project_processing.py`](../src/j1/orchestration/workflows/project_processing.py) is authoritative.

---

## 1. Overview

J1 ingestion runs through these stages:

```
upload → profile → initial plan → compile → content inventory →
  post-compile planning → (selective) enrich → graph → index → review
```

The framework does **not** run every stage for every document. The **initial planner** (`DefaultIngestPlanner`) reads cheap deterministic signals (file extension, size, native-text PDF detection) and picks an `IngestMode` that determines which optional stages run. The compile stage is always required; everything else is gated by the plan.

After compile, the parser (RAGAnything / MinerU) returns content statistics — image count, table count, scanned-page hints — that may not have been visible from the deterministic profile alone. Two things happen with that signal:

1. **Plan revision** — the workflow re-runs `DefaultIngestPlanner` with the new signals via `_merge_compile_signals` and emits `j1.progress.plan.revised` when the resulting plan unlocks (or removes) optional stages.
2. **Post-compile Processing Plan (`planning_result.json`)** — the `PlanningActivities.build_planning_result` activity composes Document Understanding + Lightweight Content Digest + Rule-based Post-Compile Assessment, optionally consults a planner LLM, validates the output, and persists the result as a `planning_result` artifact. This drives the Planning Report tab and provides per-step `enabled / scope / pages / reason` hints downstream.

The parser-output boundary is captured as a `ParsedContentManifest` artifact, persisted alongside compile output. Downstream consumers (post-compile planning, the Quality tab, future tools) read it without re-walking the storage directory.

### Initial plan vs. Post-compile Processing Plan

| | Initial plan | Post-compile Processing Plan |
|---|---|---|
| **When** | Pre-compile, after deterministic profile | After compile + content inventory |
| **Inputs** | `DocumentProfile` (extension, size, page count, OCR hints) | Initial plan + parsed-content manifest + Document Understanding |
| **Output** | `IngestPlan` audit event | `planning_result.json` artifact + `plan.revised` event |
| **Surfaces** | `GET /ingestion-runs/{id}/plan` (legacy plan card) | `GET /ingestion-runs/{id}/planning` (Planning Report tab) |
| **Drives** | Coarse stage gates (enrich/graph/index on/off) | Per-step recommendations (chunking strategy, table/vision pages, requirement/risk/quality/graph) |
| **Override-ability** | Caller-supplied `compilerKind` etc. always wins | Recommendations honor caller overrides; LLM-assist may override rules |
| **LLM cost** | None (deterministic) | None when `J1_LLM_PLANNING_ENABLED=false` (default) |

### Document Understanding

The post-compile planner runs a "title-first" Document Understanding pass to infer:

* What kind of document this is (`document_type` from a 25-entry taxonomy).
* What it's mainly about (`primary_topic`, `business_domain`).
* Who it's for (`intended_audience`).
* Which analysis bias is appropriate (prefer requirement/risk/table/graph/visual extraction).

Title sources are walked in declining-quality order — explicit metadata title, parser title block, first heading, filename, then early-page heading text. A title is graded `clear / ambiguous / generic / missing`; when the title is unclear, the planner inspects up to `J1_PLANNING_MAX_EARLY_PAGES` of digest content. The full document is **never** read.

### Why this matters: cost control through skipping

The post-compile plan is a "decide when NOT to run LLM" mechanism. Same baseline retrieval (chunking + embedding + indexing always run); expensive enrichers (vision, graph, requirement / risk / quality extraction) gate on type-aware evidence. A clean text knowledge article gets fast profile + no enrichment; an SRS gets premium profile with requirement + risk + graph; an invoice gets table extraction only.

### Domain packs

The post-compile planner can layer **domain packs** on top of its generic decisions. A domain pack is a pluggable bundle that supplies an extended document-type taxonomy, keyword-based detection rules, and per-document-type planning overlays. The bundled `civil_engineering` pack recognises BOQs, drawings, inspection reports, method statements, and 25 other construction document types and tunes planning accordingly (e.g. BOQ → premium profile + table-only enrichment; inspection report with photos → vision + risk + graph enabled).

Selection precedence: per-run override → workspace default → auto-detect (with confidence threshold) → fallback to `general`. The selected pack is recorded on `planning_result.json`'s `domain_context` block and rendered in the FE Planning Report's Domain pack panel.

See [DOMAIN_PACKS.md](DOMAIN_PACKS.md) for the full architecture, the Civil Engineering v0.1 catalogue, and instructions for adding new packs.

### Why profiles matter

Running every stage on every file is wasteful: an entity-extraction LLM call for a one-page memo costs the same as one for a complex spec. Skipping the wrong stage, on the other hand, produces an unsearchable document. The profile mechanism is the framework's answer — pick the cheapest profile that satisfies the document's actual content shape, then use post-compile signals to upgrade if reality disagreed with the deterministic profile.

---

## 2. Profile matrix

| Profile | Intended documents | Parser | Enrich | Graph | Vision | LLM role | Cost | Latency | Recommended? |
|---|---|---|---|---|---|---|---|---|---|
| `TEXT_ONLY` | `.txt`, `.md`, `.markdown`, `.rst`, `.log`; native-text PDFs | bypasses MinerU | off | off | off | fast | low | fast | dev / low-cost |
| `TEXT_WITH_LIGHT_ENRICHMENT` | most business PDFs and DOCX | MinerU | light | off | off | text | medium | balanced | **production default** |
| `TABLE_AWARE` | `.xlsx`, `.csv`, table-heavy PDFs | MinerU | tables | off | off | text | medium | balanced | spreadsheet workloads |
| `MULTIMODAL_LIGHT` | PDFs with figures + decks | MinerU | tables + visuals (selective) | off | yes | text | medium | balanced | mixed-media docs |
| `MULTIMODAL_FULL` | scanned PDFs, image-only, complex diagrams | MinerU + OCR | all | on | yes | text | **high** | **slow** | high-fidelity / scanned |
| `GRAPH_AWARE` | knowledge bases, research papers, regulatory filings | MinerU | text + tables | on | off | text | medium | balanced | KB workloads |
| `FULL_DIAGNOSTIC` | benchmarking, QA | MinerU | all | on | yes | premium | high | slow | **never as default** |

*"Recommended?" assumes the framework's bundled `auto` policy. Force a profile via `J1_INGEST_DEFAULT_POLICY=force_full` or per-job `policy` if you need every stage regardless of signals.*

---

## 3. Per-profile settings

Settings the framework consults for each profile. Where a setting maps to an env var or wiring point, the link is provided.

### `TEXT_ONLY`

| Setting | Value |
|---|---|
| `parse_method` | `txt` (forced when `J1_ENRICH_SCANNED_PAGES=false` and operator left default) |
| MinerU backend | bypassed via `_NATIVE_TEXT_EXTENSIONS` / `_is_text_extractable_pdf` |
| Steps enabled | compile, index |
| Enrich children | none (composite not registered) |
| LLM role | TEXT (could route to FAST in future) |
| Vision | not invoked |
| Cache / manifest | persisted; same artifacts as other profiles |

### `TEXT_WITH_LIGHT_ENRICHMENT`

| Setting | Value |
|---|---|
| `parse_method` | `auto` |
| MinerU backend | per `J1_RAGANYTHING_BACKEND` |
| Steps enabled | compile, enrich, index |
| Enrich children | classifier, requirement extractor, source mapper, confidence assessor |
| Table extractor | filtered out (no `tables_enabled=True` signal) |
| Visual content describer | filtered out |
| LLM role | TEXT |
| Vision | off |

### `TABLE_AWARE`

| Setting | Value |
|---|---|
| `parse_method` | `auto` |
| Steps enabled | compile, enrich, index |
| Table extractor | enabled (`tables_enabled=True`) |
| Visual content describer | filtered out unless tables are scanned |
| LLM role | TEXT |
| Vision | off (tables analysed via text LLM) |

### `MULTIMODAL_LIGHT`

| Setting | Value |
|---|---|
| `parse_method` | `auto` |
| Steps enabled | compile, enrich, index |
| Table extractor | enabled |
| Visual content describer | enabled (per-image triage decisions limit cost) |
| LLM role | TEXT for narrative, VISION per image |
| Vision | invoked selectively |

### `MULTIMODAL_FULL`

| Setting | Value |
|---|---|
| `parse_method` | `auto` (MinerU's auto includes OCR fallback) |
| Steps enabled | compile, enrich, graph, index |
| All enrich modalities | enabled |
| Visual content describer | enabled |
| LLM role | TEXT for narrative, VISION for every visual / scanned page |
| Vision | required |
| Cost warning | every page MinerU classifies as scanned hits the VLM endpoint |

### `GRAPH_AWARE`

| Setting | Value |
|---|---|
| `parse_method` | `auto` |
| Steps enabled | compile, enrich, graph, index |
| Visual content describer | filtered out unless image signals are strong |
| LLM role | TEXT |
| Vision | off by default |

### `FULL_DIAGNOSTIC`

| Setting | Value |
|---|---|
| All steps | enabled |
| All enrich children | enabled |
| LLM role | PREMIUM (currently routes to TEXT until premium is wired) |
| Vision | required |
| Use case | accuracy comparisons, parser benchmarks — **not a production default** |

---

## 4. Decision examples

How the planner picks a profile for typical inputs. The decision logic is in [`_pick_mode`](../src/j1/processing/planning.py).

| Input | Deterministic profile signal | Initial mode | Post-compile change? |
|---|---|---|---|
| `notes.txt` | `_PLAIN_TEXT_EXTENSIONS` matches | `TEXT_ONLY` | none |
| `proposal.md` | `_PLAIN_TEXT_EXTENSIONS` matches | `TEXT_ONLY` | none |
| Native-text PDF | `_is_text_extractable_pdf=True` | `TEXT_WITH_LIGHT_ENRICHMENT` | none |
| Scanned PDF | `has_scanned_pages=True` (via extension or `text_extractable_ratio<0.1`) | `MULTIMODAL_FULL` | possible: stays MULTIMODAL_FULL |
| `report.pdf` (mostly text, one figure) | text-layer detected | `TEXT_WITH_LIGHT_ENRICHMENT` initially | post-compile finds `image_count=1` → may upgrade to `MULTIMODAL_LIGHT` |
| `data.xlsx` | `_LIKELY_TABLE_EXTENSIONS` matches | `TABLE_AWARE` | none |
| `business-proposal.docx` (with diagrams) | text-layer | `TEXT_WITH_LIGHT_ENRICHMENT` | post-compile finds `image_count>0` → may upgrade to `MULTIMODAL_LIGHT` |
| `paper.pdf` with equations | text-layer | `TEXT_WITH_LIGHT_ENRICHMENT` | post-compile finds `equation_count>5` → upgrade considered |
| Unknown extension | none | `TEXT_WITH_LIGHT_ENRICHMENT` (with warnings) | depends on compile |

---

## 5. Environment variables

Selection / policy:

| Variable | Default | Effect |
|---|---|---|
| `J1_INGEST_PLANNER_ENABLED` | `true` (dev) | Master switch. Off = legacy "kind=None → skip" mode. |
| `J1_INGEST_DEFAULT_POLICY` | `auto` | One of `auto`, `cost_saving`, `balanced`, `high_accuracy`, `force_full`, `text_only`. |

Planning Report stage (consumed by `GET /ingestion-runs/{id}/planning` and the FE Planning Report tab):

| Variable | Default | Effect |
|---|---|---|
| `J1_PLANNING_ENABLED` | `true` | Surface the Planning Report projection. Off → tab stays disabled even if a plan was generated. |
| `J1_POST_COMPILE_PLANNING_ENABLED` | `true` | Run the post-compile planning activity that produces `planning_result.json`. Off → workflow falls back to the initial `IngestPlan` only (no Document Understanding, no rich Execution Plan). |
| `J1_LLM_PLANNING_ENABLED` | `false` | Enable optional LLM-assisted planning. Default OFF — rule-based planning is the documented baseline. |
| `J1_PLANNING_MODEL_PROFILE` | `fast_planner` | Named LLM role used when LLM-assisted planning runs. `fast_planner` → fast role with text fallback; `premium_planner` → premium role with text fallback; `text` → text role. |
| `J1_PLANNING_MAX_SAMPLE_BLOCKS` | `20` | Privacy cap — max text blocks sampled into the planner LLM digest. |
| `J1_PLANNING_MAX_PREVIEW_CHARS` | `300` | Privacy cap — max characters per sampled block. |
| `J1_PLANNING_MAX_EARLY_PAGES` | `3` | Max early pages whose digest is built when title is unclear. |
| `J1_PLANNING_FAIL_OPEN` | `true` | When LLM planning fails, keep the rule-based decision and continue. |
| `J1_PLANNING_TRACE_ENABLED` | `false` | Log planning timing/decisions. Operator diagnostic only — leaves prompt bodies out. |
| `J1_PLANNING_TRACE_BODY` | `false` | Logs the digest body alongside the trace. **Off in production** — the digest is privacy-capped but reviewers are the right place to inspect it. |

Domain packs (selected during post-compile planning; see [DOMAIN_PACKS.md](DOMAIN_PACKS.md) for the full architecture):

| Variable | Default | Effect |
|---|---|---|
| `J1_DOMAIN_PACKS_ENABLED` | `true` | Master switch. Off → planner always selects `general`. |
| `J1_DEFAULT_DOMAIN` | `general` | Used when no override / workspace default / auto-detect signal applies. |
| `J1_DOMAIN_DETECTION_ENABLED` | `true` | Auto-detection switch. Off → only operator overrides can pick a non-generic domain. |
| `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` | `0.65` | Confidence floor for auto-detection. |
| `J1_ALLOWED_DOMAIN_OVERRIDES` | `general,civil_engineering` | Comma-separated allowlist of domain ids operators may force. |
| `J1_WORKSPACE_DEFAULT_DOMAIN` | `general` | Workspace / project default. Falls below user override but above auto-detection. |

Enrichment kill switches:

| Variable | Default | Effect |
|---|---|---|
| `J1_ENRICH_ENABLED` | `true` | Master enrich switch. Off → skip the entire enrich stage. |
| `J1_ENRICH_IMAGES` | `true` | When all three visual flags below are False, drops `VisualContentDescriber`. |
| `J1_ENRICH_TABLES` | `true` | False → drops `TableExtractor`. |
| `J1_ENRICH_DIAGRAMS` | `true` | (Visual triple — see images.) |
| `J1_ENRICH_SCANNED_PAGES` | `true` | False also forces `J1_RAGANYTHING_PARSE_METHOD=txt` (skip OCR). |
| `J1_ENRICH_CONFIDENCE_THRESHOLD` | `0.75` | Threshold for low-confidence findings in the Quality report. |

RAGAnything parser:

| Variable | Default | Effect |
|---|---|---|
| `J1_RAGANYTHING_PARSE_METHOD` | `auto` | `auto` / `txt` / `ocr`. `txt` skips OCR even on scanned pages. |
| `J1_RAGANYTHING_BACKEND` | `vlm-http-client` | MinerU inference backend. |
| `J1_RAGANYTHING_VLM_HTTP_SERVER_URL` | (inherits VISION LLM) | VLM endpoint when `backend=vlm-http-client`. |
| `J1_RAGANYTHING_VLM_HTTP_API_KEY` | (inherits VISION LLM) | |
| `J1_RAGANYTHING_VLM_HTTP_MODEL_NAME` | (inherits VISION LLM) | |

LLM roles:

| Variable | Default | Effect |
|---|---|---|
| `J1_TEXT_LLM_*` | (no default) | Text role config: provider, base URL, model, timeouts, context window. |
| `J1_FAST_LLM_*` | (no default) | Optional cheap role for `TEXT_ONLY` profile and similar. |
| `J1_VISION_LLM_*` | (no default) | Vision role for VLM calls. |
| `J1_EMBEDDING_*` | (no default) | Embedding role; always required for indexing. |
| `J1_*_LLM_CONTEXT_WINDOW_TOKENS` | (no default) | Per-role context window cap. Disables boundary check when unset. |
| `J1_*_LLM_MAX_OUTPUT_TOKENS` | (no default) | Output token cap per role. |
| `J1_*_LLM_SAFETY_MARGIN_TOKENS` | `256` | Subtracted from `available_input_tokens`. |

Token-budget enforcement is in [`src/j1/llm/budget.py`](../src/j1/llm/budget.py) and runs once per LLM call at the OpenAI-compat boundary.

---

## 6. Operational guidance

### Production default

`J1_INGEST_PLANNER_ENABLED=true` + `J1_INGEST_DEFAULT_POLICY=auto`. Lets the planner pick `TEXT_WITH_LIGHT_ENRICHMENT` for the bulk of documents and upgrade to `MULTIMODAL_*` when post-compile signals demand it. This is the framework's intended operating mode.

### Local dev / cost-saving

For laptop ingestion against a single LM Studio instance:

```
J1_INGEST_DEFAULT_POLICY=cost_saving
J1_ENRICH_IMAGES=false
J1_ENRICH_DIAGRAMS=false
J1_ENRICH_SCANNED_PAGES=false
MAX_ASYNC=1
MAX_GLEANING=0
```

This produces low-cost ingestion with minimal LLM traffic. Cost-saving policy biases the planner toward `TEXT_ONLY` / `TEXT_WITH_LIGHT_ENRICHMENT`.

### High-accuracy

`J1_INGEST_DEFAULT_POLICY=high_accuracy` to force the planner to pick the deeper profile when signals are uncertain. Combine with `J1_RAGANYTHING_PARSE_METHOD=auto` so MinerU's OCR fallback runs when needed.

### When to use `FULL_DIAGNOSTIC`

Only for offline benchmarking and accuracy comparisons. Set policy=`force_full` per-job, never as the default.

### Debugging "wrong profile selected"

1. **Check the run's Planning Report tab** — the FE renders the planner's chosen `mode`, per-step `decision` + `reason`, the Document Understanding section, and (when `J1_LLM_PLANNING_ENABLED=true`) the LLM advisory. The reason often points directly at the offending signal. Backed by `GET /ingestion-runs/{id}/planning`, which prefers the `planning_result` artifact when present and falls back to the `plan.generated` audit entry.
2. **Inspect the run's audit log** for `j1.progress.plan.generated` and `j1.progress.plan.revised` events. The `payload.plan` field carries the full plan.
3. **Look at `compile_content_stats`** — it's persisted alongside compile artifacts as a `parsed_content_manifest` artifact. The Quality tab summarises it; for raw access read the JSON directly.
4. **Verify `_NATIVE_TEXT_EXTENSIONS` is up to date** — if a new plain-text extension produces the slow path, the bridge's set may have drifted from the planner's `_PLAIN_TEXT_EXTENSIONS`.

### Debugging "MinerU was bypassed but I expected it"

The compile activity logs one of:
- `fast-text path: routing document '<id>' to direct LightRAG insert` (bypass)
- `full-parse path: routing document '<id>' to MinerU (parse_method=...)` (parser ran)

Grep the worker logs to confirm.

### Inspecting the parsed content manifest

The compile activity emits a `parsed_content_manifest` artifact per document. Fetch via:

```
GET /ingestion-runs/{run_id}/artifacts?kind=parsed_content_manifest
GET /ingestion-runs/{run_id}/artifacts/{artifact_id}/content
```

The JSON is the canonical post-parse stats — text/image/table/equation counts, page count, quality scores. Future tools can read this without re-walking the parser's storage directory.

---

## 7. Known limitations

- **Per-modality discrimination is coarse.** `VisualContentDescriber` doesn't currently differentiate decorative images from diagrams or scanned-page captures — operators disabling individual visual flags is mostly informational. The collective gate (all three off → drop VCD) works.
- **`ParsedContentManifest.items[]` is empty in current producers.** The bridge emits stats-only manifests because per-element data is heavy and the planner only consumes aggregates. Future consumers wanting per-element data should populate `items[]` explicitly.
- **Direct content-list reuse depends on parser/vendor support.** The current implementation re-runs the parser when settings change (cache key invalidates). Future work could let downstream stages re-read the cached manifest directly without invoking the parser.
- **Post-compile replan does not re-run compile.** Compile already executed; the revised plan only affects downstream optional stages. If the parser produced poor output (e.g. wrong `parse_method` was chosen), the operator must restart the run with corrected settings.
- **Premium LLM role is not wired today.** `requires_premium_llm=True` on the plan is a flag; without a premium role configured it routes to TEXT. Adding a premium role is straightforward but out of scope for the current framework.
- **MIME content sniffing covers only the common binary formats.** Magic-byte signatures for PDF, OOXML, and OLE2 are checked; other formats fall through to the extension allow-list.
- **Selective-page enricher gating is best-effort.** The post-compile plan emits per-page recommendations (e.g. `vision_enrichment.pages=[6, 12]`) but the current `CompositeEnricher` applies modality flags at startup, not per-document. The recommendations are recorded on the artifact + Planning Report tab and inform per-image triage via the existing `metadata["vision_decision"]` mechanism, but the enricher does not strictly limit work to those pages today. A future iteration that builds a per-document enricher instance can honor the page lists exactly.
- **LLM-assisted planning is feature-flagged and skeleton-tested.** When `J1_LLM_PLANNING_ENABLED=true`, the planner activity calls the configured `model_profile` role with a strict-JSON prompt and validates the output against the PlanningResult schema. The privacy contract is enforced two ways: only the digest (capped via `J1_PLANNING_MAX_*`) reaches the prompt, and the validator rejects any string > 4 KB on the way back so an LLM that echoes raw content is caught. Default is OFF until deployments wire a planner-tuned model.
- **The post-compile artifact is per-document, not per-run.** Multi-document runs produce one `planning_result.json` per document; the FE Planning Report currently renders the latest one. Aggregating multiple documents into a single run-level Planning Report is future work.
