# Deprecated documentation tracker

This page is the single source of truth for documentation that
has been **deprecated or deleted** during the post-refactor
documentation cleanup. New contributors hitting an old link or a
search result for a removed concept should land here and get
pointed at the current authoritative page.

## Deleted docs

These four files described removed concepts (`DefaultIngestPlanner`
/ `IngestPlan` / `IngestPolicy`, "split mode" / "complete mode",
pre-compile graph/index gating). They were superseded entirely by
the current architecture docs and are no longer in the repo.

| Removed doc | Reason | Replacement |
|---|---|---|
| `docs/INGESTION_PROFILES.md` | Described the legacy pre-compile `DefaultIngestPlanner` / `IngestPlan` / `IngestPolicy` system that has been removed. | [`architecture/ingestion-pipeline.md`](../architecture/ingestion-pipeline.md) + [`architecture/domain-profiles.md`](../architecture/domain-profiles.md) + [`guides/adding-a-domain-profile.md`](../guides/adding-a-domain-profile.md) |
| `docs/DOMAIN_PACKS.md` | Superseded by the typed-contract domain-pack docs. | [`architecture/domain-profiles.md`](../architecture/domain-profiles.md) |
| `docs/ingestion-stability-audit.md` | Historical audit of the pre-refactor pipeline; referenced split mode / `IngestPlan`. Audit findings have been resolved or moved into [`tech-debt.md`](../tech-debt.md). | [`architecture/ingestion-pipeline.md`](../architecture/ingestion-pipeline.md) + [`tech-debt.md`](../tech-debt.md) |
| `docs/ingestion-stage-validation.md` | Described the legacy "split mode" / "complete mode" stage validation contract. Per-stage validation activities are part of the current workflow but the file's vocabulary did not match the current pipeline. | [`architecture/ingestion-pipeline.md`](../architecture/ingestion-pipeline.md) |

## Mixed-status docs (retained with deprecation banners)

These docs still carry CURRENT operational content but mention
legacy concepts in clearly-banner-stamped sections. They remain in
the repo because their operational mechanics (resume, rebuild
index, full re-index, delete, batch upload, SSE / progress event
contract) are unchanged.

| Retained doc | Current content | Legacy mentions |
|---|---|---|
| [`ingestion-operations.md`](../ingestion-operations.md) | Resume / rebuild-index / full-reindex / delete / multi-upload batch operational model | Pipeline-shape diagram now uses current activity names; banner at the top scopes legacy references |
| [`ingestion-progress.md`](../ingestion-progress.md) | SSE / progress event surface; macro events; FE polling-vs-streaming guidance | "Execution plan" section now links to the current `initial_execution_plan` + `post_compile_enrichment_plan` shape |

The banners on both files frame which sections describe **current**
runtime behaviour and which describe legacy concepts. The current
authoritative architecture lives entirely under
[`docs/architecture/`](../architecture/) — those files are the
source of truth.

## Retired concepts (do not reintroduce)

The following framework concepts have been removed and **must not**
reappear in new code or documentation:

- **`DefaultIngestPlanner` / `IngestPlanner` / `IngestPlan` /
  `IngestPolicy`** — the pre-compile adaptive planner. Replaced by
  the cheap pre-compile `initial_execution_plan` (built by
  `build_initial_execution_plan` activity) and the post-compile
  `post_compile_enrichment_plan` (built by the post-compile
  assessor). The new pipeline makes enrichment decisions AFTER
  compile evidence is visible.
- **Split mode / complete mode** — the legacy RAGAnything bridge
  configuration. The current compile stage treats RAGAnything as a
  single black-box `process_document` call; no `split_mode` flag
  exists in current code.
- **Pre-compile graph / index gating** — the legacy planner decided
  whether graph + index would run *before* compile output existed.
  The current pipeline gates these on the post-compile assessor's
  typed `post_compile_enrichment_plan`, with compile evidence in
  hand.
- **`final_summary` as the primary aggregate** — `final_summary`
  remains as a backward-compatible artifact for older consumers;
  the **preferred** aggregate is `final_ingestion_report` (see
  [`architecture/final-ingestion-report.md`](../architecture/final-ingestion-report.md)).

Tests in
[`test_planning_vocabulary_regression.py`](../../tests/test_planning_vocabulary_regression.py)
+ [`test_docs_and_cleanup.py`](../../tests/test_docs_and_cleanup.py)
enforce that the retired vocabulary does not reappear in runtime
identifiers, REST routes, artifact kinds, FE display labels, or
active architecture docs.

## Where to find current docs

The new authoritative documentation structure lives under
[`docs/README.md`](../README.md). Key entry points:

- [Ingestion pipeline](../architecture/ingestion-pipeline.md) — stage-by-stage
- [Domain profiles](../architecture/domain-profiles.md) — `DomainPack` + `DomainPromptPack`
- [Enrichment overlay](../architecture/enrichment-overlay.md) — modules, runner, limiter
- [Final ingestion report](../architecture/final-ingestion-report.md) — final-status vocabulary + endpoint contract
- [Adding a domain profile](../guides/adding-a-domain-profile.md)
- [Adding an enrichment module](../guides/adding-an-enrichment-module.md)
- [Production worker wiring](../operations/production-worker-wiring.md)
- [Artifact reference](../reference/artifacts.md)
- [UI / operator copy guide](../reference/ui-copy.md)
- [Known technical debt](../tech-debt.md)
