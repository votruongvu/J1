# J1 Docs Index

> Map of every doc under `docs/`. Use this as the entry point —
> [the top-level README](../README.md) links here, and the numbered
> files cross-reference each other from inside.

If you're looking for a doc by an intuitive name (`architecture`,
`deployment`, etc.), the **Common-name lookup** below points at the
numbered file that covers it. The full index below that lists every
doc with its purpose.

---

## Common-name lookup

| You're looking for… | Read this |
|---|---|
| `architecture.md` (system overview) | [01-overall-architecture.md](01-overall-architecture.md) |
| `ingest-flow.md` / `ingestion-flow.md` | [ingestion-flow.md](ingestion-flow.md) (canonical) |
| `query-flow.md` | [03-query-flow.md](03-query-flow.md) |
| `domain-enrichment.md` (configuration) | [10-domain-configuration.md](10-domain-configuration.md) |
| `settings.md` | [settings.md](settings.md) |
| `deployment.md` | [07-deployment-and-scaling.md](07-deployment-and-scaling.md) |
| `developer-onboarding.md` | [05-developer-onboarding.md](05-developer-onboarding.md) |
| `risks-and-limitations.md` | [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md) |
| `data-model.md` | [04-core-data-model.md](04-core-data-model.md) |
| `multi-kb.md` / multi-tenant model | [08-multi-kb-model.md](08-multi-kb-model.md) |
| `external-integration.md` / REST + events | [09-external-integration-model.md](09-external-integration-model.md) |
| `execution-profiles.md` | [11-ingestion-execution-profiles.md](11-ingestion-execution-profiles.md) |
| Future retrieval levels (roadmap) | [12-retrieval-intelligence-roadmap.md](12-retrieval-intelligence-roadmap.md) |
| Memory / query-layer projection contract | [unified-memory-contract.md](unified-memory-contract.md) |

If you searched for a name not in this table, the docs may use a
different vocabulary — search this page first or open the full
index below.

---

## Full index

### Architecture and flow

- [01-overall-architecture.md](01-overall-architecture.md) — Business-friendly system overview. Start here if you're new.
- [ingestion-flow.md](ingestion-flow.md) — **Canonical** end-to-end ingestion: upload → assess → compile → promote → optional Domain Enrichment.
- [02-ingestion-flow.md](02-ingestion-flow.md) — **Superseded stub.** Kept so legacy links resolve; points at `ingestion-flow.md`.
- [03-query-flow.md](03-query-flow.md) — Manual query → orchestrator → answer + citations. Covers the three surfaces (manual-test, dev trace, native debug).
- [04-core-data-model.md](04-core-data-model.md) — Tenant / Project / Document / Snapshot / Run / Profile / Knowledge Base.
- [unified-memory-contract.md](unified-memory-contract.md) — Logical projection the query layer reads through. Read after `04-core-data-model.md`.

### Operator and deployment

- [05-developer-onboarding.md](05-developer-onboarding.md) — Run, test, extend the codebase locally.
- [07-deployment-and-scaling.md](07-deployment-and-scaling.md) — Production direction, worker model, scaling shapes.
- [settings.md](settings.md) — Environment-variable reference for every `J1_*` setting. Source of truth for `.env.example`.
- [06-risks-and-known-limitations.md](06-risks-and-known-limitations.md) — Where intent and code don't yet line up. Read before promising features.

### Domain and product surface

- [08-multi-kb-model.md](08-multi-kb-model.md) — How the per-project knowledge bases compose; multi-tenant scoping.
- [09-external-integration-model.md](09-external-integration-model.md) — REST + event integration with outside systems.
- [10-domain-configuration.md](10-domain-configuration.md) — Domain packs (general + civil engineering); enrichment hints and detection.
- [11-ingestion-execution-profiles.md](11-ingestion-execution-profiles.md) — Execution profiles: `minimum_queryable` / `standard` / `advanced`.

### Roadmap

- [12-retrieval-intelligence-roadmap.md](12-retrieval-intelligence-roadmap.md) — Staged retrieval intelligence: alias broadening (current default) → LLM rewrite → graph expansion → answer grading.

---

## Conventions

- Numbered files (`NN-name.md`) form the architecture series. Order
  matters — they cross-reference forward and back.
- Unnumbered files (`ingestion-flow.md`, `settings.md`,
  `unified-memory-contract.md`) are canonical references the
  numbered files link into.
- A new doc should land in this index AND in the top-level
  [README.md](../README.md)'s short list. The test in
  `tests/test_docs_index.py` enforces that every doc here resolves.
