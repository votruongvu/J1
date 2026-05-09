# Domain Packs

How J1 layers domain-specific planning on top of the generic post-compile planner without coupling core to any one industry.

This document is the operator's lens onto the Domain Pack system. The running code in [`src/j1/domains/`](../src/j1/domains/), [`src/j1/processing/post_compile_planning.py`](../src/j1/processing/post_compile_planning.py), and [`src/j1/processing/planning_result.py`](../src/j1/processing/planning_result.py) is authoritative.

---

## 1. What is a Domain Pack?

A **Domain Pack** is a pluggable bundle that extends J1's generic post-compile planner with domain-aware decisions. Each pack supplies:

* a stable **id** (`general`, `civil_engineering`, …)
* an **extended document-type taxonomy** (e.g. `boq`, `inspection_report`, `method_statement`)
* **keyword + structural detection rules** with confidence weights
* **per-document-type planning overlays** (recommended profile, chunking strategy, per-step `enabled / scope / pages / reason`)
* an **extraction-target catalogue** (planning hints for downstream extractors)
* a **graph ontology** (entity + relationship types)
* an optional **LLM prompt addon** appended to the planner system prompt

The generic planner is the always-available baseline. Domain packs **augment** it — they don't replace it. When no pack matches above the configured confidence threshold, the planner falls back to the generic plan.

The bundled packs are:

| Pack | Status | What it does |
|---|---|---|
| `general` | always present | No-op fallback. Stable id so consumers don't special-case "no domain". |
| `civil_engineering` | v0.1 | Recognises construction / infrastructure / inspection documents and tunes planning accordingly. |

---

## 2. Selection order

J1 picks the active domain pack per document, in this precedence:

```
1. User upload override        ← per-run, validated against allow-list
2. Workspace / project default ← deployment-supplied default
3. Auto-detection              ← keyword + structural scoring
4. Generic fallback            ← always available
```

Auto-detection only selects a non-generic pack when the detector clears `J1_DOMAIN_DETECTION_MIN_CONFIDENCE`. Operator overrides (steps 1 & 2) are honored even when the evidence is weak; a warning is recorded on `domain_context.warnings` so reviewers see the override forced the choice.

The chosen pack is recorded on `planning_result.json` as `domain_context`:

```json
{
  "domain_context": {
    "selected_domain": "civil_engineering",
    "selection_source": "auto_detected",
    "confidence": 0.91,
    "domain_pack_version": "0.1",
    "evidence": [
      "Matched signals: 'bill of quantities', 'boq'.",
      "Table headers match BOQ-shaped row structure.",
      "Title 'Bill of Quantities — Road Drainage Works' carries the strongest signal.",
      "Rule detect.boq → document_type=boq."
    ],
    "applied_domain_rules": [
      "civil_engineering.detect.boq",
      "civil_engineering.plan.boq",
      "civil_engineering.plan.boq.table_enrichment"
    ],
    "warnings": [],
    "recommended_but_unsupported": [
      {
        "capability": "boq_table_normalisation",
        "reason": "BOQ tables are extracted as generic tables; no BOQ-specific row-level normaliser yet."
      }
    ]
  }
}
```

---

## 3. Configuration

Domain pack behaviour is controlled via `J1_DOMAIN_*` env vars (see [INGESTION_PROFILES.md](INGESTION_PROFILES.md) §5 for the full table):

| Variable | Default | Effect |
|---|---|---|
| `J1_DOMAIN_PACKS_ENABLED` | `true` | Master switch. Off → planner always selects `general`. |
| `J1_DEFAULT_DOMAIN` | `general` | Used when no override / workspace default / auto-detect signal applies. |
| `J1_DOMAIN_DETECTION_ENABLED` | `true` | Auto-detection switch. Off → only operator overrides can pick a non-generic domain. |
| `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` | `0.65` | Confidence floor for auto-detection. |
| `J1_ALLOWED_DOMAIN_OVERRIDES` | `general,civil_engineering` | Comma-separated allowlist of domain ids operators may force. Defence against typos / unauthorised packs. |
| `J1_WORKSPACE_DEFAULT_DOMAIN` | `general` | Workspace / project default. Falls below user override but above auto-detection. |

Per-run override is plumbed via `ProjectProcessingRequest.domain_override` (workflow request) → `BuildPlanningResultInput.domain_override` (planning activity). Workspace default flows through `workspace_default_domain` on the same payloads. Both are validated against the allow-list inside the planning activity; an unrecognised value falls back to `general` with a warning.

---

## 4. Domain detection: how it scores

Each non-generic pack supplies a `detect()` callable that takes a `DetectionContext` (title, filename, early-page text, heading outline, table captions, image captions, table header rows) and returns a `DomainDetectionResult` with a confidence in `[0, 1]`.

The Civil Engineering pack scores in two passes:

1. **Per-rule keyword scoring.** Each detection rule (`detect.boq`, `detect.inspection_report`, …) declares a list of signals + a `min_score` + a `bonus`. A rule fires when its signals + table-header signature accumulate ≥ `min_score`; the score is then bumped by `bonus`. Rules with the highest score win; the rule's `document_type` becomes the pack's detected type.
2. **Pack-level baseline.** When no specific rule fires but the document still mentions civil vocabulary, the pack-level keyword catalogue gives a baseline confidence so reviewers see the partial signal.

A document with title "Quarterly Business Review" + sales/revenue copy hits no civil signals → `confidence=0.0` → fallback to `general`.

A document with title "Bill of Quantities — Road Drainage Works" + BOQ-shaped table headers → `confidence=1.0` (capped) → `selected_domain=civil_engineering` + `document_type=boq`.

---

## 5. Civil Engineering Domain Pack v0.1

### Recognised document types

`construction_drawing`, `structural_drawing`, `architectural_drawing`, `mep_drawing`, `boq`, `quantity_takeoff`, `tender_document`, `bid_proposal`, `method_statement`, `inspection_report`, `site_report`, `daily_site_report`, `progress_report`, `rfi`, `material_submittal`, `shop_drawing_submittal`, `test_report`, `structural_calculation`, `design_specification`, `technical_specification`, `variation_order`, `change_order`, `non_conformance_report`, `safety_report`, `quality_report`, `handover_document`, `as_built_document`, `unknown_civil_document`.

These extend the generic 25-entry taxonomy, not replace it. The validator's allow-list is the union of `DOCUMENT_TYPES` ∪ every registered pack's `extends_document_types`.

### Planning behaviour

Per-document-type overlays sit on top of the generic rule-based assessment. When a civil rule fires, the matching overlay sets:

* `recommended_profile` — typically `premium` for BOQ/inspection/drawings/tenders/calculations
* `chunking_strategy` — `section_aware` (BOQ, method statement, calculation), `page_aware` (drawings, inspection report)
* per-step `step_overrides` — e.g. BOQ disables vision and graph; inspection report enables both; drawings enable vision + image captioning + quality assessment

The overlays only set the steps they care about. Steps the overlay doesn't mention fall through to the generic decision.

### Graph ontology v0.1

Entities: `Project`, `Site`, `Location`, `Zone`, `Level`, `WorkPackage`, `Drawing`, `Revision`, `Specification`, `Standard`, `Material`, `StructuralElement`, `ConstructionActivity`, `Inspection`, `InspectionFinding`, `Defect`, `TestReport`, `TestResult`, `Contractor`, `Consultant`, `Client`, `Engineer`, `Approval`, `RFI`, `VariationOrder`, `ChangeOrder`, `NCR`, `Risk`, `ActionItem`.

Relationships (selection): `Drawing_describes_StructuralElement`, `Defect_located_at_Location`, `TestResult_compared_to_AcceptanceCriteria`, `Contractor_responsible_for_WorkPackage`, `NCR_caused_by_Defect`.

Graph extraction is **not** enabled by default for every civil document — the overlay enables it for inspection reports, NCRs, method statements, drawings, structural calculations, and tender documents where relationships are present. BOQ and test reports keep graph disabled.

### Recommended-but-unsupported capabilities

The pack declares capabilities it would use but the framework doesn't ship today; these surface on the planning result so the backlog stays visible:

| Capability | Reason |
|---|---|
| `action_item_extraction` | No dedicated extractor; method statements / NCRs / inspection reports would benefit. |
| `responsibility_extraction` | No dedicated extractor; relies on graph step's entity extraction. |
| `process_step_extraction` | Method statement step sequencing relies on chunking + downstream LLM analysis. |
| `drawing_title_block_extraction` | Title-block fields require a vision-driven structured extractor. |
| `boq_table_normalisation` | BOQ tables extracted as generic tables; no row-level normaliser. |
| `code_or_standard_reference_extraction` | No dedicated extractor for ACI/EN/BS/AASHTO references. |

These never block ingestion — they're documented intent. Adding a real implementation later is a one-line removal from `domain.yaml`.

### LLM prompt addon

When `J1_LLM_PLANNING_ENABLED=true` AND the civil pack is selected, the pack's `prompt_addon` is appended to the planner system prompt. It tells the LLM to focus on drawings, BOQ tables, inspection findings, defects, structural elements, etc., and to recommend fallback to generic planning when the document doesn't clearly belong to civil engineering.

---

## 6. Adding a new domain pack

1. Create `src/j1/domains/<pack_id>/`.
2. Author `domain.yaml` with the pack's id, version, document_types, keyword_signals, detection_rules, overlays, extraction_targets, graph_entity_types, graph_relationship_types, unsupported_capabilities, and prompt_addon. Mirror [`src/j1/domains/civil_engineering/domain.yaml`](../src/j1/domains/civil_engineering/domain.yaml) as a template.
3. Add `pack.py` with a `build_<pack_id>_pack()` factory that reads `domain.yaml` and returns a `DomainPack`. The civil pack's [`pack.py`](../src/j1/domains/civil_engineering/pack.py) shows the YAML loader + detection scorer pattern — most new packs only customise the keyword catalogue and overlays.
4. Register the pack in `src/j1/domains/registry.py::default_registry()`.
5. Add the pack id to `J1_ALLOWED_DOMAIN_OVERRIDES` in your deployment env.
6. Add tests under `tests/test_<pack_id>_*.py` mirroring `tests/test_domain_planning_integration.py` — pin (a) auto-detection picks the pack on a representative document, (b) generic documents still fall back, (c) per-document-type overlays produce the expected execution plan.

The framework's domain-neutral guard (`tests/extension/test_guards.py::test_no_domain_terms_in_j1_core`) exempts the `domains/` directory but enforces neutrality everywhere else — keep domain-specific code inside the pack.

---

## 7. Operational guidance

### When operators should set `J1_WORKSPACE_DEFAULT_DOMAIN`

A workspace dedicated to construction projects should set `J1_WORKSPACE_DEFAULT_DOMAIN=civil_engineering` so even ambiguous-titled uploads (e.g. `Final_Report.pdf`) get civil planning. The auto-detect path still runs for evidence; the default just biases the choice when detection is inconclusive.

### When NOT to force `domain_override`

Per-upload overrides should be reserved for cases where the operator has out-of-band knowledge the planner can't see (e.g. a generic-titled PDF that's actually a BOQ extracted from a vendor portal). For routine uploads, trust auto-detection — it's deterministic, cheap, and audit-logged.

### Debugging "wrong domain selected"

1. **Check the run's Planning Report tab.** The Domain pack panel shows `selected_domain`, `selection_source` (user / workspace / auto_detected / fallback_general), `confidence`, and the matched evidence. `applied_domain_rules` lists the rule ids that fired.
2. **Inspect `domain_context.candidates`.** All scored candidates are recorded with their per-pack confidence; the selected one is the highest.
3. **Bump `J1_PLANNING_TRACE_ENABLED=true`** to log planning timing + decision metadata; `J1_PLANNING_TRACE_BODY=true` adds the digest body (off in production).

### Known limitations (Civil Engineering v0.1)

* Detection is keyword + structural-only. Documents with no English-language title or scanned-only content (text not extractable) won't trigger civil detection until OCR / vision inspect them.
* The pack's `overlays` only adjust generic step decisions; selective-page recommendations (e.g. `vision_enrichment.pages=[6, 12]`) are recorded on the artifact but the existing `CompositeEnricher` doesn't strictly enforce them today (see INGESTION_PROFILES.md § Known limitations).
* Graph ontology is documented as a planning hint. The graph builder consumes `candidate_entity_types` when supported but doesn't yet enforce the relationship types — those land on the planning result for future graph work.
* The pack ships a single LLM prompt addon. Per-document-type prompt customisation (e.g. distinct prompt for BOQ vs inspection report) is future work.
