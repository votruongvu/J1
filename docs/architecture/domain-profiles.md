# Domain profiles (DomainPack)

A **domain profile** is a typed bundle of data + adapters that tunes
the ingestion pipeline to a specific document family (civil
engineering, legal, healthcare, …). The profile is **data and
adapters, not workflow logic** — every behavioural difference flows
through typed contracts.

This page explains the contracts. For a step-by-step recipe see
[Adding a domain profile](../guides/adding-a-domain-profile.md).

## The contract

A profile is a `DomainPack` ([`src/j1/domains/models.py`](../../src/j1/domains/models.py))
with five typed slots:

```python
@dataclass(frozen=True)
class DomainPack:
    id: str
    display_name: str
    version: str
    keyword_signals: tuple[KeywordSignal, ...]      # detection only
    prompt_addon: str                                # prepended to every prompt
    prompt_pack: DomainPromptPack                    # per-module prompt overrides
    extraction_hints: DomainExtractionHints          # metadata / table / image / terminology
    validation_rules: DomainValidationRules          # required fields, format checks
    enrichment_policy: DomainEnrichmentPolicy        # auto / always / never + force lists
    ...
```

Every other typed slot has an empty default — a pack that fills
none of them behaves like the `general` baseline.

## The contracts in detail

### `DomainEnrichmentPolicy`

Controls whether enrichment runs, which modules are forced, and
whether failure fails the run.

```python
policy: str = ENRICHMENT_POLICY_AUTO          # auto | always | never
force_recommended_tasks: tuple[str, ...] = () # tasks the assessor must include
optional_tasks: tuple[str, ...] = ()           # tasks promoted from skipped → optional
denied_tasks: tuple[str, ...] = ()             # tasks the assessor must NOT include
require_enrichment_success: bool = False       # FAILED enrichment → fail the run
default_model_tier: str | None = None          # fast / standard / premium
reasoning: str = ""                            # operator-readable explanation
```

Precedence: per-run override → project default → domain policy →
env default → system default. The resolver is in
[`enrichment_policy.py`](../../src/j1/processing/enrichment_policy.py).

### `DomainPromptPack`

Per-module prompt overrides. Each slot is `Optional[str]`; the
matching adapter falls back to its `_BUILTIN_PROMPT` when absent.

```python
text_enrichment_prompt: str | None
metadata_enrichment_prompt: str | None
table_enrichment_prompt: str | None
image_enrichment_prompt: str | None
classification_prompt: str | None
validation_prompt: str | None
```

`prompt_addon` is prepended to whichever base prompt wins — domain
context first, task-specific instructions second.

### `DomainExtractionHints`

Cheap hints the enrichment modules read at runtime:

```python
metadata_fields: tuple[str, ...]        # e.g. ("project_number", "drawing_revision")
entity_hints: tuple[str, ...]
table_hints: tuple[str, ...]            # e.g. ("BOQ tables: item / qty / rate columns")
image_hints: tuple[str, ...]
terminology_hints: tuple[str, ...]      # e.g. ("RFI = Request for Information")
```

The metadata module reads `metadata_fields` to decide what to target;
the terminology module reads `terminology_hints` to seed its
overlay.

### `DomainValidationRules`

Per-domain checks the validation module runs:

```python
required_metadata_fields: tuple[str, ...]   # used by post-compile assessor too
format_checks: tuple[...]
```

### Keyword signals (detection only)

`keyword_signals` is consumed by `select_domain()` at the
upload-handler layer when no override is supplied. It's **not** used
by the enrichment pipeline at runtime.

## Where domain logic lives — and where it doesn't

| Concern | Where it goes |
|---|---|
| Prompt for one module | `DomainPromptPack.<module>_prompt` |
| Cross-module preamble | `DomainPack.prompt_addon` |
| "Always extract field X" | `DomainExtractionHints.metadata_fields` |
| "Require this run to enrich" | `DomainEnrichmentPolicy.require_enrichment_success` |
| "Skip this module for civil docs" | `DomainEnrichmentPolicy.denied_tasks` |
| "Synonym for X is Y" | `DomainExtractionHints.terminology_hints` |
| "BOQ tables have this column shape" | `DomainExtractionHints.table_hints` |
| "Reject if field X format is wrong" | `DomainValidationRules.format_checks` |

### What MUST NOT happen

- `if ctx.domain_pack.id == "civil_engineering": ...` in any
  workflow / activity / enrichment module. Domain checks belong in
  data, not code.
- Hard-coding civil/legal/healthcare vocabulary in builtin prompts.
  Adapter-side builtin prompts must stay generic.
- New code paths that branch on `domain_id`. The pipeline reads
  the typed contracts; domain id is for logging only.

Tests guard against drift:

- `test_legacy_enricher_modules_source_has_no_hardcoded_civil_terms`
- `test_wrappers_have_no_hardcoded_civil_engineering_terms`
- `test_civil_engineering_only_referenced_in_docstrings`

## Backward compatibility

Existing runs without a domain pack continue to work. The
`general` pack (built-in) provides:

- Empty extraction hints + validation rules.
- An empty `prompt_pack` — all enrichers fall back to their
  builtin prompts.
- `enrichment_policy = AUTO` + `require_enrichment_success = False`
  — assessor decides per-run.

A pack that lands midway through a project's run history doesn't
break older runs: each run records its `domain_profile_id` on the
`initial_execution_plan` artifact, and the final report carries it
on `domain_profile_id`. Switching packs going forward is safe.

## Related pages

- [Adding a domain profile](../guides/adding-a-domain-profile.md)
- [Adding an enrichment module](../guides/adding-an-enrichment-module.md)
- [Enrichment overlay](./enrichment-overlay.md)
