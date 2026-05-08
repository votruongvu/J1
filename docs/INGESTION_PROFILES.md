# Ingestion Profiles

How J1 picks an ingestion strategy per file, what each strategy actually does, and how to debug when the wrong one fires.

This document is the operator's lens onto J1's existing ingestion pipeline — it explains the contract, not the implementation. The running code in [`src/j1/processing/planning.py`](../src/j1/processing/planning.py), [`src/j1/processing/ingestion_profiles.py`](../src/j1/processing/ingestion_profiles.py), and [`src/j1/orchestration/workflows/project_processing.py`](../src/j1/orchestration/workflows/project_processing.py) is authoritative.

---

## 1. Overview

J1 ingestion runs through six stages:

```
upload → profile → plan → compile → (post-compile replan) → enrich → graph → index → review
```

The framework does **not** run every stage for every document. The planner reads cheap deterministic signals (file extension, size, native-text PDF detection) and picks an `IngestMode` that determines which optional stages run. The compile stage is always required; everything else is gated by the plan.

After compile, the parser (RAGAnything / MinerU) returns content statistics — image count, table count, scanned-page hints — that may not have been visible from the deterministic profile alone. The workflow re-runs the planner with these post-compile signals and emits a `j1.progress.plan.revised` event when the new plan unlocks downstream stages the original plan skipped.

The parser-output boundary is captured as a `ParsedContentManifest` artifact, persisted alongside compile output. Downstream consumers (post-compile replan, the Quality tab, future tools) read it without re-walking the storage directory.

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

1. **Check the run's plan card** — the FE displays the planner's chosen `mode` and per-step `reason`. The plan reason often points directly at the offending signal.
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
