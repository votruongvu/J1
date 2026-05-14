# 10. Domain Configuration

> Audience: engineers + product owners customising J1 for a
> specific domain.
> [Back to README](../README.md). See also
> [02-ingestion-flow.md](02-ingestion-flow.md),
> [03-query-flow.md](03-query-flow.md).

## What a domain config is

A **domain pack** customises how J1 ingests and answers questions
for a particular subject area. Civil engineering documents need
different extraction targets than legal briefs; a generic pipeline
gives generic results.

A pack supplies:

- **Detection signals** — keyword + structural rules that let J1
  auto-pick the pack for an uploaded document.
- **Extraction hints** — metadata fields, entity types, table
  shapes, image hints the enrichers consume.
- **Validation rules** — required metadata fields, expected
  document structure, low-quality warning conditions, enrichment
  triggers. Consumed by the post-compile assessor.
- **Per-enricher prompt overrides** — replace the default
  per-enricher prompt (table / image / metadata / classification /
  validation / text).
- **Pack-wide prompt addon** — a paragraph appended to every
  enricher's prompt (and the synthesizer's at query time).
- **Enrichment policy** — force-recommended tasks, denied tasks,
  optional tasks. Drives the post-compile enrich plan.
- **Planning overlays** — per-document-type recommended profiles
  + chunking strategies.

There is **no** generated-test-case question template. The
2026-05-14 product change removed the `validation_guidance`
question generator entirely; domain packs only influence
ingestion + query, not test-case authorship.

## Where domain configs live

```
src/j1/domains/
  __init__.py                 # Exports.
  models.py                   # Typed config dataclasses + Domain pack class.
  registry.py                 # DomainRegistry + default_registry().
  general.py                  # The no-op default pack.
  civil_engineering/
    __init__.py
    pack.py                   # YAML loader → DomainPack.
    domain.yaml               # The actual configuration.
```

Domain packs are constructed once at startup and held by the
`DomainRegistry`. Workflows + activities receive a `DomainContext`
that resolves to the active pack per ingestion / per query.

## The general pack

The `general` pack is the always-available baseline. It supplies
no overlays, no extraction hints, and no enrichment policy. It
keeps every default — generic chunking, generic prompts, no
domain-specific validation. Every J1 deployment has it.

A document that doesn't match any specialised pack falls through
to `general`. The pipeline still works; it just produces a
generic interpretation.

## The civil_engineering pack (worked example)

`src/j1/domains/civil_engineering/domain.yaml` is the reference
implementation. It defines:

- Keyword signals that detect civil engineering documents (e.g.
  "method statement", "design code", "soil report").
- Detection rules that combine signals into a confidence score.
- Per-document-type overlays: a method statement gets a different
  recommended profile + chunking strategy than a design report.
- Extraction hints: metadata fields like `project_scope`,
  `design_assumptions`; entity types like `Material`, `Standard`;
  table hints for BOQs and inspection schedules.
- Validation rules: required metadata fields, expected document
  structure phrases, low-quality conditions ("page_count > 50 but
  text_chars < 5000 ⇒ likely scanned-only").
- Per-enricher prompt overrides: a table prompt focused on civil
  schedules, an image prompt that looks for drawing title blocks.
- A pack-wide prompt addon that gives the LLM the domain frame
  without leaking it as evidence.
- Enrichment policy: `requirement_extraction` is force-recommended
  for method statements; `risk_extraction` is force-recommended
  whenever a risk register is detected.

Use it as the template when authoring a new pack.

## How domain config affects ingestion

### Detection

`DomainRegistry.detect(profile, document)` returns a
`DomainDetectionResult` per document at ingestion start. The
result carries:

- The selected `DomainPack` (or `general` if no specialised pack
  matched).
- A `DomainSelectionSource` (signal-driven / explicit override /
  workspace default).
- Detection metadata (which keywords matched, with what weights).

`ProjectProcessingRequest.domain_override` (operator-set) wins
over detection. `workspace_default_domain` is the project default
when no signal matched.

### Compile

Compile sees the pack via the assessment plan:

- The pack's `extraction_targets` are added to the plan's
  capabilities list. Compile fans out parse modes accordingly.
- The pack's overlays per document-type drive parse-method choice
  (`recommended_profile`) and chunking strategy.
- The pack has no direct knowledge of compile internals — it
  contributes hints, not implementation.

### Post-compile enrichment

The enrichment plan combines:

- Compile evidence (which capabilities the parser surfaced).
- The pack's `validation_rules.enrichment_triggers` (rules like
  "document_type == method_statement implies risk_extraction").
- The pack's `enrichment_policy.force_recommended_tasks` (always
  run these for this domain).
- The pack's `enrichment_policy.denied_tasks` (never run these).

For each task that fires, the matching enricher is run with the
pack's per-enricher prompt override (when set) plus the pack-wide
`prompt_addon`.

### Validation rules

The post-compile result analyser surfaces a `low_quality` warning
when the pack's `low_quality_warning_conditions` match. These do
not fail the run — they bias the final report and the operator UI
toward "consider re-ingesting with different settings".

## How domain config affects query

### Retrieval planning

- The pack's `extraction_targets` and entity types feed the
  reranker. Chunks tagged with a domain entity type get a small
  boost when the query's intent matches.
- Per-document-type overlays don't apply at query time today
  (they're ingestion-only).

### Synthesis

- The pack's `prompt_addon` is appended to the synthesizer's
  prompt for every query routed to a document tagged with that
  pack. The prompt addon is a *frame*, not evidence — the
  synthesizer is instructed to ground claims in retrieved chunks.
- Per-enricher overrides are ingestion-only — they shape the
  artifacts retrieval picks up.

### Citations

Citations are domain-agnostic. The binder validates the cited set
against the selected pack regardless of which domain the
document belongs to.

## What should remain generic vs domain-specific

Generic (live in core; don't override per-domain):

- Chunk size + overlap strategy (compile picks per parse mode).
- The set of intents the orchestrator classifies (16 generic
  intents).
- Eligibility rules (snapshot-centered, lifecycle-gated).
- Citation binder behaviour.
- The synthesiser's core prompt structure.

Domain-specific (override via the pack):

- Per-document-type recommended parse profile.
- Extraction hints (metadata fields, entity types, terminology).
- Validation rules + enrichment triggers.
- Per-enricher prompt overrides.
- Pack-wide prompt addon.

If you find yourself reaching for a per-document override, that's
a sign the domain pack is missing a knob — extend the pack, don't
hard-code in core.

## How to add a new domain pack

1. **Create the YAML.** Copy
   `src/j1/domains/civil_engineering/domain.yaml` as a template
   and edit the sections you care about. The schema is enforced by
   the loader; missing blocks fall back to dataclass defaults.

2. **Implement the builder.** Create
   `src/j1/domains/<name>/__init__.py` and
   `src/j1/domains/<name>/pack.py`:

   ```python
   from j1.domains.models import DomainPack
   # ...
   def build_<name>_pack() -> DomainPack:
       data = _load_pack_data(Path(__file__).parent / "domain.yaml")
       return DomainPack(
           id=str(data["id"]),
           display_name=str(data.get("display_name") or "<Name>"),
           version=str(data.get("version") or "0.1"),
           keyword_signals=...,
           extraction_hints=...,
           validation_rules=...,
           prompt_pack=...,
           enrichment_policy=...,
           # detect=...
       )
   ```

3. **Register it.** Add `build_<name>_pack` to
   `src/j1/domains/registry.py::default_registry()` so the
   bootstrap wires it. Pack ids are unique across the registry.

4. **Test it.** Add `tests/test_<name>_pack.py` that:
   - Loads the YAML through the builder.
   - Confirms the pack's detection rules fire on a fixture
     document.
   - Confirms the enrichment policy + prompt addon are populated.

5. **Document it.** Add a short section to this file describing
   what the pack is for and which document types it recognises.

## Known limitations of current domain behaviour

- Detection is keyword + structural rule only. A learned
  classifier would be more robust but isn't implemented.
- Detection is per-document; there is no "this whole project is
  civil engineering" override at the project level (the
  workspace default helps, but doesn't enforce).
- Per-domain *retrieval* differs only via reranker boosts. There
  is no domain-specific retrieval pipeline.
- The synthesizer prompt is shape-specific (per intent), not
  domain-specific. The domain `prompt_addon` is appended but the
  core prompt is shared.
- The civil engineering pack is the only specialised example.
  Other domains are an exercise for the integrator.
- Domain packs cannot be hot-reloaded — the registry is built at
  startup. Pack changes require a process restart.

## How to verify a pack is working

After registering and restarting:

1. Upload a sample document to a fresh project. Watch the
   `j1.domain.detected` audit event — it logs the pack id and
   the match score.
2. Look at the `final_summary` artifact for the run; the
   `domain_id` field reflects the active pack.
3. Run a Manual Test Query and check the orchestrator trace's
   `plan.intent_signals` — domain entity types should appear in
   the candidate set for relevant questions.
4. Confirm enrichment artifacts were produced for the tasks the
   pack's policy forced.
