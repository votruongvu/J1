# Adding a domain profile

This guide walks through registering a new `DomainPack` so a fresh
document family (e.g. legal contracts, healthcare records) reaches
the ingestion pipeline with the right prompts, hints, and policy.

For the contract surface see
[`domain-profiles.md`](../architecture/domain-profiles.md).

## Prerequisites

- A name + id for your domain (`legal_contracts`, `healthcare_v1`).
- Operator-facing display label.
- A list of the metadata fields the operator wants extracted.
- (optional) A glossary / terminology you want fed into retrieval.
- (optional) Per-module prompt overrides — start without these and
  add per module when the builtin prompts produce drift.

## Step 1 — Define the pack data

Domain packs live under `src/j1/domains/`. Each pack is a folder
with a `pack.py` builder + a `domain.yaml` data file:

```
src/j1/domains/legal_contracts/
├── __init__.py
├── pack.py
└── domain.yaml
```

`domain.yaml` is the operator-editable surface:

```yaml
# src/j1/domains/legal_contracts/domain.yaml
id: legal_contracts
display_name: Legal Contracts
version: "1.0"

# Detection — used at upload time when no override is supplied.
keyword_signals:
  - text: "indemnification"
    weight: 0.6
  - text: "force majeure"
    weight: 0.5

# Prompt addon — prepended to every per-module LLM prompt.
prompt_addon: |
  This document is a legal contract. Use formal legal terminology
  in your output; quote phrases verbatim where the contract
  language is the evidence.

# Per-module prompt overrides — empty by default; the adapter
# falls back to its builtin prompt when a slot is null.
prompt_pack:
  text_enrichment_prompt: |
    Extract clauses and obligations. For each clause return JSON
    with `id`, `text`, `priority` (MUST / SHOULD / MAY), `section`,
    `page`, and `cross_references[]` to other clause ids.
  classification_prompt: null
  table_enrichment_prompt: null
  image_enrichment_prompt: null
  validation_prompt: null

# Cheap hints the enrichment modules consume.
extraction_hints:
  metadata_fields:
    - "contract_party_a"
    - "contract_party_b"
    - "effective_date"
    - "governing_law"
  table_hints:
    - "Schedules / annexes: row-by-row obligation tables"
  terminology_hints:
    - "NDA = Non-Disclosure Agreement"
    - "MSA = Master Service Agreement"

# Per-domain validation rules.
validation_rules:
  required_metadata_fields:
    - "effective_date"
    - "governing_law"

# Enrichment policy.
enrichment_policy:
  policy: "always"                       # auto | always | never
  force_recommended_tasks:
    - "classification_enrichment"
  denied_tasks: []
  require_enrichment_success: false
  default_model_tier: "standard"
  reasoning: |
    Contract runs must always classify (clause vs schedule vs
    cover page) — denying that loses retrieval quality.
```

## Step 2 — Build the pack

The pack builder reads the YAML into typed dataclasses. For most
domains you can copy the civil-engineering pack's builder verbatim
([`src/j1/domains/civil_engineering/pack.py`](../../src/j1/domains/civil_engineering/pack.py))
and rename:

```python
# src/j1/domains/legal_contracts/pack.py
from pathlib import Path
from j1.domains.civil_engineering.pack import _load_yaml
from j1.domains.models import DomainPack
from j1.domains.civil_engineering.pack import (
    _parse_enrichment_policy,
    _parse_extraction_hints,
    _parse_validation_rules,
    _parse_prompt_pack,
    _parse_keyword_signals,
)

_YAML = Path(__file__).with_name("domain.yaml")


def build_legal_contracts_pack() -> DomainPack:
    data = _load_yaml(_YAML)
    return DomainPack(
        id=data["id"],
        display_name=data["display_name"],
        version=data["version"],
        keyword_signals=_parse_keyword_signals(data.get("keyword_signals")),
        prompt_addon=data.get("prompt_addon", ""),
        prompt_pack=_parse_prompt_pack(data.get("prompt_pack")),
        extraction_hints=_parse_extraction_hints(data.get("extraction_hints")),
        validation_rules=_parse_validation_rules(data.get("validation_rules")),
        enrichment_policy=_parse_enrichment_policy(data.get("enrichment_policy")),
    )
```

## Step 3 — Register the pack

Add the builder to the default registry in
[`src/j1/domains/registry.py`](../../src/j1/domains/registry.py):

```python
def default_registry() -> DomainRegistry:
    return DomainRegistry(packs=(
        build_general_pack(),
        build_civil_engineering_pack(),
        build_legal_contracts_pack(),    # ← new
    ))
```

## Step 4 — Pin the pack with tests

Each pack ships with a small test that asserts its loaded shape so
a YAML typo doesn't silently break behaviour. Mirror the
civil-engineering pack's tests:

```python
# tests/test_legal_contracts_pack.py
from j1.domains.legal_contracts.pack import build_legal_contracts_pack


def test_legal_contracts_pack_loads():
    pack = build_legal_contracts_pack()
    assert pack.id == "legal_contracts"
    assert "effective_date" in pack.extraction_hints.metadata_fields
    assert pack.enrichment_policy.policy == "always"
    assert pack.prompt_pack.text_enrichment_prompt is not None
```

## What MUST NOT happen

- **Do not** add `if pack.id == "legal_contracts"` branches in
  workflow / activity / module code. Every per-domain decision
  flows through the typed contracts.
- **Do not** hard-code your domain's vocabulary inside an
  enrichment module's builtin prompt — those must stay generic.
- **Do not** override the `general` pack's behaviour to make your
  domain win. Resolution is `override → workspace default →
  general` and lives in `select_domain()`.

## Optional — workspace default

To make a project's runs default to your pack when no override is
passed, the workspace setting `workspace_default_domain` (REST
adapter level) accepts the pack id. The selector picks it up at
upload time.

## Related pages

- [Domain profiles](../architecture/domain-profiles.md)
- [Enrichment overlay](../architecture/enrichment-overlay.md)
